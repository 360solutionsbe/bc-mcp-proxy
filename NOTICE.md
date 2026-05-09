# Notice — Privacy, Anthropic subscription, and trademarks

`360solutions-bc-mcp` (the *bc-mcp-proxy*) connects an AI client — typically
Anthropic's Claude — to your Microsoft Dynamics 365 Business Central
environment via the Model Context Protocol. When you use it, the queries you
run and the Business Central data returned to the AI client in response —
including customer records, vendors, invoices, ledger entries, and other
financial data — are processed by the AI provider as part of the assistant's
normal operation.

**Your choice of Anthropic subscription tier directly determines how that data
is handled, retained, and whether it can be used to train future models.**

## What we recommend

For any production use of this proxy against a live Business Central tenant,
we strongly recommend one of the following:

- **Claude Team** or **Claude Enterprise** — these plans operate under
  Anthropic's Commercial Terms. Customer prompts and responses are not used
  for model training by default, and a Data Processing Addendum (DPA) is
  available, which is important for GDPR compliance if you are established
  in the EU/EEA.
- **Anthropic API** access (configurable in Claude Desktop, VS Code, or
  Cursor) — also governed by Commercial Terms, with short default retention,
  no training on inputs, and DPA support. A practical alternative if
  seat-based pricing is not a fit.

We do **not** recommend using this proxy with **Claude Free**, **Pro**, or
**Max** for client work or any data subject to a confidentiality, GDPR, or
sector-specific regulatory obligation. Consumer plans may, depending on
current Anthropic policy and your individual privacy settings, allow the use
of conversations for model training. No DPA is offered on consumer plans.

Anthropic's policies on training defaults, retention, and opt-out have changed
more than once and will likely change again. Treat the bullets above as our
*current* recommendation, not a permanent guarantee — verify the live terms at
[anthropic.com/legal](https://www.anthropic.com/legal) and
[privacy.anthropic.com](https://privacy.anthropic.com) before you deploy.

## Your responsibility

You are responsible for selecting the AI subscription that matches the
sensitivity of the data you process through this proxy and for configuring its
privacy settings appropriately. 360 Solutions / Vangelder Solutions does not
control how Anthropic (or any other AI provider you point an MCP client at)
processes data sent to that provider.

If you operate from the EU/EEA, the United Kingdom, or any other jurisdiction
with data-protection law, the controller obligations (lawful basis, DPIA where
required, processor contracts, transparency to data subjects) sit with you —
not with this proxy or its maintainers.

## Not affiliated with Anthropic or Microsoft

360 Solutions (a Vangelder Solutions brand) is an independent software vendor.
It is **not** affiliated with, endorsed by, sponsored by, or a reseller of
Anthropic or Microsoft.

- "Claude" and "Anthropic" are trademarks of Anthropic, PBC.
- "Microsoft", "Dynamics 365", and "Business Central" are trademarks of
  Microsoft Corporation.
- "Model Context Protocol" is an open standard published by Anthropic.

This project is an independent integration that connects these products via
the Model Context Protocol under the terms of the [MIT License](LICENSE).
