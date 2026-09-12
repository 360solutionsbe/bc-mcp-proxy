"""Measure how Business Central's MCP server reports a permission denial.

Run it as a *restricted* user (one that lacks Read on some API pages) and once
as an admin for comparison. It never writes unless you pass `write`.

Modes:
  static    call every List<object>_PAG<id> tool once (top=1) and record the
            verdict per tool: OK / DENIED / other error
  dynamic   list actions through bc_actions_search, then invoke the actions
            named in BC_PROBE_ACTIONS (comma separated) through bc_actions_invoke
  write     try a Create on the pages named in BC_PROBE_WRITE_PAGES with an
            empty body, to capture the shape of a write denial (off by default)

Usage (repo root; reads .env like the proxy):
  python scripts/probe_permission_errors.py static  [--dump out.json]
  python scripts/probe_permission_errors.py dynamic [--dump out.json]

Use a separate token cache for the restricted user so the admin cache stays
untouched:  set BC_DEVICE_CACHE_NAME=mcp_test_limited  (first run opens the
browser sign-in for that user). BC_PROBE_TOP (default 1) sets the page size,
raise it to see how many rows a security filter lets through.

Output: a table on stdout; with --dump a JSON file with the raw tool results
(text parts only) for tests/fixtures/bc_permission_errors.json. Scrub the
company/tenant names before committing a fixture.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.types import CallToolResult, Implementation

from bc_mcp_proxy.auth import create_token_provider
from bc_mcp_proxy.config import ProxyConfig, resolve_token_scope
from bc_mcp_proxy.proxy import (
    _AsyncBearerAuth,
    _STATIC_TOOL_RE,
    _build_endpoint_url,
    _build_transport_headers,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
_PERMISSION_HINT = re.compile(
    r"permission|Authorization_|prevented the action|not allowed|access", re.IGNORECASE)


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


def _config() -> ProxyConfig:
  cfg = ProxyConfig(
      tenant_id=os.environ["BC_TENANT_ID"],
      client_id=os.environ["BC_CLIENT_ID"],
      environment=os.environ["BC_ENVIRONMENT"],
      company=os.environ["BC_COMPANY"],
      configuration_name=os.environ.get("BC_CONFIGURATION_NAME") or None,
      device_cache_name=os.environ.get("BC_DEVICE_CACHE_NAME", "bc_mcp_proxy"),
      server_name="bc-mcp-probe",
  )
  cfg.token_scope = resolve_token_scope(cfg.base_url, os.environ.get("BC_TOKEN_SCOPE"))
  return cfg


def _text(result: CallToolResult) -> str:
  return "\n".join(getattr(c, "text", "") for c in (result.content or []) if getattr(c, "text", None))


def _parse_error(text: str) -> tuple[str | None, str | None]:
  """Return (code, message) from a BC JSON error payload, if any."""
  try:
    payload = json.loads(text)
  except Exception:  # noqa: BLE001
    payload = None
  if isinstance(payload, dict):
    err = payload.get("Error") or payload.get("error")
    if isinstance(err, dict):
      return err.get("Code") or err.get("code"), err.get("Message") or err.get("message")
  # BC prefixes successful list results with a sentence and then JSON; only a
  # top-level {"Error": {...}} object counts. Never scan record fields (a
  # unit-of-measure record has a "code" field too).
  m = re.search(r'^\s*\{\s*"[Ee]rror"\s*:\s*\{[^}]*?"[Cc]ode"\s*:\s*"([^"]+)"', text)
  return (m.group(1) if m else None), None


def _verdict(result: CallToolResult) -> tuple[str, str]:
  text = _text(result)
  code, message = _parse_error(text)
  probe = (message or text)
  if code or result.isError:
    kind = "DENIED?" if _PERMISSION_HINT.search(probe or "") else "ERROR"
    return kind, f"isError={result.isError} code={code} :: {(message or text)[:160]!r}"
  return "OK", f"isError={result.isError} :: {text[:90]!r}"


_TOP = int(os.environ.get("BC_PROBE_TOP", "1"))  # raise to see a security filter's effect


def _top_argument(tool: Any) -> dict[str, Any]:
  props = (getattr(tool, "inputSchema", None) or {}).get("properties") or {}
  for key in ("top", "$top", "Top"):
    if key in props:
      return {key: _TOP}
  return {}


async def _session(cfg: ProxyConfig):
  auth = _AsyncBearerAuth(create_token_provider(cfg))
  return streamablehttp_client(
      url=_build_endpoint_url(cfg), headers=_build_transport_headers(cfg),
      timeout=cfg.http_timeout_seconds, sse_read_timeout=cfg.sse_timeout_seconds, auth=auth)


async def run(mode: str, dump: str | None) -> None:
  cfg = _config()
  records: list[dict[str, Any]] = []
  async with await _session(cfg) as (r, w, _):
    async with ClientSession(r, w, client_info=Implementation(name=cfg.server_name, version=cfg.server_version)) as s:
      init = await s.initialize()
      print(f"server {init.serverInfo.name} {init.serverInfo.version} | env {cfg.environment} | company {cfg.company} | config {cfg.configuration_name or '<none>'}")
      tools = (await s.list_tools()).tools
      names = [t.name for t in tools]
      static_list = [t for t in tools if (m := _STATIC_TOOL_RE.match(t.name)) and m.group("verb") == "List"]
      print(f"{len(tools)} tools ({len(static_list)} static List tools)\n")

      async def call(name: str, args: dict[str, Any], label: str) -> None:
        try:
          result = await asyncio.wait_for(s.call_tool(name, args), timeout=cfg.http_timeout_seconds)
        except Exception as exc:  # noqa: BLE001
          print(f"{'EXC':8s} {label:60s} {type(exc).__name__}: {str(exc)[:120]}")
          records.append({"tool": name, "arguments": args, "exception": f"{type(exc).__name__}: {exc}"})
          return
        kind, detail = _verdict(result)
        print(f"{kind:8s} {label:60s} {detail}")
        records.append({"tool": name, "arguments": args, "isError": result.isError,
                        "text": [getattr(c, "text", "") for c in (result.content or [])]})

      if mode == "static":
        if not static_list:
          print("No static List tools: the MCP configuration is in dynamic mode. Use mode 'dynamic'.")
        for t in static_list:
          await call(t.name, _top_argument(t), t.name)
      elif mode == "dynamic":
        if "bc_actions_search" not in names:
          print("bc_actions_search not available: configuration is static. Use mode 'static'.")
          return
        # Schema measured on BC 28.0: SearchText / SearchMode / ActionType are
        # required; bc_actions_invoke takes RequestParameters as a JSON *string*.
        search = await s.call_tool("bc_actions_search", {
            "SearchText": os.environ.get("BC_PROBE_QUERY", "customer, item, employee"),
            "SearchMode": "keyword", "ActionType": ["List"]})
        print("bc_actions_search:", _verdict(search)[1][:400], "\n")
        records.append({"tool": "bc_actions_search", "isError": search.isError,
                        "text": [getattr(c, "text", "") for c in (search.content or [])]})
        actions = [a.strip() for a in os.environ.get("BC_PROBE_ACTIONS", "").split(",") if a.strip()]
        if not actions:
          print("Set BC_PROBE_ACTIONS=<ActionName1>,<ActionName2> (from the search output above) to invoke.")
        for action in actions:
          await call("bc_actions_describe", {"ActionName": action}, f"describe {action}")
          await call("bc_actions_invoke", {"ActionName": action, "RequestParameters": json.dumps({"top": _TOP})},
                     f"invoke {action}")
      elif mode == "write":
        pages = [p.strip() for p in os.environ.get("BC_PROBE_WRITE_PAGES", "").split(",") if p.strip()]
        if not pages:
          print("Set BC_PROBE_WRITE_PAGES=Create<object>_PAG<id>,... to attempt writes (sandbox only!).")
        for name in pages:
          await call(name, {}, name)
      else:
        raise SystemExit(f"unknown mode {mode!r}")

  if dump:
    Path(dump).write_text(json.dumps({"environment": cfg.environment, "mode": mode, "records": records}, indent=2), encoding="utf-8")
    print(f"\nraw results written to {dump}")


def main() -> None:
  _load_dotenv(REPO_ROOT / ".env")
  args = sys.argv[1:]
  if not args or args[0] not in ("static", "dynamic", "write"):
    sys.stderr.write(__doc__)
    sys.exit(2)
  dump = args[args.index("--dump") + 1] if "--dump" in args else None
  asyncio.run(run(args[0], dump))


if __name__ == "__main__":
  main()
