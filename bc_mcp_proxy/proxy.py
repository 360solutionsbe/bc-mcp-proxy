from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import unquote

import httpx
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, Implementation

from .auth import TokenProvider, create_token_provider
from .config import ProxyConfig

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
  """Return True iff every leaf inside `exc` is a recoverable httpx error.

  The streamablehttp_client transport runs inside an anyio task group, so
  what bubbles out is often an ExceptionGroup wrapping one or more
  httpx errors. We treat the bundle as recoverable only when *all* leaves
  are recoverable — a non-recoverable cause (KeyboardInterrupt, an
  internal AssertionError, etc.) must always propagate.
  """
  leaves = list(_iter_leaf_exceptions(exc))
  if not leaves:
    return False
  return all(isinstance(leaf, _RECOVERABLE_HTTPX_ERRORS) for leaf in leaves)


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
    self.max_attempts = max_attempts
    self.base_backoff = base_backoff
    self.max_backoff = max_backoff
    self.sleep = sleep
    self._attempt = 0

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
        self.logger.warning(
            "Upstream connection error (%s); reconnecting in %.1fs (attempt %d/%d)",
            type(exc).__name__, backoff, self._attempt, self.max_attempts,
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
        self.state.set_session(remote_session, get_session_id)
        # Each successful init resets the retry budget; subsequent failures
        # start the backoff sequence over.
        self._attempt = 0
        # Block until the session terminates; the surrounding async-with's
        # exit handlers will run when the upstream raises.
        await asyncio.Event().wait()


async def run_proxy(config: ProxyConfig) -> None:
  """Run the stdio proxy until the MCP client disconnects."""
  logger = logging.getLogger("bc_mcp_proxy")
  if config.enable_debug:
    logger.setLevel(logging.DEBUG)

  token_provider = create_token_provider(config, logger=logger)

  headers = _build_transport_headers(config)
  url = _build_endpoint_url(config)

  logger.info("Connecting to Business Central MCP endpoint at %s", url)

  auth = _AsyncBearerAuth(token_provider)

  state = _UpstreamSessionHolder()

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
    session = await state.wait_active()
    logger.debug("Listing tools via remote MCP session %s", state.session_id() or "<pending>")
    return await session.list_tools()

  @server.call_tool()
  async def _call_tool(name: str, arguments: dict[str, Any]) -> Any:
    session = await state.wait_active()
    logger.debug("Calling tool '%s' (session %s)", name, state.session_id() or "<pending>")
    result = await session.call_tool(name, arguments or {})
    masked = _detect_masked_error(result)
    if masked is not None:
      logger.warning(
          "Upstream returned masked error for tool '%s'; flagging as error: %s",
          name, masked,
      )
      return _flag_as_error(result)
    return result

  init_options = server.create_initialization_options()

  manager = _UpstreamConnectionManager(
      state=state,
      config=config,
      url=url,
      headers=headers,
      auth=auth,
      logger=logger,
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


def _build_transport_headers(config: ProxyConfig) -> dict[str, str]:
  headers: dict[str, str] = {
      "X-Client-Application": config.server_name,
  }
  if config.company:
    headers["Company"] = unquote(config.company)
  if config.configuration_name:
    headers["ConfigurationName"] = unquote(config.configuration_name)
  return headers


def _build_endpoint_url(config: ProxyConfig) -> str:
  base = config.base_url.rstrip("/")
  return f"{base}/v2.0/{config.environment}/mcp"


def run_sync(config: ProxyConfig) -> None:
  """Helper to run the proxy from synchronous entry points."""
  asyncio.run(run_proxy(config))
