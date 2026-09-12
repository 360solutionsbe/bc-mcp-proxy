"""Tests for tool annotation enrichment (title / readOnlyHint / destructiveHint).

Business Central's MCP server names its tools two ways (Microsoft Learn,
"Configure Business Central MCP Server"): three system tools in dynamic tool
mode, and `<Verb><Object>_PAG<id>` tools in static mode. Anthropic's directory
requires every tool to carry a title plus readOnlyHint or destructiveHint, and
Claude uses those hints to auto-approve reads and always confirm writes.
"""

from __future__ import annotations

import logging

import pytest
from mcp.types import ListToolsResult, Tool, ToolAnnotations

from bc_mcp_proxy.proxy import (
    _ToolsCache,
    _annotate_tools,
    _classify_tool,
    _tools_signature,
)


def _tool(name: str, **kwargs) -> Tool:
  return Tool(name=name, inputSchema={"type": "object"}, **kwargs)


def _result(*names: str) -> ListToolsResult:
  return ListToolsResult(tools=[_tool(n) for n in names])


def _by_name(result: ListToolsResult) -> dict[str, Tool]:
  return {t.name: t for t in result.tools}


# -- Classification ----------------------------------------------------------


@pytest.mark.parametrize("name", ["bc_actions_search", "bc_actions_describe"])
def test_dynamic_search_and_describe_are_read_only(name: str) -> None:
  kind, title = _classify_tool(name)
  assert kind == "read"
  assert "Business Central" in title


def test_dynamic_invoke_is_destructive() -> None:
  assert _classify_tool("bc_actions_invoke")[0] == "write"


def test_static_list_is_read_only() -> None:
  kind, title = _classify_tool("ListAPIV2 - Customer_PAG30009")
  assert kind == "read"
  assert title == "List APIV2 - Customer"


def test_static_listupdate_is_not_read_only() -> None:
  # Regex trap: "ListUpdate..." must not be read as List + "Update...".
  kind, title = _classify_tool("ListUpdateAPIV2 - Customer_PAG30009")
  assert kind == "write"
  assert title == "Update APIV2 - Customer"


@pytest.mark.parametrize("name", [
    "CreateAPIV2 - Items_PAG30008",
    "DeleteAPIV2 - Items_PAG30008",
    "postSalesInvoice_PAG30013",  # bound action: no verb prefix
])
def test_static_create_delete_bound_action_are_destructive(name: str) -> None:
  assert _classify_tool(name)[0] == "write"


def test_unknown_name_is_not_classified() -> None:
  assert _classify_tool("something_else") is None
  assert _classify_tool("List") is None


# -- Enrichment --------------------------------------------------------------


def test_annotations_are_added_for_bc_shaped_tools() -> None:
  result = _annotate_tools(_result("bc_actions_search", "bc_actions_invoke"))
  tools = _by_name(result)
  search, invoke = tools["bc_actions_search"], tools["bc_actions_invoke"]
  assert search.annotations.readOnlyHint is True
  assert search.annotations.destructiveHint is False
  assert search.title == "Search Business Central actions"
  assert search.annotations.title == search.title
  assert invoke.annotations.readOnlyHint is False
  assert invoke.annotations.destructiveHint is True


def test_unknown_tool_is_forwarded_untouched() -> None:
  original = _result("mystery_tool")
  result = _annotate_tools(original)
  assert result is original
  assert result.tools[0].annotations is None
  assert result.tools[0].title is None


def test_existing_annotations_are_not_overwritten() -> None:
  # BC says it's read-only even though the name looks like a write: trust BC.
  bc_says = ToolAnnotations(title="From BC", readOnlyHint=True, destructiveHint=False)
  result = _annotate_tools(ListToolsResult(
      tools=[_tool("DeleteAPIV2 - Items_PAG30008", annotations=bc_says)]))
  ann = result.tools[0].annotations
  assert ann.readOnlyHint is True
  assert ann.destructiveHint is False
  assert ann.title == "From BC"
  assert result.tools[0].title == "From BC"


def test_partial_annotations_are_completed() -> None:
  partial = ToolAnnotations(idempotentHint=True)
  result = _annotate_tools(ListToolsResult(
      tools=[_tool("ListAPIV2 - Items_PAG30008", annotations=partial)]))
  ann = result.tools[0].annotations
  assert ann.idempotentHint is True
  assert ann.readOnlyHint is True
  assert ann.destructiveHint is False


def test_title_derived_only_when_missing() -> None:
  result = _annotate_tools(ListToolsResult(
      tools=[_tool("bc_actions_search", title="Search (BC)")]))
  assert result.tools[0].title == "Search (BC)"
  assert result.tools[0].annotations.title == "Search (BC)"


def test_annotation_is_idempotent() -> None:
  once = _annotate_tools(_result("bc_actions_search", "CreateAPIV2 - Items_PAG30008"))
  twice = _annotate_tools(once)
  assert twice.model_dump() == once.model_dump()


def test_signature_unchanged_by_annotation() -> None:
  # Otherwise every cache store would look like a tool-set change and fire a
  # spurious tools/list_changed notification.
  original = _result("bc_actions_search", "bc_actions_invoke")
  assert _tools_signature(_annotate_tools(original)) == _tools_signature(original)


def test_next_cursor_is_preserved() -> None:
  result = _annotate_tools(ListToolsResult(tools=[_tool("bc_actions_search")], nextCursor="abc"))
  assert result.nextCursor == "abc"


def test_long_tool_names_are_warned_about(caplog: pytest.LogCaptureFixture) -> None:
  name = "List" + "X" * 70 + "_PAG1"
  with caplog.at_level(logging.WARNING, logger="bc_mcp_proxy"):
    result = _annotate_tools(_result(name))
  assert result.tools[0].name == name  # never renamed: that would break call_tool
  assert any("64 characters" in r.message for r in caplog.records)


# -- Cache integration -------------------------------------------------------


def test_cache_store_annotates_when_enabled() -> None:
  cache = _ToolsCache(ttl_seconds=60, annotate=True)
  cache.store(_result("bc_actions_search"))
  assert cache.get_any().tools[0].annotations.readOnlyHint is True


def test_cache_store_respects_opt_out() -> None:
  cache = _ToolsCache(ttl_seconds=60, annotate=False)
  cache.store(_result("bc_actions_search"))
  assert cache.get_any().tools[0].annotations is None


def test_cache_default_is_no_annotation() -> None:
  cache = _ToolsCache(ttl_seconds=60)
  cache.store(_result("bc_actions_search"))
  assert cache.get_any().tools[0].annotations is None
