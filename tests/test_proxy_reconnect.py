"""Tests for upstream reconnect/backoff logic in proxy.py (Fix #1)."""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

import httpx
import pytest

from mcp.shared.exceptions import McpError
from mcp.types import INTERNAL_ERROR, ErrorData

from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

from bc_mcp_proxy.config import ProxyConfig
from bc_mcp_proxy.proxy import (
    _BaseExceptionGroup,
    _backoff_for_attempt,
    _exception_hints_at_client_cancel,
    _format_upstream_rejection,
    _is_recoverable_upstream_error,
    _parse_retry_after,
    _permanent_rejection_reason,
    _permanent_upstream_status,
    _retry_after_seconds,
    _retryable_upstream_status,
    _UpstreamConnectionManager,
    _UpstreamConnectRejected,
    _UpstreamSessionHolder,
)


def _http_status_error(
    status: int, headers: Optional[dict[str, str]] = None,
) -> httpx.HTTPStatusError:
  request = httpx.Request("POST", "https://mcp.businesscentral.dynamics.com")
  response = httpx.Response(status, request=request, headers=headers)
  return httpx.HTTPStatusError(
      f"{status}", request=request, response=response)


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


# -- Transient HTTP statuses (BC online rate limits / operational limits) ----
#
# Before this, a 429 from Business Central was neither "permanent" (429 was
# excluded from _permanent_upstream_status) nor "recoverable" (HTTPStatusError
# is not in _RECOVERABLE_HTTPX_ERRORS), so run() re-raised and the proxy
# process exited on the first rate limit.


@pytest.mark.parametrize("status", [408, 429, 503, 504])
def test_transient_statuses_are_recoverable(status: int) -> None:
  assert _is_recoverable_upstream_error(_http_status_error(status))
  assert _is_recoverable_upstream_error(
      _BaseExceptionGroup("wrapped", [_http_status_error(status)]))
  assert _permanent_upstream_status(_http_status_error(status)) is None
  assert _retryable_upstream_status(_http_status_error(status)) == status


@pytest.mark.parametrize("status", [500, 502])
def test_other_5xx_is_not_recoverable(status: int) -> None:
  # An unexpected 5xx is neither a config problem nor a documented transient;
  # let it propagate so the failure is visible rather than retried blindly.
  assert not _is_recoverable_upstream_error(_http_status_error(status))
  assert _permanent_upstream_status(_http_status_error(status)) is None


def test_429_wrapped_in_mcp_error_cause_chain_is_recoverable() -> None:
  # The client lib wraps the transport error during initialize(); the status
  # must be found along __cause__.
  status_error = _http_status_error(429)
  wrapped = McpError(ErrorData(code=INTERNAL_ERROR, message="init failed"))
  wrapped.__cause__ = status_error
  assert _is_recoverable_upstream_error(wrapped)
  assert _retryable_upstream_status(_BaseExceptionGroup("g", [wrapped])) == 429
  assert _permanent_rejection_reason(wrapped) is None


def test_429_does_not_hint_at_client_cancellation() -> None:
  assert not _exception_hints_at_client_cancel(_http_status_error(429))
  assert _exception_hints_at_client_cancel(_http_status_error(404))


# -- Retry-After --------------------------------------------------------------


def test_retry_after_seconds_integer() -> None:
  assert _retry_after_seconds(_http_status_error(429, {"Retry-After": "30"})) == 30.0


def test_retry_after_http_date() -> None:
  now = datetime(2026, 9, 12, 12, 0, 0, tzinfo=timezone.utc)
  when = format_datetime(now + timedelta(seconds=45), usegmt=True)
  assert _retry_after_seconds(_http_status_error(503, {"Retry-After": when}), now=now) == 45.0


def test_retry_after_in_the_past_is_ignored() -> None:
  now = datetime(2026, 9, 12, 12, 0, 0, tzinfo=timezone.utc)
  when = format_datetime(now - timedelta(seconds=5), usegmt=True)
  assert _retry_after_seconds(_http_status_error(503, {"Retry-After": when}), now=now) is None


