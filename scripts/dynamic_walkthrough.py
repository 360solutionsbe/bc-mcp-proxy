"""Manual test — walk the Dynamic MCP cycle: search -> describe -> invoke.

Goal: get the top 5 customers via the dynamic tooling pattern (the same
data the static `My MCP Configuration` config exposed as `ListCustomers_PAG30009`).

Schema notes derived from `bc_actions_*` inputSchemas:
- bc_actions_search   : {SearchText, SearchMode in {keyword, semantic},
                        ActionType: array of {List, Create, Modify, Delete,
                        BoundAction}, Top?: int 5..50, default 15}
- bc_actions_describe : {ActionName}
- bc_actions_invoke   : {ActionName, Arguments (JSON-encoded STRING)}
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.shared.exceptions import McpError
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


def _extract_text(result) -> str:
  parts = []
  for item in (result.content or []):
    text = getattr(item, "text", None)
    if text:
      parts.append(text)
  return "\n".join(parts)


async def _call(session: ClientSession, name: str, arguments: dict) -> tuple[bool, str]:
  """Returns (ok, text). On McpError or isError, ok=False and text is the error message."""
  print(f">>> {name}({json.dumps(arguments)[:200]})")
  try:
    result = await session.call_tool(name, arguments)
  except McpError as exc:
    err = exc.error
    msg = f"McpError code={getattr(err, 'code', '?')} message={getattr(err, 'message', str(err))}"
    data = getattr(err, "data", None)
    if data is not None:
      msg += f"\n  data: {json.dumps(data, indent=2)[:1500]}"
    print(f"!!! {msg}")
    return False, msg
  text = _extract_text(result)
  if result.isError:
    print(f"!!! result.isError=True text={text[:300]}")
    return False, text
  return True, text


async def main() -> None:
  _load_dotenv(REPO_ROOT / ".env")

  config = ProxyConfig(
      tenant_id=os.environ["BC_TENANT_ID"],
      client_id=os.environ["BC_CLIENT_ID"],
      environment=os.environ["BC_ENVIRONMENT"],
      company=os.environ["BC_COMPANY"],
      configuration_name=os.environ.get("BC_CONFIGURATION_NAME") or None,
  )

  print(f"endpoint: {_build_endpoint_url(config)}")
  print(f"config:   {config.configuration_name}")
  print(f"company:  {config.company}\n")

  auth = _AsyncBearerAuth(create_token_provider(config))

  async with streamablehttp_client(
      url=_build_endpoint_url(config),
      headers=_build_transport_headers(config),
      timeout=config.http_timeout_seconds,
      sse_read_timeout=config.sse_timeout_seconds,
      auth=auth,
  ) as (rread, rwrite, _):
    client_info = Implementation(name=config.server_name, version=config.server_version)
    async with ClientSession(rread, rwrite, client_info=client_info) as session:
      await session.initialize()

      # --- step 1: keyword search for customer-related List actions ----------
      print("=== STEP 1: bc_actions_search ===")
      ok, text = await _call(
          session,
          "bc_actions_search",
          {"SearchText": "customer", "SearchMode": "keyword", "ActionType": ["List"], "Top": 10},
      )
      if not ok:
        sys.exit(2)
      print(f"\nresponse:\n{text[:3000]}\n")

      parsed = None
      try:
        parsed = json.loads(text)
      except json.JSONDecodeError:
        pass

      candidates = []
      if isinstance(parsed, list):
        candidates = parsed
      elif isinstance(parsed, dict):
        for key in ("value", "results", "actions", "Actions"):
          if isinstance(parsed.get(key), list):
            candidates = parsed[key]
            break

      def _action_name(rec) -> str:
        for k in ("ActionName", "name", "Name", "id", "identifier"):
          v = rec.get(k) if isinstance(rec, dict) else None
          if isinstance(v, str) and v:
            return v
        return ""

      def _score(rec) -> int:
        name = _action_name(rec).lower()
        return (("customer" in name) * 3) + (name.startswith("listcustomers") * 5) + ("list" in name)

      candidates_sorted = sorted(candidates, key=_score, reverse=True)
      if not candidates_sorted:
        print("No actions returned by search; aborting.")
        sys.exit(3)

      best = candidates_sorted[0]
      action_name = _action_name(best)
      print(f"best candidate: {json.dumps(best, indent=2)[:600]}")
      print(f"chosen action  : {action_name}\n")
      if not action_name:
        sys.exit(4)

      # --- step 2: describe ---------------------------------------------------
      print("=== STEP 2: bc_actions_describe ===")
      ok, text = await _call(session, "bc_actions_describe", {"ActionName": action_name})
      if not ok:
        sys.exit(5)
      print(f"\nresponse:\n{text[:2500]}\n")

      # --- step 3: invoke (Arguments is a JSON-encoded STRING) ----------------
      print("=== STEP 3: bc_actions_invoke ===")
      ok, text = await _call(
          session,
          "bc_actions_invoke",
          {"ActionName": action_name, "Arguments": json.dumps({"top": 5})},
      )
      if not ok:
        sys.exit(6)

      try:
        invoke_parsed = json.loads(text)
      except json.JSONDecodeError:
        invoke_parsed = None

      print("\n=== Top 5 customers via Dynamic MCP ===")
      records = None
      if isinstance(invoke_parsed, list):
        records = invoke_parsed
      elif isinstance(invoke_parsed, dict):
        for key in ("value", "results", "data"):
          if isinstance(invoke_parsed.get(key), list):
            records = invoke_parsed[key]
            break

      if records:
        for i, rec in enumerate(records[:5], 1):
          name = rec.get("displayName") or rec.get("name") or "(no name)"
          number = rec.get("number") or ""
          city = rec.get("city") or ""
          country = rec.get("country") or ""
          balance = rec.get("balanceDue")
          print(f"  [{i}] #{number} {name:<28} {city:<15} {country:<3} balance={balance}")
      else:
        print(text[:3000])


if __name__ == "__main__":
  try:
    asyncio.run(main())
  except KeyboardInterrupt:
    sys.stderr.write("\nInterrupted.\n")
    sys.exit(130)
