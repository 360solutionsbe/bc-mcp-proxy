"""Tests for the device-flow user prompt routing.

stdout is the MCP stdio transport channel — the client reads it line by
line and parses each line as JSON-RPC. Anything we write to stdout that
isn't a valid JSON-RPC message breaks the transport and the client
disconnects with `Unexpected token … is not valid JSON`. The default
device-flow callback used to `print()` the "To sign in, use code …"
prompt to stdout, which corrupted the protocol channel on the first
authentication of every fresh tenant/client pair.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from bc_mcp_proxy.auth import MsalDeviceCodeTokenProvider


class _FakeMsalApp:
  def get_accounts(self) -> list[dict[str, Any]]:  # pragma: no cover - unused here
    return []


def _build_provider(tmp_path: Path) -> MsalDeviceCodeTokenProvider:
  with patch("bc_mcp_proxy.auth.PersistedTokenCache"), patch(
      "bc_mcp_proxy.auth.FilePersistence"
  ), patch("bc_mcp_proxy.auth.msal.PublicClientApplication", return_value=_FakeMsalApp()):
    return MsalDeviceCodeTokenProvider(
        tenant_id="tenant",
        client_id="client",
        scopes=["scope/.default"],
        cache_path=tmp_path / "cache.bin",
        logger=logging.getLogger("test"),
    )


def test_default_callback_does_not_write_to_stdout(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
  """Locks down the contract that the MCP stdio channel stays clean."""
  provider = _build_provider(tmp_path)
  flow = {
      "user_code": "GG35EP4XV",
      "verification_uri": "https://login.microsoft.com/device",
      "message": "To sign in, use code GG35EP4XV at https://login.microsoft.com/device.",
  }

  provider._default_flow_callback(flow)

  captured = capsys.readouterr()
  assert captured.out == "", f"stdout was corrupted with: {captured.out!r}"
  assert "GG35EP4XV" in captured.err


def test_default_callback_falls_back_when_message_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
  """If MSAL omits the pre-formatted `message`, we still emit something useful — and still to stderr."""
  provider = _build_provider(tmp_path)
  flow = {
      "user_code": "GG35EP4XV",
      "verification_uri": "https://login.microsoft.com/device",
  }

  provider._default_flow_callback(flow)

  captured = capsys.readouterr()
  assert captured.out == ""
  assert "GG35EP4XV" in captured.err
  assert "https://login.microsoft.com/device" in captured.err


def test_default_callback_also_logs_through_logger(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
  """The logger path is kept alongside the stderr print so structured
  log handlers (e.g. JSON file logs) still capture the prompt."""
  provider = _build_provider(tmp_path)
  flow = {"message": "To sign in, use code XYZ at https://example/devicelogin."}

  with caplog.at_level(logging.WARNING, logger="test"):
    provider._default_flow_callback(flow)

  assert any("XYZ" in r.message for r in caplog.records)
