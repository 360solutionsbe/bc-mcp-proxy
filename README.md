# vangelder-bc-mcp-proxy

> **Fork of [microsoft/BCTech `samples/BcMCPProxyPython`](https://github.com/microsoft/BCTech/tree/master/samples/BcMCPProxyPython)** — see [Why this fork?](#why-this-fork) below.
>
> Original: Copyright (c) Microsoft Corporation. Modifications: Copyright (c) 2026 Vangelder Solutions. Licensed under the MIT License.

A resilient Python MCP stdio proxy that bridges MCP-compatible clients (Claude Desktop, VS Code, Cursor, Claude Code) to the Microsoft Dynamics 365 Business Central MCP HTTP endpoint.

## What this fork adds

- ✅ **Reconnect on transient upstream errors.** `httpx.ReadTimeout`, `RemoteProtocolError`, and `NetworkError` (including the same errors wrapped in an `ExceptionGroup` by anyio) trigger an exponential backoff reconnect — `1s → 2s → 4s → 8s → 16s`, default 5 attempts. The local stdio pipe to your MCP client stays open while reconnecting.
- ✅ **Pre-emptive MSAL silent token refresh.** Each acquired access token's expiry is tracked locally; when the remaining validity drops below `token_refresh_skew_seconds` (default 300) the next call asks MSAL to mint a new token via `acquire_token_silent(force_refresh=True)` instead of letting Business Central reject the stale one with `Authentication_InvalidCredentials`.
- ✅ **Surface masked upstream errors.** Some Business Central MCP responses ship with `isError: false` but the content is actually an error message ("Semantic search is not enabled", "Authentication_InvalidCredentials", etc.). The proxy now flags those as real MCP errors so the client sees them.
- ✅ **Pytest test suite.** 35 tests cover error classification, backoff progression including the cap, the give-up path, the attempt-counter reset after a successful connect, MSAL refresh-skew boundaries, and masked-error pattern matching.

The CLI surface is unchanged — every flag and env var from the upstream sample still works.

## Status

> ⚠️ **Experimental.** Not for production use. The Business Central MCP endpoint itself is in preview and changes regularly. This fork is a working tool for development and evaluation, not a supported product.

## Prerequisites

- Microsoft Dynamics 365 Business Central environment with the MCP preview feature enabled
- Azure tenant with appropriate permissions
- Python 3.10 or later
- An MCP-compatible client (Claude Desktop, VS Code, Cursor, …)

## Installation

```bash
python -m pip install --upgrade vangelder-bc-mcp-proxy
```

The PyPI distribution name is `vangelder-bc-mcp-proxy`; the Python module and CLI are still `bc_mcp_proxy` / `bc-mcp-proxy`, so existing client configurations from the upstream sample keep working.

> Until this fork is published to PyPI, install from a local clone:
> ```bash
> git clone https://github.com/VangelderSolutions/bc-mcp-proxy.git
> cd bc-mcp-proxy
> python -m pip install -e .
> ```

## Setup

### 1. Set up an Azure AD app registration

1. Open [ms.portal.azure.com](https://ms.portal.azure.com).
2. Navigate to **Microsoft Entra ID** and create a new **App Registration**.
3. Under **Authentication**, add a desktop redirect URL of the form:
   ```
   ms-appx-web://Microsoft.AAD.BrokerPlugin/<clientID>
   ```
   Enable **"Allow public client flows"**.
4. Add API permissions (Delegated):
   - **Dynamics 365 Business Central**
     - `Financials.ReadWrite.All`
     - `user_impersonation`

### 2. Run the interactive setup

```bash
python -m bc_mcp_proxy setup
```

The wizard prompts for tenant ID, client ID, environment, and company; runs the device-code flow; and writes ready-to-paste configurations into `~/.bc_mcp_proxy/` (or `%USERPROFILE%\.bc_mcp_proxy\` on Windows) plus install URLs for Cursor and VS Code and a snippet for Claude Desktop.

### 3. Add the proxy to your MCP client

The setup command produces:

- a clickable **Cursor** install URL,
- a clickable **VS Code** install URL,
- a `claude_mcp.json` snippet to drop into your `claude_desktop_config.json`.

Restart your MCP client; Business Central tools should appear.

## Running manually

```bash
python -m bc_mcp_proxy \
  --TenantId    "<tenant-id>" \
  --ClientId    "<client-id>" \
  --Environment "<environment>" \
  --Company     "<company>"
```

or via the entry point script:

```bash
bc-mcp-proxy --TenantId "<tenant-id>" --ClientId "<client-id>" --Environment "<environment>" --Company "<company>"
```

## Configuration parameters

| Parameter           | CLI argument           | Env var                  | Default                                                       |
|---------------------|------------------------|--------------------------|---------------------------------------------------------------|
| Tenant ID           | `--TenantId`           | `BC_TENANT_ID`           | *required*                                                    |
| Client ID           | `--ClientId`           | `BC_CLIENT_ID`           | *required*                                                    |
| Environment         | `--Environment`        | `BC_ENVIRONMENT`         | `Production`                                                  |
| Company             | `--Company`            | `BC_COMPANY`             | *required*                                                    |
| Configuration Name  | `--ConfigurationName`  | `BC_CONFIGURATION_NAME`  | unset                                                         |
| Custom Auth Header  | `--CustomAuthHeader`   | `BC_CUSTOM_AUTH_HEADER`  | unset (skips device flow when provided)                       |
| Base URL            | `--BaseUrl`            | `BC_BASE_URL`            | `https://api.businesscentral.dynamics.com`                    |
| Token Scope         | `--TokenScope`         | `BC_TOKEN_SCOPE`         | `https://api.businesscentral.dynamics.com/.default`           |
| HTTP Timeout (s)    | `--HttpTimeoutSeconds` | `BC_HTTP_TIMEOUT_SECONDS`| `30.0`                                                        |
| SSE Timeout (s)     | `--SseTimeoutSeconds`  | `BC_SSE_TIMEOUT_SECONDS` | `300.0`                                                       |
| Log Level           | `--LogLevel`           | `BC_LOG_LEVEL`           | `INFO`                                                        |
| Debug               | `--Debug`              | `BC_DEBUG=1`             | off                                                           |

Token cache locations (when no custom auth header is supplied):

- **Windows**: `%LOCALAPPDATA%\BcMCPProxyPython\bc_mcp_proxy.bin`
- **macOS**: `~/Library/Caches/BcMCPProxyPython/bc_mcp_proxy.bin`
- **Linux**: `$XDG_CACHE_HOME/BcMCPProxyPython/bc_mcp_proxy.bin` (or `~/.cache/…`)

## Why this fork?

Three issues were reproducible against the upstream `BcMCPProxyPython` sample in production-style use:

1. **Process crash on `httpx.ReadTimeout`.** The upstream `streamablehttp_client` is wrapped in an anyio task group; an unhandled timeout bubbles out as a `BaseExceptionGroup` and tears down the entire proxy, killing the stdio pipe to the MCP client. This fork wraps the upstream connection in a manager that reconnects with exponential backoff while keeping the local stdio server alive.

2. **No silent token refresh.** Access tokens are valid for ~60 minutes. Once expired, every `bc_actions_invoke` call returns `Authentication_InvalidCredentials` until the proxy is restarted. This fork tracks each token's `expires_in` and refreshes pre-emptively when remaining validity drops below `token_refresh_skew_seconds`.

3. **Masked errors.** Several upstream responses set `isError: false` even when the content is an error message — for example *"Semantic search is not enabled for this environment"*. Clients display those as normal output and the user has no idea why their query "didn't work." This fork inspects responses for known error patterns and re-flags them as MCP errors.

The CLI surface and on-disk configuration layout are unchanged so this is a drop-in replacement.

## Claude Desktop Extension

A `.dxt` bundle for one-click install in Claude Desktop is built from the source in [`dxt/`](dxt/README.md):

```bash
pwsh dxt/build.ps1     # Windows
./dxt/build.sh         # macOS / Linux
```

The output (`dist/bc-mcp-proxy-<version>.dxt`) installs into Claude Desktop, prompts for tenant ID / client ID / environment / company, and runs the same proxy as the CLI version.

## Development

```bash
git clone https://github.com/VangelderSolutions/bc-mcp-proxy.git
cd bc-mcp-proxy
python -m pip install -e ".[test]"
python -m pytest
```

## Troubleshooting

- **Authentication failures.** Verify the redirect URL format (`ms-appx-web://Microsoft.AAD.BrokerPlugin/<clientID>`) and that *"Allow public client flows"* is enabled on the Azure app registration; ensure all API permissions are granted (and admin-consented where required); rerun setup if the device flow times out.
- **Frequent reconnects in logs.** Inspect upstream availability — the proxy logs `Upstream connection error (...); reconnecting in Xs (attempt N/M)` whenever it retries. After the configured budget the proxy gives up and the local stdio pipe closes.
- **Repeated sign-in prompts.** The MSAL token cache may not be writable. Pass `--DeviceCacheLocation` to point at a directory you control.
- **`No module named bc_mcp_proxy`.** Install the distribution into the same Python interpreter your MCP client is configured to launch (`python -m pip install --upgrade vangelder-bc-mcp-proxy`).

## Security

- Delegated Azure permissions only; no application secrets are stored or required.
- Tokens cached via `msal-extensions` using platform-appropriate secure storage.
- The proxy never logs the access token or the refresh token; debug logs include only token expiry timestamps.

## License

MIT — see `LICENSE`.
