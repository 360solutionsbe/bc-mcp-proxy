"""Tests for the silent → interactive → device-code acquisition chain
and the auth_mode switch (auto / interactive / device_code).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from bc_mcp_proxy.auth import MsalDeviceCodeTokenProvider


class _FakeApp:
  """Stand-in for msal.PublicClientApplication covering all three paths."""

  def __init__(self) -> None:
    self.accounts: list[dict[str, Any]] = []
    self.silent_result: dict[str, Any] | None = None
    self.interactive_calls = 0
    self.interactive_result: dict[str, Any] | None = None
    self.interactive_raises: BaseException | None = None
    self.device_flow_calls = 0
    self.device_result: dict[str, Any] = {
        "access_token": "device-token", "expires_in": 3600}

  def get_accounts(self) -> list[dict[str, Any]]:
    return list(self.accounts)

  def acquire_token_silent(self, scopes, account=None, **_: Any):
    return self.silent_result

  def acquire_token_interactive(self, scopes, **_: Any):
    self.interactive_calls += 1
    if self.interactive_raises is not None:
      raise self.interactive_raises
    return self.interactive_result

  def initiate_device_flow(self, scopes):
    self.device_flow_calls += 1
    return {
        "user_code": "ABC123",
        "verification_uri": "https://login.microsoft.com/device",
        "message": "To sign in, use code ABC123 ...",
    }

  def acquire_token_by_device_flow(self, flow):
    return self.device_result


def _provider(tmp_path: Path, app: _FakeApp, *, auth_mode: str) -> MsalDeviceCodeTokenProvider:
  with patch("bc_mcp_proxy.auth.PersistedTokenCache"), patch(
      "bc_mcp_proxy.auth.FilePersistence"
  ), patch("bc_mcp_proxy.auth.msal.PublicClientApplication", return_value=app):
    return MsalDeviceCodeTokenProvider(
        tenant_id="tenant",
        client_id="client-abc",
        scopes=["scope/.default"],
        cache_path=tmp_path / "cache.bin",
        logger=logging.getLogger("test"),
        auth_mode=auth_mode,
    )


async def test_interactive_mode_returns_token_without_device_flow(tmp_path: Path) -> None:
  app = _FakeApp()
  app.interactive_result = {"access_token": "interactive-token", "expires_in": 3600}

  provider = _provider(tmp_path, app, auth_mode="interactive")
  token = await provider.get_token()

  assert token == "interactive-token"
  assert app.interactive_calls == 1
  assert app.device_flow_calls == 0


async def test_auto_falls_back_to_device_code_when_interactive_raises(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
  app = _FakeApp()
  app.interactive_raises = RuntimeError("no browser / cannot bind loopback port")

  provider = _provider(tmp_path, app, auth_mode="auto")
  with caplog.at_level(logging.WARNING, logger="test"):
    token = await provider.get_token()

  assert token == "device-token"
  assert app.interactive_calls == 1
  assert app.device_flow_calls == 1
  assert any("falling back to device code" in r.message for r in caplog.records)


async def test_interactive_mode_raises_actionable_on_redirect_uri_misconfig(
    tmp_path: Path,
) -> None:
  app = _FakeApp()
  app.interactive_result = {
      "error": "invalid_request",
      "error_description": "AADSTS500113: No reply address is registered for the application.",
  }

  provider = _provider(tmp_path, app, auth_mode="interactive")
  with pytest.raises(RuntimeError) as excinfo:
    await provider.get_token()

  msg = str(excinfo.value)
  assert "redirect URI" in msg
  assert "http://localhost" in msg
  assert "client-abc" in msg  # names the offending app registration
  assert app.device_flow_calls == 0  # interactive mode must not fall back


async def test_auto_falls_back_when_interactive_returns_redirect_misconfig(
    tmp_path: Path,
) -> None:
  app = _FakeApp()
  app.interactive_result = {
      "error": "invalid_request",
      "error_description": "AADSTS50011: redirect URI does not match.",
  }

  provider = _provider(tmp_path, app, auth_mode="auto")
  token = await provider.get_token()

  assert token == "device-token"
  assert app.interactive_calls == 1
  assert app.device_flow_calls == 1


async def test_device_code_mode_skips_interactive_entirely(tmp_path: Path) -> None:
  app = _FakeApp()
  app.interactive_result = {"access_token": "should-not-be-used", "expires_in": 3600}

  provider = _provider(tmp_path, app, auth_mode="device_code")
  token = await provider.get_token()

  assert token == "device-token"
  assert app.interactive_calls == 0
  assert app.device_flow_calls == 1


async def test_silent_path_wins_before_interactive(tmp_path: Path) -> None:
  app = _FakeApp()
  app.accounts = [{"username": "user@example.com"}]
  app.silent_result = {"access_token": "silent-token", "expires_in": 3600}
  app.interactive_result = {"access_token": "interactive-token", "expires_in": 3600}

  provider = _provider(tmp_path, app, auth_mode="auto")
  token = await provider.get_token()

  assert token == "silent-token"
  assert app.interactive_calls == 0
  assert app.device_flow_calls == 0
