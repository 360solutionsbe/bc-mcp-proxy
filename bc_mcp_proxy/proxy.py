from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import unquote

import httpx
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.server import Server
from mcp.server.lowlevel.server import NotificationOptions
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import McpError
from mcp.types import CallToolResult, Implementation, ListToolsResult

import os

from . import tools_cache
from .auth import TokenProvider, create_token_provider
from .config import ProxyConfig, is_v28_endpoint, validate_base_url

# Re-exported for backward compatibility — older callers (and the existing
# v28 endpoint test suite) import _is_v28_endpoint from this module.
_is_v28_endpoint = is_v28_endpoint

try:
  # Python 3.11+
  _BaseExceptionGroup = BaseExceptionGroup  # type: ignore[name-defined]
except NameError:  # pragma: no cover - exercised only on 3.10
  from exceptiongroup import BaseExceptionGroup as _BaseExceptionGroup  # type: ignore[no-redef]

# httpx errors we treat as recoverable upstream blips and retry through.
_RECOVERABLE_HTTPX_ERRORS: tuple[type[BaseException], ...] = (
    httpx.TimeoutException,
    httpx.NetworkError,
    httpx.RemoteProtocolError,
)

DEFAULT_RECONNECT_MAX_ATTEMPTS = 5
DEFAULT_RECONNECT_BASE_BACKOFF = 1.0
DEFAULT_RECONNECT_MAX_BACKOFF = 16.0

# MCP streamable_http client emits this code when BC returns HTTP 404 to a
# tool POST — see mcp/client/streamable_http.py:_send_session_terminated_error.
# BC invalidates its session some time after the original access token
# expires; every reuse of the cached session_id 404s after that point.
_SESSION_TERMINATED_ERROR_CODE = 32600


class _UpstreamSessionExpiredError(Exception):
  """Internal signal that triggers a reconnect from inside _open_and_serve.

  The connection manager treats this as a recoverable error, so the existing
  backoff/reconnect loop reopens the HTTP connection and runs initialize()
  again — which mints a new session_id and (via _AsyncBearerAuth) asks MSAL
  for a fresh access token."""

# Substrings that indicate the upstream returned an error message inside a
# successful (isError=False) response. Match is case-insensitive.
_MASKED_ERROR_PATTERNS: tuple[str, ...] = (
    "Authentication_InvalidCredentials",
    "is not enabled",
    "Internal Server Error",
    "BadRequest_NotFound",
    "Bad Request",
)


class _AsyncBearerAuth(httpx.Auth):
  """httpx authentication helper that fetches tokens on-demand."""

  def __init__(self, token_provider: TokenProvider) -> None:
    self._token_provider = token_provider

  async def async_auth_flow(self, request: httpx.Request) -> Any:
    token = await self._token_provider.get_token()
    request.headers["Authorization"] = f"Bearer {token}"
    yield request


def _iter_leaf_exceptions(exc: BaseException):
  if isinstance(exc, _BaseExceptionGroup):
    for sub in exc.exceptions:
      yield from _iter_leaf_exceptions(sub)
  else:
    yield exc


def _is_recoverable_upstream_error(exc: BaseException) -> bool:
  """Return True iff every leaf inside `exc` is a recoverable httpx error
  (or the deliberate `_UpstreamSessionExpiredError` reconnect signal).

  The streamablehttp_client transport runs inside an anyio task group, so
  what bubbles out is often an ExceptionGroup wrapping one or more
  httpx errors — or, when we raise our own reconnect signal from inside
  `_open_and_serve`, an ExceptionGroup wrapping a `_UpstreamSessionExpiredError`.
  We treat the bundle as recoverable only when *all* leaves are recoverable
  — a non-recoverable cause (KeyboardInterrupt, an internal AssertionError,
  etc.) must always propagate.
  """
  if isinstance(exc, _UpstreamSessionExpiredError):
    return True
  leaves = list(_iter_leaf_exceptions(exc))
  if not leaves:
    return False
  recoverable_leaf_types = _RECOVERABLE_HTTPX_ERRORS + (_UpstreamSessionExpiredError,)
  return all(isinstance(leaf, recoverable_leaf_types) for leaf in leaves)


