"""Tests for upstream reconnect/backoff logic in proxy.py (Fix #1)."""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

import httpx
import pytest

from bc_mcp_proxy.config import ProxyConfig
from bc_mcp_proxy.proxy import (
    _BaseExceptionGroup,
    _backoff_for_attempt,
    _is_recoverable_upstream_error,
    _UpstreamConnectionManager,
    _UpstreamSessionHolder,
)


# -- Recoverable error classification ----------------------------------------


def test_read_timeout_is_recoverable() -> None:
  assert _is_recoverable_upstream_error(httpx.ReadTimeout("upstream stalled"))


def test_remote_protocol_error_is_recoverable() -> None:
  assert _is_recoverable_upstream_error(httpx.RemoteProtocolError("disconnected"))


def test_connect_error_is_recoverable() -> None:
  assert _is_recoverable_upstream_error(httpx.ConnectError("dns"))


def test_value_error_is_not_recoverable() -> None:
  assert not _is_recoverable_upstream_error(ValueError("boom"))


def test_exception_group_with_only_recoverable_leaves_is_recoverable() -> None:
  eg = _BaseExceptionGroup(
      "upstream errors",
      [httpx.ReadTimeout("a"), httpx.ConnectError("b")],
  )
  assert _is_recoverable_upstream_error(eg)


def test_exception_group_with_one_non_recoverable_leaf_is_not_recoverable() -> None:
  eg = _BaseExceptionGroup(
      "mixed",
      [httpx.ReadTimeout("a"), ValueError("not transient")],
  )
  assert not _is_recoverable_upstream_error(eg)


def test_nested_exception_group_walks_to_leaves() -> None:
  inner = _BaseExceptionGroup("inner", [httpx.ReadTimeout("x")])
  outer = _BaseExceptionGroup("outer", [inner, httpx.ConnectError("y")])
  assert _is_recoverable_upstream_error(outer)


# -- Backoff progression ------------------------------------------------------


def test_backoff_progression_doubles_until_cap() -> None:
  assert _backoff_for_attempt(0) == 1.0
  assert _backoff_for_attempt(1) == 2.0
  assert _backoff_for_attempt(2) == 4.0
  assert _backoff_for_attempt(3) == 8.0
  assert _backoff_for_attempt(4) == 16.0
  assert _backoff_for_attempt(5) == 16.0  # capped


def test_backoff_negative_attempt_returns_base() -> None:
  assert _backoff_for_attempt(-1) == 1.0


# -- Reconnect loop -----------------------------------------------------------


class _FakeManager(_UpstreamConnectionManager):
  """Stand-in that lets tests script the outcome of each connect attempt."""

  def __init__(self, *, actions: list[str], state: _UpstreamSessionHolder, **kwargs) -> None:
    super().__init__(
        state=state,
        config=ProxyConfig(),
        url="https://example.test/mcp",
        headers={},
        auth=httpx.BasicAuth("u", "p"),  # placeholder, never used
        logger=logging.getLogger("test.reconnect"),
        **kwargs,
    )
    self._actions = list(actions)
    self.attempts: list[str] = []

  async def _open_and_serve(self) -> None:
    if not self._actions:
      raise AssertionError("No more scripted actions")
    action = self._actions.pop(0)
    self.attempts.append(action)
    if action == "fail-read-timeout":
      raise httpx.ReadTimeout("upstream stalled")
    if action == "fail-grouped":
      raise _BaseExceptionGroup(
          "wrapped", [httpx.ReadTimeout("inside group")],
      )
    if action == "succeed-then-fail":
      # Mark as healthy (resets attempt counter), then disconnect.
      self.state.set_session(object(), lambda: "fake-session")  # type: ignore[arg-type]
      self._attempt = 0
      raise httpx.ReadTimeout("disconnected after success")
    if action == "succeed-then-graceful":
      self.state.set_session(object(), lambda: "fake-session")  # type: ignore[arg-type]
      self._attempt = 0
      return
    if action == "fail-non-recoverable":
      raise ValueError("not a transient error")
    raise AssertionError(f"Unknown action: {action}")


