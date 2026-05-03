# Build a .dxt bundle for Claude Desktop from the current source tree.
#
# Usage (from the repo root):
#   pwsh dxt/build.ps1
#
# Output: dist/bc-mcp-proxy-<version>.dxt
#
# Requires:
#   - Node + npx, OR a working `dxt` CLI on PATH (https://github.com/anthropics/dxt)
#   - Python 3.10+ (only used to read the package version from __init__.py)

$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

# Resolve version from bc_mcp_proxy/__init__.py.
$version = (Select-String -Path 'bc_mcp_proxy/__init__.py' -Pattern '__version__\s*=\s*"([^"]+)"').Matches[0].Groups[1].Value
if (-not $version) { throw 'Could not determine package version from bc_mcp_proxy/__init__.py.' }

$buildDir = Join-Path $repoRoot 'dxt/build'
$distDir  = Join-Path $repoRoot 'dist'
$bundle   = Join-Path $distDir  "bc-mcp-proxy-$version.dxt"

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

# Strip __pycache__ before packing.
Get-ChildItem -Path $buildDir -Recurse -Directory -Filter '__pycache__' | Remove-Item -Recurse -Force

Write-Host "Packing $bundle ..."
$dxt = Get-Command dxt -ErrorAction SilentlyContinue
if ($dxt) {
  & dxt pack $buildDir $bundle
} else {
  & npx --yes @anthropic-ai/dxt pack $buildDir $bundle
}

Write-Host "Built $bundle"