def _is_session_terminated_error(exc: BaseException) -> bool:
  """True iff `exc` is the McpError the client lib raises when BC has
  invalidated our server-side session (HTTP 404 on the tool POST)."""
  if not isinstance(exc, McpError):
    return False
  error = getattr(exc, "error", None)
  if error is None:
    return False
  return getattr(error, "code", None) == _SESSION_TERMINATED_ERROR_CODE


def _exception_hints_at_client_cancel(exc: BaseException) -> bool:
  """Heuristic: did this disconnect look like the client cancelled mid-call?

  Claude Desktop's hardcoded 30s timeout fires `notifications/cancelled`,
  the SSE GET stream drops, and the next reconnect attempt sees HTTP 4xx
  because the session id is now invalid. Surfacing that link in logs
  helps users understand the failure mode.
  """
  for leaf in _iter_leaf_exceptions(exc):
    if isinstance(leaf, httpx.HTTPStatusError):
      status = getattr(leaf.response, "status_code", None)
      if status is not None and 400 <= status < 500:
        return True
    if isinstance(leaf, httpx.RemoteProtocolError):
      return True
  return False


def _detect_masked_error(result: CallToolResult) -> Optional[str]:
  """If `result` claims success but its text content contains a known error
  pattern, return the offending text. Otherwise return None.

  Example: the BC MCP endpoint returns `isError: false` with content
  `"Semantic search is not enabled for this environment"` when the
  feature isn't licensed — clients then treat the failure as a normal
  tool result, hiding the cause from the user.
  """
  if getattr(result, "isError", False):
    return None
  content = getattr(result, "content", None) or []
  for item in content:
    text = getattr(item, "text", None)
    if not isinstance(text, str) or not text:
      continue
    lowered = text.lower()
    for pattern in _MASKED_ERROR_PATTERNS:
      if pattern.lower() in lowered:
        return text
  return None


def _flag_as_error(result: CallToolResult) -> CallToolResult:
  """Return a CallToolResult with isError=True, preserving content."""
  return result.model_copy(update={"isError": True})


def _backoff_for_attempt(
    zero_based_attempt: int,
    base: float = DEFAULT_RECONNECT_BASE_BACKOFF,
    max_value: float = DEFAULT_RECONNECT_MAX_BACKOFF,
) -> float:
  """1.0, 2.0, 4.0, 8.0, 16.0, 16.0, ... — capped at max_value."""
  if zero_based_attempt < 0:
    return base
  return min(base * (2 ** zero_based_attempt), max_value)


def _tools_signature(result: Optional[ListToolsResult]) -> int:
  """Order-independent fingerprint of a tools/list result.

  Used to decide whether the tool set the client currently holds differs
  from a freshly fetched one — i.e. whether a tools/list_changed push is
  warranted. Keyed on the sorted tool names; an empty list (the cold-start
  placeholder) hashes distinctly from any populated list."""
  tools = getattr(result, "tools", None) or []
  names = tuple(sorted(getattr(t, "name", "") for t in tools))
  return hash(names)


class _ClientNotifier:
  """Bridges the background upstream pre-warm to the connected MCP client.

  The stdio request handlers run inside an MCP request context (where
  `server.request_context.session` is valid); the upstream pre-warm task
  does not. We capture the ServerSession from the first request handler
  call so the pre-warm can later push `notifications/tools/list_changed`
  when the tool set transitions — most importantly empty placeholder ->
  real list once first-run auth completes, so the client refetches without
  a restart."""

  def __init__(self, logger: logging.Logger) -> None:
    self._session: Any = None
    self._logger = logger
    self._last_signature: Optional[int] = None

  def capture(self, session: Any) -> None:
    if self._session is None and session is not None:
      self._session = session

  def record_served(self, result: Optional[ListToolsResult]) -> None:
    """Remember what the client now holds so we only notify on real change."""
    self._last_signature = _tools_signature(result)

  async def maybe_notify(self, result: Optional[ListToolsResult]) -> None:
    signature = _tools_signature(result)
    if signature == self._last_signature:
      return
    self._last_signature = signature
    if self._session is None:
      # No client request has happened yet; the client will pick up the
      # fresh list on its first tools/list call, so no push is needed.
      return
    try:
      await self._session.send_tool_list_changed()
      self._logger.debug("Pushed notifications/tools/list_changed to client")
    except Exception as exc:  # noqa: BLE001 - notification is best-effort
      self._logger.warning(
          "Failed to push tools/list_changed (%s); client will refresh on its "
          "next tools/list", type(exc).__name__,
      )


