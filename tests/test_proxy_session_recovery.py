"""Tests for session-terminated recovery in proxy.py."""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData

from bc_mcp_proxy.config import ProxyConfig
from bc_mcp_proxy.proxy import (
    _SESSION_TERMINATED_ERROR_CODE,
    _UpstreamConnectionManager,
    _UpstreamSessionExpiredError,
    _UpstreamSessionHolder,
    _invoke_with_session_recovery,
    _is_recoverable_upstream_error,
    _is_session_terminated_error,
)


# -- Error classification ----------------------------------------------------


def test_constant_matches_mcp_lib_emission() -> None:
  # The upstream MCP lib hard-codes 32600 for the session-terminated path.
  # If they ever change it our detector silently stops working — pin the
  # constant explicitly so a version bump fails this test instead.
  assert _SESSION_TERMINATED_ERROR_CODE == 32600


def test_session_terminated_mcp_error_is_detected() -> None:
  exc = McpError(ErrorData(code=32600, message="Session terminated"))
  assert _is_session_terminated_error(exc)


def test_other_mcp_error_codes_are_not_session_terminated() -> None:
  exc = McpError(ErrorData(code=-32602, message="Invalid params"))
  assert not _is_session_terminated_error(exc)


def test_non_mcp_exception_is_not_session_terminated() -> None:
  assert not _is_session_terminated_error(RuntimeError("Session terminated"))
  assert not _is_session_terminated_error(httpx.ReadTimeout("x"))


def test_session_expired_signal_is_recoverable() -> None:
  # The connection manager must treat the deliberate teardown signal as
  # recoverable so its existing backoff/reconnect loop kicks in.
  assert _is_recoverable_upstream_error(_UpstreamSessionExpiredError("bye"))


# -- Connection manager: request_reconnect plumbing --------------------------


def _make_manager() -> tuple[_UpstreamConnectionManager, _UpstreamSessionHolder]:
  state = _UpstreamSessionHolder()
  mgr = _UpstreamConnectionManager(
      state=state,
      config=ProxyConfig(),
      url="https://example.test/mcp",
      headers={},
      auth=httpx.BasicAuth("u", "p"),
      logger=logging.getLogger("test.recovery"),
  )
  return mgr, state


def test_request_reconnect_clears_state_and_sets_event() -> None:
  mgr, state = _make_manager()
  state.set_session(object(), lambda: "live-session")  # type: ignore[arg-type]

  mgr.request_reconnect(reason="test reconnect")

  assert state._session is None
  assert mgr._reconnect_requested.is_set()
  assert mgr._reconnect_reason == "test reconnect"


async def test_request_reconnect_makes_waiters_block_until_new_session() -> None:
  """A second waiter calling wait_active() after request_reconnect() must
  block until a *new* session is set — otherwise it would race onto the
  dead one and immediately re-fail with 'Session terminated'."""
  mgr, state = _make_manager()
  state.set_session(object(), lambda: "s1")  # type: ignore[arg-type]

  mgr.request_reconnect(reason="x")

  # No new session yet — wait_active must still be pending.
  with pytest.raises(asyncio.TimeoutError):
    await asyncio.wait_for(state.wait_active(), timeout=0.05)

  # Once the manager would (in real code) set the new session, waiters wake.
  new_session = object()
  state.set_session(new_session, lambda: "s2")  # type: ignore[arg-type]
  resolved = await asyncio.wait_for(state.wait_active(), timeout=0.5)
  assert resolved is new_session


# -- Connection manager: _open_and_serve raises on reconnect request ---------


class _ScriptedManager(_UpstreamConnectionManager):
  """Stand-in that emulates the real _open_and_serve without spinning up a
  streamable_http transport. Each cycle sets a fresh fake session, then
  blocks on the reconnect event until request_reconnect() fires."""

  def __init__(self, **kwargs: Any) -> None:
    super().__init__(**kwargs)
    self.opens = 0

  async def _open_and_serve(self) -> None:
    self.opens += 1
    self.state.set_session(
        MagicMock(name=f"session-{self.opens}"),
        lambda i=self.opens: f"sid-{i}",
    )
    self._attempt = 0
    self._reconnect_requested.clear()
    await self._reconnect_requested.wait()
    raise _UpstreamSessionExpiredError(self._reconnect_reason or "test")


def _build_scripted_manager() -> tuple[_ScriptedManager, _UpstreamSessionHolder, list[float]]:
  state = _UpstreamSessionHolder()
  sleeps: list[float] = []

  async def fake_sleep(seconds: float) -> None:
    sleeps.append(seconds)

  mgr = _ScriptedManager(
      state=state,
      config=ProxyConfig(),
      url="https://example.test/mcp",
      headers={},
      auth=httpx.BasicAuth("u", "p"),
      logger=logging.getLogger("test.scripted"),
      sleep=fake_sleep,
  )
  return mgr, state, sleeps


# -- _invoke_with_session_recovery -------------------------------------------


