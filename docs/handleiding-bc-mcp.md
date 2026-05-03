# Business Central MCP — Praktische handleiding voor Dynamics 365 BC klanten

> Deze handleiding leert je in ongeveer een half uur hoe je **AI-assistenten zoals Claude, ChatGPT-stijl agents en Copilot** rechtstreeks laat praten met je **Microsoft Dynamics 365 Business Central** omgeving via het Model Context Protocol (MCP). Inclusief de Azure-stappen, BC-configuratie, en wat te doen wanneer het *niet* werkt.
>
> Geschreven door [Vangelder Solutions](https://www.vangeldersolutions.be) — open source en gratis te gebruiken. We bouwen en onderhouden de [`vangelder-bc-mcp-proxy`](https://github.com/VangelderSolutions/bc-mcp-proxy) als fork van Microsoft's referentie-implementatie, met fixes voor de drie issues die we in productie tegenkwamen.

---

## TL;DR

- **Wat het oplevert**: je vraagt Claude (of een andere AI client) "*toon me de top 10 klanten in CRONUS BE op openstaand saldo*" en hij haalt die data live uit **jouw** BC environment via een veilige, geauthenticeerde verbinding. Geen export, geen kopieerwerk, geen tussenstap.
- **Wat het kost**: niets aan licenties — MCP zit ingebouwd in BC vanaf versie 26 (mei 2025 wave). De AI-client kies je zelf (Claude Desktop is gratis voor persoonlijk gebruik).
- **Wat de moeilijkheid is**: Azure App Registration aanmaken met de juiste permissions. Voor IT-mensen 10 minuten werk, voor niet-IT-mensen het hardste deel van het hele plaatje. We staan klaar om te helpen.

---

## 1. Wat is het Model Context Protocol?

MCP (Model Context Protocol) is een **open standaard** die Anthropic in 2024 publiceerde, en die intussen door OpenAI, Microsoft en de hele tooling-industrie wordt overgenomen. Het lost één probleem op: hoe geef je een AI-assistent veilig toegang tot jouw data en systemen, zonder voor elke combinatie een aparte integratie te bouwen?

In de praktijk:

- Een **MCP-server** zit aan de kant van het systeem dat data of acties aanbiedt — in ons geval Business Central.
- Een **MCP-client** is de AI-tool die de gebruiker bedient — Claude Desktop, VS Code Copilot, Cursor, ChatGPT desktop met MCP-support.
- Tussenin worden **tools** uitgewisseld in een gestandaardiseerd formaat: hun naam, beschrijving, parameter-schema en het resultaat van een aanroep.

Microsoft heeft sinds **Business Central 2025 release wave 1 (versie 26)** een ingebouwde MCP-server in elke BC-omgeving. Die luistert op een Microsoft-gehoste endpoint, valideert je OAuth-token, en biedt jouw configuratie aan als een set tools die de AI-client kan oproepen.

---

## 2. Wat onze proxy doet (en waarom je 'm nodig hebt)

De BC MCP-server praat **HTTP** — een streamable-HTTP variant met Server-Sent Events. De meeste AI-clients (Claude Desktop, VS Code, Cursor) draaien lokaal op jouw machine en spreken **stdio** met hun MCP-servers, niet HTTP.

De proxy is de vertaler:

```
┌─────────────────┐  stdio    ┌──────────────────┐  HTTPS + OAuth   ┌────────────────────────┐
│  Claude Desktop │ ◄────────►│ bc-mcp-proxy     │ ◄───────────────►│ Business Central MCP   │
│  / VS Code      │           │ (op jouw machine)│                  │ (Microsoft-gehost)     │
└─────────────────┘           └──────────────────┘                  └────────────────────────┘
```

Microsoft levert een referentie-implementatie in hun [BCTech sample repo](https://github.com/microsoft/BCTech/tree/master/samples/BcMCPProxyPython). Onze fork **`vangelder-bc-mcp-proxy`** voegt drie productie-fixes toe die we tegenkwamen tijdens echte klantgebruik:

1. **Reconnect bij netwerk-hiccups**. Een enkele `ReadTimeout` crashte de originele proxy en sneed je AI-sessie af. Wij vangen 'm op met exponential backoff en houden de sessie open.
2. **Pre-emptive token refresh**. Access tokens vervallen na ~60 minuten. De originele proxy detecteerde dat pas wanneer BC een 401 gooide — wij checken expiry vóór elke call en vernieuwen tijdig.
3. **Foutmeldingen zichtbaar maken**. BC geeft soms `isError: false` terug met een foutmelding *ín* de inhoud ("Semantic search is not enabled"). Wij hervlaggen die als echte errors zodat de AI-client en de gebruiker het zien.

Plus: ondersteuning voor zowel het v26/v27 endpoint formaat als het nieuwe v28 formaat (`mcp.businesscentral.dynamics.com`).

De proxy is **MIT-licensed**, **open source** en wordt actief onderhouden. Je kunt 'm via `pip install` direct gebruiken of als Claude Desktop Extension (`.dxt`) één-klik installeren.

---

## 3. Wat je nodig hebt vóór je begint

| Item | Detail |
|---|---|
| **BC environment** | Versie 26.0 of hoger. Sandbox of production. MCP-feature staat standaard aan vanaf v26. |
| **Microsoft Entra (Azure AD) tenant** | Met **administrator** rechten — je gaat een App Registration aanmaken en API permissions toekennen. |
| **Een AI-client** | Claude Desktop (gratis), VS Code met MCP-ondersteuning, Cursor, of een ander stdio-MCP capable hulpprogramma. |
| **Python 3.10+** op je computer | Of je wacht tot we de DXT-installer publiceren waarbij Python automatisch wordt ingericht. |

---

## 4. Stap-voor-stap setup

### Stap 1 — Azure App Registration (de lastigste stap)

In Azure portal:

1. Open **Microsoft Entra ID** → **App registrations** → **New registration**.
2. Naam: kies iets herkenbaars, bv. `BC MCP Proxy — productie`.
3. Supported account types: **Accounts in this organizational directory only** (single tenant).
4. Redirect URI laat je leeg voor nu — we voegen 'm zo toe.
5. Klik **Register**. Noteer de **Application (client) ID** en de **Directory (tenant) ID** uit de Overview.

Daarna in dezelfde app:

6. **Authentication** → **Add a platform** → kies **Mobile and desktop applications** → vul deze redirect-URI in:
   ```
   ms-appx-web://Microsoft.AAD.BrokerPlugin/<jouw-client-id>
   ```
   Vervang `<jouw-client-id>` door de Application ID uit stap 5.
7. Onderaan dezelfde Authentication-pagina: zet **"Allow public client flows"** op **Yes** en sla op.

Permissions:

8. **API permissions** → **Add a permission** → **Dynamics 365 Business Central** → **Delegated permissions**:
   - Vink `Financials.ReadWrite.All` aan (of `Financials.Read.All` als je read-only wil).
   - Vink `user_impersonation` aan.
9. Klik **Add permissions**, daarna **Grant admin consent for [tenant]**.

> **Belangrijk**: zonder admin consent gaat de eerste login niet werken. De groene vinkjes na *Grant admin consent* zijn het signaal dat je klaar bent.

### Stap 2 — BC MCP Configuration aanmaken

In Business Central, in je doel-omgeving (sandbox of productie):

1. Zoek naar **"MCP Server Configurations"** via de zoekbalk (loep-icoon). De pagina heet ook wel *Model Context Protocol Server Configurations*.
2. Klik **+ New**.
3. Vul in:
   - **Name**: kies een herkenbare naam (bv. `Default MCP`). De naam wordt later doorgestuurd als header — let op spaties en hoofdletters, dat moet exact matchen.
   - **Active**: **zet aan**. ← Dit is *de* meest gemaakte fout: je slaat de pagina op (BC toont "Saved"), maar Active staat per default uit. Zonder Active=Yes accepteert BC geen tool calls op deze configuration.
   - **Dynamic Tool Mode**: kies bewust:
     - **Uit (Static mode)** — BC genereert per geselecteerde page een aparte `List_<EntityName>_PAG<id>` tool. Voorspelbaar, snel, maar je moet vooraf de tools selecteren.
     - **Aan (Dynamic mode)** — BC biedt drie generieke tools (`bc_actions_search`, `bc_actions_describe`, `bc_actions_invoke`) waarmee de AI-client zelf op runtime een actie zoekt, beschrijft en oproept. Veel flexibeler, maar trager (zie de waarschuwing onderaan).
   - **Discover Additional Objects** (alleen relevant in dynamic mode): vink dit aan als je wilt dat BC ook objecten buiten je expliciet aangewezen toolset blootstelt voor read-only discovery.
4. Voeg eventueel **System Tools** of **Available Tools** toe — afhankelijk van static of dynamic mode.
5. **Save**.

> **Tip**: maak twee configurations met dezelfde toolset, één met `Active=No` en één met `Active=Yes`. Zo kun je veilig wijzigingen testen voor je productie-config aanraakt.

### Stap 3 — Proxy installeren

Op de machine waar je AI-client draait:

```bash
python -m pip install --upgrade vangelder-bc-mcp-proxy
```

Of, als je liever vanuit GitHub installeert:

```bash
git clone https://github.com/VangelderSolutions/bc-mcp-proxy.git
cd bc-mcp-proxy
python -m pip install -e .
```

Test dat de proxy bereikbaar is:

```bash
python -m bc_mcp_proxy --help
```

### Stap 4 — Configureren

Maak een `.env`-bestand naast de proxy met je waarden:

```ini
BC_TENANT_ID=<jouw-tenant-id>
BC_CLIENT_ID=<jouw-client-id-uit-stap-1>
BC_ENVIRONMENT=Production
BC_COMPANY=CRONUS BE
BC_CONFIGURATION_NAME=Default MCP
```

**Voor BC v28 of hoger**:

```ini
BC_BASE_URL=https://mcp.businesscentral.dynamics.com
```

(Voor v26/v27 hoef je `BC_BASE_URL` niet te zetten — de default `api.businesscentral.dynamics.com` is correct.)

### Stap 5 — Verbinden met je AI-client

#### Claude Desktop

In `%APPDATA%\Claude\claude_desktop_config.json` (Windows) of `~/Library/Application Support/Claude/claude_desktop_config.json` (Mac):

```json
{
  "mcpServers": {
    "business-central": {
      "command": "python",
      "args": [
        "-m", "bc_mcp_proxy",
        "--TenantId", "<jouw-tenant-id>",
        "--ClientId", "<jouw-client-id>",
        "--Environment", "Production",
        "--Company", "CRONUS BE",
        "--ConfigurationName", "Default MCP"
      ]
    }
  }
}
```

Restart Claude Desktop. Je BC-tools zijn nu beschikbaar in elke chat.

#### VS Code / Cursor

Beide ondersteunen MCP via een vergelijkbare JSON-config. De Vangelder Solutions repo bevat een `python -m bc_mcp_proxy setup` wizard die ready-to-paste install-links voor Cursor en VS Code, plus een Claude Desktop snippet, automatisch genereert.

```bash
python -m bc_mcp_proxy setup
```

### Stap 6 — Eerste login

De allereerste keer dat je proxy een tool call doet, doorloopt MSAL de **device code flow**:

```
To sign in, use a web browser to open https://microsoft.com/devicelogin
and enter the code ABCD-1234 to authenticate.
```

Open die URL in een browser, plak de code, log in met je Azure-account dat de Dynamics 365 BC permissions heeft. Na succesvolle login wordt je token lokaal gecached (gebruik makend van `msal-extensions` met platform-specifieke secure storage). Toekomstige proxy-runs zijn non-interactive tot je token verloopt — en dan vernieuwt de proxy 'm in stilte via de refresh token.

---

## 5. Eerste test: vraag iets aan je AI

Restart je AI-client en typ een vraag zoals:

- *"Toon me de top 5 klanten in CRONUS BE."*
- *"Welke vendors hebben een openstaand saldo van meer dan €5.000?"*
- *"Geef me de Sales Invoices van vorige maand met status Open."*

In Static mode kiest de AI direct de juiste tool (bv. `ListCustomers_PAG30009`) en parameters (`top: 5`).

In Dynamic mode zie je drie stappen — eerst zoekt de AI welke action er beschikbaar is (`bc_actions_search`), dan haalt hij het schema op (`bc_actions_describe`), dan voert hij de action uit (`bc_actions_invoke`).

Beide modes geven dezelfde data terug. Welke je kiest hangt af van hoe groot je BC-installatie is en hoeveel flexibiliteit je de AI wil geven.

---

## 6. Static vs Dynamic Tool Mode — wanneer welk?

| | **Static** | **Dynamic** |
|---|---|---|
| **Tools voor de AI** | Eén per geselecteerde page (bv. 10 tools) | Drie generieke tools |
| **Eerste call performance** | Snel (sub-seconde) | Langzaam (50–60s eerste keer als *Discover Additional Objects* aan staat) |
| **Daarna** | Snel | Snel — de catalogus is gecached |
| **AI-promptkosten** | Hoger (de AI ziet alle tool-schemas in de prompt) | Lager (alleen drie meta-tools) |
| **Best voor** | Beperkte set use cases die je expliciet wilt blootstellen | Flexibele exploratie, vooral als je veel pages hebt |

Onze ervaring: begin met **static mode** voor de eerste test (snel succes, je weet welke tools er zijn). Schakel naar **dynamic** wanneer je gebruikers vragen dingen waarvoor jij niet expliciet een tool hebt gemaakt.

---

## 7. Welke BC-versie heb je?

Microsoft heeft de MCP-endpoint URL gewijzigd in v28:

| BC versie | Endpoint URL |
|---|---|
| **26 / 27** | `https://api.businesscentral.dynamics.com/v2.0/{environment}/mcp` |
| **28+** | `https://mcp.businesscentral.dynamics.com` (environment via header) |

Je kunt dit controleren door in BC naar **Help & Support → About** te gaan, of door de connection-string op je MCP-configuratie pagina te bekijken (BC v28 toont een **Copy Connection String** dialoog die de juiste URL voor jouw versie laat zien).

Onze proxy detecteert dit automatisch op basis van `BC_BASE_URL` — je hoeft alleen die ene env-var aan te passen wanneer je over de v28-grens gaat.

---

## 8. Veelvoorkomende fouten en hoe je ze oplost

### "The MCP Configuration named X was not found or not active"

99% van de tijd: je MCP Configuration in BC heeft **Active=No**. Dit is de meest verraderlijke fout omdat BC bovenaan "✓ Saved" toont, maar de Active-toggle staat per default uit en moet je expliciet aanzetten.

Andere oorzaken:
- Een spatie aan het einde van de configuration name (vergelijk letterlijk met de UI-waarde, kopieer & plak).
- Je hebt de configuration in een andere environment dan waar je nu naar wijst.

### Calls hangen of geven `httpx.ReadTimeout`

Vooral in dynamic mode met *Discover Additional Objects* aan: de eerste `bc_actions_search` enumereert de hele catalogus en kan **50–60 seconden** server-side duren op een gemiddelde Cronus demo, langer op een productie met veel custom apps.

Onze proxy default sinds v0.3.0 op een 120-seconden HTTP-timeout — voldoende voor de eerste call. Vervolgcalls in dezelfde sessie zijn meestal sub-seconde. Verhoog `BC_HTTP_TIMEOUT_SECONDS` als je nog langere first-call latency ervaart.

### `JSON-RPC -32603 "An error occurred."` zonder details

BC's catch-all foutmelding wanneer iets in een dynamic-tool call misgaat. De wire-response is bewust mager. Je vindt de **echte** reden in **Azure Application Insights** als event `RT0054` met custom dimension `toolInvocationFailureReason`:

```kql
traces
| where timestamp > ago(15m)
| where customDimensions.eventId == 'RT0054'
| where customDimensions.toolInvocationResult == 'Failure'
| project timestamp,
          configurationName = tostring(customDimensions.configurationName),
          toolName = tostring(customDimensions.toolName),
          dynamicToolName = tostring(customDimensions.dynamicToolName),
          toolInvocationFailureReason = tostring(customDimensions.toolInvocationFailureReason)
| order by timestamp desc
```

Telemetry inschakelen vereist een Application Insights-resource in Azure die je via je BC admin center aan het environment koppelt — een aparte stap die voor productie-deployments sowieso aan te raden is.

### Authentication-fouten / device flow timed out

Doorloop je App Registration:

- Redirect URI in formaat `ms-appx-web://Microsoft.AAD.BrokerPlugin/<client-id>`
- "Allow public client flows" op **Yes**
- API permissions toegekend **én** admin consent gegeven (groene vinkjes naast elke permission)
- De gebruiker waarmee je in je browser inlogt is dezelfde tenant en heeft toegang tot het BC environment

### Repeated sign-in prompts

Je MSAL token-cache is niet schrijfbaar. Pass `--DeviceCacheLocation` als argument aan de proxy met een directory waar jouw user-account schrijfrechten heeft.

---

## 9. Beveiligingsoverwegingen

- **Geen application secrets**. De proxy gebruikt alleen *delegated* permissions via de device-code flow. Er is geen client-secret om te beheren of te roteren.
- **Tokens lokaal gecached**. Via `msal-extensions` met OS-specifieke secure storage (DPAPI op Windows, Keychain op macOS, libsecret op Linux). Geen plaintext.
- **Geen tokens in logs**. Onze proxy logt nooit access- of refresh-tokens; debug-output bevat alleen vervaltimestamps voor diagnose.
- **Permissions zijn delegated**. Wat de proxy kan zien en doen, kan de ingelogde gebruiker ook handmatig in BC. De AI heeft geen extra rechten.
- **Configuration name als gate**. In BC bepaal je per MCP Configuration welke pages/objecten beschikbaar zijn. Zet je gevoelige data op een aparte configuration die je alleen voor specifieke gebruikers active maakt.

---

## 10. Een Claude Desktop Extension publiceren

Als je end-users wilt laten installeren met één klik (zonder dat ze Python moeten draaien), bouwt onze repo een `.dxt` (Desktop Extension) bundle:

```bash
git clone https://github.com/VangelderSolutions/bc-mcp-proxy.git
cd bc-mcp-proxy
pwsh dxt/build.ps1     # Windows
./dxt/build.sh         # Mac/Linux
```

Het resultaat (`dist/bc-mcp-proxy-<versie>.dxt`) sleep je in Claude Desktop. Bij installatie vraagt Claude om de tenant ID, client ID, environment, company en configuration name. Geen JSON-bestanden, geen pip-install — drag & drop.

We werken aan het submitten naar de Anthropic Extensions Directory zodat het ook publiek doorzoekbaar wordt; tot die tijd is de DXT direct downloadbaar uit onze GitHub releases.

---

## 11. Hulp nodig?

De Azure App Registration en de juiste permissions vragen aandacht. Voor klanten die liever niet zelf met `Manifest.json`, redirect-URIs en delegated permissions werken, biedt **Vangelder Solutions** een **end-to-end MCP setup pakket** aan:

- Azure App Registration aangemaakt en gevalideerd in jouw tenant
- BC MCP Configuration aangemaakt op de juiste environment(s)
- Proxy + AI-client (Claude Desktop, VS Code, Cursor) geïnstalleerd en getest op jouw werkstation
- Korte training voor de gebruikers: welke vragen werken goed, welke niet, wat zijn de privacy- en kostencurves
- Optioneel: Application Insights telemetry-koppeling zodat je later kunt zien wie welke tools gebruikt

Eén afspraak (online of on-site), configuratie afgerond, MCP werkend in jouw productie-omgeving.

📧 **stephane@vangeldersolutions.be**  
🌐 [www.vangeldersolutions.be](https://www.vangeldersolutions.be)  
📦 [github.com/VangelderSolutions/bc-mcp-proxy](https://github.com/VangelderSolutions/bc-mcp-proxy)

---

## Bronnen

- [`vangelder-bc-mcp-proxy` op GitHub](https://github.com/VangelderSolutions/bc-mcp-proxy) — onze fork, MIT-licensed
- [`microsoft/BCTech BcMCPProxyPython`](https://github.com/microsoft/BCTech/tree/master/samples/BcMCPProxyPython) — Microsoft's referentie-implementatie
- [Configure Business Central MCP Server](https://learn.microsoft.com/en-us/dynamics365/business-central/dev-itpro/ai/configure-mcp-server) — Microsoft Learn
- [Analyze MCP Server Tool Calls Telemetry](https://learn.microsoft.com/en-us/dynamics365/business-central/dev-itpro/administration/telemetry-mcp-server-trace) — RT0054 event reference
- [Model Context Protocol specificatie](https://modelcontextprotocol.io) — Anthropic
- [Claude Desktop Extensions (DXT)](https://github.com/anthropics/dxt) — voor één-klik install bundles

---

*Versie van deze handleiding: 1.0 — gepubliceerd door Vangelder Solutions, gebaseerd op `vangelder-bc-mcp-proxy 0.3.0`. Vragen of suggesties? Open een issue op GitHub of mail ons direct.*
