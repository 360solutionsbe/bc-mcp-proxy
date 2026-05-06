"""Tests for the persistent tools/list disk cache."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest
from mcp.types import ListToolsResult, Tool

from bc_mcp_proxy import tools_cache
from bc_mcp_proxy.config import ProxyConfig


def _make_result(name: str = "ListItems_PAG30008") -> ListToolsResult:
  return ListToolsResult(
      tools=[
          Tool(name=name, description="d", inputSchema={"type": "object"}),
      ],
  )


def _make_config(**overrides: Any) -> ProxyConfig:
  base: dict[str, Any] = {
      "tenant_id": "aaaa-bbbb",
      "environment": "Demo",
      "company": "CRONUS BE",
      "configuration_name": "Demo MCP",
      "base_url": "https://mcp.businesscentral.dynamics.com",
  }
  base.update(overrides)
  return ProxyConfig(**base)


@pytest.fixture(autouse=True)
def _isolate_cache_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
  """Redirect the cache directory to a per-test tmp dir.

  Patching `_default_cache_dir` keeps tests away from the real
  %LOCALAPPDATA%/XDG_CACHE_HOME path."""
  monkeypatch.setattr(tools_cache, "_default_cache_dir", lambda: tmp_path)
  return tmp_path


def test_save_then_load_round_trips(_isolate_cache_dir: Path) -> None:
  config = _make_config()
  result = _make_result()
  tools_cache.save_disk_cache(config, result)
  loaded = tools_cache.load_disk_cache(config)
  assert loaded is not None
  assert [t.name for t in loaded.tools] == [t.name for t in result.tools]


def test_load_returns_none_when_no_file(_isolate_cache_dir: Path) -> None:
  config = _make_config()
  assert tools_cache.load_disk_cache(config) is None


def test_load_returns_none_when_expired(
    _isolate_cache_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
  config = _make_config(tools_disk_cache_ttl_seconds=10.0)
  tools_cache.save_disk_cache(config, _make_result())
  # Move time forward past the TTL.
  fake_now = time.time() + 60
  monkeypatch.setattr(tools_cache.time, "time", lambda: fake_now)
  assert tools_cache.load_disk_cache(config) is None


def test_load_returns_none_for_unknown_schema(_isolate_cache_dir: Path) -> None:
  config = _make_config()
  path = tools_cache.cache_path(config)
  path.parent.mkdir(parents=True, exist_ok=True)
  path.write_text(
      json.dumps({"schema": 999, "fetched_at": time.time(), "tools": {}}),
      encoding="utf-8",
  )
  assert tools_cache.load_disk_cache(config) is None


def test_load_returns_none_for_corrupt_json(_isolate_cache_dir: Path) -> None:
  config = _make_config()
  path = tools_cache.cache_path(config)
  path.parent.mkdir(parents=True, exist_ok=True)
  path.write_text("{not json", encoding="utf-8")
  assert tools_cache.load_disk_cache(config) is None


def test_save_uses_atomic_write_and_leaves_no_tmp(_isolate_cache_dir: Path) -> None:
  config = _make_config()
  tools_cache.save_disk_cache(config, _make_result())
  path = tools_cache.cache_path(config)
  assert path.is_file()
  # No leftover .tmp file after a successful write.
  assert not path.with_suffix(path.suffix + ".tmp").exists()


def test_cache_key_changes_with_company(_isolate_cache_dir: Path) -> None:
  a = tools_cache.cache_path(_make_config(company="A"))
  b = tools_cache.cache_path(_make_config(company="B"))
  assert a != b


def test_cache_key_changes_with_base_url(_isolate_cache_dir: Path) -> None:
  v27 = tools_cache.cache_path(_make_config(base_url="https://api.businesscentral.dynamics.com"))
  v28 = tools_cache.cache_path(_make_config(base_url="https://mcp.businesscentral.dynamics.com"))
  assert v27 != v28


def test_cache_key_stable_across_calls(_isolate_cache_dir: Path) -> None:
  cfg = _make_config()
  assert tools_cache.cache_path(cfg) == tools_cache.cache_path(cfg)


def test_save_failure_is_swallowed(
    _isolate_cache_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
  """Disk-write failures must not propagate — the proxy works without the cache."""
  def boom(*args: Any, **kwargs: Any) -> None:
    raise OSError("disk full")

  monkeypatch.setattr(Path, "write_text", boom)
  # Should not raise.
  tools_cache.save_disk_cache(_make_config(), _make_result())