def _build_manager(actions: list[str], **kwargs) -> tuple[_FakeManager, list[float]]:
  state = _UpstreamSessionHolder()
  sleeps: list[float] = []

  async def fake_sleep(seconds: float) -> None:
    sleeps.append(seconds)

  mgr = _FakeManager(
      actions=actions,
      state=state,
      sleep=fake_sleep,
      **kwargs,
  )
  return mgr, sleeps


async def test_succeeds_on_first_attempt_no_sleep() -> None:
  mgr, sleeps = _build_manager(["succeed-then-graceful"])
  await mgr.run()
  assert mgr.attempts == ["succeed-then-graceful"]
  assert sleeps == []


async def test_retries_then_succeeds_uses_exponential_backoff() -> None:
  mgr, sleeps = _build_manager(
      ["fail-read-timeout", "fail-read-timeout", "succeed-then-graceful"],
      max_attempts=5,
  )
  await mgr.run()
  assert mgr.attempts == [
      "fail-read-timeout", "fail-read-timeout", "succeed-then-graceful",
  ]
  assert sleeps == [1.0, 2.0]


async def test_gives_up_after_max_attempts() -> None:
  mgr, sleeps = _build_manager(
      ["fail-read-timeout"] * 5,
      max_attempts=3,
  )
  with pytest.raises(httpx.ReadTimeout):
    await mgr.run()
  assert len(mgr.attempts) == 3
  # After 3 failures we hit max_attempts and raise — only 2 sleeps.
  assert sleeps == [1.0, 2.0]


async def test_unwraps_exception_group_around_read_timeout() -> None:
  mgr, sleeps = _build_manager(
      ["fail-grouped", "succeed-then-graceful"],
  )
  await mgr.run()
  assert sleeps == [1.0]


async def test_non_recoverable_error_propagates_without_retry() -> None:
  mgr, sleeps = _build_manager(
      ["fail-non-recoverable"],
      max_attempts=5,
  )
  with pytest.raises(ValueError):
    await mgr.run()
  assert sleeps == []


async def test_attempt_counter_resets_after_successful_connect() -> None:
  """A successful connect must reset the retry budget — otherwise a long-lived
  proxy that flaps once an hour would eventually stop trying.

  Without a reset, the third action (succeed-then-fail) would push the
  attempt counter to 3 == max_attempts and we'd give up. With a reset,
  the counter goes back to 1 after that action and we're free to retry."""
  mgr, sleeps = _build_manager(
      [
          "fail-read-timeout", "fail-read-timeout",
          "succeed-then-fail",
          "fail-read-timeout", "succeed-then-graceful",
      ],
      max_attempts=3,
  )
  await mgr.run()
  assert sleeps == [1.0, 2.0, 1.0, 2.0]


async def test_cancelled_error_clears_session_and_propagates() -> None:
  state = _UpstreamSessionHolder()
  state.set_session(object(), lambda: "id")  # type: ignore[arg-type]

  class _CancelOnce(_UpstreamConnectionManager):
    async def _open_and_serve(self) -> None:
      raise asyncio.CancelledError()

  mgr = _CancelOnce(
      state=state,
      config=ProxyConfig(),
      url="x",
      headers={},
      auth=httpx.BasicAuth("u", "p"),
      logger=logging.getLogger("test.cancel"),
  )

  with pytest.raises(asyncio.CancelledError):
    await mgr.run()
  # state must be cleared so any waiters block on the next reconnect.
  assert state._session is None


# -- Session holder -----------------------------------------------------------


async def test_holder_wait_active_blocks_until_set() -> None:
  state = _UpstreamSessionHolder()
  marker = object()

  async def setter() -> None:
    await asyncio.sleep(0.01)
    state.set_session(marker, lambda: "s")  # type: ignore[arg-type]

  async with asyncio.TaskGroup() as tg:
    tg.create_task(setter())
    session = await asyncio.wait_for(state.wait_active(), timeout=1.0)
    assert session is marker


async def test_holder_clear_makes_subsequent_waiters_block() -> None:
  state = _UpstreamSessionHolder()
  state.set_session(object(), lambda: "s1")  # type: ignore[arg-type]
  state.clear_session()

  with pytest.raises(asyncio.TimeoutError):
    await asyncio.wait_for(state.wait_active(), timeout=0.05)
