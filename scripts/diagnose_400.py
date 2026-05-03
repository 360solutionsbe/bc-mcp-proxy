"""Diagnostic — hit the BC MCP endpoint manually and print the response body.

Reuses the MSAL token cache populated by the previous smoke run (no
interactive sign-in unless the cache is empty), then POSTs a minimal
initialize payload to https://api.businesscentral.dynamics.com/v2.0/<env>/mcp
and prints status + headers + body so we can see exactly why BC is
returning 4xx.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import httpx

from bc_mcp_proxy.auth import create_token_provider
from bc_mcp_proxy.config import ProxyConfig
from bc_mcp_proxy.proxy import _build_endpoint_url, _build_transport_headers

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
  if not path.is_file():
    return
  for raw in path.read_text(encoding="utf-8").splitlines():
    line = raw.strip()
    if not line or line.startswith("#") or "=" not in line:
      continue
    key, _, value = line.partition("=")
    key = key.strip()
    value = value.strip().strip('"').strip("'")
    if key and key not in os.environ:
      os.environ[key] = value


async def main() -> None:
  _load_dotenv(REPO_ROOT / ".env")

  config = ProxyConfig(
      tenant_id=os.environ["BC_TENANT_ID"],
      client_id=os.environ["BC_CLIENT_ID"],
      environment=os.environ["BC_ENVIRONMENT"],
      company=os.environ["BC_COMPANY"],
      configuration_name=os.environ.get("BC_CONFIGURATION_NAME") or None,
  )

  endpoint = _build_endpoint_url(config)
  base_headers = _build_transport_headers(config)
  print(f"endpoint    : {endpoint}")
  print(f"environment : {config.environment}")
  print(f"company     : {config.company}")
  print(f"transport headers: {dict(base_headers)}")
  print()

  provider = create_token_provider(config)
  token = await provider.get_token()
  print(f"OK acquired token (length={len(token)})")
  print()

  initialize_payload = {
      "jsonrpc": "2.0",
      "id": 1,
      "method": "initialize",
      "params": {
          "protocolVersion": "2024-11-05",
          "capabilities": {},
          "clientInfo": {"name": "bc-mcp-smoke-diag", "version": "0.0.1"},
      },
  }

  request_headers = {
      **base_headers,
      "Authorization": f"Bearer {token}",
      "Content-Type": "application/json",
      "Accept": "application/json, text/event-stream",
      "MCP-Protocol-Version": "2024-11-05",
  }

  async with httpx.AsyncClient(timeout=30.0) as client:
    resp = await client.post(endpoint, headers=request_headers, json=initialize_payload)

  print(f"status      : {resp.status_code} {resp.reason_phrase}")
  print(f"resp headers:")
  for k, v in resp.headers.items():
    print(f"  {k}: {v}")
  print()
  print("body:")
  body = resp.text
  print(body if body else "<empty>")
  print()
  # If JSON, also pretty-print it for readability.
  ct = resp.headers.get("content-type", "")
  if "json" in ct.lower() and body:
    try:
      parsed = json.loads(body)
    except json.JSONDecodeError:
      pass
    else:
      print("body (parsed):")
      print(json.dumps(parsed, indent=2))


if __name__ == "__main__":
  try:
    asyncio.run(main())
  except KeyboardInterrupt:
    sys.stderr.write("\nInterrupted.\n")
    sys.exit(130)
