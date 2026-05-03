"""Manual test — show ListCustomers_PAG30009 schema, then call it with top=5."""

from __future__ import annotations

import asyncio
import json
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
TOOL_NAME = "ListCustomers_PAG30009"


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

  auth = _AsyncBearerAuth(create_token_provider(config))
  endpoint = _build_endpoint_url(config)
  headers = _build_transport_headers(config)

  async with streamablehttp_client(
      url=endpoint,
      headers=headers,
      timeout=config.http_timeout_seconds,
      sse_read_timeout=config.sse_timeout_seconds,
      auth=auth,
  ) as (rread, rwrite, _):
    client_info = Implementation(name=config.server_name, version=config.server_version)
    async with ClientSession(rread, rwrite, client_info=client_info) as session:
      await session.initialize()

      tools = (await session.list_tools()).tools
      tool = next((t for t in tools if t.name == TOOL_NAME), None)
      if tool is None:
        sys.stderr.write(f"Tool {TOOL_NAME} not found in tool list.\n")
        sys.exit(2)

      print(f"\n=== {TOOL_NAME} schema ===")
      print(f"description: {tool.description}")
      print("inputSchema:")
      print(json.dumps(tool.inputSchema, indent=2))
      print()

      # First attempt: OData-style top.
      arguments_to_try = [
          {"$top": 5},
          {"top": 5},
          {"$top": 5, "$select": "displayName,number,phoneNumber,email,city"},
          {},
      ]
      result = None
      used = None
      for args in arguments_to_try:
        print(f">>> calling {TOOL_NAME} with arguments={args}")
        result = await session.call_tool(TOOL_NAME, args)
        if not result.isError:
          used = args
          break
        # Print error and try next.
        for item in result.content:
          text = getattr(item, "text", "")
          print(f"    error response: {text[:300]}")

      if result is None or result.isError:
        print("\nAll argument shapes returned errors; aborting.")
        sys.exit(3)

      print(f"\n=== Result (arguments={used}) ===")
      for item in result.content:
        text = getattr(item, "text", "")
        if not text:
          continue
        # Try to JSON-pretty-print. BC often returns either a JSON array
        # or {"value": [...]} OData payload.
        try:
          parsed = json.loads(text)
        except json.JSONDecodeError:
          print(text[:4000])
          continue
        records = parsed.get("value") if isinstance(parsed, dict) else parsed
        if isinstance(records, list):
          print(f"received {len(records)} record(s); top 5:")
          for i, rec in enumerate(records[:5], 1):
            print(f"\n  [{i}]")
            for k, v in rec.items():
              if v in (None, "", []):
                continue
              vs = str(v)
              if len(vs) > 80:
                vs = vs[:77] + "…"
              print(f"      {k}: {vs}")
        else:
          print(json.dumps(parsed, indent=2)[:4000])


if __name__ == "__main__":
  try:
    asyncio.run(main())
  except KeyboardInterrupt:
    sys.stderr.write("\nInterrupted.\n")
    sys.exit(130)
