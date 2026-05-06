#!/usr/bin/env bash
# Build a .dxt bundle for Claude Desktop from the current source tree.
#
# Usage (from the repo root):
#   ./dxt/build.sh
#
# Output: dist/bc-mcp-proxy-<version>-<platform>.dxt
#
# What this does:
#   1. Stages the proxy source under dxt/build/server/bc_mcp_proxy.
#   2. Vendors all Python dependencies into dxt/build/server/ alongside
#      the proxy. Python sees them under PYTHONPATH=${__dirname}/server,
#      so Claude Desktop can launch the proxy on a fresh machine without
#      needing a pre-existing `pip install`.
#   3. Wheels are pinned to Python 3.10 + the host's platform tag.
#
# Requires:
#   - Python 3.10+ on PATH
#   - Node + npx, OR a working `mcpb` / `dxt` CLI on PATH

set -euo pipefail

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$repo_root"

version="$(awk -F\" '/^__version__/ {print $2; exit}' bc_mcp_proxy/__init__.py)"
if [[ -z "${version:-}" ]]; then
  echo "Could not determine package version from bc_mcp_proxy/__init__.py." >&2
  exit 1
fi

# Detect platform tag pip uses for wheel filenames.
case "$(uname -s)" in
  Darwin)
    arch="$(uname -m)"
    if [[ "$arch" == "arm64" ]]; then
      pip_platform="macosx_11_0_arm64"
      platform_tag="darwin-arm64"
    else
      pip_platform="macosx_10_15_x86_64"
      platform_tag="darwin-x86_64"
    fi
    ;;
  Linux)
    pip_platform="manylinux2014_x86_64"
    platform_tag="linux-x86_64"
    ;;
  *)
    echo "Unsupported host: $(uname -s). Run dxt/build.ps1 on Windows." >&2
    exit 1
    ;;
esac

build_dir="$repo_root/dxt/build"
dist_dir="$repo_root/dist"
bundle="$dist_dir/bc-mcp-proxy-$version-$platform_tag.dxt"

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

echo "Vendoring Python dependencies into $build_dir/server ..."
python3 -m pip install \
  --target "$build_dir/server" \
  --upgrade \
  --no-compile \
  --python-version '3.10' \
  --only-binary ':all:' \
  --platform "$pip_platform" \
  -r dxt/requirements.txt

# Strip caches and dist-info metadata that bloat the bundle without affecting runtime.
find "$build_dir" -type d -name __pycache__ -prune -exec rm -rf {} +
find "$build_dir/server" -maxdepth 1 -type d -name '*.dist-info' -exec rm -rf {} +

echo "Packing $bundle ..."
if command -v mcpb >/dev/null 2>&1; then
  mcpb pack "$build_dir" "$bundle"
elif command -v dxt >/dev/null 2>&1; then
  dxt pack "$build_dir" "$bundle"
else
  npx --yes @anthropic-ai/mcpb pack "$build_dir" "$bundle"
fi

bundle_size="$(du -h "$bundle" | cut -f1)"
echo "Built $bundle ($bundle_size)"