class _ToolsCache:
  """In-memory tools/list cache shared between stdio handler and upstream
  pre-warm. Reads are lock-free; writes use a lock so concurrent refreshers
  can't interleave a partial state."""

  def __init__(self, ttl_seconds: float) -> None:
    self._ttl = ttl_seconds
    self._result: Optional[ListToolsResult] = None
    self._fetched_at: float = 0.0
    self._lock = asyncio.Lock()

  def get_fresh(self, now: Optional[float] = None) -> Optional[ListToolsResult]:
    if self._result is None:
      return None
    if (now or time.monotonic()) - self._fetched_at > self._ttl:
      return None
    return self._result

  def get_any(self) -> Optional[ListToolsResult]:
    return self._result

  def store(self, result: ListToolsResult, now: Optional[float] = None) -> None:
    self._result = result
    self._fetched_at = now if now is not None else time.monotonic()

  @property
  def lock(self) -> asyncio.Lock:
    return self._lock


class _UpstreamSessionHolder:
  """Thread-safe-ish holder for the currently active upstream ClientSession.

  list_tools / call_tool callbacks await wait_active() and then call into
  the session. While a reconnect is in progress, set_session has been
  cleared and waiters block until the new session is up.
  """

  def __init__(self) -> None:
    self._session: Optional[ClientSession] = None
    self._get_session_id: Optional[Callable[[], Optional[str]]] = None
    self._ready = asyncio.Event()

  def set_session(
      self,
      session: ClientSession,
      get_session_id: Callable[[], Optional[str]],
  ) -> None:
    self._session = session
    self._get_session_id = get_session_id
    self._ready.set()

  def clear_session(self) -> None:
    self._session = None
    self._get_session_id = None
    self._ready.clear()

  async def wait_active(self) -> ClientSession:
    while True:
      if self._session is not None:
        return self._session
      await self._ready.wait()

  def session_id(self) -> Optional[str]:
    if self._get_session_id is None:
      return None
    try:
      return self._get_session_id()
    except Exception:  # pragma: no cover - defensive
      return None


