"""Tests for whitespace handling in CLI/env config values.

A trailing space on `BC_TENANT_ID` (easy to introduce when copy-pasting a
GUID from the Azure portal) used to surface as
`LocalProtocolError("Illegal header value …")` from httpx because the v28
host puts tenant_id into a request header. The config boundary now strips
surrounding whitespace before it gets that far. See the prospect report
from 2026-05-14 (BC v27.5 → v28).
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from bc_mcp_proxy.__main__ import parse_args


_BC_ENV_VARS = (
    "BC_TENANT_ID", "BC_CLIENT_ID", "BC_COMPANY", "BC_ENVIRONMENT",
    "BC_CONFIGURATION_NAME", "BC_CUSTOM_AUTH_HEADER", "BC_BASE_URL",
    "BC_TOKEN_SCOPE", "BC_SERVER_NAME", "BC_SERVER_VERSION",
    "BC_INSTRUCTIONS", "BC_HTTP_TIMEOUT_SECONDS", "BC_SSE_TIMEOUT_SECONDS",
    "BC_DEVICE_CACHE_LOCATION", "BC_DEVICE_CACHE_NAME", "BC_LOG_LEVEL",
)


def _run_parse(monkeypatch: pytest.MonkeyPatch, **env: str) -> Any:
  """Run parse_args() with a clean env containing only the supplied vars."""
  for name in _BC_ENV_VARS:
    monkeypatch.delenv(name, raising=False)
  for name, value in env.items():
    monkeypatch.setenv(name, value)
  return parse_args([])


def test_trailing_whitespace_stripped_from_env_tenant_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
  cfg = _run_parse(monkeypatch, BC_TENANT_ID="424d4f18-97e7-4dca-8b0e-804a146eca73 ")
  assert cfg.tenant_id == "424d4f18-97e7-4dca-8b0e-804a146eca73"


def test_leading_whitespace_stripped_from_env_tenant_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
  cfg = _run_parse(monkeypatch, BC_TENANT_ID=" 424d4f18-97e7-4dca-8b0e-804a146eca73")
  assert cfg.tenant_id == "424d4f18-97e7-4dca-8b0e-804a146eca73"


def test_trailing_whitespace_stripped_from_cli_tenant_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
  for name in ("BC_TENANT_ID", "BC_CLIENT_ID", "BC_BASE_URL"):
    monkeypatch.delenv(name, raising=False)
  cfg = parse_args(["--TenantId", "424d4f18-97e7-4dca-8b0e-804a146eca73 "])
  assert cfg.tenant_id == "424d4f18-97e7-4dca-8b0e-804a146eca73"


def test_environment_whitespace_stripped(monkeypatch: pytest.MonkeyPatch) -> None:
  cfg = _run_parse(monkeypatch, BC_ENVIRONMENT="Production ")
  assert cfg.environment == "Production"


def test_internal_whitespace_in_company_preserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
  """`CRONUS USA` has a legitimate internal space — keep it."""
  cfg = _run_parse(monkeypatch, BC_COMPANY=" CRONUS USA ")
  assert cfg.company == "CRONUS USA"


def test_base_url_whitespace_stripped(monkeypatch: pytest.MonkeyPatch) -> None:
  cfg = _run_parse(
      monkeypatch, BC_BASE_URL="https://mcp.businesscentral.dynamics.com ",
  )
  assert cfg.base_url == "https://mcp.businesscentral.dynamics.com"


def test_warning_logged_when_stripping_happens(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
  """A WARNING surfaces in support logs so the cause is visible after the fact."""
  with caplog.at_level(logging.WARNING, logger="bc_mcp_proxy"):
    _run_parse(monkeypatch, BC_TENANT_ID="424d4f18-97e7-4dca-8b0e-804a146eca73 ")
  assert any("tenant_id" in r.message for r in caplog.records), caplog.records


def test_no_warning_when_value_is_already_clean(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
  with caplog.at_level(logging.WARNING, logger="bc_mcp_proxy"):
    _run_parse(monkeypatch, BC_TENANT_ID="424d4f18-97e7-4dca-8b0e-804a146eca73")
  assert not any("Stripped" in r.message for r in caplog.records)