def test_retry_after_garbage_ignored() -> None:
  assert _parse_retry_after("soon") is None
  assert _parse_retry_after("") is None
  assert _parse_retry_after(None) is None
  assert _retry_after_seconds(_http_status_error(429)) is None
  assert _retry_after_seconds(httpx.ReadTimeout("x")) is None


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
    if action == "fail-400":
      raise _http_status_error(400)
    if action == "fail-400-grouped":
      raise _BaseExceptionGroup("wrapped", [_http_status_error(400)])
    if action == "fail-connect-rejected":
      # Mirrors _open_and_serve converting an McpError at initialize().
      raise _BaseExceptionGroup(
          "wrapped", [_UpstreamConnectRejected("Session terminated")])
    if action.startswith("fail-429"):
      # "fail-429" or "fail-429-retry-after-<seconds>"
      headers = None
      if action.startswith("fail-429-retry-after-"):
        headers = {"Retry-After": action.rsplit("-", 1)[1]}
      raise _BaseExceptionGroup("wrapped", [_http_status_error(429, headers)])
    if action == "fail-connect-429":
      # initialize() raised McpError whose cause is the transport's 429.
      err = McpError(ErrorData(code=INTERNAL_ERROR, message="init failed"))
      err.__cause__ = _http_status_error(429)
      raise _BaseExceptionGroup("wrapped", [err])
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


async def test_429_is_retried_instead_of_crashing() -> None:
  mgr, sleeps = _build_manager(["fail-429", "succeed-then-graceful"])
  await mgr.run()
  assert mgr.attempts == ["fail-429", "succeed-then-graceful"]
  assert sleeps == [1.0]
  assert mgr.state.fatal is None


async def test_manager_honors_retry_after_capped() -> None:
  # Retry-After: 60 on the first attempt -> sleep the cap (16s), not 60s
  # and not the 1s exponential step.
  mgr, sleeps = _build_manager(
      ["fail-429-retry-after-60", "succeed-then-graceful"])
  await mgr.run()
  assert sleeps == [16.0]


async def test_manager_retry_after_below_backoff_keeps_backoff() -> None:
  # Second failure would back off 2s; a Retry-After of 1s must not shorten it.
  mgr, sleeps = _build_manager(
      ["fail-429", "fail-429-retry-after-1", "succeed-then-graceful"])
  await mgr.run()
  assert sleeps == [1.0, 2.0]


async def test_manager_retry_after_between_step_and_cap_is_used() -> None:
  mgr, sleeps = _build_manager(
      ["fail-429-retry-after-5", "succeed-then-graceful"])
  await mgr.run()
  assert sleeps == [5.0]


async def test_connect_429_not_treated_as_permanent() -> None:
  # A rate-limited handshake must back off and retry, not park as fatal.
  mgr, sleeps = _build_manager(["fail-connect-429", "succeed-then-graceful"])
  await mgr.run()
  assert mgr.attempts == ["fail-connect-429", "succeed-then-graceful"]
  assert sleeps == [1.0]
  assert mgr.state.fatal is None


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

  setter_task = asyncio.create_task(setter())
  try:
    session = await asyncio.wait_for(state.wait_active(), timeout=1.0)
    assert session is marker
  finally:
    await setter_task


async def test_holder_clear_makes_subsequent_waiters_block() -> None:
  state = _UpstreamSessionHolder()
  state.set_session(object(), lambda: "s1")  # type: ignore[arg-type]
  state.clear_session()

  with pytest.raises(asyncio.TimeoutError):
    await asyncio.wait_for(state.wait_active(), timeout=0.05)


# -- Permanent 4xx: stay-alive + clean error ---------------------------------


@pytest.mark.parametrize("status", [400, 401, 403, 404, 405])
def test_4xx_is_permanent(status: int) -> None:
  assert _permanent_upstream_status(_http_status_error(status)) == status


def test_permanent_status_unwraps_exception_group() -> None:
  eg = _BaseExceptionGroup("wrapped", [_http_status_error(400)])
  assert _permanent_upstream_status(eg) == 400


@pytest.mark.parametrize("status", [429, 500, 503])
def test_429_and_5xx_are_not_permanent(status: int) -> None:
  # 429 = transient rate limit; 5xx = server-side. Both stay off this path.
  assert _permanent_upstream_status(_http_status_error(status)) is None


def test_non_http_errors_are_not_permanent() -> None:
  assert _permanent_upstream_status(httpx.ReadTimeout("stall")) is None
  assert _permanent_upstream_status(ValueError("boom")) is None


def test_rejection_message_is_actionable() -> None:
  cfg = ProxyConfig(environment="Sandbox", company="CRONUS BE",
                     configuration_name="Demo MCP")
  msg = _format_upstream_rejection("HTTP 400", cfg)
  assert "HTTP 400" in msg
  assert "Configuration Name" in msg  # points at the most common cause
  assert "'Sandbox'" in msg and "'CRONUS BE'" in msg and "'Demo MCP'" in msg
  assert "authentication" in msg.lower()  # clarifies it's NOT a sign-in problem


