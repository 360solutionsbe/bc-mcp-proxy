# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in `VGS-BC-MCP`, please report it privately so we can fix it before it is publicly disclosed.

**Email**: [support@vangeldersolutions.be](mailto:support@vangeldersolutions.be)

Please include, where possible:

- A description of the issue and its potential impact
- Steps to reproduce (proof-of-concept code, request payloads, or environment details)
- Affected version(s) of the proxy
- Any mitigations you have already verified

We aim to acknowledge new reports within **2 working days** and to ship a fix or coordinated disclosure plan within **30 days** of confirmation, depending on severity and scope.

If you prefer GitHub's built-in private channel, you can also use [GitHub Security Advisories](https://github.com/VangelderSolutions/bc-mcp-proxy/security/advisories/new) once the repository becomes public.

## Out of scope

This repository is the open-source proxy that connects MCP clients to the Microsoft Dynamics 365 Business Central MCP endpoint. The following are explicitly **out of scope** for this project:

- Vulnerabilities in Microsoft's BC MCP server itself — please report those to Microsoft via their [responsible disclosure programme](https://www.microsoft.com/en-us/msrc).
- Vulnerabilities in the `msal`, `mcp` or `httpx` Python libraries — please report those to their upstream maintainers.
- Misconfiguration of an end-user's Azure App Registration or Business Central environment.

## Supported versions

We support security fixes on the latest released minor version (`0.x`) on the `main` branch. There is no separate long-term-support branch.

| Version | Supported          |
|---------|--------------------|
| 0.5.x   | ✅                 |
| < 0.5.0 | ❌ (please upgrade) |

## Security review notes

### Dependency hardening (0.5.0, floors maintained since)

The Snyk scan against `dxt/requirements.txt` flagged 16 vulnerabilities in transitive dependencies pulled in by `mcp`, `msal`, and `httpx`. None were in code we author. As of 0.5.0 we pin explicit security floors for the affected transitive deps in both `dxt/requirements.in` and `pyproject.toml` so pip resolves to the patched versions. The floors are raised whenever the weekly Snyk scan flags a new advisory against the current pin — the table below is the current set:

| Package           | Floor pin    | Headline issue                                              |
|-------------------|--------------|-------------------------------------------------------------|
| `h11`             | `>=0.16.0`   | HTTP Request Smuggling (Critical) — SNYK-PYTHON-H11-10293728 |
| `cryptography`    | `>=50.0.0`   | Out-of-bounds read, improper certificate validation, timing attack, unthrottled allocation (High×4) |
| `pyjwt`           | `>=2.13.0`   | Improper authentication (High) — SNYK-PYTHON-PYJWT-17053408 |
| `python-multipart`| `>=0.0.30`   | Inefficient algorithmic complexity (High)                   |
| `starlette`       | `>=1.3.1`    | SSRF, unthrottled allocation, incorrectly-resolved name (High×3) |
| `urllib3`         | `>=2.7.0`    | Data-amplification (High×2) + open-redirect (Medium×2)      |
| `requests`        | `>=2.33.1`   | Sensitive-info leakage / insecure tempfile (Medium×2)       |
| `python-dotenv`   | `>=1.2.2`    | Symlink attack (Medium)                                     |

When upstream `mcp`/`msal`/`httpx` releases bring their own pins up to or above these floors, our explicit pins become redundant and can be removed.

### `mcp` version cap (<2.0.0)

`mcp` is constrained to `>=1.28.1,<2.0.0`. The floor is the fix version for two High-severity advisories in the 1.x line (SNYK-PYTHON-MCP-18016318, authorization bypass through user-controlled key; SNYK-PYTHON-MCP-18016320, missing origin validation in WebSockets). The cap is an API constraint, not a security one: `mcp` 2.x removes `mcp.client.streamable_http.streamablehttp_client` and `mcp.shared.exceptions.McpError`, both of which `bc_mcp_proxy/proxy.py` builds on. Lifting the cap requires porting the proxy to the 2.x client API.

### SSRF hardening (0.5.0)

The Snyk Code SAST scan flagged the `base_url` argument as a potential SSRF sink because it flows into the upstream HTTP client. The proxy now validates `BC_BASE_URL` at startup (in `__main__.py`) and again at the call boundary (in `proxy.run_proxy`):

- The scheme must be `https` — bearer tokens are never sent over plain HTTP.
- The host must end with `.businesscentral.dynamics.com` (covers `api.*` for v26/v27, `mcp.*` for v28+, and any future regional or staging subdomain Microsoft introduces).
- For local mock-server testing, set `BC_ALLOW_NON_STANDARD_BASE_URL=1` to opt out of the host check. The https requirement is non-bypassable.

### Acknowledged-but-unactionable findings

Snyk Code reports four LOW path-traversal findings that we have reviewed and consider non-vulnerabilities given the proxy's threat model. They are documented here so a future reviewer can see the rationale rather than re-litigating each scan:

| Location                                | Snyk message                                              | Why it's a false positive                                                                                                                                                                                                       |
|-----------------------------------------|-----------------------------------------------------------|----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `bc_mcp_proxy/tools_cache.py:44`        | env var (LOCALAPPDATA/XDG_CACHE_HOME) flows into a path   | This is the OS-standard mechanism for resolving the user's per-user cache directory. The same pattern is used by `pip`, `msal-extensions`, the AWS CLI, and effectively every Python desktop tool. The user owns the env var. |
| `bc_mcp_proxy/tools_cache.py:108`       | env var flows into `os.replace`                           | Same root as above — the cache filename itself is a SHA-256 of the proxy config and is not user-controlled.                                                                                                                      |
| `bc_mcp_proxy/__main__.py:89` (path)    | CLI arg flows into `os.replace` (via `tools_cache`)       | The CLI args become part of the SHA-256 cache key, not the path. Only the (user-owned) cache root comes from an env var.                                                                                                         |
| `bc_mcp_proxy/setup_flow.py:179`        | user input flows into `json.dump`                         | The path is `OUTPUT_DIR + "<hardcoded>.json"`; user input flows into the JSON *contents*, not the file path.                                                                                                                     |
| `bc_mcp_proxy/__main__.py:99` (Medium SSRF) | CLI/env arg flows into `streamablehttp_client` URL  | Validated. `BC_BASE_URL` passes through `config.validate_base_url()` (`bc_mcp_proxy/config.py`) before it reaches the network layer: scheme must be `https`, host must be `*.businesscentral.dynamics.com` (override via `BC_ALLOW_NON_STANDARD_BASE_URL=1` for local mock testing). Snyk's data-flow analyser does not recognize raise-on-invalid validators or URL reconstruction as sanitization, so the finding remains. The `tests/test_base_url_validation.py` suite covers the validation behaviour. |

The Snyk dashboard "Ignore" feature is the right place to record these decisions for the org-wide view, with a link back to this section as the justification.

## Hall of fame

Researchers who report a valid security issue are listed here with their permission. (Empty for now — this section will be populated as reports come in.)
