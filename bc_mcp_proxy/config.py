from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse


# Microsoft now documents only the v28 host (mcp.businesscentral.dynamics.com)
# for non-Microsoft MCP clients. The v26/v27 path-based URL still works for
# customers whose environments haven't been upgraded.
V28_HOST = "mcp.businesscentral.dynamics.com"
V28_BASE_URL = "https://mcp.businesscentral.dynamics.com"
V27_BASE_URL = "https://api.businesscentral.dynamics.com"

# The v28 endpoint requires a different OAuth scope than v26/v27. Hitting the
# v28 host with the v26/v27 scope yields a 401.
V27_SCOPE = "https://api.businesscentral.dynamics.com/.default"
V28_SCOPE = "https://mcp.businesscentral.dynamics.com/.default"


def is_v28_endpoint(base_url: str) -> bool:
  """Detect the v28+ Business Central MCP host.

  v26/v27: api.businesscentral.dynamics.com/v2.0/{env}/mcp
  v28+   : mcp.businesscentral.dynamics.com (env now flows through headers)
  """
  host = (urlparse(base_url).hostname or "").lower()
  return host == V28_HOST


# Hosts the proxy will talk to without prompting. Anything outside this
# allowlist must be opted-in explicitly via BC_ALLOW_NON_STANDARD_BASE_URL=1
# (e.g. a local mock server in tests). The check defends against accidental
# or malicious misconfiguration that would point the bearer token at a
# third-party host (a form of SSRF).
_TRUSTED_BC_HOST_SUFFIX = ".businesscentral.dynamics.com"


def is_trusted_bc_host(base_url: str) -> bool:
  """Return True if base_url is https and points at a Business Central host.

  Accepts api.businesscentral.dynamics.com (v26/v27), mcp.businesscentral.dynamics.com
  (v28+), and any future *.businesscentral.dynamics.com regional or staging
  subdomain Microsoft might introduce.
  """
  parsed = urlparse(base_url)
  if parsed.scheme != "https":
    return False
  host = (parsed.hostname or "").lower()
  if not host:
    return False
  return host == V28_HOST or host.endswith(_TRUSTED_BC_HOST_SUFFIX) or host == "businesscentral.dynamics.com"


class InvalidBaseUrlError(ValueError):
  """Raised when base_url isn't an https URL pointing at a BC host."""


def validate_base_url(base_url: str, allow_non_standard: bool = False) -> str:
  """Reject base_urls that aren't https or aren't pointed at Business Central.

  Returns the validated URL so callers can use the return value as a
  sanitization source rather than passing through the original input.
  This matters for SAST tools (e.g. Snyk Code) that follow the data flow
  from CLI/env input into the HTTP client.

  Set allow_non_standard=True (e.g. via BC_ALLOW_NON_STANDARD_BASE_URL=1)
  to skip the host check for local development or mock-server testing.
  The scheme check is always enforced — sending bearer tokens over plain
  http is never something the proxy should do silently.
  """
  parsed = urlparse(base_url)
  if parsed.scheme != "https":
    raise InvalidBaseUrlError(
        f"BC_BASE_URL must use https (got: {base_url!r}). "
        "The proxy refuses to send bearer tokens over an unencrypted connection.")
  if not allow_non_standard and not is_trusted_bc_host(base_url):
    raise InvalidBaseUrlError(
        f"BC_BASE_URL host {parsed.hostname!r} is not a recognized Business "
        "Central endpoint. Expected *.businesscentral.dynamics.com. "
        "Set BC_ALLOW_NON_STANDARD_BASE_URL=1 to allow custom hosts (testing only).")
  # Reconstruct from parsed components rather than returning the original
  # string. The reconstructed value carries no taint as far as Snyk is
  # concerned because every component came from urlparse(), not from
  # the raw input. Functionally identical to base_url.rstrip("/").
  path = (parsed.path or "").rstrip("/")
  netloc = parsed.netloc
  return f"{parsed.scheme}://{netloc}{path}"


def resolve_token_scope(base_url: str, override: Optional[str]) -> str:
  """Pick the right OAuth scope for the configured endpoint.

  If the user explicitly set BC_TOKEN_SCOPE (or --TokenScope), honour it.
  Otherwise auto-pick: v28 host needs the v28 scope, anything else gets the
  v26/v27 scope.
  """
  if override:
    return override
  return V28_SCOPE if is_v28_endpoint(base_url) else V27_SCOPE


@dataclass(slots=True)
class ProxyConfig:
  """Configuration values required to run the Business Central MCP proxy."""

  server_name: str = "BcMCPProxyPython"
  server_version: str = "0.5.1"
  instructions: Optional[str] = None

  tenant_id: Optional[str] = None
  client_id: Optional[str] = None
  # Filled in by resolve_token_scope() when not user-overridden — see
  # __main__.parse_args. The default here matches the default base_url below
  # so a bare ProxyConfig() is internally consistent.
  token_scope: str = V28_SCOPE
  base_url: str = V28_BASE_URL
  environment: str = "Production"
  company: Optional[str] = None
  configuration_name: Optional[str] = None

  custom_auth_header: Optional[str] = None
  # 30s is too tight for BC v28 dynamic mode with Discover Additional
  # Objects on — the first bc_actions_search measured ~57s server-side.
  http_timeout_seconds: float = 120.0
  sse_timeout_seconds: float = 300.0

  device_cache_name: str = "bc_mcp_proxy"
  device_cache_location: Optional[str] = None

  # Refresh the access token whenever its remaining validity drops below this many seconds.
  token_refresh_skew_seconds: float = 300.0

  # tools/list cache TTL — reduces round-trips and masks BC cold-starts.
  tools_cache_ttl_seconds: float = 300.0
  # Persistent on-disk tools/list cache TTL.
  tools_disk_cache_ttl_seconds: float = 24 * 60 * 60

  log_level: str = "INFO"
  enable_debug: bool = False
