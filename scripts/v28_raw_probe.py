"""Raw probe — does our v26/v27 OAuth token + flow work against the v28
MCP host (https://mcp.businesscentral.dynamics.com)?

v28 changes the endpoint shape: no /v2.0/{env}/mcp path; the env now
travels as an EnvironmentName header alongside TenantId.
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

REPO_ROOT = Path(__file__).resolve().parent.parent

V28_URL = "https://mcp.businesscentral.dynamics.com"
V28_ENVIRONMENT = "MyDevEnv"
V28_CONFIGURATION = "My MCP Configuration"


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


def _data_lines(body: str):
  for ln in body.splitlines():
    if ln.startswith("data:"):
      try:
        yield json.loads(ln[len("data:"):].strip())
      except json.JSONDecodeError:
        pass


async def main() -> None:
  _load_dotenv(REPO_ROOT / ".env")

  # Use the v28 values explicitly (don't depend on .env contents that may
  # still point at the v26/v27 environment).
  config = ProxyConfig(
      tenant_id=os.environ["BC_TENANT_ID"],
      client_id=os.environ["BC_CLIENT_ID"],
      environment=V28_ENVIRONMENT,
      company="CRONUS USA",
      configuration_name=V28_CONFIGURATION,
  )

  token = await create_token_provider(config).get_token()
  print(f"OK acquired token (length={len(token)})\n")

  base_headers = {
      "X-Client-Application": config.server_name,
      "TenantId": config.tenant_id,
      "EnvironmentName": config.environment,
      "Company": config.company,
      "ConfigurationName": config.configuration_name or "",
      "Authorization": f"Bearer {token}",
      "Content-Type": "application/json",
      "Accept": "application/json, text/event-stream",
      "MCP-Protocol-Version": "2025-06-18",
  }

  print(f"endpoint     : {V28_URL}")
  print(f"environment  : {V28_ENVIRONMENT}")
  print(f"configuration: {V28_CONFIGURATION}\n")

  async with httpx.AsyncClient(timeout=120.0) as client:
    # 1) initialize
    init_body = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "bc-mcp-v28-probe", "version": "0.0.1"},
        },
    }
    r = await client.post(V28_URL, headers=base_headers, json=init_body)
    print(f"[init] status={r.status_code}")
    print(f"[init] mcp-session-id={r.headers.get('mcp-session-id')}")
    print(f"[init] ms-correlation-x={r.headers.get('ms-correlation-x', '?').split(',')[0].strip()}")
    print(f"[init] body[:500]: {r.text[:500]}\n")

    sid = r.headers.get("mcp-session-id")
    if r.status_code != 200 or not sid:
      print("init failed — stopping.")
      return

    # 2) initialized notification
    h2 = {**base_headers, "Mcp-Session-Id": sid}
    r = await client.post(V28_URL, headers=h2,
                          json={"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
    print(f"[initialized] status={r.status_code}\n")

    # 3) tools/list
    r = await client.post(V28_URL, headers=h2,
                          json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    print(f"[tools/list] status={r.status_code}")
    for parsed in _data_lines(r.text):
      if "result" in parsed:
        tools = parsed["result"].get("tools", [])
        print(f"[tools/list] {len(tools)} tool(s):")
        for t in tools:
          desc = (t.get("description") or "").splitlines()[0][:80]
          print(f"  - {t.get('name', '?'):<35} {desc}")
      elif "error" in parsed:
        print(f"[tools/list] ERR: {parsed['error']}")

    # 4) bc_actions_search (to see if dynamic config works on v28)
    print()
    call = {
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
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
    r = await client.post(V28_URL, headers=h2, json=call)
    print(f"[bc_actions_search] status={r.status_code}")
    print(f"[bc_actions_search] ms-correlation-x={r.headers.get('ms-correlation-x', '?').split(',')[0].strip()}")
    for parsed in _data_lines(r.text):
      if "error" in parsed:
        print(f"[bc_actions_search] ERR: {parsed['error']}")
      elif "result" in parsed:
        text = "".join(it.get("text", "") for it in parsed["result"].get("content", []))
        print(f"[bc_actions_search] OK ({len(text)} chars)")
        print(f"  body[:1500]: {text[:1500]}")


if __name__ == "__main__":
  try:
    asyncio.run(main())
  except KeyboardInterrupt:
    sys.exit(130)
