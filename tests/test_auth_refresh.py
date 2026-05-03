"""Tests for pre-emptive MSAL silent token refresh in auth.MsalDeviceCodeTokenProvider."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from bc_mcp_proxy.auth import MsalDeviceCodeTokenProvider


class _FakeMsalApp:
  """Hand-rolled stand-in for msal.PublicClientApplication."""

  def __init__(self) -> None:
    self.accounts: list[dict[str, Any]] = []
    self.silent_calls: list[dict[str, Any]] = []
    self.silent_results: list[dict[str, Any] | None] = []
    self.device_flow_calls = 0

  def get_accounts(self) -> list[dict[str, Any]]:
    return list(self.accounts)

  def acquire_token_silent(self, scopes, account=None, force_refresh=False, **_: Any):
    self.silent_calls.append({"scopes": scopes, "account": account, "force_refresh": force_refresh})
    if not self.silent_results:
      return None
    return self.silent_results.pop(0)

  def initiate_device_flow(self, scopes):  # pragma: no cover - not exercised in this fix
    self.device_flow_calls += 1
    return {"user_code": "ABC123", "verification_uri": "https://example/devicelogin"}

  def acquire_token_by_device_flow(self, flow):  # pragma: no cover
    return {"access_token": "device-flow-token", "expires_in": 3600}


def _build_provider(
    tmp_path: Path,
    msal_app: _FakeMsalApp,
    *,
    skew: float = 300.0,
    now: float = 1000.0,
) -> MsalDeviceCodeTokenProvider:
  clock = {"value": now}
  with patch("bc_mcp_proxy.auth.PersistedTokenCache"), patch(
      "bc_mcp_proxy.auth.FilePersistence"
  ), patch("bc_mcp_proxy.auth.msal.PublicClientApplication", return_value=msal_app):
    provider = MsalDeviceCodeTokenProvider(
        tenant_id="tenant",
        client_id="client",
        scopes=["scope/.default"],
        cache_path=tmp_path / "cache.bin",
        logger=logging.getLogger("test"),
        refresh_skew_seconds=skew,
        time_source=lambda: clock["value"],
    )
  # Expose the clock so tests can advance time.
  provider._clock = clock  # type: ignore[attr-defined]
  return provider


def _advance(provider: MsalDeviceCodeTokenProvider, seconds: float) -> None:
  provider._clock["value"] += seconds  # type: ignore[attr-defined]


async def test_first_call_acquires_silent_and_caches_expiry(tmp_path: Path) -> None:
  msal_app = _FakeMsalApp()
  msal_app.accounts = [{"username": "user@example.com"}]
  msal_app.silent_results = [{"access_token": "token-A", "expires_in": 3600}]

  provider = _build_provider(tmp_path, msal_app)

  token = await provider.get_token()

  assert token == "token-A"
  assert len(msal_app.silent_calls) == 1
  assert msal_app.silent_calls[0]["force_refresh"] is False


async def test_second_call_within_validity_returns_cached_token_without_msal(
    tmp_path: Path,
) -> None:
  msal_app = _FakeMsalApp()
  msal_app.accounts = [{"username": "user@example.com"}]
  msal_app.silent_results = [{"access_token": "token-A", "expires_in": 3600}]

  provider = _build_provider(tmp_path, msal_app, skew=300.0)

  await provider.get_token()
  # Advance time so token still has 30 minutes left, well above 5-min skew.
  _advance(provider, 1800)

  token = await provider.get_token()

  assert token == "token-A"
  # Critical: no second MSAL call — purely cached.
  assert len(msal_app.silent_calls) == 1


async def test_call_close_to_expiry_forces_refresh(tmp_path: Path) -> None:
  msal_app = _FakeMsalApp()
  msal_app.accounts = [{"username": "user@example.com"}]
  msal_app.silent_results = [
      {"access_token": "token-A", "expires_in": 3600},
      {"access_token": "token-B", "expires_in": 3600},
  ]

  provider = _build_provider(tmp_path, msal_app, skew=300.0)

  await provider.get_token()
  # Token now has only 240s left — inside the 300s skew window.
  _advance(provider, 3360)

  token = await provider.get_token()

  assert token == "token-B"
  assert len(msal_app.silent_calls) == 2
  # First call may not force; second must, because we know the cached one is stale.
  assert msal_app.silent_calls[1]["force_refresh"] is True


async def test_call_at_exactly_skew_boundary_still_refreshes(tmp_path: Path) -> None:
  """Remaining validity == skew should not be considered "still valid"."""
  msal_app = _FakeMsalApp()
  msal_app.accounts = [{"username": "user@example.com"}]
  msal_app.silent_results = [
      {"access_token": "token-A", "expires_in": 600},
      {"access_token": "token-B", "expires_in": 3600},
  ]

  provider = _build_provider(tmp_path, msal_app, skew=300.0)

  await provider.get_token()
  # Remaining validity now exactly equals skew (300s). Must refresh.
  _advance(provider, 300)

  token = await provider.get_token()

  assert token == "token-B"
  assert msal_app.silent_calls[-1]["force_refresh"] is True


async def test_missing_expires_in_falls_back_to_zero(tmp_path: Path) -> None:
  """If MSAL returns a token without expires_in, treat it as immediately stale."""
  msal_app = _FakeMsalApp()
  msal_app.accounts = [{"username": "user@example.com"}]
  msal_app.silent_results = [
      {"access_token": "token-A"},  # no expires_in
      {"access_token": "token-B", "expires_in": 3600},
  ]

  provider = _build_provider(tmp_path, msal_app, skew=300.0)

  await provider.get_token()
  # No time advance — but expires_in=0 means expires_at == now, well inside skew.
  token = await provider.get_token()

  assert token == "token-B"
  assert msal_app.silent_calls[-1]["force_refresh"] is True


async def test_invalid_expires_in_does_not_crash(tmp_path: Path) -> None:
  msal_app = _FakeMsalApp()
  msal_app.accounts = [{"username": "user@example.com"}]
  msal_app.silent_results = [{"access_token": "token-A", "expires_in": "not-a-number"}]

  provider = _build_provider(tmp_path, msal_app)

  token = await provider.get_token()

  assert token == "token-A"


def test_negative_skew_is_clamped_to_zero(tmp_path: Path) -> None:
  msal_app = _FakeMsalApp()
  provider = _build_provider(tmp_path, msal_app, skew=-100.0)
  assert provider._refresh_skew_seconds == 0.0
