# Accounting Cloud

Free, private household finances that run on your own computer: budget,
bills, a 12-month look ahead at every account, credit card statements,
investments, debts, goals and net worth. No subscription, no ads, no account
to sign up for, and your data never leaves your machine.

## Quickstart

You don't need to be technical. It takes about 10 minutes, most of it
waiting for downloads.

**What you need:** a Windows, Mac or Linux computer with a few GB of free
disk space.

### 1. Install Docker Desktop

Docker Desktop is a free app (for personal use) that runs Accounting Cloud
for you.

- Download it from **<https://www.docker.com/products/docker-desktop/>** and
  install it like any other app. Windows may ask you to restart.
- Open Docker Desktop and wait until it says the engine is **running**. If it
  asks you to sign in or create an account, you can skip that.
- Linux: install [Docker Engine](https://docs.docker.com/engine/install/)
  instead if you prefer; either works.

### 2. Open a terminal

- **Mac:** press <kbd>⌘ Cmd</kbd> + <kbd>Space</kbd>, type `Terminal`, press
  <kbd>Enter</kbd>.
- **Windows:** open the Start menu, type `PowerShell`, press <kbd>Enter</kbd>.
- **Linux:** open your Terminal app.

### 3. Paste one line and press Enter

**Mac or Linux:**

```bash
curl -fsSL https://github.com/avansledright/accounting-cloud-oss/releases/latest/download/install.sh | sh
```

**Windows:**

```powershell
irm https://github.com/avansledright/accounting-cloud-oss/releases/latest/download/install.ps1 | iex
```

The first run downloads the app (a couple of minutes), then your browser
opens to **<http://localhost:5052>**.

### 4. Follow the setup wizard

The wizard asks for your main checking account, your paychecks and your
regular bills, then shows your next 12 months. Just want to look around?
Click **Load demo data instead** on the first page.

### Using it day to day

- **Open it:** go to <http://localhost:5052> (bookmark it). Windows also gets
  an **Accounting Cloud** shortcut on the desktop and in the Start menu.
  Docker Desktop has to be running; turn on *Start Docker Desktop when you
  sign in* in its settings so you never have to think about it.
- **Update:** paste the same line from step 3 again. Your data is kept.
- **Back up:** in a terminal, run the line for your computer. It saves a
  backup file in the `accounting-cloud` folder in your home folder; copy it
  somewhere safe.
  - Mac or Linux: `~/accounting-cloud/accounting-cloud backup`
  - Windows: `cd ~\accounting-cloud; .\accounting-cloud backup`
- **Remove:** the same, with `uninstall` instead of `backup`. Add
  `--delete-data` at the end to erase your data too.

**Good to know**

- It only opens on the computer you installed it on, and it has **no
  password**. Anyone using that computer can see it. To reach it from your
  phone or another computer, see [docs/deployment.md](docs/deployment.md);
  that part is for technical users.
- The AI features are **optional and off**. Nothing is sent to any AI
  service unless you set one up yourself. To turn them on (advisor reports,
  smarter sorting of card purchases, spending tips), go to **Admin → AI
  settings** and follow the steps there to connect Claude, ChatGPT or Gemini
  with your own account, usually a few cents to a couple of dollars a month.
  [How it works](docs/ai.md)
- The only things it reaches out to the internet for are stock prices and
  mortgage rates.
- Stuck? [Open an issue](https://github.com/avansledright/accounting-cloud-oss/issues)
  and describe what you see (never include real financial details).

A longer walkthrough with the core concepts and your first week of use is in
[docs/getting-started.md](docs/getting-started.md).

---

# For self-hosters and developers

Everything below is the technical detail: what's inside, other ways to run
it, configuration and architecture.

Built with Flask, MongoDB and server-rendered Jinja2 templates. No cloud
services required; the optional AI features use Claude, ChatGPT or Gemini
with the user's own API key, or any Ollama-compatible endpoint, including one
on your own machine.

> [!WARNING]
> **There is no built-in authentication.** Anyone who can reach the web port
> can see and change everything, including a one-click full export. Run it on
> localhost, over a VPN, or behind a reverse proxy with SSO or basic auth.
> The bundled Compose file binds to `127.0.0.1` for this reason. See
> [SECURITY.md](SECURITY.md) and [docs/deployment.md](docs/deployment.md#reverse-proxy-and-sso).

## Screenshots

_Screenshots coming soon. To take your own without real data, load the demo
data set (`scripts/seed_demo.py`)._

<!--
![Dashboard](docs/images/dashboard.png)
![Ledger and projections](docs/images/ledger.png)
![Credit cards](docs/images/credit-cards.png)
-->

## Features

- **Budget**: recurring income and bills (weekly, bi-weekly, monthly with
  several days such as `1,15`, bi-monthly, yearly), recurring transfers shown as
  savings/investment budget lines, and category targets with a default
  template plus per-month overrides. [docs](docs/budget.md)
- **Ledger and projections**: actual transactions merged with projected
  occurrences for a rolling 12 months, per account; running and lowest balance
  per month, negative-balance warnings and forecast cards; per-occurrence
  overrides and skips; a what-if simulator. [docs](docs/ledger-projections.md)
- **Loan-aware projections**: loan payments are matched to their debt/asset
  and simulated against the real balance; the payoff payment is capped and
  later ones dropped, and paid-off budget lines archive themselves.
- **Accounts and transfers**: checking, savings, IRA, Roth IRA, 401(k), HSA,
  brokerage, deferred comp; one-time and recurring transfers that project on
  both sides.
- **Credit card statements**: CSV import (Chase, Citi, Amex and a generic
  format) with de-duplication, refunds netted as credits, keyword
  categorization (plus an optional AI pass for unknown merchants),
  subscription detection, payment plans, and missing-statement checks.
  [docs](docs/credit-cards.md)
- **Portfolio**: holdings with live prices and dividend history from Yahoo
  Finance (Alpha Vantage fallback), brokerage CSV import, dividend income,
  and trailing returns that feed projections. [docs](docs/portfolio.md)
- **Investment projections**: monthly compounding with each account's
  expected return plus its recurring contributions, 5 to 30 year horizons, and
  a 24-month net worth projection.
- **Net worth**: accounts + assets - liabilities, snapshots and history.
- **Debts**: credit cards, student loans (deferment, subsidized), personal
  loans; avalanche/snowball payoff planner; no-interest payment plans.
- **Assets**: real estate and vehicles with attached loans, amortization
  schedules and escrow simulation.
- **Goals**: manual goals and live ones tracking account balances, months of
  emergency fund, retirement targets, net worth milestones or debt payoff.
- **Benefits**: tax-year contribution limits (401(k), IRA, HSA, catch-up,
  overrides), employer match, paystub YTD reconciliation, and credit card
  perk tracking with researched templates and expiry alerts.
  [docs](docs/benefits.md)
- **Home-move / mortgage planner**: sell-and-buy estimate with live Freddie
  Mac rates, closing costs you set for your area, PITI, ARM and buydown
  scenarios, and a reverse "what price fits this payment" solver.
  [docs](docs/home-move.md)
- **Analytics, calendar and history** views of spending and upcoming bills.
- **Household payroll**: wages and taxes for a household employee, W-2,
  Schedule H and SSA EFW2 export with a local validator.
- **MCP server** (optional): about 50 read and write tools over stateless streamable
  HTTP with bearer-token auth, for Claude and other MCP clients.
  [docs](docs/mcp-server.md)
- **AI extras** (optional, off by default): weekly, monthly and portfolio
  briefings about your money, smarter categorization of unknown merchants,
  and spending tips. Set up in the app (Admin → AI settings) with Claude,
  ChatGPT or Gemini using your own API key, or a local Ollama; a
  step-by-step walkthrough is on the page. Without it, everything else works
  the same. [docs](docs/ai.md)
  [docs](docs/advisor-agent.md)

A page-by-page tour is in [docs/features.md](docs/features.md).

## Installing

### The installer

The Quickstart commands download `install.sh` (macOS, Linux) or `install.ps1`
(Windows) from the latest release. They need Docker with the Compose plugin
and:

- create `~/accounting-cloud` with the release's `docker-compose.yml` (pinned
  to that version's image, `ghcr.io/avansledright/accounting-cloud`), a `.env`
  with a random `SECRET_KEY`, your time zone and `WEB_PORT`, and an
  `accounting-cloud` helper (`start|stop|status|logs|open|update|backup|uninstall`);
- pull and start the stack (Compose project `accountingcloud`), then open the
  browser;
- keep `.env` and the data volumes when run again, so re-running updates;
- refuse a first install if `accountingcloud` data volumes already exist,
  unless `ACCOUNTING_CLOUD_REUSE_DATA=1`.

Options (environment variables): `ACCOUNTING_CLOUD_DIR`,
`ACCOUNTING_CLOUD_VERSION` (e.g. `0.3.0`), `WEB_PORT`, `NO_BROWSER=1`.
Everything in [docs/deployment.md](docs/deployment.md) applies; run the
`docker compose` commands in the install folder.

### From a clone

```bash
git clone https://github.com/avansledright/accounting-cloud-oss.git
cd accounting-cloud-oss
cp .env.example .env
# put a random value in SECRET_KEY:
python3 -c "import secrets; print(secrets.token_hex(32))"
docker compose up -d --build
```

Open <http://localhost:5052>. A fresh install starts the setup wizard. Demo
data from the command line (in the install folder or clone):

```bash
docker compose exec web python scripts/seed_demo.py
```

Kubernetes manifests, reverse-proxy examples, backups and upgrades:
[docs/deployment.md](docs/deployment.md). Running without Docker, and running
the tests: [docs/development.md](docs/development.md).

### Optional extras

- **AI features:** in the app, **Admin → AI settings** (`/settings/ai`):
  Claude, ChatGPT, Gemini or Ollama, with the key stored encrypted with
  `SECRET_KEY`. The older `OLLAMA_URL` / `AGENT_OLLAMA_HOST` variables still
  work until settings are saved there. Off by default; nothing contacts an AI
  service until it's set up. [docs](docs/ai.md)
- **MCP server** for AI clients such as Claude: set `MCP_API_TOKEN` in
  `.env`, then `docker compose --profile mcp up -d`.
  [docs](docs/mcp-server.md)

## Configuration

Everything is configured with environment variables (`.env` for Compose). All
are optional except `SECRET_KEY`, which you should always set.

| Variable | Default | Purpose |
|---|---|---|
| `SECRET_KEY` | random at each start | Flask secret for sessions and CSRF tokens. |
| `MONGO_URI` | `mongodb://localhost:27017/` | MongoDB connection string (Compose points it at its `db` service). |
| `MONGO_DB_NAME` | `accounting_cloud` | Database name. |
| `APPLICATION_ROOT` | `/` | Sub-path when served behind a proxy, e.g. `/accounting`. A proxy can send `X-Script-Name` instead. |
| `STARTING_BALANCE` | `0` | Ledger balance used only until the first account exists. |
| `FLASK_DEBUG` | `false` | Debug mode for `python app.py`. Never in production. |
| `TZ` | `UTC` | Time zone for schedules (17:00 price refresh, 06:00 reports) and for "today". |
| `CC_STATEMENT_DIR` | `./data/cc_statements` | Where uploaded statement CSVs are kept. |
| `ESCROW_PROPERTY_TAX_MONTHS` | `6,12` | Months the escrow simulation pays property tax. |
| `ESCROW_INSURANCE_MONTH` | `1` | Month the escrow simulation pays homeowner's insurance. |
| `ALPHA_VANTAGE_KEY` | empty | Fallback market-data API key; Yahoo Finance needs none. |
| `OLLAMA_URL` | empty (AI off) | Ollama-compatible endpoint for card categorization and spending tips. |
| `OLLAMA_MODEL` | `granite4.2:3b` | Model for categorization (small and fast is fine). |
| `OLLAMA_API_KEY` | empty | Sent as a bearer token if set. |
| `CC_LLM_ENABLED` | `true` | Turn the LLM categorization pass on or off. |
| `CC_LLM_TIMEOUT` | `60` | Seconds per categorization request. |
| `ADVISOR_ENABLED` | `false` | Run the scheduled advisor reports (Mondays and the 1st, 06:00). |
| `AGENT_OLLAMA_HOST` | empty (AI off) | Endpoint for advisor reports. |
| `AGENT_OLLAMA_MODEL` | `gemma4:e4b` | Model for advisor reports. |
| `AGENT_OLLAMA_API_KEY` | empty | Bearer token for the advisor endpoint. |
| `AGENT_OLLAMA_TIMEOUT` | `3600` | Seconds to wait for a report. |
| `ADVISOR_PROFILE_FILE` | empty | Path to your household profile ([example](agent/household_profile.example.md)). |
| `MCP_API_TOKEN` | empty | Required by the MCP server; it refuses every request until set. |
| `MCP_HOST` | `0.0.0.0` | MCP bind address. |
| `MCP_PORT` | `8765` | MCP port (also the Compose host port). |
| `MCP_MOUNT_PATH` | empty | Public prefix if a proxy strips one in front of the MCP server. |
| `WEB_PORT` | `5052` | Compose host port for the web app (bound to `127.0.0.1`). |

The optional outbound connections are Yahoo Finance and Alpha Vantage
(ticker symbols only), FRED (mortgage rates for the planner), and the LLM
endpoints you configure.

## Architecture

```
                 reverse proxy with auth (you provide)
                               |
   browser ---------> web: Flask + gunicorn (app.py, templates/) ----+
                       | background threads:                         |
                       |  - 17:00 price refresh                      |
                       |  - advisor report scheduler (opt-in)        v
                       |                                         MongoDB
   AI client --------> mcp: mcp_server.py (bearer token) ------------^
                       uses agent/tools/* (shared read/write helpers)

   optional outbound: Yahoo Finance / Alpha Vantage, FRED, the AI provider you set up
```

- `app.py`: every route and the business logic (projection engine, imports,
  analytics). `models.py`: document classes and shared constants (categories,
  account types, merchant rules). `benefits.py` and `home_move.py`: pure
  calculation modules. `payroll_forms.py` / `payroll_validator.py`: tax forms.
- `agent/`: data-summary tools shared by the advisor and the MCP server,
  report generation, prompts and the scheduler.
- Data lives in about 30 MongoDB collections; the JSON export includes all of
  them automatically.

Details: [docs/architecture.md](docs/architecture.md).

## Documentation

| Topic | |
|---|---|
| **Getting started: install, setup wizard, first week, core concepts** | [docs/getting-started.md](docs/getting-started.md) |
| **AI features: set up Claude, ChatGPT, Gemini or Ollama** | [docs/ai.md](docs/ai.md) |
| Architecture and data model | [docs/architecture.md](docs/architecture.md) |
| Budget system | [docs/budget.md](docs/budget.md) |
| Ledger projection engine | [docs/ledger-projections.md](docs/ledger-projections.md) |
| Portfolio, prices and investment projections | [docs/portfolio.md](docs/portfolio.md) |
| Benefits and card perks | [docs/benefits.md](docs/benefits.md) |
| Credit card import and categorization | [docs/credit-cards.md](docs/credit-cards.md) |
| Home-move / mortgage planner | [docs/home-move.md](docs/home-move.md) |
| Other pages (debts, goals, net worth, payroll, ...) | [docs/features.md](docs/features.md) |
| MCP server | [docs/mcp-server.md](docs/mcp-server.md) |
| Advisor agent | [docs/advisor-agent.md](docs/advisor-agent.md) |
| Deployment, reverse proxy, Kubernetes, backups | [docs/deployment.md](docs/deployment.md) |
| Development and tests | [docs/development.md](docs/development.md) |

## Security

The web app has **no login**. Put it behind authentication (SSO/forward auth,
basic auth over TLS) or keep it on a private network. The MCP server requires
a bearer token and has full write access. Backups and statement files contain
everything; encrypt them. Report vulnerabilities privately as described in
[SECURITY.md](SECURITY.md).

This software is not financial, tax or legal advice. Tax figures (contribution
limits, payroll rates) are seed defaults that change every year; check them
against official sources and override them in the app.

## Contributing

Contributions are welcome. Please read [CONTRIBUTING.md](CONTRIBUTING.md),
and never include real financial data in issues or pull requests.

## License

[MIT](LICENSE)
