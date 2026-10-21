# Getting started

This walkthrough takes you from install to a working household ledger and
through the first week of use. It assumes Docker: Docker Desktop on Windows
and Mac, Docker Engine with the Compose plugin on Linux. To run from source
instead, see [development.md](development.md).

- [1. Install](#1-install)
- [2. Put it behind authentication](#2-put-it-behind-authentication)
- [3. The setup wizard](#3-the-setup-wizard)
- [4. Core concepts](#4-core-concepts)
- [5. Your first week](#5-your-first-week)
- [6. Where to go next](#6-where-to-go-next)
- [Starting over](#starting-over)

## 1. Install

### The installer (easiest)

Install [Docker Desktop](https://docs.docker.com/desktop/) (Windows, Mac) or
Docker Engine (Linux), start it once, then run:

**macOS / Linux:**

```bash
curl -fsSL https://github.com/avansledright/accounting-cloud-oss/releases/latest/download/install.sh | sh
```

**Windows** (in PowerShell):

```powershell
irm https://github.com/avansledright/accounting-cloud-oss/releases/latest/download/install.ps1 | iex
```

It creates an `accounting-cloud` folder in your home directory with three
files you might look at:

| File | What it is |
|---|---|
| `.env` | Your settings. The installer writes a random `SECRET_KEY`, your time zone (`TZ`) and the port (`WEB_PORT`, 5052). Other options are in [.env.example](../.env.example). |
| `docker-compose.yml` | The app, pinned to the release you installed. Replaced on update. |
| `accounting-cloud` | A helper: `start`, `stop`, `status`, `logs`, `open`, `update`, `backup` and `uninstall` (Windows: `accounting-cloud.cmd`). |

When it finishes, your browser opens <http://localhost:5052>. On Windows
there is also an **Accounting Cloud** shortcut on the desktop and in the Start
menu; on Linux, in the applications menu. The app restarts by itself
whenever Docker starts.

Installer options, set as environment variables before running it:
`ACCOUNTING_CLOUD_DIR` (install folder), `ACCOUNTING_CLOUD_VERSION` (a
specific release, e.g. `0.3.0`), `WEB_PORT` (if 5052 is taken) and
`NO_BROWSER=1`.

Skip to [2. Put it behind authentication](#2-put-it-behind-authentication).

### From a clone

```bash
git clone https://github.com/avansledright/accounting-cloud-oss.git
cd accounting-cloud-oss
cp .env.example .env
```

Edit `.env`. Only one value is required:

```bash
# generate a secret and paste it into SECRET_KEY=
python3 -c "import secrets; print(secrets.token_hex(32))"
```

Also worth setting now:

- `TZ`: your time zone, e.g. `TZ=America/New_York`. "Today" in projections
  and the scheduled jobs use it; the default is UTC.
- `WEB_PORT`: the host port, if 5052 is taken.

Everything else (market data key, LLM endpoints, MCP token) is optional and
explained in [.env.example](../.env.example) and the
[configuration table](../README.md#configuration).

Start the stack:

```bash
docker compose up -d --build
docker compose ps          # web and db should be "healthy" after ~30 seconds
```

Open <http://localhost:5052>. A fresh database opens the **setup wizard**.

## 2. Put it behind authentication

> [!WARNING]
> The web app has **no login**. Anyone who can reach the port can read and
> change everything, including a one-click export of all your data.

The Compose file binds the port to `127.0.0.1`, so out of the box only the
machine running Docker can reach it. That is fine for trying it out. Before
you open it to other devices:

- put it behind a reverse proxy with authentication (SSO/forward auth, or at
  least basic auth over TLS), or
- reach it only over a VPN such as WireGuard or Tailscale.

Do **not** just change the bind address to `0.0.0.0`. Working Caddy, nginx and
Traefik examples are in [deployment.md](deployment.md#reverse-proxy-and-sso).

## 3. The setup wizard

The wizard appears when the database has no accounts and setup has not been
finished or skipped. Every step writes ordinary records (the same ones the
Accounts, Budget and Debts pages create), so anything you enter can be edited
later on those pages.

| Step | What you enter | What it creates |
|---|---|---|
| **1. Welcome** | An optional household name (shown on the dashboard). The page also shows the server's time zone. | A setting only |
| **2. Primary account** | Name, bank, type (checking or savings) and today's balance | Your **primary account** |
| **3. Income** | Each regular paycheck: take-home amount, how often (every 2 weeks, twice a month, monthly, weekly) and the next payday | Recurring income items on the primary account |
| **4. Bills** | Rent or mortgage, utilities, phone, insurance...: amount, category, how often, next due date, auto-pay | Recurring expense items on the primary account |
| **5. Extras** | Optional savings and investment accounts, and debts you carry | Accounts and debts |
| **6. Done** | Nothing: a summary, your first three projected months, and next steps | Marks setup complete |

Tips:

- **Use today's balance** from your bank in step 2. Nothing is projected
  before today, because that money is already reflected in the balance.
- **Paydays:** for "every 2 weeks" any recent or upcoming payday works; the
  wizard keeps the 14-day rhythm and starts from the next one. For "twice a
  month" give the next payday and the day of the other one (for example the
  1st and the 15th).
- **Bills vs spending:** only add things that repeat on a schedule. Groceries,
  dining and fuel are better handled with budget targets (below).
- **Save and add more** keeps you on the same step if you have more rows than
  the form shows. Already-added items are listed above the form.
- Step back with the links at the top. Re-saving step 2 updates the primary
  account instead of creating a second one.

Other options on the welcome page:

- **Load demo data instead** fills the empty database with an invented
  household (Demo Bank, "Alex & Sam Example") so you can explore every page.
  It is the same data as `docker compose exec web python scripts/seed_demo.py`.
- **Skip setup** takes you straight to an empty dashboard.

You can walk through the wizard again at any time from **Admin → Setup &
Getting Started → Re-run setup wizard**. Your data is kept.

## 4. Core concepts

**Primary account.** One cash account (normally checking) is marked primary.
The ledger, the dashboard's cash-flow warnings and the forecast cards are all
about this account by default, and a budget item without an account belongs
to it. Change which account is primary under Accounts → Edit. Other accounts
still have their own ledgers (pick them in the ledger's account selector) and
count toward net worth.

**Recurring items vs transactions.**

- A *recurring item* (Budget page) is a rule: "$2,000 paycheck every other
  Friday", "$1,500 rent on the 1st". It never changes a balance by itself.
- A *transaction* (Ledger page) is something that actually happened, with a
  date and an amount. Recording one changes the account's calculated balance.

**Projections.** For each account the ledger lays out the next 12 months:
your recorded transactions plus a *projected* occurrence of every recurring
item. When you record the real transaction with the same description (the
**Verify** button does this for you), it replaces the projection, so nothing
is counted twice. Each month shows its ending balance and its **low point**;
a red low point means the account is projected to dip below zero that month
even if it recovers by month end. Details: [ledger-projections.md](ledger-projections.md).

**Account balance.** The balance stored on an account works as an *opening
balance*: every transaction you record for that account is applied on top of
it. That is why the wizard asks for today's balance and starts projections
from today.

**Budget targets.** Targets are monthly spending limits per category
(Groceries $600, Dining $250, ...). Set a *default template* once and override
single months when needed. The Budget page compares targets with planned
recurring items and with actual credit card spending in each category. Details:
[budget.md](budget.md).

## 5. Your first week

**Day 1: look at the forecast.** Open the **Ledger**. Expand the current month
and check that your paydays and bills land on the right dates. If a month's
low point is red, the dashboard shows a cash-flow warning. Move a bill, change
an amount, or try ideas in the **Simulator**, which never saves anything.

**As money moves: record transactions.**

- When a projected item happens, click **Verify** on its row. It opens a form
  prefilled with the description, date and amount; adjust the amount if the
  bill came in different, then save.
- If a projected item will not happen this time, **Skip** it.
- If one occurrence will be a different amount (a bigger electric bill),
  click the projected amount and edit just that occurrence.
- For anything unplanned (a repair, a refund), use the **Add Transaction**
  panel docked at the bottom of the ledger.

**End of week: reconcile.** Compare the ledger's balance for today with your
bank's balance.

- If they match, you are done.
- If the bank is lower, there is usually an unrecorded transaction: add it.
- If something was recorded twice or with the wrong amount, edit or delete it
  on the ledger.

Because the stored balance is an opening balance, prefer fixing transactions
over overwriting the account balance; if you do overwrite it, remember that
already-recorded transactions are applied again on top.

**Set budget targets.** On **Budget**, fill the Target column for everyday
categories and click **Save as Default Template**. **Seed from Recurring**
pre-fills targets from your recurring bills.

**Import a credit card statement.** On **Cards**, open *Upload Statement*, add
your card, and upload the CSV export from your issuer (Chase, Citi and Amex
formats, or a generic one). Transactions are de-duplicated and categorized, and
appear as actual spending on the Budget page. Details:
[credit-cards.md](credit-cards.md).

**Take a net worth snapshot.** On **Net Worth**, click **Take Snapshot Now**.
Do it about once a month after updating balances to build the history chart.

The dashboard's **Getting started** checklist ticks these off from your data
as you go. Hide it when you no longer need it; Admin can bring it back.

## 6. Where to go next

- **Savings and investments:** add accounts on **Accounts**, holdings on
  **Portfolio** (live prices and dividends), and recurring transfers on
  **Transfers** so contributions show up on both accounts' projections.
  [portfolio.md](portfolio.md)
- **Loans and property:** add debts on **Debts** and houses or cars with their
  loans on **Assets**. Loan payments in your budget are matched to the loan by
  description and simulated against the real balance. [features.md](features.md)
- **Goals:** emergency fund, retirement or payoff goals that track live
  balances.
- **Benefits:** contribution limits, employer match and card perks.
  [benefits.md](benefits.md)
- **AI assistants:** the optional MCP server lets Claude or another MCP client
  read and update your data. [mcp-server.md](mcp-server.md)
- **AI (optional):** advisor reports, smarter sorting of card purchases and
  spending tips, using Claude, ChatGPT or Gemini with your own key, or a local
  Ollama. Set it up on **Admin → AI settings**, which walks you through it.
  [ai.md](ai.md)
- **Backups:** set them up before you rely on the app.
  [deployment.md](deployment.md#backups-and-restore)

## Starting over

To throw everything away (for example after exploring the demo data) and get
the wizard back, remove the database volume:

```bash
docker compose down -v      # deletes ALL data in this stack, including uploaded statements
docker compose up -d
```

With the installer, run those two commands in the install folder, or
`accounting-cloud uninstall --delete-data` and then the installer again.

To keep your data and just walk the wizard again, use **Admin → Re-run setup
wizard** instead.
