"""Diagnose Internal_CompanyNotFound against the Business Central MCP endpoint.

BC's message for this code ("specify a default company in the service
configuration file") is on-premises advice and sends you after the configured
company name. Measurement on 2026-08-17 said the name is not the cause, so
this script re-runs the measurements instead of trusting the message.

Four modes:

  headers   log the real outgoing HTTP headers per request, so you can see
            whether Company is on the tool-call POST and not only on connect
  matrix    vary company / configuration / endpoint one at a time
  poll      retry until it clears, printing an elapsed clock
  v27       try the older path-based endpoint as a possible fallback

Usage:
  python scripts/diagnose_company_not_found.py headers
  python scripts/diagnose_company_not_found.py matrix
  python scripts/diagnose_company_not_found.py poll
  python scripts/diagnose_company_not_found.py v27

Configure through the same environment variables the proxy uses
(BC_TENANT_ID, BC_CLIENT_ID, BC_ENVIRONMENT, BC_COMPANY,
BC_CONFIGURATION_NAME), or through a .env in the repo root.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import httpx
import msal
from msal_extensions import FilePersistence, PersistedTokenCache
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client, streamablehttp_client

from bc_mcp_proxy.auth import _default_cache_dir
from bc_mcp_proxy.config import V27_SCOPE, V28_BASE_URL, V28_SCOPE

# A read-only standard page, present in every environment; the point is the
# company context, not the data.
PROBE_ACTION = os.getenv("BC_PROBE_ACTION", "List_CustomerFinancialDetails_PAG20048")
POLL_INTERVAL = float(os.getenv("BC_PROBE_INTERVAL", "45"))
POLL_MAX_MINUTES = float(os.getenv("BC_PROBE_MAX_MINUTES", "30"))

HEADERS_OF_INTEREST = ("company", "configurationname", "tenantid",
                       "environmentname", "mcp-session-id",
                       "mcp-protocol-version")


def _env(name: str, default: str | None = None) -> str:
  value = os.getenv(name, default)
  if not value:
    raise SystemExit(f"[fail] {name} is not set")
  return value


TENANT = _env("BC_TENANT_ID")
CLIENT = _env("BC_CLIENT_ID")
ENVIRONMENT = _env("BC_ENVIRONMENT")
COMPANY = _env("BC_COMPANY")
CONFIGURATION = os.getenv("BC_CONFIGURATION_NAME") or None


def token(scope: str) -> str:
  """Silent token from the proxy's own MSAL cache -- never prompts.

  If there is nothing cached, run the proxy once first; minting a token here
  would open a browser in the middle of a diagnostic.
  """
  cache_path = Path(os.getenv("BC_DEVICE_CACHE_LOCATION") or _default_cache_dir())
  cache_file = cache_path / "bc_mcp_proxy.bin"
  app = msal.PublicClientApplication(
      client_id=CLIENT,
      authority=f"https://login.microsoftonline.com/{TENANT}",
      token_cache=PersistedTokenCache(FilePersistence(str(cache_file))),
  )
  for account in app.get_accounts() or []:
    result = app.acquire_token_silent([scope], account=account)
    if result and "access_token" in result:
      return result["access_token"]
  raise SystemExit(
      f"[fail] no cached token for {scope}. Run the proxy once so it can sign in.")


def base_headers(tok: str, *, company: str, configuration: str | None,
                 environment: str, v28: bool) -> dict[str, str]:
  headers = {
      "X-Client-Application": "bc-mcp-diagnose",
      "Company": company,
      "Authorization": f"Bearer {tok}",
  }
  if configuration:
    headers["ConfigurationName"] = configuration
  if v28:
    # The v28 host has no environment in the path, so routing rides along here.
    headers["TenantId"] = TENANT
    headers["EnvironmentName"] = environment
  return headers


def verdict(result) -> str:
  """Collapse a tool result to its BC error code, or an OK plus a sample."""
  text = "".join(getattr(c, "text", "") for c in (result.content or []))
  try:
    error = json.loads(text).get("Error")
    if error:
      return error.get("Code", "?")
  except Exception:  # noqa: BLE001 - a normal result is not JSON
    pass
  return "OK " + text[:70].replace("\n", " ")


async def call_once(url: str, headers: dict[str, str]) -> str:
  async with streamablehttp_client(
      url=url, headers=headers, timeout=180, sse_read_timeout=300,
  ) as (read, write, _sid):
    async with ClientSession(read, write) as session:
      await session.initialize()
      result = await session.call_tool("bc_actions_invoke", {
          "ActionName": PROBE_ACTION,
          "RequestParameters": json.dumps({"top": 1}),
      })
      return verdict(result)


async def safe_call(url: str, headers: dict[str, str]) -> str:
  try:
    return await call_once(url, headers)
  except Exception as exc:  # noqa: BLE001 - this is a diagnostic
    return f"CONNECT-FAIL {type(exc).__name__}"


async def mode_headers() -> None:
  """Print every outgoing request with the headers BC actually receives.

  This is the measurement that rules out "the Company header is only sent at
  connect" -- the headers live on the httpx client, so they ride on the
  tool-call POST too.
  """
  tok = token(V28_SCOPE)

  async def on_request(request: httpx.Request) -> None:
    shown = {k: v for k, v in request.headers.items()
             if k.lower() in HEADERS_OF_INTEREST}
    print(f"  --> {request.method} {request.url}")
    print(f"      {shown}")

  async def on_response(response: httpx.Response) -> None:
    print(f"  <-- {response.status_code}")

  client = httpx.AsyncClient(
      headers=base_headers(tok, company=COMPANY, configuration=CONFIGURATION,
                           environment=ENVIRONMENT, v28=True),
      timeout=httpx.Timeout(120.0, read=300.0),
      event_hooks={"request": [on_request], "response": [on_response]},
  )
  async with client:
    async with streamable_http_client(V28_BASE_URL, http_client=client) as (r, w, _):
      async with ClientSession(r, w) as session:
        await session.initialize()
        result = await session.call_tool("bc_actions_invoke", {
            "ActionName": PROBE_ACTION,
            "RequestParameters": json.dumps({"top": 1}),
        })
        print(f"  RESULT isError={result.isError} -> {verdict(result)}")


async def mode_matrix() -> None:
  """Vary one thing at a time: company, configuration, endpoint shape."""
  tok = token(V28_SCOPE)
  cases = [
      ("configured pair", COMPANY, CONFIGURATION),
      ("bogus company", "No Such Company At All", CONFIGURATION),
      ("bogus configuration", COMPANY, "NoSuchConfiguration"),
      ("no configuration", COMPANY, None),
  ]
  for label, company, configuration in cases:
    headers = base_headers(tok, company=company, configuration=configuration,
                           environment=ENVIRONMENT, v28=True)
    print(f"{label:22} company={company!r:26} "
          f"cfg={(configuration or '<omitted>')!r:22} "
          f"-> {await safe_call(V28_BASE_URL, headers)}")
  print("\nA bogus company and a bogus configuration are both rejected at "
        "connect (404 -> 'Session terminated'), not at the tool call. So an\n"
        "Internal_CompanyNotFound from the tool call means the name already "
        "resolved once for this session -- it is not a typo in BC_COMPANY.")


async def mode_v27() -> None:
  """Same call against the older path-based endpoint -- a possible fallback."""
  tok = token(V27_SCOPE)
  url = f"https://api.businesscentral.dynamics.com/v2.0/{ENVIRONMENT}/mcp"
  headers = base_headers(tok, company=COMPANY, configuration=CONFIGURATION,
                         environment=ENVIRONMENT, v28=False)
  print(f"{url}\n  -> {await safe_call(url, headers)}")


async def mode_once() -> None:
  """One call, one line, then exit -- the "is it back yet?" check.

  Kept separate from `poll` on purpose: poll runs for up to
  BC_PROBE_MAX_MINUTES, which is the wrong tool for a quick look and will sit
  there for half an hour if you reach for it by reflex.
  """
  tok = token(V28_SCOPE)
  headers = base_headers(tok, company=COMPANY, configuration=CONFIGURATION,
                         environment=ENVIRONMENT, v28=True)
  print(await safe_call(V28_BASE_URL, headers))


async def mode_poll() -> None:
  """Retry until it clears, so the outage length is a number, not a feeling."""
  tok = token(V28_SCOPE)
  headers = base_headers(tok, company=COMPANY, configuration=CONFIGURATION,
                         environment=ENVIRONMENT, v28=True)
  start = time.monotonic()
  while time.monotonic() - start < POLL_MAX_MINUTES * 60:
    outcome = await safe_call(V28_BASE_URL, headers)
    elapsed = int(time.monotonic() - start)
    print(f"[{elapsed:5d}s] {outcome}", flush=True)
    if outcome.startswith("OK"):
      print(f"recovered after {elapsed}s", flush=True)
      return
    await asyncio.sleep(POLL_INTERVAL)
  print("gave up; still failing", flush=True)


MODES = {
    "once": mode_once,
    "headers": mode_headers,
    "matrix": mode_matrix,
    "poll": mode_poll,
    "v27": mode_v27,
}


def main() -> None:
  mode = sys.argv[1] if len(sys.argv) > 1 else "matrix"
  if mode not in MODES:
    raise SystemExit(f"usage: {sys.argv[0]} [{'|'.join(MODES)}]")
  print(f"environment={ENVIRONMENT!r} company={COMPANY!r} "
        f"configuration={CONFIGURATION!r} action={PROBE_ACTION!r}\n")
  asyncio.run(MODES[mode]())


if __name__ == "__main__":
  main()