class _UpstreamConnectionManager:
  """Owns the upstream connection and reconnects on transient httpx errors.

  Exposes `_open_and_serve` as a hook so tests can substitute a fake
  connection routine without mocking the full streamable-http stack.
  """

  def __init__(
      self,
      *,
      state: _UpstreamSessionHolder,
      config: ProxyConfig,
      url: str,
      headers: dict[str, str],
      auth: httpx.Auth,
      logger: logging.Logger,
      tools_cache_obj: Optional[_ToolsCache] = None,
      notifier: Optional[_ClientNotifier] = None,
      max_attempts: int = DEFAULT_RECONNECT_MAX_ATTEMPTS,
      base_backoff: float = DEFAULT_RECONNECT_BASE_BACKOFF,
      max_backoff: float = DEFAULT_RECONNECT_MAX_BACKOFF,
      sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
  ) -> None:
    self.state = state
    self.config = config
    self.url = url
    self.headers = headers
    self.auth = auth
    self.logger = logger
    self.tools_cache_obj = tools_cache_obj
    self.notifier = notifier
    self.max_attempts = max_attempts
    self.base_backoff = base_backoff
    self.max_backoff = max_backoff
    self.sleep = sleep
    self._attempt = 0
    # Set by request_reconnect() to break out of the in-serve wait below
    # and force the run-loop to reopen the upstream connection.
    self._reconnect_requested = asyncio.Event()
    self._reconnect_reason: str = ""

  def request_reconnect(self, *, reason: str) -> None:
    """Tear down the current upstream and reconnect on the next loop iteration.

    Called when an in-flight tool call discovers the server-side session has
    been invalidated (BC returns 404 / "Session terminated"). Clearing the
    holder up-front makes any concurrent waiters block on `wait_active()`
    until `_open_and_serve` brings the new session online.
    """
    self._reconnect_reason = reason
    self.state.clear_session()
    self._reconnect_requested.set()

  async def run(self) -> None:
    while True:
      try:
        await self._open_and_serve()
        return  # graceful shutdown — upstream closed without error.
      except asyncio.CancelledError:
        self.state.clear_session()
        raise
      except BaseException as exc:
        if not _is_recoverable_upstream_error(exc):
          self.state.clear_session()
          raise
        session_id = self.state.session_id()
        self.state.clear_session()
        self._attempt += 1
        if self._attempt >= self.max_attempts:
          self.logger.error(
              "Upstream reconnect gave up after %d attempts: %s",
              self._attempt, exc,
          )
          raise
        backoff = _backoff_for_attempt(
            self._attempt - 1, self.base_backoff, self.max_backoff,
        )
        hint = (
            " — possible client-side cancellation"
            if _exception_hints_at_client_cancel(exc) else ""
        )
        self.logger.warning(
            "Upstream connection error (%s); session=%s%s; reconnecting in %.1fs (attempt %d/%d)",
            type(exc).__name__,
            session_id or "<none>",
            hint,
            backoff,
            self._attempt,
            self.max_attempts,
        )
        await self.sleep(backoff)

  async def _open_and_serve(self) -> None:
    async with streamablehttp_client(
        url=self.url,
        headers=self.headers,
        timeout=self.config.http_timeout_seconds,
        sse_read_timeout=self.config.sse_timeout_seconds,
        auth=self.auth,
    ) as (remote_read, remote_write, get_session_id):
      client_info = Implementation(
          name=self.config.server_name,
          version=self.config.server_version,
      )
      async with ClientSession(
          remote_read,
          remote_write,
          client_info=client_info,
      ) as remote_session:
        init_result = await remote_session.initialize()
        self.logger.debug(
            "Connected to remote MCP server (protocol %s)",
            init_result.protocolVersion,
        )

        # Pre-warm tools/list before exposing the session so the stdio
        # handler can answer Claude's first request from cache instead of
        # racing BC's cold-start. If pre-warm fails for any reason, fall
        # back to the existing behaviour — set the session active and
        # let the stdio handler hit upstream lazily.
        if self.tools_cache_obj is not None:
          try:
            tools_result = await remote_session.list_tools()
            self.tools_cache_obj.store(tools_result)
            tools_cache.save_disk_cache(self.config, tools_result)
            self.logger.info(
                "Pre-warmed tools/list cache (%d tools)",
                len(getattr(tools_result, "tools", []) or []),
            )
            # Cold first run: the client was handed an empty placeholder
            # list while auth was pending. Now that real tools exist, push
            # tools/list_changed so it refetches without a restart.
            if self.notifier is not None:
              await self.notifier.maybe_notify(tools_result)
          except Exception as exc:
            # Log but don't propagate — a warmed cache is best-effort.
            self.logger.warning(
                "tools/list pre-warm failed (%s); cache stays cold",
                type(exc).__name__,
            )

        self.state.set_session(remote_session, get_session_id)
        # Each successful init resets the retry budget; subsequent failures
        # start the backoff sequence over.
        self._attempt = 0
        # Park here until the session dies on its own (the upstream raises
        # out from under us) or _call_tool calls request_reconnect() because
        # BC told us the session is gone. Either way, raising on wakeup lets
        # the surrounding async-with's run their cleanup before run() retries.
        self._reconnect_requested.clear()
        await self._reconnect_requested.wait()
        reason = self._reconnect_reason or "reconnect requested"
        self._reconnect_reason = ""
        raise _UpstreamSessionExpiredError(reason)


