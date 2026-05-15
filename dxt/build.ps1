# Build a .dxt bundle for Claude Desktop from the current source tree.
#
# Usage (from the repo root):
#   pwsh dxt/build.ps1
#
# Output: dist/bc-mcp-proxy-<version>-win-amd64.dxt
#
# What this does:
#   1. Stages the proxy source under dxt/build/server/bc_mcp_proxy.
#   2. Vendors all Python dependencies once per supported ABI under
#      dxt/build/server/wheels/cp{310,311,312,313,314}/. Several deps
#      (pydantic_core, charset_normalizer, rpds, mypyc-built ones)
#      ship Python-version-specific compiled wheels rather than abi3,
#      so a single ABI's wheels won't load on a different Python.
#      The shim in bc_mcp_proxy/__init__.py picks the right dir at
#      startup based on sys.version_info.
#   3. Targets win_amd64 wheels.
#
# Requires:
#   - Python 3.10+ on PATH (used to invoke pip)
#   - Node + npx, OR a working `dxt` / `mcpb` CLI on PATH

$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

# Resolve version from bc_mcp_proxy/__init__.py.
$version = (Select-String -Path 'bc_mcp_proxy/__init__.py' -Pattern '__version__\s*=\s*"([^"]+)"').Matches[0].Groups[1].Value
if (-not $version) { throw 'Could not determine package version from bc_mcp_proxy/__init__.py.' }

$platformTag = 'win-amd64'
$buildDir = Join-Path $repoRoot 'dxt/build'
$distDir  = Join-Path $repoRoot 'dist'
$bundle   = Join-Path $distDir  "bc-mcp-proxy-$version-$platformTag.dxt"

if (Test-Path $buildDir) { Remove-Item -Recurse -Force $buildDir }
New-Item -ItemType Directory -Force -Path $buildDir | Out-Null
New-Item -ItemType Directory -Force -Path "$buildDir/server" | Out-Null
New-Item -ItemType Directory -Force -Path $distDir | Out-Null

Write-Host "Staging extension contents in $buildDir ..."
Copy-Item -Path 'dxt/manifest.json'      -Destination "$buildDir/manifest.json"
Copy-Item -Path 'dxt/requirements.txt'   -Destination "$buildDir/server/requirements.txt"
Copy-Item -Recurse -Path 'bc_mcp_proxy'  -Destination "$buildDir/server/bc_mcp_proxy"
Copy-Item -Path 'LICENSE'                -Destination "$buildDir/LICENSE"
if (Test-Path 'dxt/icon.png') {
  Copy-Item -Path 'dxt/icon.png' -Destination "$buildDir/icon.png"
} else {
  Write-Host "  (no dxt/icon.png — bundle will ship without an icon)"
}

$pythonAbis = @('310', '311', '312', '313', '314')
foreach ($abi in $pythonAbis) {
  $pyVer = '3.' + $abi.Substring(1)
  $abiDir = Join-Path $buildDir "server/wheels/cp$abi"
  Write-Host "Vendoring wheels for Python $pyVer (cp$abi) into $abiDir ..."
  & python -m pip install `
      --target $abiDir `
      --upgrade `
      --no-compile `
      --python-version $pyVer `
      --only-binary ':all:' `
      --platform 'win_amd64' `
      -r 'dxt/requirements.txt'
  if ($LASTEXITCODE -ne 0) { throw "pip install for cp$abi failed (exit $LASTEXITCODE)" }
}

# Strip only __pycache__. Do NOT strip *.dist-info — the mcp package
# (and any other dep that calls importlib.metadata.version("<self>") at
# import time) needs that metadata to be present in the bundle.
Get-ChildItem -Path $buildDir -Recurse -Directory -Filter '__pycache__' | Remove-Item -Recurse -Force

Write-Host "Packing $bundle ..."
# Anthropic renamed @anthropic-ai/dxt to @anthropic-ai/mcpb in late 2025.
# The CLI binary remains `mcpb` (or `dxt` on older installs).
$cli = Get-Command mcpb -ErrorAction SilentlyContinue
if (-not $cli) { $cli = Get-Command dxt -ErrorAction SilentlyContinue }
if ($cli) {
  & $cli.Source pack $buildDir $bundle
} else {
  & npx --yes @anthropic-ai/mcpb pack $buildDir $bundle
}

$bundleSize = [math]::Round((Get-Item $bundle).Length / 1MB, 2)
Write-Host "Built $bundle ($bundleSize MB)"
