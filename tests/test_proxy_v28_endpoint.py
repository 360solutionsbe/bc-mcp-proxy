"""Tests for the v28 endpoint format detection in proxy.py."""

from __future__ import annotations

import pytest

from bc_mcp_proxy._version import __version__
from bc_mcp_proxy.config import ProxyConfig
from bc_mcp_proxy.proxy import (
    _build_endpoint_url,
    _build_transport_headers,
    _encode_header_value,
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


def test_regional_subdomain_is_modern() -> None:
  """Any *.businesscentral.dynamics.com host other than the legacy api.*
  host follows the header-routed contract Microsoft documents for v28+."""
  assert _is_v28_endpoint("https://eu.businesscentral.dynamics.com") is True


def test_regional_subdomain_url_is_bare() -> None:
  cfg = ProxyConfig(base_url="https://eu.businesscentral.dynamics.com/", environment="Production")
  assert _build_endpoint_url(cfg) == "https://eu.businesscentral.dynamics.com"


def test_regional_subdomain_headers_include_tenant_and_env() -> None:
  cfg = ProxyConfig(
      base_url="https://eu.businesscentral.dynamics.com",
      tenant_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
      environment="Production",
  )
  h = _build_transport_headers(cfg)
  assert h["TenantId"] == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
  assert h["EnvironmentName"] == "Production"


def test_apex_bc_host_routes_modern() -> None:
  cfg = ProxyConfig(base_url="https://businesscentral.dynamics.com", environment="Production")
  assert _build_endpoint_url(cfg) == "https://businesscentral.dynamics.com"


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


def test_v28_headers_strip_surrounding_whitespace() -> None:
  """A trailing space on tenant_id/environment must not reach the wire.

  Without stripping, httpx/h11 raises `LocalProtocolError("Illegal header
  value …")` because HTTP forbids trailing whitespace in header values.
  Reproduces the v27.5 prospect report from 2026-05-14.
  """
  cfg = ProxyConfig(
      base_url="https://mcp.businesscentral.dynamics.com",
      tenant_id="424d4f18-97e7-4dca-8b0e-804a146eca73 ",
      environment=" Sandbox ",
      company=" CRONUS USA ",
      configuration_name=" My MCP Configuration ",
  )
  h = _build_transport_headers(cfg)
  assert h["TenantId"] == "424d4f18-97e7-4dca-8b0e-804a146eca73"
  assert h["EnvironmentName"] == "Sandbox"
  assert h["Company"] == "CRONUS USA"
  assert h["ConfigurationName"] == "My MCP Configuration"


def test_v28_headers_preserve_internal_whitespace() -> None:
  """Only surrounding whitespace is stripped — `CRONUS USA` stays as-is."""
  cfg = ProxyConfig(
      base_url="https://mcp.businesscentral.dynamics.com",
      tenant_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
      environment="Sandbox",
      company="CRONUS USA",
      configuration_name="My MCP Configuration",
  )
  h = _build_transport_headers(cfg)
  assert h["Company"] == "CRONUS USA"
  assert h["ConfigurationName"] == "My MCP Configuration"


def test_company_and_configuration_name_are_not_url_decoded() -> None:
  """Values are sent literally. The upstream sample ran these through
  urllib.parse.unquote, which silently corrupts any name containing
  '%' or '+' — our config is entered verbatim, never URL-encoded."""
  cfg = ProxyConfig(
      base_url="https://mcp.businesscentral.dynamics.com",
      tenant_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
      environment="Production",
      company="R&D %1 + Co",
      configuration_name="Cfg %2B name+",
  )
  h = _build_transport_headers(cfg)
  # unquote() would have turned "%1"/"%2B"/"+" into other characters.
  assert h["Company"] == "R&D %1 + Co"
  assert h["ConfigurationName"] == "Cfg %2B name+"


# -- SEP-2243 Base64 encoding of non-ASCII header values ---------------------
#
# Microsoft Learn ("Connect to Business Central MCP server with non-Microsoft
# hosts"): Company / ConfigurationName values containing non-ASCII characters
# must be sent as `=?base64?<base64 of UTF-8>?=`. The documented example is
# `Cronus Århus A/S` -> `=?base64?Q3JvbnVzIMOFcmh1cyBBL1M=?=`.


def test_non_ascii_company_is_base64_encoded_like_microsoft_example() -> None:
  # Microsoft's example string decodes to "Cronus Århus A/S" (mixed case).
  cfg = ProxyConfig(company="Cronus Århus A/S")
  assert _build_transport_headers(cfg)["Company"] == "=?base64?Q3JvbnVzIMOFcmh1cyBBL1M=?="


def test_non_ascii_configuration_name_is_base64_encoded() -> None:
  cfg = ProxyConfig(configuration_name="ÅrhusSalesTeamConfig")
  assert (_build_transport_headers(cfg)["ConfigurationName"]
          == "=?base64?w4VyaHVzU2FsZXNUZWFtQ29uZmln?=")


def test_ascii_company_is_sent_verbatim() -> None:
  assert _encode_header_value("CRONUS USA, Inc.") == "CRONUS USA, Inc."


def test_non_ascii_company_is_stripped_before_encoding() -> None:
  assert _encode_header_value(" Crónus ") == _encode_header_value("Crónus")
  assert _encode_header_value("Crónus") == "=?base64?Q3LDs251cw==?="


def test_base64_encoding_applies_on_legacy_host_too() -> None:
  cfg = ProxyConfig(base_url="https://api.businesscentral.dynamics.com", company="Société")
  assert _build_transport_headers(cfg)["Company"].startswith("=?base64?")


@pytest.mark.parametrize("value", ["", "R&D %1 + Co", "plain"])
def test_encode_header_value_leaves_ascii_alone(value: str) -> None:
  assert _encode_header_value(value) == value.strip()


def test_encode_header_value_handles_four_byte_utf8() -> None:
  # Emoji is 4 bytes in UTF-8; must round-trip through base64 intact.
  import base64
  encoded = _encode_header_value("Shop 🛒")
  assert encoded.startswith("=?base64?") and encoded.endswith("?=")
  assert base64.b64decode(encoded[9:-2]).decode("utf-8") == "Shop 🛒"


# -- X-Client-Application -----------------------------------------------------


def test_client_application_header_carries_product_and_version() -> None:
  """BC telemetry (event RT0054) records this as clientName."""
  h = _build_transport_headers(ProxyConfig())
  assert h["X-Client-Application"] == f"vgs-bc-mcp/{__version__}"


def test_client_application_header_honours_custom_server_name() -> None:
  h = _build_transport_headers(ProxyConfig(server_name="my-proxy", server_version="9.9"))
  assert h["X-Client-Application"] == "my-proxy/9.9"
