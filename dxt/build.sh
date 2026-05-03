#!/usr/bin/env bash
# Build a .dxt bundle for Claude Desktop from the current source tree.
#
# Usage (from the repo root):
#   ./dxt/build.sh
#
# Output: dist/bc-mcp-proxy-<version>.dxt
#
# Requires:
#   - Node + npx, OR a working `dxt` CLI on PATH (https://github.com/anthropics/dxt)

set -euo pipefail

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$repo_root"

version="$(awk -F\" '/^__version__/ {print $2; exit}' bc_mcp_proxy/__init__.py)"
if [[ -z "${version:-}" ]]; then
  echo "Could not determine package version from bc_mcp_proxy/__init__.py." >&2
  exit 1
fi

build_dir="$repo_root/dxt/build"
dist_dir="$repo_root/dist"
bundle="$dist_dir/bc-mcp-proxy-$version.dxt"

rm -rf "$build_dir"
mkdir -p "$build_dir/server" "$dist_dir"

echo "Staging extension contents in $build_dir ..."
cp dxt/manifest.json    "$build_dir/manifest.json"
cp dxt/requirements.txt "$build_dir/server/requirements.txt"
cp -R bc_mcp_proxy      "$build_dir/server/bc_mcp_proxy"
cp LICENSE              "$build_dir/LICENSE"
if [[ -f dxt/icon.png ]]; then
  cp dxt/icon.png "$build_dir/icon.png"
else
  echo "  (no dxt/icon.png — bundle will ship without an icon)"
fi

# Strip __pycache__ before packing.
find "$build_dir" -type d -name __pycache__ -prune -exec rm -rf {} +

echo "Packing $bundle ..."
# Anthropic renamed @anthropic-ai/dxt to @anthropic-ai/mcpb in late 2025.
# The CLI binary remains `mcpb` (or `dxt` on older installs).
if command -v mcpb >/dev/null 2>&1; then
  mcpb pack "$build_dir" "$bundle"
elif command -v dxt >/dev/null 2>&1; then
  dxt pack "$build_dir" "$bundle"
else
  npx --yes @anthropic-ai/mcpb pack "$build_dir" "$bundle"
fi

echo "Built $bundle"
