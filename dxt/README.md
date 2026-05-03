# Claude Desktop Extension (.dxt) packaging

This directory contains everything needed to build a Claude Desktop Extension
bundle from the proxy. The resulting `.dxt` file is a one-click install for
Claude Desktop and can later be submitted to Anthropic's Extensions Directory.

## What's here

| File              | Purpose                                                                                       |
|-------------------|-----------------------------------------------------------------------------------------------|
| `manifest.json`   | The DXT manifest. Defines metadata, the user_config schema (TenantId, ClientId, Environment, Company, …) and how Claude Desktop should launch the proxy. |
| `requirements.txt`| Python dependencies that Claude Desktop installs into the extension's runtime.                |
| `build.ps1`       | PowerShell build script (Windows / cross-platform via `pwsh`).                                |
| `build.sh`        | POSIX shell build script (macOS / Linux).                                                     |
| `icon.png`        | *(Optional)* 256×256 PNG icon. Add one before publishing.                                     |

## Building

From the repo root:

```bash
# Windows / pwsh
pwsh dxt/build.ps1

# macOS / Linux
./dxt/build.sh
```

The script:
1. Stages `manifest.json`, `requirements.txt`, the `bc_mcp_proxy` package and `LICENSE` into `dxt/build/`.
2. Packs that staging directory into `dist/bc-mcp-proxy-<version>.dxt` using the `@anthropic-ai/dxt` CLI (via `npx` if `dxt` is not on PATH).

The build artifact (`dist/*.dxt` and `dxt/build/`) is git-ignored.

## Installing locally

Drag the `.dxt` file onto Claude Desktop (or open Settings → Extensions → Install from file). Claude Desktop will:

1. Prompt for each `user_config` value (Tenant ID, Client ID, Environment, Company, optional Configuration Name, log level).
2. Install Python dependencies from `requirements.txt` into a managed runtime.
3. Launch the server on demand using the `mcp_config.command` + `args` from the manifest, substituting the user-config values.

The first BC tool call triggers the standard MSAL device-code login (the bundled proxy is unchanged from the standalone CLI version).

## Publishing

To submit to the Anthropic Extensions Directory:

1. Add an icon (`dxt/icon.png`, 256×256 PNG, transparent or branded background).
2. Make sure the `homepage`, `repository`, `support` and `documentation` URLs in `manifest.json` resolve.
3. Ship a public version on GitHub (the fork is currently private).
4. Follow the submission instructions at <https://github.com/anthropics/dxt> (typically a PR adding the extension's manifest URL to the directory index).

## Verifying the manifest

The DXT CLI ships a validator:

```bash
npx --yes @anthropic-ai/dxt validate dxt/build/manifest.json
```

Run this after any manifest edit to catch schema regressions before packing.
