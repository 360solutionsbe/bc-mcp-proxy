"""Persistent on-disk cache for tools/list responses.

BC MCP endpoints cold-start in 30s+ on idle Demo/Sandbox environments —
longer than Claude Desktop's hardcoded MCP request timeout. Caching the
last successful tools/list to disk lets a freshly-launched proxy answer
the client's first call instantly while a background pre-warm refreshes
upstream.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from mcp.types import ListToolsResult

from .config import ProxyConfig

LOGGER = logging.getLogger("bc_mcp_proxy.tools_cache")

_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class _CacheEntry:
  fetched_at: float
  result: ListToolsResult


def _default_cache_dir() -> Path:
  if sys.platform.startswith("win"):
    root = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local")
  elif sys.platform == "darwin":
    root = Path.home() / "Library/Caches"
  else:
    root = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
  return root / "bc_mcp_proxy"


def _cache_key(config: ProxyConfig) -> str:
  parts = [
      config.tenant_id or "",
      config.environment or "",
      config.company or "",
      config.configuration_name or "",
      config.base_url or "",
  ]
  raw = "|".join(parts).encode("utf-8")
  return hashlib.sha256(raw).hexdigest()[:16]


def cache_path(config: ProxyConfig) -> Path:
  return _default_cache_dir() / f"tools_cache_{_cache_key(config)}.json"


def load_disk_cache(config: ProxyConfig) -> Optional[ListToolsResult]:
  """Return the cached ListToolsResult if present and not expired.

  Any deserialization failure is treated as a cache miss — corrupt files
  shouldn't break the proxy.
  """
  path = cache_path(config)
  if not path.is_file():
    return None
  try:
    payload = json.loads(path.read_text(encoding="utf-8"))
  except (OSError, ValueError) as exc:
    LOGGER.warning("Discarding unreadable tools cache at %s: %s", path, exc)
    return None
  if payload.get("schema") != _SCHEMA_VERSION:
    return None
  fetched_at = payload.get("fetched_at")
  if not isinstance(fetched_at, (int, float)):
    return None
  age = time.time() - float(fetched_at)
  if age < 0 or age > config.tools_disk_cache_ttl_seconds:
    return None
  tools_payload = payload.get("tools")
  if not isinstance(tools_payload, dict):
    return None
  try:
    return ListToolsResult.model_validate(tools_payload)
  except Exception as exc:  # pragma: no cover - pydantic raises a few classes
    LOGGER.warning("Discarding tools cache that fails validation: %s", exc)
    return None


def save_disk_cache(config: ProxyConfig, result: ListToolsResult) -> None:
  """Persist the tools result atomically. Errors are logged but not raised —
  losing the cache is non-fatal."""
  path = cache_path(config)
  try:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": _SCHEMA_VERSION,
        "fetched_at": time.time(),
        "tools": result.model_dump(mode="json"),
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(tmp, path)
  except OSError as exc:
    LOGGER.warning("Failed to persist tools cache to %s: %s", path, exc)
