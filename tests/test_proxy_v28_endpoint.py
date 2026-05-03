"""Tests for the v28 endpoint format detection in proxy.py."""

from __future__ import annotations

from bc_mcp_proxy.config import ProxyConfig
from bc_mcp_proxy.proxy import (
    _build_endpoint_url,
    _build_transport_headers,
    _is_v28_endpoint,
)


def test_legacy_host_is_not_v28() -> None:
  assert _is_v28_endpoint("https://api.businesscentral.dynamics.com") is False
  assert _is_v28_endpoint("https://api.businesscentral.dynamics.com/v2.0/x/mcp") is False


def test_new_host_is_v28() -> None:
  assert _is_v28_endpoint("https://mcp.businesscentral.dynamics.com") is True


def test_v28_detection_is_case_insensitive() -> None:
  assert _is_v28_endpoint("https://MCP.BusinessCentral.Dynamics.Com") is True


def test_v28_detection_handles_trailing_slash_and_path() -> None:
  assert _is_v28_endpoint("https://mcp.businesscentral.dynamics.com/") is True


def test_legacy_endpoint_url_has_path_with_environment() -> None:
  cfg = ProxyConfig(
      base_url="https://api.businesscentral.dynamics.com",
      environment="Production",
  )
  assert _build_endpoint_url(cfg) == "https://api.businesscentral.dynamics.com/v2.0/Production/mcp"


def test_legacy_endpoint_url_strips_trailing_slash_on_base() -> None:
  cfg = ProxyConfig(
      base_url="https://api.businesscentral.dynamics.com/",
      environment="Sandbox",
  )
  assert _build_endpoint_url(cfg) == "https://api.businesscentral.dynamics.com/v2.0/Sandbox/mcp"


def test_v28_endpoint_url_is_bare_host() -> None:
  cfg = ProxyConfig(
      base_url="https://mcp.businesscentral.dynamics.com",
      environment="Sandbox",
  )
  assert _build_endpoint_url(cfg) == "https://mcp.businesscentral.dynamics.com"


def test_legacy_headers_do_not_include_tenant_or_environment() -> None:
  cfg = ProxyConfig(
      base_url="https://api.businesscentral.dynamics.com",
      tenant_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
      environment="Production",
      company="CRONUS USA",
      configuration_name="My MCP Configuration",
  )
  h = _build_transport_headers(cfg)
  assert "Company" in h
  assert "ConfigurationName" in h
  # These are inferred from URL path on the legacy host — must not duplicate.
  assert "TenantId" not in h
  assert "EnvironmentName" not in h


def test_v28_headers_include_tenant_id_and_environment_name() -> None:
  cfg = ProxyConfig(
      base_url="https://mcp.businesscentral.dynamics.com",
      tenant_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
      environment="Sandbox",
      company="CRONUS USA",
      configuration_name="My MCP Configuration",
  )
  h = _build_transport_headers(cfg)
  assert h["TenantId"] == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
  assert h["EnvironmentName"] == "Sandbox"
  assert h["Company"] == "CRONUS USA"
  assert h["ConfigurationName"] == "My MCP Configuration"


def test_v28_headers_omit_tenant_when_not_configured() -> None:
  """If tenant_id isn't set (e.g. custom_auth_header path), don't emit a stub."""
  cfg = ProxyConfig(
      base_url="https://mcp.businesscentral.dynamics.com",
      tenant_id=None,
      environment="Sandbox",
  )
  h = _build_transport_headers(cfg)
  assert "TenantId" not in h
  assert h["EnvironmentName"] == "Sandbox"
