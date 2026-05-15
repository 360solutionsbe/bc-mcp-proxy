from __future__ import annotations

from dataclasses import dataclass
import asyncio
import logging
import os
import time
from pathlib import Path
import sys
from typing import Any, Callable, Optional, Protocol

import msal
from msal_extensions import FilePersistence, PersistedTokenCache

from .config import ProxyConfig

DEFAULT_REFRESH_SKEW_SECONDS = 300.0


class TokenProvider(Protocol):
  async def get_token(self) -> str:
    """Return a fresh OAuth token for Business Central requests."""


@dataclass
class StaticTokenProvider:
  """Simple token provider that always returns the same bearer token."""

  token: str

  async def get_token(self) -> str:
    return self.token


DeviceFlowCallback = Callable[[dict[str, str]], None]


class MsalDeviceCodeTokenProvider(TokenProvider):
  """Token provider that acquires tokens via the MSAL device code flow."""

  def __init__(
      self,
      tenant_id: str,
      client_id: str,
      scopes: list[str],
      cache_path: Path,
      logger: Optional[logging.Logger] = None,
      device_flow_callback: Optional[DeviceFlowCallback] = None,
      refresh_skew_seconds: float = DEFAULT_REFRESH_SKEW_SECONDS,
      time_source: Callable[[], float] = time.time,
  ) -> None:
    if not tenant_id:
      raise ValueError("Tenant ID is required for device code authentication.")
    if not client_id:
      raise ValueError("Client ID is required for device code authentication.")
    if not scopes:
      raise ValueError("At least one scope must be supplied.")

    cache_path.parent.mkdir(parents=True, exist_ok=True)

    self._logger = logger or logging.getLogger(__name__)
    self._scopes = scopes
    self._cache = PersistedTokenCache(FilePersistence(str(cache_path)))
    authority = f"https://login.microsoftonline.com/{tenant_id}"
    self._app = msal.PublicClientApplication(
        client_id=client_id,
        authority=authority,
        token_cache=self._cache,
    )
    self._lock = asyncio.Lock()
    self._flow_callback = device_flow_callback or self._default_flow_callback
    self._refresh_skew_seconds = max(0.0, refresh_skew_seconds)
    self._time = time_source
    self._cached_token: Optional[str] = None
    self._cached_expires_at: float = 0.0

  async def get_token(self) -> str:
    async with self._lock:
      if self._cached_token is not None and self._remaining_validity() > self._refresh_skew_seconds:
        return self._cached_token
      return await asyncio.to_thread(self._acquire_token)

  def _remaining_validity(self) -> float:
    return self._cached_expires_at - self._time()

  def _acquire_token(self) -> str:
    # If we previously cached a token but it has now drifted into the
    # refresh window, ask MSAL to bypass its in-memory access-token cache
    # and use the refresh token to mint a new one.
    needs_force_refresh = self._cached_token is not None
    accounts = self._app.get_accounts() or []
    for account in accounts:
      kwargs: dict[str, Any] = {"account": account}
      if needs_force_refresh:
        kwargs["force_refresh"] = True
      result = self._app.acquire_token_silent(self._scopes, **kwargs)
      token = self._store_result(result)
      if token:
        self._logger.debug(
            "Acquired %s MSAL token for account %s (valid for %.0fs)",
            "refreshed" if needs_force_refresh else "silent",
            account.get("username"),
            self._remaining_validity(),
        )
        return token

    flow = self._app.initiate_device_flow(scopes=self._scopes)
    if "user_code" not in flow:
      message = flow.get("error_description") or "Unable to initiate device code flow."
      raise RuntimeError(message)

    self._flow_callback(flow)

    result = self._app.acquire_token_by_device_flow(flow)
    token = self._store_result(result)
    if not token:
      message = (result or {}).get("error_description") or str(result)
      raise RuntimeError(f"Authentication failed: {message}")

    return token

  def _store_result(self, result: Optional[dict[str, Any]]) -> Optional[str]:
    """Capture the access token and its expiry so we can refresh pre-emptively."""
    if not result or "access_token" not in result:
      return None
    token = result["access_token"]
    expires_in_raw = result.get("expires_in", 0)
    try:
      expires_in = float(expires_in_raw)
    except (TypeError, ValueError):
      expires_in = 0.0
    self._cached_token = token
    self._cached_expires_at = self._time() + expires_in
    return token

  def _default_flow_callback(self, flow: dict[str, str]) -> None:
    message = flow.get(
        "message",
        f"To sign in, use code {flow.get('user_code')} at {flow.get('verification_uri')}.")
    self._logger.warning(message)
    # Must go to stderr: stdout is the MCP stdio protocol channel, and the
    # client (Claude Desktop, Cursor, etc.) tries to parse every line as
    # JSON-RPC. A bare `print()` here breaks the transport with
    # `Unexpected token 'T', "To sign in"... is not valid JSON`.
    print(message, file=sys.stderr, flush=True)


def create_token_provider(
    config: ProxyConfig,
    logger: Optional[logging.Logger] = None,
) -> TokenProvider:
  """Create an appropriate token provider based on the configuration."""
  if config.custom_auth_header:
    return StaticTokenProvider(token=config.custom_auth_header)

  scopes = [config.token_scope]
  cache_path = _resolve_cache_path(config)
  return MsalDeviceCodeTokenProvider(
      tenant_id=_require_value(config.tenant_id, "TenantId"),
      client_id=_require_value(config.client_id, "ClientId"),
      scopes=scopes,
      cache_path=cache_path,
      logger=logger,
      refresh_skew_seconds=config.token_refresh_skew_seconds,
  )


def _resolve_cache_path(config: ProxyConfig) -> Path:
  if config.device_cache_location:
    base = Path(config.device_cache_location).expanduser()
  else:
    base = _default_cache_dir()

  filename = config.device_cache_name
  if not filename.endswith(".bin"):
    filename = f"{filename}.bin"
  return base / filename


def _default_cache_dir() -> Path:
  if sys.platform.startswith("win"):
    root = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local")
  elif sys.platform == "darwin":
    root = Path.home() / "Library/Caches"
  else:
    root = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
  return root / "BcMCPProxyPython"


def _require_value(value: Optional[str], name: str) -> str:
  if value:
    return value
  raise ValueError(f"{name} is required when device code authentication is used.")

