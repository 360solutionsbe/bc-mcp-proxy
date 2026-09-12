---
title: Privacy Policy
permalink: /privacy/
---

# Privacy Policy — Business Central MCP Proxy (`vgs-bc-mcp`)

**Effective date:** 12 September 2026
**Publisher:** Vangelder Solutions BV, Belgium — support@vangeldersolutions.be

This policy covers the `vgs-bc-mcp` software (the "Proxy"): the Python package published on PyPI, the source on GitHub, and the Claude Desktop extension bundle (`.mcpb`/`.dxt`) built from it. The Proxy runs entirely on your own computer and bridges an MCP client (for example Claude Desktop, Visual Studio Code or Cursor) to your organisation's Microsoft Dynamics 365 Business Central environment.

## 1. What the Proxy does with data

The Proxy is a pass-through. It does not operate any server of its own and it sends nothing to Vangelder Solutions.

| Data | Where it goes | Why |
|---|---|---|
| Your Microsoft Entra sign-in (browser or device-code flow) | Microsoft Entra ID (`login.microsoftonline.com`) | To obtain an access token for Business Central in your own tenant. The Proxy never sees your password. |
| Access and refresh tokens | Cached locally on your device, encrypted by the operating system (DPAPI on Windows, Keychain on macOS, libsecret on Linux) via `msal-extensions` | So you do not have to sign in on every start. |
| Tenant ID, environment name, company name, MCP configuration name | Sent as request headers to `https://mcp.businesscentral.dynamics.com` (or the legacy `api.businesscentral.dynamics.com`) | To route requests to your Business Central environment. |
| Tool calls made by your AI client and the Business Central data they return | Forwarded between your MCP client and Business Central over HTTPS | This is the purpose of the Proxy. |
| The list of tools Business Central exposes (names, descriptions, input schemas — no business records) | Cached locally in a JSON file for up to 24 hours | To answer the client's first request instantly while Business Central cold-starts. |
| `X-Client-Application: vgs-bc-mcp/<version>` | Sent to Business Central | Business Central telemetry records it as the client name so administrators can identify Proxy traffic. |

The Proxy collects no analytics, no crash reports and no usage statistics. It makes no network connection other than to Microsoft Entra ID and to the Business Central endpoint you configure (the Proxy refuses any host that is not `*.businesscentral.dynamics.com` unless you explicitly override it for testing).

## 2. Data that leaves your device through your AI client

Business Central data returned by a tool call is handed to the MCP client you connected the Proxy to. That client — and the AI provider behind it, most commonly Anthropic's Claude — processes it under **its own** terms and privacy policy, not this one. What is retained, where it is stored and whether it may be used for model training depends on your subscription with that provider. For business data we recommend a plan that offers a data processing agreement (for Claude: Team, Enterprise or API access). See Anthropic's privacy policy at https://www.anthropic.com/privacy and the `NOTICE.md` file in the repository.

## 3. Storage, retention and deletion

Everything the Proxy stores lives on your device:

- **Token cache:** `%LOCALAPPDATA%\BcMCPProxyPython\` (Windows), `~/Library/Caches/BcMCPProxyPython/` (macOS), `$XDG_CACHE_HOME/BcMCPProxyPython/` (Linux). Tokens expire on the schedule set by Microsoft Entra ID; deleting the file signs you out.
- **Tools cache:** `%LOCALAPPDATA%\bc_mcp_proxy\`, `~/Library/Caches/bc_mcp_proxy/` or `$XDG_CACHE_HOME/bc_mcp_proxy/`; entries expire after 24 hours and can be deleted at any time.
- **Configuration:** the values you enter in the extension settings, environment variables, a `.env` file or `~/.bc_mcp_proxy/config.json` if you used the setup wizard.

Uninstalling the extension or package and deleting these folders removes all data the Proxy created. Logs go to the standard error stream of the process (shown in your MCP client's log viewer) and never contain tokens.

## 4. Third parties

The Proxy shares data only with:

- **Microsoft** (Entra ID and Dynamics 365 Business Central), under your organisation's agreement with Microsoft;
- **the AI provider of the MCP client you choose**, under your agreement with that provider.

There are no other recipients. Vangelder Solutions has no access to your tenant, your tokens, your prompts or your Business Central data through the Proxy.

## 5. Your responsibilities

You (or your organisation) are the data controller for the Business Central data you expose. Business Central's MCP Server Configuration decides which API pages are visible and whether write operations are allowed; the signed-in user's Business Central permissions apply to every call. Enable write access ("Unblock Edit Tools") only for configurations and users where that is appropriate.

## 6. Children

The Proxy is a business tool and is not directed at children under 16.

## 7. Changes

Changes to this policy are published at this address and recorded in the repository's history. Material changes are announced in the release notes of the version that introduces them.

## 8. Contact

Vangelder Solutions BV — support@vangeldersolutions.be — https://www.vangeldersolutions.be
Security reports: see `SECURITY.md` in the repository.

## 9. Trademarks

Microsoft, Dynamics 365 and Business Central are trademarks of the Microsoft group of companies. Claude and Anthropic are trademarks of Anthropic, PBC. Vangelder Solutions is not affiliated with, endorsed by or sponsored by Microsoft or Anthropic.
