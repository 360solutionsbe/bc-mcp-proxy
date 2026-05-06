"""Tests for OAuth scope auto-resolution from base_url."""

from __future__ import annotations

from bc_mcp_proxy.config import (
    V27_SCOPE,
    V28_SCOPE,
    is_v28_endpoint,
    resolve_token_scope,
)


def test_v28_host_resolves_to_v28_scope() -> None:
  assert resolve_token_scope("https://mcp.businesscentral.dynamics.com", None) == V28_SCOPE


def test_v27_host_resolves_to_v27_scope() -> None:
  assert resolve_token_scope("https://api.businesscentral.dynamics.com", None) == V27_SCOPE


def test_explicit_override_always_wins_on_v28() -> None:
  assert (
      resolve_token_scope("https://mcp.businesscentral.dynamics.com", "https://custom/.default")
      == "https://custom/.default"
  )


def test_explicit_override_always_wins_on_v27() -> None:
  assert (
      resolve_token_scope("https://api.businesscentral.dynamics.com", V28_SCOPE) == V28_SCOPE
  )


def test_unknown_host_falls_back_to_v27_scope() -> None:
  assert resolve_token_scope("https://example.com", None) == V27_SCOPE


def test_v28_detection_round_trips_through_resolve() -> None:
  url = "https://MCP.BusinessCentral.Dynamics.Com/"
  assert is_v28_endpoint(url) is True
  assert resolve_token_scope(url, None) == V28_SCOPE


def test_empty_override_string_does_not_count_as_override() -> None:
  # _select() in __main__ may pass through an empty string from env vars;
  # we treat that the same as "not set".
  assert resolve_token_scope("https://mcp.businesscentral.dynamics.com", "") == V28_SCOPE
