"""Tests for the tenant/client GUID validation at startup."""

from __future__ import annotations

import pytest

from bc_mcp_proxy.__main__ import _guid_problem, main
from bc_mcp_proxy.config import ProxyConfig

GOOD = "d03a7984-401f-4c99-9644-83fbaaede76e"


def test_valid_guids_pass() -> None:
  assert _guid_problem(ProxyConfig(tenant_id=GOOD, client_id=GOOD)) is None


def test_truncated_tenant_id_is_explained() -> None:
  # The exact failure seen on 2026-09-12: the last character was lost on paste.
  problem = _guid_problem(ProxyConfig(tenant_id=GOOD[:-1], client_id=GOOD))
  assert problem is not None
  assert "Tenant ID" in problem and "got 35" in problem and "36 characters" in problem


def test_missing_client_id_is_explained() -> None:
  problem = _guid_problem(ProxyConfig(tenant_id=GOOD, client_id=None))
  assert problem is not None and problem.startswith("Client ID is required")


def test_custom_auth_header_skips_validation() -> None:
  assert _guid_problem(ProxyConfig(tenant_id="nope", client_id=None, custom_auth_header="Bearer x")) is None


def test_uppercase_guid_is_accepted() -> None:
  assert _guid_problem(ProxyConfig(tenant_id=GOOD.upper(), client_id=GOOD)) is None


def test_main_exits_with_code_2_on_bad_tenant(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
  for name in ("BC_TENANT_ID", "BC_CLIENT_ID", "BC_CUSTOM_AUTH_HEADER", "BC_BASE_URL", "BC_AUTH_MODE"):
    monkeypatch.delenv(name, raising=False)
  with pytest.raises(SystemExit) as excinfo:
    main(["--TenantId", GOOD[:-1], "--ClientId", GOOD, "--Company", "X"])
  assert excinfo.value.code == 2
  assert "not a valid GUID" in capsys.readouterr().err
