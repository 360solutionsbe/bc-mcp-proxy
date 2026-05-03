"""Raw HTTP probe — call bc_actions_search via direct JSON-RPC POST so we
can see exactly what BC returns when the mcp library wraps it as -32603."""

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
    key, value = key.strip(), value.strip().strip('"').strip("'")
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

  token = await create_token_provider(config).get_token()
  endpoint = _build_endpoint_url(config)
  base_headers = _build_transport_headers(config)

  async with httpx.AsyncClient(timeout=30.0) as client:
    # 1) initialize
    init_body = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "bc-mcp-raw-probe", "version": "0.0.1"},
        },
    }
    headers = {
        **base_headers,
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2025-06-18",
    }
    r = await client.post(endpoint, headers=headers, json=init_body)
    print(f"[init] status={r.status_code}")
    sid = r.headers.get("mcp-session-id")
    print(f"[init] mcp-session-id={sid}")
    print(f"[init] body: {r.text[:600]}\n")

    if not sid:
      print("No session id; aborting.")
      return

    # 2) initialized notification
    notif_headers = {**headers, "Mcp-Session-Id": sid}
    notif = {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}
    r = await client.post(endpoint, headers=notif_headers, json=notif)
    print(f"[initialized] status={r.status_code}\n")

    # 3) tools/call -> bc_actions_search
    call = {
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {
            "name": "bc_actions_search",
            "arguments": {
                "SearchText": "customer",
                "SearchMode": "keyword",
                "ActionType": ["List"],
                "Top": 10,
            },
        },
    }
    r = await client.post(endpoint, headers=notif_headers, json=call)
    print(f"[bc_actions_search] status={r.status_code}")
    print(f"[bc_actions_search] content-type: {r.headers.get('content-type')}")
    body = r.text
    print(f"[bc_actions_search] body ({len(body)} bytes):")
    print(body[:6000])

    # Try to extract JSON-RPC error.data if present.
    parsed = None
    if body:
      # SSE: lines like "data: {json}". Strip prefix.
      lines = [ln for ln in body.splitlines() if ln.startswith("data:")]
      if lines:
        try:
          parsed = json.loads(lines[0][len("data:"):].strip())
        except json.JSONDecodeError:
          pass
      else:
        try:
          parsed = json.loads(body)
        except json.JSONDecodeError:
          pass
    if parsed:
      print("\n[parsed JSON-RPC]")
      print(json.dumps(parsed, indent=2)[:3000])


if __name__ == "__main__":
  try:
    asyncio.run(main())
  except KeyboardInterrupt:
    sys.exit(130)
