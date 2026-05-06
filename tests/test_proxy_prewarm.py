"""Tests for the tools/list pre-warm cache and stdio handler integration.

These tests focus on the cache layer rather than wiring up a full upstream
session — they verify that the in-memory cache honours TTL, that the
stdio _list_tools handler short-circuits when a fresh entry exists, and
that disk-cache prepopulation makes the very first call instant.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from mcp.types import ListToolsResult, Tool

from bc_mcp_proxy import tools_cache
from bc_mcp_proxy.config import ProxyConfig
from bc_mcp_proxy.proxy import _ToolsCache


def _result(name: str = "ListItems_PAG30008") -> ListToolsResult:
  return ListToolsResult(
      tools=[Tool(name=name, description="d", inputSchema={"type": "object"})],
  )


def _config(**overrides: Any) -> ProxyConfig:
  base: dict[str, Any] = {
      "tenant_id": "t", "environment": "Demo",
      "company": "C", "configuration_name": "X",
  }
  base.update(overrides)
  return ProxyConfig(**base)


# -- _ToolsCache --------------------------------------------------------------


def test_cache_returns_none_when_empty() -> None:
  cache = _ToolsCache(ttl_seconds=300.0)
  assert cache.get_fresh() is None
  assert cache.get_any() is None


def test_cache_returns_fresh_within_ttl() -> None:
  cache = _ToolsCache(ttl_seconds=10.0)
  cache.store(_result(), now=100.0)
  assert cache.get_fresh(now=105.0) is not None
  assert cache.get_fresh(now=109.99) is not None


def test_cache_misses_after_ttl() -> None:
  cache = _ToolsCache(ttl_seconds=10.0)
  cache.store(_result(), now=100.0)
  assert cache.get_fresh(now=111.0) is None


def test_cache_get_any_returns_stale_value_after_ttl() -> None:
  """get_any() exposes the last-known value even when expired so the
  stdio handler can serve stale cache while a refresh runs in the background."""
  cache = _ToolsCache(ttl_seconds=10.0)
  cache.store(_result(name="OldTool"), now=100.0)
  stale = cache.get_any()
  assert stale is not None
  assert stale.tools[0].name == "OldTool"


def test_cache_store_overwrites() -> None:
  cache = _ToolsCache(ttl_seconds=300.0)
  cache.store(_result(name="A"), now=100.0)
  cache.store(_result(name="B"), now=200.0)
  fresh = cache.get_fresh(now=201.0)
  assert fresh is not None
  assert fresh.tools[0].name == "B"


# -- Disk cache prepopulation -------------------------------------------------


def test_disk_cache_prepopulates_in_memory_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
  """Mimics what run_proxy does at boot: load disk cache, store in memory.
  The very first stdio _list_tools call must then hit fresh cache."""
  monkeypatch.setattr(tools_cache, "_default_cache_dir", lambda: tmp_path)
  cfg = _config()
  tools_cache.save_disk_cache(cfg, _result(name="FromDisk"))

  cache = _ToolsCache(ttl_seconds=300.0)
  loaded = tools_cache.load_disk_cache(cfg)
  assert loaded is not None
  cache.store(loaded)

  fresh = cache.get_fresh()
  assert fresh is not None
  assert fresh.tools[0].name == "FromDisk"


def test_disk_cache_round_trip_preserves_tool_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
  monkeypatch.setattr(tools_cache, "_default_cache_dir", lambda: tmp_path)
  cfg = _config()
  original = ListToolsResult(
      tools=[
          Tool(
              name="ListCustomers",
              description="Customer master data",
              inputSchema={"type": "object", "properties": {"top": {"type": "integer"}}},
          ),
      ],
  )
  tools_cache.save_disk_cache(cfg, original)
  loaded = tools_cache.load_disk_cache(cfg)
  assert loaded is not None
  assert loaded.tools[0].name == "ListCustomers"
  assert loaded.tools[0].description == "Customer master data"
  assert loaded.tools[0].inputSchema == {
      "type": "object", "properties": {"top": {"type": "integer"}},
  }


# -- Concurrency safety -------------------------------------------------------


async def test_cache_lock_serializes_writers() -> None:
  """The cache lock is what prevents two concurrent _list_tools waiters
  from each round-tripping to BC when only one round-trip is needed."""
  import asyncio

  cache = _ToolsCache(ttl_seconds=300.0)
  upstream_calls = 0

  async def fake_fetch_and_cache() -> None:
    nonlocal upstream_calls
    async with cache.lock:
      if cache.get_fresh() is not None:
        return
      upstream_calls += 1
      await asyncio.sleep(0.01)  # simulate network round-trip
      cache.store(_result())

  await asyncio.gather(*(fake_fetch_and_cache() for _ in range(5)))
  assert upstream_calls == 1