async def run_proxy(config: ProxyConfig) -> None:
  """Run the stdio proxy until the MCP client disconnects."""
  logger = logging.getLogger("bc_mcp_proxy")
  if config.enable_debug:
    logger.setLevel(logging.DEBUG)

  # Defense-in-depth: re-validate the URL at the boundary just before it's
  # handed to the HTTP client. __main__ also validates on startup, but
  # callers that construct ProxyConfig directly (tests, embedders) need
  # this guard too — and using the *returned* sanitized URL (rather than
  # the original) is what lets Snyk's data-flow analysis recognize the
  # sanitization.
  sanitized_base_url = validate_base_url(
      config.base_url,
      allow_non_standard=_env_flag("BC_ALLOW_NON_STANDARD_BASE_URL"),
  )

  token_provider = create_token_provider(config, logger=logger)

  headers = _build_transport_headers(config)
  url = _build_endpoint_url(config, base_url_override=sanitized_base_url)

  logger.info("Connecting to Business Central MCP endpoint at %s", url)

  auth = _AsyncBearerAuth(token_provider)

  state = _UpstreamSessionHolder()
  cache = _ToolsCache(ttl_seconds=config.tools_cache_ttl_seconds)
  notifier = _ClientNotifier(logger)

  # Prepopulate the in-memory cache from disk (if a previous run cached
  # tools for this exact tenant/env/company/config). This is the only
  # thing that lets a freshly-launched proxy answer Claude's first
  # tools/list within Claude's 30s window when BC is mid-cold-start.
  disk_cached = tools_cache.load_disk_cache(config)
  if disk_cached is not None:
    cache.store(disk_cached)
    logger.info(
        "Loaded tools/list from disk cache (%d tools)",
        len(getattr(disk_cached, "tools", []) or []),
    )

  instructions = config.instructions or (
      "Bridge MCP stdio clients to Microsoft Dynamics 365 Business Central."
      " All tool definitions and executions are forwarded to the configured Business"
      " Central environment.")
  server = Server(
      name=config.server_name,
      version=config.server_version,
      instructions=instructions,
  )

  @server.list_tools()
  async def _list_tools() -> Any:
    # Capture the live ServerSession so the background upstream pre-warm
    # can push tools/list_changed once auth completes. request_context is
    # only valid inside a request — which this always is.
    try:
      notifier.capture(server.request_context.session)
    except LookupError:  # pragma: no cover - defensive; always in a request here
      pass

    fresh = cache.get_fresh()
    if fresh is not None:
      logger.debug("Serving tools/list from cache")
      notifier.record_served(fresh)
      return fresh

    stale = cache.get_any()
    if stale is not None:
      # We have something cached but it's beyond the TTL. Serve it now
      # to keep the client unblocked, and refresh in the background.
      logger.debug("Serving stale tools/list; refreshing in background")
      notifier.record_served(stale)
      asyncio.create_task(
          _refresh_tools_cache(state, cache, config, logger, notifier))
      return stale

    # Nothing cached (cold first run, auth almost certainly still pending).
    # Do NOT block on the upstream session here — that is exactly what made
    # the first tools/list hang past Claude's ~30s request timeout. Return
    # an empty list immediately; the upstream pre-warm task will populate
    # the cache and push notifications/tools/list_changed so the client
    # refetches and the tools appear, with no restart.
    logger.info(
        "tools/list requested before upstream is ready; returning empty list "
        "and will push tools/list_changed once authentication completes")
    empty = ListToolsResult(tools=[])
    notifier.record_served(empty)
    return empty

  @server.call_tool()
  async def _call_tool(name: str, arguments: dict[str, Any]) -> Any:
    logger.debug("Calling tool '%s' (session %s)", name, state.session_id() or "<pending>")
    result = await _invoke_with_session_recovery(
        state, manager, logger,
        f"call_tool[{name}]",
        lambda s: s.call_tool(name, arguments or {}),
    )
    masked = _detect_masked_error(result)
    if masked is not None:
      logger.warning(
          "Upstream returned masked error for tool '%s'; flagging as error: %s",
          name, masked,
      )
      return _flag_as_error(result)
    return result

  # Advertise tools.listChanged so the client honours the
  # notifications/tools/list_changed we push after a cold-start auth.
  init_options = server.create_initialization_options(
      NotificationOptions(tools_changed=True))

  manager = _UpstreamConnectionManager(
      state=state,
      config=config,
      url=url,
      headers=headers,
      auth=auth,
      logger=logger,
      tools_cache_obj=cache,
      notifier=notifier,
  )

  async with stdio_server() as (local_read, local_write):
    upstream_task = asyncio.create_task(manager.run(), name="bc-mcp-upstream")
    server_task = asyncio.create_task(
        server.run(local_read, local_write, init_options),
        name="bc-mcp-stdio-server",
    )
    done, pending = await asyncio.wait(
        {upstream_task, server_task},
        return_when=asyncio.FIRST_COMPLETED,
    )
    for task in pending:
      task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    for task in done:
      task.result()  # re-raise upstream/server failures


