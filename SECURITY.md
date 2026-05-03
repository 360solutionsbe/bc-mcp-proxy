# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in `360Solutions-BC-MCP`, please report it privately so we can fix it before it is publicly disclosed.

**Email**: [dev@360solutions.be](mailto:dev@360solutions.be)

Please include, where possible:

- A description of the issue and its potential impact
- Steps to reproduce (proof-of-concept code, request payloads, or environment details)
- Affected version(s) of the proxy
- Any mitigations you have already verified

We aim to acknowledge new reports within **2 working days** and to ship a fix or coordinated disclosure plan within **30 days** of confirmation, depending on severity and scope.

If you prefer GitHub's built-in private channel, you can also use [GitHub Security Advisories](https://github.com/360solutionsbe/bc-mcp-proxy/security/advisories/new) once the repository becomes public.

## Out of scope

This repository is the open-source proxy that connects MCP clients to the Microsoft Dynamics 365 Business Central MCP endpoint. The following are explicitly **out of scope** for this project:

- Vulnerabilities in Microsoft's BC MCP server itself — please report those to Microsoft via their [responsible disclosure programme](https://www.microsoft.com/en-us/msrc).
- Vulnerabilities in the `msal`, `mcp` or `httpx` Python libraries — please report those to their upstream maintainers.
- Misconfiguration of an end-user's Azure App Registration or Business Central environment.

## Supported versions

We support security fixes on the latest released minor version (`0.x`) on the `main` branch. There is no separate long-term-support branch.

| Version | Supported          |
|---------|--------------------|
| 0.4.x   | ✅                 |
| < 0.4.0 | ❌ (please upgrade) |

## Hall of fame

Researchers who report a valid security issue are listed here with their permission. (Empty for now — this section will be populated as reports come in.)
