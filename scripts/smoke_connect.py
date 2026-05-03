"""Manual smoke test — connect to Business Central MCP and list tools.

Reads BC_* values from <repo-root>/.env (or the existing process env), runs
the device-code flow if no token is cached, prints the protocol version,
session id, and the first few tool definitions, then exits.

Usage (from the repo root):
    python scripts/smoke_connect.py

The first invocation prints a "To sign in, use code XYZ123 at
https://microsoft.com/devicelogin" line. Open that URL in a browser, paste
the code, and complete the sign-in. The token is cached so subsequent
runs are non-interactive until expiry.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.types import Implementation

from bc_mcp_proxy.auth import create_token_provider
from bc_mcp_proxy.config import ProxyConfig
from bc_mcp_proxy.proxy import (
    _AsyncBearerAuth,
    _build_endpoint_url,
    _build_transport_headers,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
  """Minimal .env loader. Sets only keys not already present in os.environ."""
  if not path.is_file():
    return
  for raw in path.read_text(encoding="utf-8").splitlines():
    line = raw.strip()
    if not line or line.startswith("#"):
      continue
    if "=" not in line:
      continue
    key, _, value = line.partition("=")
    key = key.strip()
    value = value.strip().strip('"').strip("'")
    if key and key not in os.environ:
      os.environ[key] = value


def _require(env_var: str) -> str:
  value = os.environ.get(env_var, "").strip()
  if not value:
    sys.stderr.write(f"ERROR: {env_var} is empty. Fill it in .env or export it.\n")
    sys.exit(2)
  return value


async def main() -> None:
  _load_dotenv(REPO_ROOT / ".env")

  logging.basicConfig(
      level=os.environ.get("BC_LOG_LEVEL", "INFO"),
      format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
  )

  config = ProxyConfig(
      tenant_id=_require("BC_TENANT_ID"),
      client_id=_require("BC_CLIENT_ID"),
      environment=_require("BC_ENVIRONMENT"),
      company=_require("BC_COMPANY"),
      configuration_name=os.environ.get("BC_CONFIGURATION_NAME") or None,
  )

  endpoint = _build_endpoint_url(config)
  headers = _build_transport_headers(config)

  print()
  print("=== BC MCP smoke test ===")
  print(f"  endpoint   : {endpoint}")
  print(f"  company    : {config.company}")
  print(f"  environment: {config.environment}")
  print(f"  client_id  : {config.client_id[:8]}…")
  print()

  token_provider = create_token_provider(config)
  auth = _AsyncBearerAuth(token_provider)

  async with streamablehttp_client(
      url=endpoint,
      headers=headers,
      timeout=config.http_timeout_seconds,
      sse_read_timeout=config.sse_timeout_seconds,
      auth=auth,
  ) as (remote_read, remote_write, get_session_id):
    client_info = Implementation(name=config.server_name, version=config.server_version)
    async with ClientSession(remote_read, remote_write, client_info=client_info) as session:
      init = await session.initialize()
      print(f"OK  initialize — protocol {init.protocolVersion}")
      print(f"OK  session id  — {get_session_id() or '<pending>'}")
      print()

      tools_result = await session.list_tools()
      tools = tools_result.tools
      print(f"OK  list_tools  — {len(tools)} tool(s)")
      for tool in tools[:15]:
        first_line = (tool.description or "").splitlines()[0] if tool.description else ""
        print(f"     - {tool.name:<40} {first_line[:60]}")
      if len(tools) > 15:
        print(f"     … and {len(tools) - 15} more")
      print()
      print("=== Done ===")


if __name__ == "__main__":
  try:
    asyncio.run(main())
  except KeyboardInterrupt:
    sys.stderr.write("\nInterrupted.\n")
    sys.exit(130)