def test_rejection_message_marks_unset_configuration_name() -> None:
  cfg = ProxyConfig(environment="Production", company="X",
                     configuration_name=None)
  assert "<not set>" in _format_upstream_rejection("HTTP 404", cfg)


# -- Permanent rejection reason (4xx OR 404-as-session-terminated) -----------


def test_rejection_reason_for_httpx_4xx() -> None:
  assert _permanent_rejection_reason(_http_status_error(400)) == "HTTP 400"
  eg = _BaseExceptionGroup("wrapped", [_http_status_error(401)])
  assert _permanent_rejection_reason(eg) == "HTTP 401"


def test_rejection_reason_for_connect_rejected() -> None:
  # What _open_and_serve raises when initialize() returns an McpError.
  reason = _permanent_rejection_reason(_UpstreamConnectRejected("Session terminated"))
  assert reason is not None and "404" in reason


def test_rejection_reason_for_session_terminated_mcperror() -> None:
  # Defensive: a bare session-terminated McpError leaf is also permanent.
  err = McpError(ErrorData(code=32600, message="Session terminated"))
  reason = _permanent_rejection_reason(_BaseExceptionGroup("g", [err]))
  assert reason is not None and "404" in reason


def test_rejection_reason_none_for_transient() -> None:
  assert _permanent_rejection_reason(httpx.ReadTimeout("stall")) is None
  assert _permanent_rejection_reason(_http_status_error(429)) is None


def test_rejection_messages_are_ascii_only() -> None:
  """The headline diagnostic is written to stderr and read by users on
  Windows consoles (cp1252). Non-ASCII (em-dashes, arrows) mojibakes to
  '?' there and can even raise UnicodeEncodeError. Keep it ASCII."""
  cfg = ProxyConfig(environment="Sandbox", company="CRONUS BE",
                     configuration_name="Demo MCP")
  msg = _format_upstream_rejection("HTTP 400", cfg)
  assert msg.isascii(), msg
  for exc in (_UpstreamConnectRejected("x"),
              McpError(ErrorData(code=32600, message="Session terminated"))):
    reason = _permanent_rejection_reason(_BaseExceptionGroup("g", [exc]))
    assert reason is not None and reason.isascii(), reason
    assert _format_upstream_rejection(reason, cfg).isascii()


async def test_connect_rejected_parks_alive_and_records_fatal() -> None:
  """A 404-style connect rejection (McpError at initialize) must behave
  exactly like a 400: clean error, stay alive, no crash-loop."""
  mgr, sleeps = _build_manager(["fail-connect-rejected"])
  task = asyncio.create_task(mgr.run())
  try:
    for _ in range(100):
      if mgr.state.fatal is not None:
        break
      await asyncio.sleep(0)
    assert mgr.state.fatal is not None, "fatal not recorded for connect rejection"
    assert "404" in mgr.state.fatal.error.message
    await asyncio.sleep(0)
    assert not task.done(), "run() must stay parked, not crash"
    assert sleeps == [], "permanent rejection must not retry/backoff"
  finally:
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
      await task


async def test_wait_active_raises_recorded_fatal() -> None:
  state = _UpstreamSessionHolder()
  err = McpError(ErrorData(code=INTERNAL_ERROR, message="BC rejected: HTTP 400"))
  state.set_fatal(err)

  with pytest.raises(McpError) as excinfo:
    await asyncio.wait_for(state.wait_active(), timeout=0.5)
  assert excinfo.value is err
  # Sticky: a later session does not override the fatal.
  state.set_session(object(), lambda: "s")  # type: ignore[arg-type]
  with pytest.raises(McpError):
    await state.wait_active()


async def test_permanent_4xx_parks_alive_and_records_fatal() -> None:
  mgr, sleeps = _build_manager(["fail-400"])
  task = asyncio.create_task(mgr.run())
  try:
    for _ in range(100):
      if mgr.state.fatal is not None:
        break
      await asyncio.sleep(0)
    assert mgr.state.fatal is not None, "fatal error was not recorded"
    assert "HTTP 400" in mgr.state.fatal.error.message
    await asyncio.sleep(0)
    assert not task.done(), "run() must stay parked, not exit/crash"
    assert sleeps == [], "permanent 4xx must not retry/backoff"
  finally:
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
      await task


async def test_permanent_4xx_grouped_also_parks() -> None:
  mgr, _ = _build_manager(["fail-400-grouped"])
  task = asyncio.create_task(mgr.run())
  try:
    for _ in range(100):
      if mgr.state.fatal is not None:
        break
      await asyncio.sleep(0)
    assert mgr.state.fatal is not None
    assert not task.done()
  finally:
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
      await task
