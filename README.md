# Accounting Cloud

A self-hosted personal finance app for one household: budget, ledger with a
12-month cash-flow projection, accounts, credit card statements, portfolio,
debts, goals, net worth, workplace benefits and card perks, a home-move
planner, plus an MCP server so AI assistants can read and update your data.

Built with Flask, MongoDB and server-rendered Jinja2 templates. No cloud
services required; the optional AI features work with any Ollama-compatible
endpoint, including one on your own machine.

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
  categorization, an optional two-vote LLM pass for unknown merchants,
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
- **MCP server**: about 50 read and write tools over stateless streamable
  HTTP with bearer-token auth, for Claude and other MCP clients.
  [docs](docs/mcp-server.md)
- **Advisor agent**: optional weekly, monthly and portfolio briefings written
  by a local LLM from your data and a household profile you provide.
  [docs](docs/advisor-agent.md)

A page-by-page tour is in [docs/features.md](docs/features.md).

## Quick start

> **New here? Follow [docs/getting-started.md](docs/getting-started.md)**: a
> step-by-step walkthrough from install to the setup wizard and your first
> week of use, with the core concepts explained.

Requires [Docker Desktop](https://docs.docker.com/desktop/) (Windows, Mac) or
Docker Engine with the Compose plugin (Linux). Then run one command:

**macOS / Linux:**

```bash
curl -fsSL https://github.com/avansledright/accounting-cloud-oss/releases/latest/download/install.sh | sh
```

**Windows** (in PowerShell):

```powershell
irm https://github.com/avansledright/accounting-cloud-oss/releases/latest/download/install.ps1 | iex
```

The installer puts everything in an `accounting-cloud` folder in your home
directory. It generates the secret key, picks up your time zone, downloads
and starts the app, then opens <http://localhost:5052>. Run it again to
update; your settings and data are kept. Afterwards, manage the app with
`~/accounting-cloud/accounting-cloud start|stop|update|backup|uninstall`
(Windows: `.\accounting-cloud` in that folder, or use the **Accounting Cloud**
shortcut).

From a clone instead:

```bash
git clone https://github.com/avansledright/accounting-cloud-oss.git
cd accounting-cloud-oss
cp .env.example .env
# put a random value in SECRET_KEY:
python3 -c "import secrets; print(secrets.token_hex(32))"
docker compose up -d --build
```

Open <http://localhost:5052>. A fresh install starts a short **setup
wizard**: your primary checking account, recurring income, regular bills and
optional extras. The ledger then shows a 12-month projection, and a
getting-started checklist on the dashboard covers the rest.

Want to look around first? Click **Load demo data instead** on the wizard's
first page, or from the command line:

```bash
docker compose exec web python scripts/seed_demo.py   # run in the install folder or clone
```

Optional extras:

```bash
# MCP server for AI clients (set MCP_API_TOKEN in .env first)
docker compose --profile mcp up -d
```

Running without Docker, and running the tests, is covered in
[docs/development.md](docs/development.md).

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
| `OLLAMA_URL` | `http://localhost:11434` | Ollama-compatible endpoint for card categorization and spending tips. |
| `OLLAMA_MODEL` | `granite4.2:3b` | Model for categorization (small and fast is fine). |
| `OLLAMA_API_KEY` | empty | Sent as a bearer token if set. |
| `CC_LLM_ENABLED` | `true` | Turn the LLM categorization pass on or off. |
| `CC_LLM_TIMEOUT` | `60` | Seconds per categorization request. |
| `ADVISOR_ENABLED` | `false` | Run the scheduled advisor reports (Mondays and the 1st, 06:00). |
| `AGENT_OLLAMA_HOST` | `http://localhost:11434` | Endpoint for advisor reports. |
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

   optional outbound: Yahoo Finance / Alpha Vantage, FRED, Ollama-compatible LLM
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
