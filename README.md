<div align="center">

# ⚡ OwnAPI

### **OwnAPI — The Plaid-Style User-Delegated Browser Middleware for AI Agents. Turn any API-less, WAF-protected website into a self-healing MCP & Function-Calling tool.**

[![License: MIT](https://img.shields.io/badge/License-MIT-emerald.svg)](#license)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![MCP Ready](https://img.shields.io/badge/MCP-Model%20Context%20Protocol-indigo.svg)](#enterprise-web-dashboard--mcp-server)
[![Registry Validation](https://img.shields.io/badge/Verified%20Registry-11%2F11%20(100%25%203x%20Auto--Test)-success.svg)](#pre-built-registry-marketplace)
[![Architecture](https://img.shields.io/badge/Architecture-User--Delegated%20Browser%20Tunnel-purple.svg)](#system-architecture)

**Zero Proxies · Zero TLS Spoofing · 3x Auto-Validated Connectors · Self-Healing 2.0 · Async HITL Queue · Cryptographic ToS Shield**

[Quickstart](#-30-second-quickstart) · [Why OwnAPI?](#-why-ownapi-the-problem-with-scrapers) · [Architecture](#-system-architecture) · [Comparison](#-how-ownapi-compares) · [Pre-Built Marketplace](#-pre-built-registry-marketplace) · [Enterprise REST & Dashboard](#-enterprise-web-dashboard--rest-api)

</div>

---

## 🚨 Why OwnAPI? (The Problem with Scrapers)

Over **90% of enterprise and consumer workflows**—airlines, local banking portals, legacy ERPs, regional e-commerce platforms, and healthcare systems— either have **no public API** or hide their real capabilities behind undocumented internal XHR/Fetch endpoints protected by **Cloudflare Turnstile, PerimeterX (HUMAN), Akamai Bot Manager, and DataDome**.

Today, engineering teams building AI Agents face two broken choices:

1. **Traditional Scrapers (`curl_cffi`, residential proxies, headless Selenium):**
   - **Technically Fragile:** Modern WAFs inspect TLS JA3/JA4 fingerprints, HTTP/2 frame ordering, Canvas/WebGL entropy, and behavioral telemetry. Spoofed requests break within days.
   - **Legally Toxic:** Bypassing access controls from third-party servers using rotating proxy pools violates the Computer Fraud and Abuse Act (CFAA) and website Terms of Service—making it uninvestable and unusable for regulated enterprises.
2. **Raw Cloud Browser Infrastructure (Browserbase / Raw Playwright DOM Agents):**
   - **Slow & Expensive:** Letting an LLM click DOM elements step-by-step takes 30–90 seconds per task, burns $0.10–$0.50 in vision/DOM tokens per run, and fails whenever a CSS class changes.

### 🛡️ The OwnAPI Paradigm Shift: User-Delegated Browser Middleware

**OwnAPI is not a scraper.** Inspired by how **Plaid** pioneered user-delegated financial connectivity, OwnAPI acts as an **authenticated, user-delegated browser middleware (`User-Delegated In-Browser Fetch Tunnel`)**:

- **100% Real Browser Execution (`page.evaluate("fetch(...)")`):** Instead of spoofing TLS packets from a server, OwnAPI executes structured `fetch()` calls directly inside the user's real, authenticated Chromium browser context using the user's genuine IP, hardware fingerprint, and cookies.
- **Compiler + 3x Auto-Test Validator Loop:** You perform an action once in a browser window. OwnAPI intercepts the underlying JSON XHR/Fetch traffic, synthesizes a clean Python SDK & MCP connector via GPT-4o, and **automatically runs 3 consecutive live validation tests**—self-repairing any schema bugs before registering the tool.
- **Self-Healing 2.0 (Nightly Auto-Repair):** If a target website changes its internal API schema at 2:00 AM, OwnAPI's `AutoRepairEngine` detects the drift, re-captures live network traffic, rewrites `client.py`, and re-validates it with 3x Auto-Test automatically.
- **Async Human-in-the-Loop (HITL) Queue:** When running unattended background agents (`non_interactive=True`), if a target site triggers a CAPTCHA or 2FA challenge, the agent never crashes—OwnAPI pauses the task in a persistent `PENDING_USER_APPROVAL` queue so a human operator can solve the challenge with one click from the CLI or Web Dashboard.
- **Cryptographic ToS & User-Delegation Audit Trail:** Every onboarding and execution logs a SHA-256 signed user-delegation consent record (`.sessions/{tenant_id}/consent_audit.json`), providing enterprise compliance teams with an immutable audit log.

---

## 📊 How OwnAPI Compares

| Capability | Traditional Scrapers (`curl_cffi` / Proxies) | Cloud Browser Infra (Browserbase / Raw Playwright) | ⚡ **OwnAPI (User-Delegated Middleware)** |
| :--- | :---: | :---: | :---: |
| **Execution Latency** | ~500ms *(until blocked)* | 25s – 90s *(LLM DOM clicking)* | **1.5s – 3.0s *(Direct In-Browser `fetch()`)* ** |
| **WAF Survival (Cloudflare / PerimeterX / Akamai)** | ❌ Low *(TLS/IP bans)* | ⚠️ Medium *(Datacenter ASN flagged)* | **✅ 100% *(Genuine User Session + HITL Fallback)* ** |
| **Self-Healing Schema Drift** | ❌ Manual rewrite required | ⚠️ Brittle DOM selectors | **✅ Automated Nightly `AutoRepairEngine` + 3x Validator** |
| **Legal & ToS Compliance Shield** | ❌ High Risk *(Proxy evasion)* | ⚠️ Grey Area | **✅ Plaid-Style User Delegation + SHA-256 Audit Log** |
| **Async Human-in-the-Loop (HITL) Queue** | ❌ Crashes on CAPTCHA | ❌ Manual custom code | **✅ Built-in `PENDING_USER_APPROVAL` Queue & Replay** |
| **MCP & OpenAI Function-Calling Ready** | ❌ Raw HTML/JSON | ❌ Requires custom wrapper | **✅ Instant `master_mcp.py` & Unified `OwnAPIClient`** |
| **Marginal Cost per Call** | $$$ *(Residential Proxy GBs)* | $$$ *(Vision LLM + Cloud VM mins)* | **$0.00 *(Runs over compiled JSON tunnel)* ** |

---

## 🏗️ System Architecture

```text
+---------------------------------------------------------------------------------------------------+
|                                   AI AGENTS & ENTERPRISE CLIENTS                                  |
|         Claude Desktop / Cursor (MCP)   |   OpenAI Function Calling   |   REST API & Web UI       |
+-----------------------------------------+-----------------------------+---------------------------+
                                          |
                                          v
+---------------------------------------------------------------------------------------------------+
|                      5. ENTERPRISE WEB DASHBOARD, REST API & MASTER MCP SERVER                    |
|   server.py (FastAPI + static/index.html)   |   master_mcp.py (Dynamic FastMCP Tool Router)       |
|   own_api/sdk.py (OwnAPIClient)             |   core/consent.py (SHA-256 ToS Delegation Audit)    |
+---------------------------------------------------------------------------------------------------+
         |                                                |                                  |
         | (1) Onboard & Compile                          | (2) Execute Tool Call            | (3) Nightly Cron (03:00)
         v                                                v                                  v
+----------------------------------+    +------------------------------------+    +---------------------------------+
|  1. RECORD, COMPILE & 3x VERIFY  |    | 2. USER-DELEGATED BROWSER TUNNEL   |    |  3. SELF-HEALING 2.0 ENGINE     |
|        (core/onboarding.py)      |    |   (healer.py & core/token_store)   |    |      (core/auto_repair.py)      |
|                                  |    |                                    |    |                                 |
| - Captures live XHR/Fetch JSON   |    | - Multi-Tenant Isolated Profiles   |    | - Runs validate_all_services()  |
| - Filters telemetry & noise      |    |   (.sessions/{tenant}/{service})   |    | - Detects schema/endpoint drift |
| - Synthesizes BaseOwnClient code |    | - Local Desktop Chrome OR Cloud    |    | - Re-captures live site traffic |
|   & JSON Schema via GPT-4o       |    |   CDP WebSocket (mode=local|cloud) |    | - GPT-4o AST self-repair loop   |
| - Runs 3x Live Auto-Test Loop    |    | - Executes in-page fetch() with    |    | - Writes timestamped audit to   |
| - Auto-repairs on failure        |    |   real cookies, IP & WebGL state   |    |   auto_repair_report.json       |
+----------------------------------+    +-----------------+------------------+    +---------------------------------+
                                                          |
                                    +---------------------+---------------------+
                                    | WAF / CAPTCHA / 2FA Challenge Detected?   |
                                    +---------------------+---------------------+
                                                          |
                            +-----------------------------+-----------------------------+
                            | Interactive Mode                                          | Non-Interactive Agent Mode
                            v                                                           v
           +---------------------------------+                         +------------------------------------+
           |   Interactive Browser Prompt    |                         |  4. ASYNC HITL APPROVAL QUEUE      |
           | - Brings Chrome window to front |                         |       (core/hitl_queue.py)         |
           | - User solves CAPTCHA once      |                         | - Enqueues PENDING_USER_APPROVAL   |
           | - Harvests fresh cookies        |                         | - Saves to .sessions/hitl_queue    |
           | - Replays fetch() automatically |                         | - 1-Click Approve & Replay via     |
           +---------------------------------+                         |   Web Dashboard or `own-api queue` |
                                                                       +------------------------------------+
```

---

## 🚀 30-Second Quickstart

### 1. Installation

```bash
git clone https://github.com/your-org/own-api.git
cd own-api
pip install -e .
playwright install chromium
```

Configure your OpenAI API key in `.env` (used for natural language routing, zero-code site compilation, and nightly self-healing):

```bash
echo "OPENAI_API_KEY=sk-your-api-key" > .env
```

### 2. Launch the Enterprise Web Dashboard & Live Playground

Start the FastAPI server and interactive SaaS Web Dashboard on `http://localhost:8000`:

```bash
own-api serve --port 8000
```

Open **`http://localhost:8000`** in your browser to explore:
- **Registry Marketplace:** Inspect all 11 pre-built connectors with live `3/3 Auto-Test Verified` badges.
- **Live Playground:** Execute natural language queries (`POST /api/v1/ask`) or direct parameterized tool calls (`POST /api/v1/call`) with real-time latency metrics.
- **HITL Queue & Self-Healing 2.0:** Trigger full-fleet validation (`POST /api/v1/auto-repair`) or approve pending CAPTCHA/2FA tasks with one click.
- **ToS Consent Audit:** Inspect cryptographic SHA-256 user-delegation signatures (`GET /api/v1/consent`).

### 3. CLI Commands (`own-api`)

```bash
# List all registered & 3x-verified connectors
own-api list

# Ask the AI Agent in natural language (automatically selects & runs the right tool)
own-api ask "What is the current stock price of NVDA on Yahoo Finance?"
own-api ask "Find the top 5 trending repositories for 'mcp server' on GitHub"
own-api ask "Search Bitcoin market cap and USD price on CoinGecko"

# Call a specific connector directly with JSON parameters
own-api call yahoo_finance --params '{"symbol": "NVDA"}' --tenant default --mode local

# Turn ANY website into a verified API in 60 seconds (Record -> Compile -> 3x Auto-Test)
own-api add stripe_docs --url "https://docs.stripe.com" --desc "Search Stripe documentation"

# Run Self-Healing 2.0 validation & automatic repair across all services
own-api validate --all

# Start the nightly background Self-Healing 2.0 daemon (runs daily at 03:00)
own-api auto-repair --daemon --schedule "03:00"

# Inspect and approve Human-in-the-Loop (HITL) tasks
own-api queue list
own-api queue approve <task_id>
```

### 4. Python SDK (`OwnAPIClient`)

Use `OwnAPIClient` directly inside your LangChain, LlamaIndex, CrewAI, or custom agent pipelines:

```python
from own_api import OwnAPIClient

# Initialize with multi-tenant isolation, execution mode ("local" or "cloud"), and HITL policy
client = OwnAPIClient(
    tenant_id="enterprise_user_42",
    mode="local",              # "local" (user's Chrome) or "cloud" (CDP WebSocket / Docker)
    non_interactive=False,     # Set True for background agents to enqueue CAPTCHAs to HITL Queue
    auto_accept_tos=True,      # Logs SHA-256 User-Delegation Consent signature
)

# 1. Direct deterministic connector call
nvda_quote = client.call("yahoo_finance", symbol="NVDA")
print(nvda_quote["quote"]["regularMarketPrice"])

# 2. Natural language agent execution (GPT-4o tool routing)
answer = client.ask("Compare Bitcoin price on CoinGecko and find top MCP repos on GitHub")
print(answer)

# 3. Export OpenAI Function-Calling / Tool definitions for your own agent loop
tools = client.get_openai_tools()
```

### 5. Connect to Claude Desktop / Cursor (MCP Server)

Add `master_mcp.py` to your `claude_desktop_config.json` or `.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "own-api": {
      "command": "python",
      "args": ["master_mcp.py"]
    }
  }
}
```

Every connector inside `registry/` is automatically exposed as a strongly-typed MCP tool (`own_yahoo_finance`, `own_coingecko`, `own_github_trending`, `own_thy`, etc.) alongside meta-tools for health diagnostics and HITL queue management.

---

## 🌐 Pre-Built Registry Marketplace

OwnAPI ships with **11 pre-built, live-tested connectors** in `registry/`, all verified through our **3x Auto-Test Validator Loop (`100%` pass rate)**:

### 🌍 Global Developer, Finance & Knowledge Connectors

| Service ID | Target Platform | Primary Method & Signature | Protection / Mode | Auto-Test Status |
| :--- | :--- | :--- | :--- | :---: |
| **`yahoo_finance`** | Yahoo Finance | `get_stock_quote(symbol="NVDA")` | Rate-Limited Financial Edge / In-Browser Tunnel | ✅ `BAŞARILI (3/3)` |
| **`coingecko`** | CoinGecko | `search_crypto(query="bitcoin")` | Cloudflare WAF / In-Browser Tunnel | ✅ `BAŞARILI (3/3)` |
| **`github_trending`** | GitHub | `search_repositories(query="mcp server", limit=5)` | GitHub API Edge / In-Browser Tunnel | ✅ `BAŞARILI (3/3)` |
| **`hackernews`** | Hacker News (Algolia) | `search_stories(query="AI", limit=5)` | Cloudfront / In-Browser Tunnel | ✅ `BAŞARILI (3/3)` |
| **`wikipedia`** | Wikipedia REST API | `search_summary(query="Artificial intelligence", lang="en")` | Wikimedia Edge / In-Browser Tunnel | ✅ `BAŞARILI (3/3)` |

### ✈️ Enterprise Airlines & E-Commerce Connectors (High-WAF Showcase)

| Service ID | Target Platform | Primary Method & Signature | WAF / Bot Protection | Auto-Test Status |
| :--- | :--- | :--- | :--- | :---: |
| **`thy`** | Turkish Airlines | `search_flights(origin="IST", destination="SAW", date=...)` | **Akamai + PerimeterX (HUMAN)** | ✅ `BAŞARILI (3/3)` |
| **`pegasus`** | Pegasus Airlines | `search_flights(origin="SAW", destination="AYT", date=...)` | **Cloudflare Enterprise** | ✅ `BAŞARILI (3/3)` |
| **`enuygun`** | Enuygun Travel | `search_flights(origin="IST", destination="ADB", date=...)` | **Cloudflare Turnstile** | ✅ `BAŞARILI (3/3)` |
| **`ucak_bileti`** | Uçak Bileti | `search_flights(origin="IST", destination="ESB", date=...)` | **WAF + Session Token Guard** | ✅ `BAŞARILI (3/3)` |
| **`trendyol`** | Trendyol E-Commerce | `search_products(query="iphone", limit=5)` | **Cloudflare + Custom Anti-Bot** | ✅ `BAŞARILI (3/3)` |
| **`hepsiburada`** | Hepsiburada Marketplace | `search_products(query="laptop", limit=5)` | **Akamai Bot Manager** | ✅ `BAŞARILI (3/3)` |

---

## 🖥️ Enterprise Web Dashboard & REST API

When you run `own-api serve --port 8000`, `server.py` exposes a complete REST API for your internal microservices alongside the single-page SaaS Dashboard (`static/index.html`):

| Endpoint | Method | Description |
| :--- | :---: | :--- |
| `/` | `GET` | Serves the 4-tab TailwindCSS Dark-Mode Enterprise Web Dashboard & Live Playground. |
| `/api/v1/services` | `GET` | Lists all registered services, schemas, `validation_status`, and fleet-wide `validation_rate`. |
| `/api/v1/call` | `POST` | Executes a deterministic service call (`service`, `params`, `tenant_id`, `mode`, `non_interactive`) with latency tracking. |
| `/api/v1/ask` | `POST` | Runs the natural language GPT-4o agent across all registered tools (`prompt`, `tenant_id`). |
| `/api/v1/validate/{service}` | `POST` | Triggers the 3x Auto-Test Validator Loop (+ GPT-4o self-repair if needed) for a single service. |
| `/api/v1/auto-repair` | `POST` | Triggers `AutoRepairEngine.validate_all_services()` across the entire registry and saves the audit report. |
| `/api/v1/auto-repair/report` | `GET` | Returns the latest `.sessions/auto_repair_report.json` health & repair telemetry. |
| `/api/v1/hitl/queue` | `GET` | Returns all `PENDING_USER_APPROVAL` and resolved Human-in-the-Loop tasks. |
| `/api/v1/hitl/approve/{task_id}` | `POST` | Opens the interactive browser window for the human operator to solve CAPTCHA/2FA and replays the queued request. |
| `/api/v1/consent` | `GET` | Returns the cryptographic SHA-256 User-Delegation & ToS consent audit trail across all tenants. |

---

## 🔒 Security, Privacy & Legal Architecture

1. **Zero Credential Harvesting:** OwnAPI never asks for or stores plaintext usernames or passwords. Authentication happens directly inside the user's local Chrome profile (`.sessions/{tenant_id}/chrome_profile_{service}`).
2. **Multi-Tenant Isolation:** Every tenant's cookies, browser storage, and consent audit logs are strictly isolated under `.sessions/{tenant_id}/`.
3. **Explicit User Delegation (`core/consent.py`):** Before onboarding a target site or executing delegated calls, OwnAPI records a timestamped, SHA-256 hashed attestation confirming the user is acting as an authorized human account holder delegating their own browser session.

---

## 📄 License

Distributed under the **MIT License**. Built for the global AI Agent ecosystem.
