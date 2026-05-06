"""Tests for SSRF hardening on BC_BASE_URL."""

from __future__ import annotations

import pytest

from bc_mcp_proxy.config import (
    InvalidBaseUrlError,
    is_trusted_bc_host,
    validate_base_url,
)


# -- is_trusted_bc_host -------------------------------------------------------


def test_v28_host_is_trusted() -> None:
  assert is_trusted_bc_host("https://mcp.businesscentral.dynamics.com") is True


def test_v27_host_is_trusted() -> None:
  assert is_trusted_bc_host("https://api.businesscentral.dynamics.com") is True


def test_apex_bc_host_is_trusted() -> None:
  assert is_trusted_bc_host("https://businesscentral.dynamics.com") is True


def test_arbitrary_subdomain_of_bc_is_trusted() -> None:
  # Microsoft has historically introduced new subdomains (regional, staging).
  # We accept any *.businesscentral.dynamics.com to avoid breaking on those.
  assert is_trusted_bc_host("https://eu-prod.businesscentral.dynamics.com") is True


def test_http_scheme_is_not_trusted() -> None:
  assert is_trusted_bc_host("http://mcp.businesscentral.dynamics.com") is False


def test_unrelated_host_is_not_trusted() -> None:
  assert is_trusted_bc_host("https://evil.example.com") is False


def test_lookalike_host_is_not_trusted() -> None:
  # `businesscentral.dynamics.com.evil.com` must not be considered a BC host.
  assert is_trusted_bc_host("https://businesscentral.dynamics.com.evil.com") is False


def test_empty_url_is_not_trusted() -> None:
  assert is_trusted_bc_host("") is False


# -- validate_base_url --------------------------------------------------------


def test_validate_accepts_v28_default() -> None:
  validate_base_url("https://mcp.businesscentral.dynamics.com")


def test_validate_accepts_v27_default() -> None:
  validate_base_url("https://api.businesscentral.dynamics.com")


def test_validate_rejects_http_even_for_bc_host() -> None:
  with pytest.raises(InvalidBaseUrlError, match="https"):
    validate_base_url("http://mcp.businesscentral.dynamics.com")


def test_validate_rejects_unrelated_host() -> None:
  with pytest.raises(InvalidBaseUrlError, match="not a recognized"):
    validate_base_url("https://evil.example.com")


def test_validate_rejects_lookalike_host() -> None:
  with pytest.raises(InvalidBaseUrlError):
    validate_base_url("https://businesscentral.dynamics.com.evil.com")


def test_allow_non_standard_permits_arbitrary_https_host() -> None:
  validate_base_url("https://localhost:8443/mock", allow_non_standard=True)


def test_allow_non_standard_still_rejects_http() -> None:
  """The https requirement is non-bypassable — bearer tokens never go over plain HTTP."""
  with pytest.raises(InvalidBaseUrlError, match="https"):
    validate_base_url("http://localhost:8080/mock", allow_non_standard=True)
