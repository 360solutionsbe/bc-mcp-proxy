"""Tests for the opt-in hiding of static tools the user may not use.

Covers the registry, the read-side cache filter, and the probe that reads
one record per List tool after connecting. The probe runs against a
scripted call_tool so no upstream is needed.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from mcp.shared.exceptions import McpError
from mcp.types import CallToolResult, ErrorData, ListToolsResult, TextContent, Tool

from bc_mcp_proxy.permissions import (
    PermissionDenial,
    PermissionRegistry,
    probe_arguments,
    probe_static_permissions,
    static_page_id,
    static_verb,
)
from bc_mcp_proxy.proxy import _ToolsCache

DENIAL_TEXT = json.dumps({"error": {
    "code": "Internal_ServerError",
    "message": "Sorry, the current permissions prevented the action. "
               "(TableData 5200 Employee Read: _Exclude_APIV2_)"}})
STRUCTURAL_TEXT = json.dumps({"error": {
    "code": "BadRequest_NotFound", "message": "Bad Request - Error in query syntax."}})


def _tool(name: str, top: bool = True) -> Tool:
  schema: dict[str, Any] = {"type": "object", "properties": {}}
  if top:
    schema["properties"]["top"] = {"type": "integer"}
  return Tool(name=name, description="d", inputSchema=schema)


def _tools(*names: str) -> ListToolsResult:
  return ListToolsResult(tools=[_tool(n) for n in names])


def _denial() -> PermissionDenial:
  return PermissionDenial(code="Internal_ServerError", message="m", kind="TableData",
                          object_id=5200, object_name="Employee", permission="Read")


# -- name parsing --------------------------------------------------------------


@pytest.mark.parametrize("name,page,verb", [
    ("List_Customers_PAG30009", "30009", "List"),
    ("ListUpdate_Customers_PAG30009", "30009", "ListUpdate"),
    ("Create_Customers_PAG30009", "30009", "Create"),
    ("Delete_Customers_PAG30009", "30009", "Delete"),
    ("Post_SalesInvoices_PAG30012", "30012", ""),
    ("bc_actions_invoke", None, None),
])
def test_static_name_parsing(name: str, page: str | None, verb: str | None) -> None:
  assert static_page_id(name) == page
  assert static_verb(name) == verb


def test_probe_arguments_follow_schema() -> None:
  assert probe_arguments(_tool("List_X_PAG1")) == {"top": 1}
  assert probe_arguments(_tool("List_X_PAG1", top=False)) == {}
  dollar = Tool(name="List_X_PAG1", description="d",
                inputSchema={"type": "object", "properties": {"$top": {}}})
  assert probe_arguments(dollar) == {"$top": 1}


# -- registry ------------------------------------------------------------------


def test_denied_list_hides_all_siblings_of_the_page() -> None:
  registry = PermissionRegistry()
  registry.replace({"30017": _denial()}, {"30009"})
  listed = _tools("List_Employees_PAG30017", "Create_Employees_PAG30017",
                  "ListUpdate_Employees_PAG30017", "Delete_Employees_PAG30017",
                  "Terminate_Employees_PAG30017", "List_Customers_PAG30009")
  assert [t.name for t in registry.filter(listed).tools] == ["List_Customers_PAG30009"]
  assert registry.is_hidden("Delete_Employees_PAG30017")
  assert not registry.is_hidden("bc_actions_invoke")


def test_filter_returns_same_object_when_nothing_hidden() -> None:
  registry = PermissionRegistry()
  listed = _tools("List_Customers_PAG30009")
  assert registry.filter(listed) is listed
  registry.replace({"99999": _denial()}, set())
  assert registry.filter(listed) is listed  # denied page not in this list


def test_replace_is_atomic_and_mark_denied_is_incremental() -> None:
  registry = PermissionRegistry()
  registry.replace({"1": _denial()}, {"2"})
  registry.replace({"3": _denial()}, {"1"})
  assert set(registry.denied) == {"3"} and registry.allowed == {"1"}
  assert registry.mark_denied("1", _denial()) is True
  assert registry.mark_denied("1", _denial()) is False
  assert registry.allowed == set() and set(registry.denied) == {"1", "3"}
  assert "2 page(s) hidden" in registry.summary()


# -- cache read filter ---------------------------------------------------------


def test_cache_filters_on_read_but_stores_unfiltered() -> None:
  registry = PermissionRegistry()
  registry.replace({"30017": _denial()}, set())
  cache = _ToolsCache(ttl_seconds=10.0, read_filter=registry.filter)
  cache.store(_tools("List_Employees_PAG30017", "List_Customers_PAG30009"), now=100.0)
  assert [t.name for t in cache.get_fresh(now=101.0).tools] == ["List_Customers_PAG30009"]
  assert [t.name for t in cache.get_any().tools] == ["List_Customers_PAG30009"]
  # What goes to disk / the next store round-trip is the complete list.
  assert len(cache.get_unfiltered().tools) == 2


def test_cache_without_filter_is_unchanged() -> None:
  cache = _ToolsCache(ttl_seconds=10.0)
  listed = _tools("List_Employees_PAG30017")
  cache.store(listed, now=100.0)
  assert cache.get_fresh(now=101.0) is listed


# -- probe ---------------------------------------------------------------------


class _Scripted:
  """call_tool stand-in: behaviour per tool name, measuring concurrency."""

  def __init__(self, script: dict[str, str], delay: float = 0.01) -> None:
    self.script = script
    self.delay = delay
    self.calls: list[tuple[str, dict[str, Any]]] = []
    self.in_flight = 0
    self.max_in_flight = 0

  async def __call__(self, name: str, args: dict[str, Any]) -> CallToolResult:
    self.calls.append((name, args))
    self.in_flight += 1
    self.max_in_flight = max(self.max_in_flight, self.in_flight)
    try:
      await asyncio.sleep(self.delay)
      kind = self.script.get(name, "ok")
      if kind == "ok":
        return CallToolResult(content=[TextContent(type="text", text="Returned all 1 record.")], isError=False)
      if kind == "denied":
        return CallToolResult(content=[TextContent(type="text", text=DENIAL_TEXT)], isError=True)
      if kind == "other-error":
        return CallToolResult(content=[TextContent(type="text", text=STRUCTURAL_TEXT)], isError=True)
      if kind == "timeout":
        await asyncio.sleep(10)
      if kind == "session-terminated":
        raise McpError(ErrorData(code=-32600, message="Session terminated"))
      if kind == "mcp-error":
        raise McpError(ErrorData(code=-32603, message="boom"))
      raise RuntimeError(kind)
    finally:
      self.in_flight -= 1


async def test_probe_classifies_and_only_calls_list_tools() -> None:
  script = _Scripted({"List_Employees_PAG30017": "denied", "List_Broken_PAG2147": "other-error"})
  listed = _tools("List_Employees_PAG30017", "Create_Employees_PAG30017",
                  "List_Customers_PAG30009", "List_Broken_PAG2147", "Post_X_PAG5")
  outcome = await probe_static_permissions(script, listed, timeout=1.0)
  assert set(outcome.denied) == {"30017"}
  assert outcome.allowed == {"30009"}
  assert outcome.unknown == 1 and outcome.aborted is False
  called = {name for name, _ in script.calls}
  assert called == {"List_Employees_PAG30017", "List_Customers_PAG30009", "List_Broken_PAG2147"}
  assert all(args == {"top": 1} for _, args in script.calls)


async def test_probe_makes_no_calls_in_dynamic_mode() -> None:
  script = _Scripted({})
  listed = _tools("bc_actions_search", "bc_actions_describe", "bc_actions_invoke")
  outcome = await probe_static_permissions(script, listed, timeout=1.0)
  assert script.calls == [] and not outcome.denied and not outcome.allowed


async def test_transient_failures_never_hide() -> None:
  script = _Scripted({"List_A_PAG1": "timeout", "List_B_PAG2": "mcp-error", "List_C_PAG3": "crash"})
  outcome = await probe_static_permissions(script, _tools("List_A_PAG1", "List_B_PAG2", "List_C_PAG3"),
                                           timeout=0.05)
  assert not outcome.denied and not outcome.allowed and outcome.unknown == 3


async def test_probe_concurrency_is_bounded() -> None:
  script = _Scripted({}, delay=0.02)
  listed = _tools(*[f"List_T{i}_PAG{i}" for i in range(12)])
  outcome = await probe_static_permissions(script, listed, timeout=1.0, concurrency=3)
  assert script.max_in_flight <= 3
  assert len(outcome.allowed) == 12


async def test_session_terminated_aborts_without_verdicts() -> None:
  names = [f"List_T{i}_PAG{i}" for i in range(8)]
  script = _Scripted({names[0]: "session-terminated"}, delay=0.0)
  outcome = await probe_static_permissions(script, _tools(*names), timeout=1.0, concurrency=1)
  assert outcome.aborted is True
  assert len(script.calls) == 1  # the rest is skipped, not marked


# -- bc-mcp-guard ----------------------------------------------------------------

from bc_mcp_proxy.permissions import (  # noqa: E402
    guard_tool_name, parse_guard_rows, read_guard_permissions, verdicts_from_guard)


def _guard_rows() -> list[dict[str, Any]]:
  return [
      {"pageId": 30009, "pageName": "APIV2 - Customers", "sourceTableId": 18, "sourceTableName": "Customer",
       "canRead": True, "canInsert": False, "canModify": True, "canDelete": False, "canExecute": True,
       "hasSecurityFilter": True, "securityFilter": "Customer: No.=10000"},
      {"pageId": 30017, "pageName": "APIV2 - Employees", "sourceTableId": 5200, "sourceTableName": "Employee",
       "canRead": False, "canInsert": False, "canModify": False, "canDelete": False, "canExecute": True,
       "hasSecurityFilter": False, "securityFilter": ""},
      {"pageId": 30008, "pageName": "APIV2 - Items", "sourceTableId": 27, "sourceTableName": "Item",
       "canRead": True, "canInsert": True, "canModify": True, "canDelete": True, "canExecute": False,
       "hasSecurityFilter": False, "securityFilter": ""},
      {"pageId": 50100, "pageName": "MCP Guard Eff. Permissions", "sourceTableId": 50100,
       "sourceTableName": "MCP Guard Eff. Permission", "canRead": True, "canInsert": True,
       "canModify": True, "canDelete": True, "canExecute": True, "hasSecurityFilter": False, "securityFilter": ""},
  ]


def _guard_text() -> str:
  return "Returned all 4 records.\n" + json.dumps({"@odata.count": 4, "value": _guard_rows()})


def test_guard_tool_is_recognised_by_entity_set_name() -> None:
  assert guard_tool_name(_tools("List_Customers_PAG30009", "List_EffectivePermissions_PAG50100")) == "List_EffectivePermissions_PAG50100"
  assert guard_tool_name(_tools("List_EffectivePermissions_PAG70123456")) == "List_EffectivePermissions_PAG70123456"
  assert guard_tool_name(_tools("List_Customers_PAG30009")) is None
  assert guard_tool_name(_tools("bc_actions_search")) is None


def test_parse_guard_rows_needs_the_guard_shape() -> None:
  assert parse_guard_rows(_guard_text()) is not None
  assert parse_guard_rows("Returned all 1 record.\n" + json.dumps({"value": [{"number": "10000"}]})) is None
  assert parse_guard_rows("No results found.") is None
  assert parse_guard_rows(DENIAL_TEXT) is None


def test_verdicts_from_guard() -> None:
  outcome = verdicts_from_guard(_guard_rows())
  assert outcome.source == "bc-mcp-guard"
  assert set(outcome.denied) == {"30017", "30008"}
  assert outcome.denied["30017"].object_label == "TableData 5200 Employee"
  assert outcome.denied["30008"].permission == "Execute"
  assert outcome.denied_verbs == {"30009": {"Create", "Delete"}}
  assert outcome.allowed == set()  # readable pages are never marked allowed
  assert outcome.filtered_pages == {"30009": "Customer: No.=10000"}


def test_registry_hides_write_verbs_individually_and_never_the_guard() -> None:
  registry = PermissionRegistry()
  outcome = verdicts_from_guard(_guard_rows())
  registry.replace(outcome.denied, outcome.allowed, outcome.denied_verbs, source=outcome.source)
  listed = _tools("List_Customers_PAG30009", "Create_Customers_PAG30009", "ListUpdate_Customers_PAG30009",
                  "Delete_Customers_PAG30009", "List_Employees_PAG30017", "List_Items_PAG30008",
                  "Create_Items_PAG30008", "List_EffectivePermissions_PAG50100")
  assert [t.name for t in registry.filter(listed).tools] == [
      "List_Customers_PAG30009", "ListUpdate_Customers_PAG30009", "List_EffectivePermissions_PAG50100"]
  assert registry.source == "bc-mcp-guard"
  assert "2 write tool(s) hidden on 1 readable page(s)" in registry.summary()
  # a live denial on a readable page still hides it
  assert registry.mark_denied("30009", _denial()) is True
  assert registry.is_hidden("List_Customers_PAG30009")


async def test_read_guard_permissions_uses_one_call_with_a_large_top() -> None:
  calls: list[tuple[str, dict[str, Any]]] = []

  async def call(name: str, args: dict[str, Any]) -> CallToolResult:
    calls.append((name, args))
    return CallToolResult(content=[TextContent(type="text", text=_guard_text())], isError=False)

  listed = _tools("List_Customers_PAG30009", "List_EffectivePermissions_PAG50100")
  outcome = await read_guard_permissions(call, listed, timeout=1.0)
  assert outcome is not None and set(outcome.denied) == {"30017", "30008"}
  assert calls == [("List_EffectivePermissions_PAG50100", {"top": 5000})]


async def test_read_guard_permissions_falls_back_when_absent_or_broken() -> None:
  async def denied(name: str, args: dict[str, Any]) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=DENIAL_TEXT)], isError=True)

  async def garbage(name: str, args: dict[str, Any]) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text="No results found.")], isError=False)

  async def boom(name: str, args: dict[str, Any]) -> CallToolResult:
    raise RuntimeError("network")

  assert await read_guard_permissions(garbage, _tools("List_Customers_PAG30009"), timeout=1.0) is None
  guard_list = _tools("List_EffectivePermissions_PAG50100")
  assert await read_guard_permissions(denied, guard_list, timeout=1.0) is None
  assert await read_guard_permissions(garbage, guard_list, timeout=1.0) is None
  assert await read_guard_permissions(boom, guard_list, timeout=1.0) is None
