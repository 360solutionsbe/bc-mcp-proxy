"""Tests for non-blocking tools/list + the tools/list_changed push.

Cold first run used to block the first tools/list on auth and die on
Claude's ~30s timeout. Now the proxy returns an empty list immediately
and pushes notifications/tools/list_changed once the upstream pre-warm
populates real tools, so the client refetches without a restart.
"""

from __future__ import annotations

import logging

import pytest
from mcp.server import Server
from mcp.server.lowlevel.server import NotificationOptions
from mcp.types import ListToolsResult, Tool

from bc_mcp_proxy.proxy import _ClientNotifier, _tools_signature


def _result(*names: str) -> ListToolsResult:
  return ListToolsResult(
      tools=[Tool(name=n, inputSchema={"type": "object"}) for n in names])


def test_signature_distinguishes_empty_from_populated() -> None:
  assert _tools_signature(_result()) != _tools_signature(_result("a"))


def test_signature_is_order_independent() -> None:
  assert _tools_signature(_result("a", "b")) == _tools_signature(_result("b", "a"))


def test_signature_handles_none() -> None:
  # Treated the same as an empty result — must not raise.
  assert _tools_signature(None) == _tools_signature(_result())


def test_init_options_advertise_tools_list_changed() -> None:
  """The capability must be on or clients ignore our list_changed push.

  The tools capability is only emitted when a list_tools handler is
  registered (as the real proxy does), so register a no-op one here.
  """
  server = Server(name="t", version="0", instructions="i")

  @server.list_tools()
  async def _lt():  # noqa: ANN202 - test stub
    return []

  opts = server.create_initialization_options(
      NotificationOptions(tools_changed=True))
  assert opts.capabilities.tools is not None
  assert opts.capabilities.tools.listChanged is True


class _FakeSession:
  def __init__(self) -> None:
    self.calls = 0

  async def send_tool_list_changed(self) -> None:
    self.calls += 1


async def test_no_notification_before_a_client_session_is_captured() -> None:
  """Pre-warm finishing before the first client request must not crash;
  the client will pick up the fresh list on its first tools/list."""
  notifier = _ClientNotifier(logging.getLogger("test"))
  await notifier.maybe_notify(_result("a", "b"))  # no session captured yet
  # Baseline is now recorded so a later identical result won't double-fire.


async def test_empty_then_populated_pushes_list_changed() -> None:
  notifier = _ClientNotifier(logging.getLogger("test"))
  session = _FakeSession()
  notifier.capture(session)

  # Cold start: client was served an empty placeholder.
  notifier.record_served(_result())
  # Upstream pre-warm completes with the real tools.
  await notifier.maybe_notify(_result("bc_actions_search", "bc_run_action"))

  assert session.calls == 1


async def test_no_push_when_tool_set_unchanged() -> None:
  notifier = _ClientNotifier(logging.getLogger("test"))
  session = _FakeSession()
  notifier.capture(session)

  notifier.record_served(_result("a", "b"))
  await notifier.maybe_notify(_result("b", "a"))  # same set, different order

  assert session.calls == 0


async def test_capture_keeps_first_session_only() -> None:
  notifier = _ClientNotifier(logging.getLogger("test"))
  first, second = _FakeSession(), _FakeSession()
  notifier.capture(first)
  notifier.capture(second)

  notifier.record_served(_result())
  await notifier.maybe_notify(_result("a"))

  assert first.calls == 1
  assert second.calls == 0


async def test_notify_failure_is_swallowed(caplog: pytest.LogCaptureFixture) -> None:
  class _BoomSession:
    async def send_tool_list_changed(self) -> None:
      raise RuntimeError("client went away")

  notifier = _ClientNotifier(logging.getLogger("test"))
  notifier.capture(_BoomSession())
  notifier.record_served(_result())

  with caplog.at_level(logging.WARNING, logger="test"):
    await notifier.maybe_notify(_result("a"))  # must not raise

  assert any("tools/list_changed" in r.message for r in caplog.records)