async def test_invoke_returns_result_when_no_error() -> None:
  mgr, state = _make_manager()
  state.set_session(MagicMock(), lambda: "s1")  # type: ignore[arg-type]

  async def do(_session: Any) -> str:
    return "ok"

  result = await _invoke_with_session_recovery(
      state, mgr, logging.getLogger("test"), "noop", do,
  )
  assert result == "ok"
  assert not mgr._reconnect_requested.is_set()


async def test_invoke_retries_once_after_session_terminated() -> None:
  state = _UpstreamSessionHolder()
  state.set_session(MagicMock(name="s1"), lambda: "s1")  # type: ignore[arg-type]
  reconnects: list[str] = []

  fake_manager = MagicMock(spec=_UpstreamConnectionManager)

  def fake_request_reconnect(*, reason: str) -> None:
    reconnects.append(reason)
    # Simulate the manager: after asking for a reconnect, the run loop
    # tears down the old session and brings up a new one. We collapse
    # that whole dance into "swap a fresh session in atomically" so the
    # second wait_active() finds a new one without blocking.
    state.clear_session()
    state.set_session(MagicMock(name="s2"), lambda: "s2")  # type: ignore[arg-type]

  fake_manager.request_reconnect.side_effect = fake_request_reconnect

  calls = 0

  async def do(_session: Any) -> str:
    nonlocal calls
    calls += 1
    if calls == 1:
      raise McpError(ErrorData(code=32600, message="Session terminated"))
    return "ok-after-reconnect"

  result = await _invoke_with_session_recovery(
      state, fake_manager, logging.getLogger("test"), "call_tool[X]", do,
  )

  assert result == "ok-after-reconnect"
  assert calls == 2
  assert reconnects == ["session terminated during call_tool[X]"]


async def test_invoke_propagates_non_session_terminated_mcp_error() -> None:
  state = _UpstreamSessionHolder()
  state.set_session(MagicMock(), lambda: "s1")  # type: ignore[arg-type]
  fake_manager = MagicMock(spec=_UpstreamConnectionManager)

  async def do(_session: Any) -> str:
    raise McpError(ErrorData(code=-32602, message="Invalid params"))

  with pytest.raises(McpError) as excinfo:
    await _invoke_with_session_recovery(
        state, fake_manager, logging.getLogger("test"), "call_tool[X]", do,
    )
  assert excinfo.value.error.code == -32602
  fake_manager.request_reconnect.assert_not_called()


async def test_invoke_gives_up_after_second_session_terminated() -> None:
  """If even the freshly minted session reports 'Session terminated',
  the failure is real — surface it instead of looping forever."""
  state = _UpstreamSessionHolder()
  state.set_session(MagicMock(), lambda: "s1")  # type: ignore[arg-type]

  fake_manager = MagicMock(spec=_UpstreamConnectionManager)

  def fake_request_reconnect(*, reason: str) -> None:
    state.clear_session()
    state.set_session(MagicMock(), lambda: "s2")  # type: ignore[arg-type]

  fake_manager.request_reconnect.side_effect = fake_request_reconnect

  async def do(_session: Any) -> str:
    raise McpError(ErrorData(code=32600, message="Session terminated"))

  with pytest.raises(McpError) as excinfo:
    await _invoke_with_session_recovery(
        state, fake_manager, logging.getLogger("test"), "call_tool[X]", do,
    )
  assert excinfo.value.error.code == 32600
  # Only one reconnect was requested — we don't keep asking forever.
  assert fake_manager.request_reconnect.call_count == 1


async def test_invoke_propagates_unrelated_exceptions() -> None:
  state = _UpstreamSessionHolder()
  state.set_session(MagicMock(), lambda: "s1")  # type: ignore[arg-type]
  fake_manager = MagicMock(spec=_UpstreamConnectionManager)

  async def do(_session: Any) -> str:
    raise RuntimeError("boom")

  with pytest.raises(RuntimeError, match="boom"):
    await _invoke_with_session_recovery(
        state, fake_manager, logging.getLogger("test"), "call_tool[X]", do,
    )
  fake_manager.request_reconnect.assert_not_called()


# -- End-to-end: reconnect loop drives _open_and_serve -----------------------


async def test_scripted_manager_reconnects_after_request() -> None:
  """End-to-end: a single request_reconnect() call against the real run
  loop drives exactly one reopen cycle, with the new session_id appearing
  in the holder so a retried tool call can pick it up."""
  mgr, state, sleeps = _build_scripted_manager()

  async def reconnect_once() -> None:
    await state.wait_active()
    initial = state.session_id()
    assert initial == "sid-1"

    mgr.request_reconnect(reason="test")

    # Spin until the new session is up. Bounded by the test's overall
    # timeout — if the run loop never reopens this will hang and fail loudly.
    while state.session_id() in (None, initial):
      await asyncio.sleep(0)

  run_task = asyncio.create_task(mgr.run())
  await reconnect_once()
  run_task.cancel()
  with pytest.raises(asyncio.CancelledError):
    await run_task

  # Two opens: original + post-reconnect. Exactly one backoff sleep between.
  assert mgr.opens == 2
  assert sleeps == [1.0]
