"""Tests for resources/* and prompts/* pass-through.

BC v28+ can return large datasets as embedded resources / file references and
v29 adds data-query and report tools; a tools-only proxy would leave the client
unable to follow those references. The handlers forward verbatim, never block
on a cold upstream for list calls, and reuse the session-terminated recovery.
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from mcp.server import Server
from mcp.server.lowlevel.server import NotificationOptions
from mcp.shared.exceptions import McpError
from mcp.types import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    BlobResourceContents,
    ErrorData,
    GetPromptRequest,
    GetPromptRequestParams,
    GetPromptResult,
    ListPromptsRequest,
    ListPromptsResult,
    ListResourcesRequest,
    ListResourcesResult,
    ListResourceTemplatesRequest,
    ListResourceTemplatesResult,
    Prompt,
    PromptMessage,
    ReadResourceRequest,
    ReadResourceRequestParams,
    ReadResourceResult,
    Resource,
    ResourceTemplate,
    ServerCapabilities,
    ServerResult,
    TextContent,
    TextResourceContents,
)

from bc_mcp_proxy.proxy import (
    _UpstreamConnectionManager,
    _UpstreamSessionHolder,
    _register_resource_and_prompt_handlers,
)


class _FakeUpstream:
  """Minimal stand-in for mcp.client.session.ClientSession."""

  def __init__(self) -> None:
    self.calls: list[tuple[str, Any]] = []
    self.fail_first_with: Any = None

  async def _maybe_fail(self, op: str) -> None:
    if self.fail_first_with is not None:
      exc, self.fail_first_with = self.fail_first_with, None
      raise exc

  async def list_resources(self) -> ListResourcesResult:
    self.calls.append(("list_resources", None))
    await self._maybe_fail("list_resources")
    return ListResourcesResult(
        resources=[Resource(uri="bc://customers/export.csv", name="customers")],
        nextCursor="page-2")

  async def list_resource_templates(self) -> ListResourceTemplatesResult:
    self.calls.append(("list_resource_templates", None))
    return ListResourceTemplatesResult(resourceTemplates=[
        ResourceTemplate(uriTemplate="bc://reports/{id}", name="report")])

  async def read_resource(self, uri: Any) -> ReadResourceResult:
    self.calls.append(("read_resource", str(uri)))
    await self._maybe_fail("read_resource")
    return ReadResourceResult(contents=[
        TextResourceContents(uri="bc://part/1", mimeType="text/csv", text="a,b"),
        BlobResourceContents(uri="bc://part/2", mimeType="application/pdf", blob="QUJD"),
    ])

  async def list_prompts(self) -> ListPromptsResult:
    self.calls.append(("list_prompts", None))
    return ListPromptsResult(prompts=[Prompt(name="month-end", description="Close the month")])

  async def get_prompt(self, name: str, arguments: Any) -> GetPromptResult:
    self.calls.append(("get_prompt", (name, arguments)))
    return GetPromptResult(messages=[
        PromptMessage(role="user", content=TextContent(type="text", text=f"run {name}"))])


def _caps(resources: bool = True, prompts: bool = True) -> ServerCapabilities:
  return ServerCapabilities(
      resources={"subscribe": False, "listChanged": False} if resources else None,
      prompts={"listChanged": False} if prompts else None,
  )


def _build(
    *, connected: bool = True, resources: bool = True, prompts: bool = True,
) -> tuple[Server, _UpstreamSessionHolder, _FakeUpstream, MagicMock]:
  server = Server(name="t", version="0", instructions="i")
  state = _UpstreamSessionHolder()
  upstream = _FakeUpstream()
  manager = MagicMock(spec=_UpstreamConnectionManager)
  if connected:
    state.set_session(upstream, lambda: "sid", _caps(resources, prompts))  # type: ignore[arg-type]
  _register_resource_and_prompt_handlers(
      server, state, lambda: manager, logging.getLogger("test.passthrough"))
  return server, state, upstream, manager


async def _call(server: Server, request: Any) -> Any:
  handler = server.request_handlers[type(request)]
  result = await handler(request)
  assert isinstance(result, ServerResult)
  return result.root


# -- Capability advertisement ------------------------------------------------


def test_init_options_advertise_resources_and_prompts() -> None:
  server, *_ = _build()

  @server.list_tools()
  async def _lt():  # noqa: ANN202 - test stub
    return []

  opts = server.create_initialization_options(NotificationOptions(tools_changed=True))
  assert opts.capabilities.tools.listChanged is True
  assert opts.capabilities.resources is not None
  assert opts.capabilities.prompts is not None


def test_handlers_registered_for_all_five_requests() -> None:
  server, *_ = _build()
  for request_type in (ListResourcesRequest, ListResourceTemplatesRequest,
                       ReadResourceRequest, ListPromptsRequest, GetPromptRequest):
    assert request_type in server.request_handlers


# -- Forwarding ---------------------------------------------------------------


async def test_list_resources_forwarded_verbatim() -> None:
  server, _, upstream, _ = _build()
  result = await _call(server, ListResourcesRequest(method="resources/list"))
  assert isinstance(result, ListResourcesResult)
  assert [r.name for r in result.resources] == ["customers"]
  assert result.nextCursor == "page-2"
  assert upstream.calls == [("list_resources", None)]


async def test_list_resource_templates_forwarded() -> None:
  server, *_ = _build()
  result = await _call(server, ListResourceTemplatesRequest(method="resources/templates/list"))
  assert [t.name for t in result.resourceTemplates] == ["report"]


async def test_read_resource_preserves_multiple_contents_and_uris() -> None:
  """Regression for the decorator bypass: the SDK's @read_resource re-wraps
  results using the request URI, which would collapse this two-part result
  onto one URI and drop the blob part."""
  server, _, upstream, _ = _build()
  request = ReadResourceRequest(
      method="resources/read",
      params=ReadResourceRequestParams(uri="bc://customers/export.csv"))
  result = await _call(server, request)
  assert isinstance(result, ReadResourceResult)
  assert [str(c.uri) for c in result.contents] == ["bc://part/1", "bc://part/2"]
  assert isinstance(result.contents[1], BlobResourceContents)
  assert result.contents[1].blob == "QUJD"
  assert upstream.calls == [("read_resource", "bc://customers/export.csv")]


async def test_get_prompt_forwards_arguments() -> None:
  server, _, upstream, _ = _build()
  request = GetPromptRequest(
      method="prompts/get",
      params=GetPromptRequestParams(name="month-end", arguments={"period": "2026-09"}))
  result = await _call(server, request)
  assert isinstance(result, GetPromptResult)
  assert result.messages[0].content.text == "run month-end"
  assert upstream.calls == [("get_prompt", ("month-end", {"period": "2026-09"}))]


async def test_list_prompts_forwarded() -> None:
  server, *_ = _build()
  result = await _call(server, ListPromptsRequest(method="prompts/list"))
  assert [p.name for p in result.prompts] == ["month-end"]


# -- Cold start / unsupported capability --------------------------------------


async def test_lists_are_empty_before_upstream_is_connected() -> None:
  # Never block on a cold upstream: mirror the tools/list behaviour.
  server, _, upstream, _ = _build(connected=False)
  assert (await _call(server, ListResourcesRequest(method="resources/list"))).resources == []
  assert (await _call(server, ListPromptsRequest(method="prompts/list"))).prompts == []
  assert (await _call(
      server, ListResourceTemplatesRequest(method="resources/templates/list"))).resourceTemplates == []
  assert upstream.calls == []


async def test_lists_are_empty_when_upstream_lacks_capability() -> None:
  server, _, upstream, _ = _build(resources=False, prompts=False)
  assert (await _call(server, ListResourcesRequest(method="resources/list"))).resources == []
  assert (await _call(server, ListPromptsRequest(method="prompts/list"))).prompts == []
  assert upstream.calls == []


async def test_read_and_get_raise_invalid_params_without_capability() -> None:
  server, *_ = _build(resources=False, prompts=False)
  with pytest.raises(McpError) as excinfo:
    await _call(server, ReadResourceRequest(
        method="resources/read", params=ReadResourceRequestParams(uri="bc://x")))
  assert excinfo.value.error.code == INVALID_PARAMS
  with pytest.raises(McpError) as excinfo:
    await _call(server, GetPromptRequest(
        method="prompts/get", params=GetPromptRequestParams(name="p")))
  assert excinfo.value.error.code == INVALID_PARAMS


async def test_fatal_upstream_error_is_surfaced() -> None:
  server, state, *_ = _build()
  state.set_fatal(McpError(ErrorData(code=INTERNAL_ERROR, message="BC rejected the connection")))
  with pytest.raises(McpError, match="rejected"):
    await _call(server, ListResourcesRequest(method="resources/list"))


# -- Session recovery ---------------------------------------------------------


async def test_read_resource_retries_once_on_session_terminated() -> None:
  server, state, upstream, manager = _build()
  upstream.fail_first_with = McpError(ErrorData(code=32600, message="Session terminated"))

  def fake_request_reconnect(*, reason: str) -> None:
    state.clear_session()
    state.set_session(upstream, lambda: "sid-2", _caps())  # type: ignore[arg-type]

  manager.request_reconnect.side_effect = fake_request_reconnect
  request = ReadResourceRequest(
      method="resources/read", params=ReadResourceRequestParams(uri="bc://x"))
  result = await _call(server, request)
  assert isinstance(result, ReadResourceResult)
  assert manager.request_reconnect.call_count == 1
  assert [c[0] for c in upstream.calls] == ["read_resource", "read_resource"]


# -- Session holder ------------------------------------------------------------


def test_holder_tracks_capabilities_and_clears_them() -> None:
  state = _UpstreamSessionHolder()
  assert state.upstream_capabilities is None
  assert not state.upstream_supports("resources")
  state.set_session(object(), lambda: "s", _caps(prompts=False))  # type: ignore[arg-type]
  assert state.upstream_supports("resources")
  assert not state.upstream_supports("prompts")
  state.clear_session()
  assert state.upstream_capabilities is None


def test_set_session_without_capabilities_stays_compatible() -> None:
  # Existing callers/tests pass only (session, get_session_id).
  state = _UpstreamSessionHolder()
  state.set_session(object(), lambda: "s")  # type: ignore[arg-type]
  assert not state.upstream_supports("resources")