async def _invoke_with_session_recovery(
    state: _UpstreamSessionHolder,
    manager: _UpstreamConnectionManager,
    logger: logging.Logger,
    operation: str,
    do: Callable[[ClientSession], Awaitable[Any]],
) -> Any:
  """Run `do(session)`, retrying once on a "Session terminated" error.

  When the upstream raises McpError(code=32600), call request_reconnect()
  and try again with the new session. A second failure propagates so the
  client sees it instead of the proxy looping."""
  for attempt in range(2):
    session = await state.wait_active()
    try:
      return await do(session)
    except McpError as exc:
      if attempt == 0 and _is_session_terminated_error(exc):
        logger.warning(
            "Upstream returned 'Session terminated' during %s; "
            "forcing reconnect and retrying once", operation,
        )
        manager.request_reconnect(reason=f"session terminated during {operation}")
        continue
      raise


async def _refresh_tools_cache(
    state: _UpstreamSessionHolder,
    cache: _ToolsCache,
    config: ProxyConfig,
    logger: logging.Logger,
    notifier: Optional[_ClientNotifier] = None,
) -> None:
  """Background refresh used when serving a stale cached entry."""
  try:
    session = await state.wait_active()
    async with cache.lock:
      result = await session.list_tools()
      cache.store(result)
      tools_cache.save_disk_cache(config, result)
    logger.debug("Refreshed stale tools/list cache")
    if notifier is not None:
      # If the refreshed set differs from what the client holds, nudge it.
      await notifier.maybe_notify(result)
  except Exception as exc:
    logger.warning("Background tools/list refresh failed: %s", type(exc).__name__)


def _build_transport_headers(config: ProxyConfig) -> dict[str, str]:
  headers: dict[str, str] = {
      "X-Client-Application": config.server_name,
  }
  if config.company:
    headers["Company"] = unquote(config.company).strip()
  if config.configuration_name:
    headers["ConfigurationName"] = unquote(config.configuration_name).strip()
  if is_v28_endpoint(config.base_url):
    # The v28 host requires routing info in headers because the URL no
    # longer carries the environment in its path. .strip() here is
    # defense-in-depth — __main__._clean already strips at the CLI/env
    # boundary, but callers that build ProxyConfig directly (tests,
    # embedders) bypass that path. A trailing space here surfaces as
    # `LocalProtocolError("Illegal header value …")` from httpx/h11.
    if config.tenant_id:
      headers["TenantId"] = config.tenant_id.strip()
    if config.environment:
      headers["EnvironmentName"] = config.environment.strip()
  return headers


def _build_endpoint_url(config: ProxyConfig, base_url_override: Optional[str] = None) -> str:
  # base_url_override carries a value that has been through validate_base_url();
  # use it whenever provided so the URL flowing into the HTTP client can be
  # traced back to the sanitizer. When called directly (e.g. by tests), fall
  # back to validating config.base_url ourselves so there is no path that
  # forwards an unvalidated URL into the network layer.
  if base_url_override is not None:
    base = base_url_override
  else:
    base = validate_base_url(config.base_url, allow_non_standard=True)
  base = base.rstrip("/")
  if is_v28_endpoint(base):
    # v28 host expects the bare URL — no /v2.0/{env}/mcp path.
    return base
  return f"{base}/v2.0/{config.environment}/mcp"


def run_sync(config: ProxyConfig) -> None:
  """Helper to run the proxy from synchronous entry points."""
  asyncio.run(run_proxy(config))


def _env_flag(name: str) -> bool:
  value = os.getenv(name)
  if value is None:
    return False
  return value.strip().lower() in {"1", "true", "yes", "on"}
