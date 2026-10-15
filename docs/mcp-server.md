# MCP server

`mcp_server.py` is a standalone [Model Context Protocol](https://modelcontextprotocol.io)
server that exposes the app's MongoDB data to AI clients (Claude Code, Claude
Desktop, or any MCP-capable client) as a set of tools. It has **read tools**
(balances, transactions, budgets, cash flow, portfolio, net worth, debts),
**write tools** (create, update and delete transactions, recurring items,
transfers, accounts, assets, debts, budget targets, goals, holdings and
dividends) and two **report tools** that trigger the
[advisor reports](advisor-agent.md).

It is a separate process from the Flask web app. It talks to the same MongoDB
database directly and does **not** import `app.py`, so it does not go through
the web routes. That matters for write tools: see
[Write tools and app side effects](#write-tools-and-app-side-effects).

> **Security warning.** The MCP server has full write access to your financial
> data. Anyone holding the token can create and delete transactions, accounts,
> debts and holdings. Keep it off the public internet, or put it behind TLS
> (a reverse proxy with HTTPS), and rotate `MCP_API_TOKEN` if it may have
> leaked. The token travels in a header on every request, so plain HTTP over
> an untrusted network exposes it.

## Running it

### Locally

```bash
export MONGO_URI=mongodb://localhost:27017/
export MCP_API_TOKEN=$(python -c "import secrets; print(secrets.token_urlsafe(32))")
python mcp_server.py
```

The server listens on `MCP_HOST:MCP_PORT` (default `0.0.0.0:8765`). It is
served by uvicorn from the `if __name__ == "__main__":` block at the bottom of
`mcp_server.py`.

`python mcp_server.py` does not read `.env` by itself; export the variables in
your shell (or use Docker Compose, which reads `.env`).

### With Docker Compose

The `mcp` service in `docker-compose.yml` is behind the `mcp` profile, so it
only starts when asked for:

```bash
cp .env.example .env     # set SECRET_KEY and MCP_API_TOKEN
docker compose --profile mcp up -d --build
```

It reuses the web app image (`accounting-cloud:latest`) with
`command: ["python", "mcp_server.py"]`, points `MONGO_URI` at the compose `db`
service, and publishes the port on `127.0.0.1:${MCP_PORT:-8765}` only. Compose
reads the rest of `.env` through `env_file`, so `AGENT_OLLAMA_*` and
`ADVISOR_PROFILE_FILE` set there also reach the MCP container.

For Kubernetes, see [deployment.md](deployment.md) and `deploy/kubernetes/mcp.yaml`.

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `MONGO_URI` | `mongodb://localhost:27017/` | MongoDB connection string. |
| `MONGO_DB_NAME` | `accounting_cloud` | Database name. Must match the web app. |
| `MCP_HOST` | `0.0.0.0` | Bind address. |
| `MCP_PORT` | `8765` | Bind port. |
| `MCP_MOUNT_PATH` | empty | Public path prefix when a reverse proxy serves the server under a sub-path and strips it (for example `/accounting-mcp`). It is passed to the SSE app so the message endpoint it advertises to clients includes the prefix. Leave empty for direct access. |
| `MCP_API_TOKEN` | empty | Bearer token required on every request. **Required**: with no token the server refuses everything with 503. |
| `AGENT_OLLAMA_HOST` | `http://localhost:11434` | Ollama-compatible server used by the two report tools. |
| `AGENT_OLLAMA_MODEL` | `gemma4:e4b` | Model for the report tools. |
| `AGENT_OLLAMA_API_KEY` | empty | Sent as `Authorization: Bearer ...` to the LLM server when set. |
| `AGENT_OLLAMA_TIMEOUT` | `3600` | Seconds to wait for one report generation. |
| `ADVISOR_PROFILE_FILE` | empty | Household profile for the report prompts. See [advisor-agent.md](advisor-agent.md#household-profile). |

The `AGENT_OLLAMA_*` variables are read by `agent/reports.py` at import time;
they only matter if you call the report tools.

## Endpoints and transports

`mcp_server.py` builds the FastMCP streamable HTTP app and grafts the legacy
SSE routes onto it, so one process serves both:

| Path | Transport | Notes |
|---|---|---|
| `/http` | Streamable HTTP, **stateless** (`stateless_http=True`) | Preferred. Every request is self-contained, so it works behind a load balancer or a multi-replica Service. |
| `/sse` | Legacy SSE event stream | For older clients only. |
| `/messages/` | Legacy SSE JSON-RPC POSTs | Paired with `/sse`. |

SSE keeps its session in one process's memory. A load balancer may route the
follow-up POSTs to `/messages/` to a different replica, which breaks the
session, so **SSE only works against a single replica**. Point clients at
`/http` whenever they support it.

There is no unauthenticated health endpoint; every HTTP request goes through
the bearer check.

## Authentication

Every HTTP request must carry:

```
Authorization: Bearer <MCP_API_TOKEN>
```

The check is implemented by `BearerAuthASGI` in `mcp_server.py`:

- **Fails closed.** If `MCP_API_TOKEN` is empty, every request gets
  `503 {"error":"mcp token not configured"}`. A missing secret never silently
  exposes write access.
- **Wrong or missing token** (or a scheme other than `Bearer`) gets
  `401 {"error":"unauthorized"}`.
- **Constant-time compare.** The token is checked with `hmac.compare_digest`.
- It is written as raw ASGI middleware rather than Starlette's
  `BaseHTTPMiddleware`, because the latter buffers response bodies and would
  break SSE streaming.

The web app is expected to sit behind a reverse proxy with SSO, but MCP
clients usually cannot complete an interactive browser login, which is why
this surface uses a static token instead.

Generate a token with:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

To rotate it, set a new value, restart the server, and update every client.

## Connecting clients

Replace `HOST` with the machine running the server (`localhost` if the client
runs on the same machine as Docker Compose, since the port is bound to
`127.0.0.1`).

### Claude Code

```bash
claude mcp add --transport http accounting http://HOST:8765/http \
  --header "Authorization: Bearer $MCP_API_TOKEN"
```

### Claude Desktop and other clients

Clients that support remote HTTP servers take a JSON entry with the URL, the
transport, and headers. The exact key names vary between clients; the shape is
usually:

```json
{
  "mcpServers": {
    "accounting": {
      "type": "http",
      "url": "http://HOST:8765/http",
      "headers": {
        "Authorization": "Bearer YOUR_MCP_API_TOKEN"
      }
    }
  }
}
```

If a client only supports local (stdio) servers, bridge it with a
stdio-to-HTTP proxy that can forward a custom header. If it only supports
SSE, use `http://HOST:8765/sse` and run a single replica.

### curl

The streamable HTTP endpoint is stateless, so a single `tools/call` request
works without an `initialize` handshake. The client must accept both JSON and
event-stream responses:

```bash
curl -sS -X POST http://localhost:8765/http \
  -H "Authorization: Bearer $MCP_API_TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call",
       "params":{"name":"account_balances","arguments":{}}}'
```

With arguments:

```bash
curl -sS -X POST http://localhost:8765/http \
  -H "Authorization: Bearer $MCP_API_TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call",
       "params":{"name":"transactions",
                 "arguments":{"description":"grocery","date_from":"2030-01-01","limit":5}}}'
```

Use `"method":"tools/list"` to get the full tool schema from the server.

## Tool reference

All tools return a human-readable text string, not structured JSON. All dates
are `YYYY-MM-DD`. Monetary amounts are plain numbers.

The server's instructions tell clients to call the `list_*` tools first to
discover IDs before calling `update_*` or `delete_*` tools. Rows from
`list_accounts`, `list_recurring`, `list_holdings`, `list_debts`,
`list_assets`, `list_goals` and `transactions` start with the MongoDB
`ObjectId` followed by `|`.

There are 51 tools: 23 read, 26 write and 2 report tools.

### Read tools

Implemented in `agent/tools/*.py` (the same functions the advisor reports
use) plus the ID-listing helpers in `agent/tools/writes.py`.

| Tool | Arguments | What it does | Implementation |
|---|---|---|---|
| `account_balances` | none | Every account with type, balance, institution; marks `[PRIMARY]`. | `accounts.get_account_balances` |
| `account_summary` | none | Totals grouped as cash (checking and savings), investment (`INVESTMENT_ACCOUNT_TYPES`) and other. | `accounts.get_account_summary` |
| `list_accounts` | none | Every account **with its ID**. Call this first when you need an `account_id`. | `writes.list_accounts` |
| `transactions` | `description=""`, `date_from=""`, `date_to=""`, `transaction_type=""` (`expense`/`income`), `limit=20`, `account_id=""` | Search ledger transactions, newest first. Each row shows ID, date, signed amount, description and account. Covers every account unless `account_id` is given; transactions with no `account_id` count as the primary account's. Ends with a net total. | `transactions.search_transactions` |
| `cc_transactions` | `description=""`, `card=""`, `date_from=""`, `date_to=""`, `category=""`, `limit=20` | Search imported credit card transactions, newest first. `card` matches card name or issuer; `category` is an exact, case-insensitive match. Charges are positive, credits negative. Rows do not include IDs. | `transactions.search_cc_transactions` |
| `spending_by_category` | `month=""` (`YYYY-MM`, default current) | Credit card spending per category for one month. | `transactions.get_spending_by_category` |
| `budget_status` | `month=""` (`YYYY-MM`, default current) | Budget targets (default `budget_targets` overlaid with `monthly_budget_targets`) against actual credit card spending per category, with `OK`/`OVER`. | `budget.get_budget_status` |
| `recurring_items` | `item_type=""` (`expense`/`income`/`transfer`) | Recurring transactions (excluding archived paid-off loan lines) and recurring transfers, with an approximate monthly equivalent. No IDs. | `budget.get_recurring_items` |
| `list_recurring` | `item_type=""` (`expense`/`income`/`transfer`) | Recurring transactions and recurring transfers **with IDs**, for the update/delete tools. Includes archived lines. | `writes.list_recurring` |
| `cash_flow_outlook` | `account_id=""` | 12-month projection: per-month income, expenses, net and ending balance, plus the lowest projected balance. Defaults to the primary account's starting balance. | `cashflow.get_cash_flow_outlook` |
| `negative_balance_months` | `account_id=""` | Months whose projected ending balance is below zero (same projection as above). | `cashflow.get_negative_balance_months` |
| `portfolio_summary` | `account_id=""` | Holding count, total cost basis and YTD dividends, with a per-account breakdown when there is more than one account. Despite the docstring, it reports cost basis, not live market value. | `portfolio.get_portfolio_summary` |
| `holdings` | `account_id=""` | Holdings with symbol, quantity, cost basis and account name. No IDs. | `portfolio.get_holdings` |
| `list_holdings` | `account_id=""` | Holdings **with IDs** (symbol, quantity, account ID). | `writes.list_holdings_with_ids` |
| `dividend_history` | `symbol=""`, `limit=20` | Recent dividend payments, newest first, with a total. | `portfolio.get_dividend_history` |
| `ytd_dividends` | none | Year-to-date dividend totals per symbol. | `portfolio.get_ytd_dividends` |
| `current_net_worth` | none | Accounts, assets, asset loans and debts, with totals and net worth. | `networth.get_current_net_worth` |
| `net_worth_history` | `limit=12` | Recent `networth_snapshots`, oldest first, with the change over the period. | `networth.get_net_worth_history` |
| `debts` | none | Every debt with type, balance, APR, minimum payment, and deferment end date (marked subsidised where set). | `debts.get_debts` |
| `list_debts` | none | Debts **with IDs**. | `writes.list_debts` |
| `total_debt` | none | Sum of all debt balances, with a per-debt list. | `debts.get_total_debt` |
| `list_assets` | none | Assets **with IDs**, value and liability balance. | `writes.list_assets` |
| `list_goals` | none | Goals **with IDs**, current and target amounts. | `writes.list_goals` |

Notes on how the read tools differ from the web pages:

- **Debt and loan balances are the stored values.** In the web app, a debt's
  `balance` (and an asset's `liability_balance`) is the *starting* balance;
  the current balance is computed by walking linked ledger payments
  (`_decorate_debt`, `_decorate_asset_liability` in `app.py`). The MCP tools
  `debts`, `total_debt`, `list_debts`, `list_assets` and `current_net_worth`
  read the stored field directly, so they can differ from the Debts and Net
  Worth pages once payments are linked.
- **The cash flow tools use a simplified projection** (`_generate_outlook` in
  `agent/tools/cashflow.py`), not the ledger's `calculate_yearly_outlook()`.
  It counts recurring items per month by frequency (weekly as 4, monthly as
  the number of listed days), ignores actual transactions, skips, per-date
  overrides and loan payoff capping, and only applies recurring transfers when
  an `account_id` is given. Treat it as a rough outlook; the ledger page is
  authoritative. See [ledger-projections.md](ledger-projections.md).
- **Budget and category spending come from credit card transactions only**
  (`cc_transactions`), not the ledger. See [budget.md](budget.md) and
  [credit-cards.md](credit-cards.md).

### Write tools

Implemented in `agent/tools/writes.py`. Each returns a status string such as
`Added transaction <id>: ...`, `Updated.`, `Nothing to update.` or an
`Error: ...` message. Invalid IDs and bad dates are reported as errors rather
than raised.

For `update_*` tools, **an empty string or `0` means "leave unchanged"**, so
these tools cannot set a field to zero or clear it.

#### Transactions

| Tool | Arguments | What it does |
|---|---|---|
| `add_transaction` | `description`, `amount` (> 0), `date`, `transaction_type` (`income`/`expense`), `account_id=""` | Inserts one document into `transactions`. Validates the type, date, positive amount and that the account exists. With no `account_id` the document is stored with `account_id: null`, which the app treats as the primary account. |
| `update_transaction` | `transaction_id`, `description=""`, `amount=0`, `date=""`, `transaction_type=""`, `account_id=""` | `$set` on the given fields only. Validates type and account. |
| `delete_transaction` | `transaction_id` | Deletes the document permanently. |

#### Recurring items

| Tool | Arguments | What it does |
|---|---|---|
| `add_recurring` | `description`, `amount`, `frequency` (`weekly`/`monthly`/`bi-monthly`/`yearly`), `start_date`, `transaction_type` (`income`/`expense`), `category=""`, `day_of_month=""` (`"1"` or `"1,15"`), `account_id=""`, `auto_pay=false`, `payment_method=""`, `notes=""` | Inserts a budget item into `recurring_transactions` with `end_date: null`. |
| `update_recurring` | `recurring_id`, `description=""`, `amount=0`, `frequency=""`, `category=""`, `day_of_month=""`, `account_id=""` | `$set` on the given fields only. |
| `delete_recurring` | `recurring_id` | Deletes the budget item. |
| `override_recurring_amount` | `recurring_id`, `date`, `amount` | Upserts a `projected_overrides` document for one occurrence. |

#### Transfers

| Tool | Arguments | What it does |
|---|---|---|
| `add_transfer` | `from_account_id`, `to_account_id`, `amount`, `date`, `description=""` (default `Account Transfer`), `notes=""` | Inserts one document into `transfers`. Both accounts must exist. |
| `add_recurring_transfer` | `from_account_id`, `to_account_id`, `amount`, `frequency` (`weekly`/`monthly`/`bi-monthly`/`yearly`), `start_date`, `day_of_month=""`, `description=""` (default `Recurring Transfer`) | Inserts into `recurring_transfers`. |
| `delete_recurring_transfer` | `transfer_id` | Deletes a recurring transfer. |

#### Accounts

| Tool | Arguments | What it does |
|---|---|---|
| `add_account` | `name`, `account_type`, `institution`, `balance=0`, `is_primary=false`, `estimated_return_rate=0`, `management_type=""` (`managed`/`self_managed`) | Creates an account. `account_type` is validated against `ACCOUNT_TYPES` in `models.py` (`checking`, `savings`, `ira`, `roth_ira`, `brokerage`, `401k`, `hsa`, `deferred_comp`, `other`; the tool docstring lists only some of them). Stamps `balance_as_of_date` with now. `is_primary=true` demotes every other account first. |
| `update_account_balance` | `account_id`, `balance` | Sets `balance` and stamps `balance_as_of_date` with now. |
| `update_account` | `account_id`, `name=""`, `institution=""`, `estimated_return_rate=0`, `is_primary=false` | Updates metadata. `is_primary=true` demotes every other account. Passing `false` does not unset primary. |

#### Assets and debts

| Tool | Arguments | What it does |
|---|---|---|
| `add_asset` | `name`, `asset_type` (`real_estate`/`vehicle`/`other`), `estimated_value`, `liability_balance=0`, `interest_rate=0`, `monthly_payment=0`, `address=""`, `notes=""` | Inserts an asset with an optional attached loan. Fields such as `payment_match`, escrow and `payment_frequency` are not set; edit them in the web UI. |
| `update_asset` | `asset_id`, `estimated_value=0`, `liability_balance=0`, `monthly_payment=0` | Updates the given fields; `0` leaves a field unchanged. |
| `add_debt` | `name`, `debt_type` (`credit_card`/`personal_loan`/`student_loan`/`medical`/`other`), `balance`, `interest_rate=0`, `minimum_payment=0`, `creditor=""`, `notes=""` | Inserts a debt. Deferment, `subsidized` and `payment_match` are not settable here. |
| `update_debt_balance` | `debt_id`, `balance` | Overwrites the stored `balance`. |
| `make_debt_payment` | `debt_id`, `amount` | Subtracts `amount` from the stored `balance` (floored at 0). |

#### Budget targets and goals

| Tool | Arguments | What it does |
|---|---|---|
| `set_budget_target` | `category`, `amount`, `month=""` | With no month, upserts the default template in `budget_targets`; with `YYYY-MM`, upserts a per-month override in `monthly_budget_targets`. `amount=0` deletes the target or override. |
| `add_goal` | `name`, `target_amount`, `current_amount=0`, `target_date=""`, `category=""`, `notes=""` | Inserts a goal into `savings_goals`. No `goal_type` is stored, so the app treats it as a `manual` goal. |
| `update_goal_amount` | `goal_id`, `current_amount` | Sets `current_amount`. Only visible for manual goals; live goal types compute progress from data. |
| `delete_goal` | `goal_id` | Deletes the goal. |

#### Holdings and dividends

| Tool | Arguments | What it does |
|---|---|---|
| `add_holding` | `account_id`, `symbol`, `quantity`, `cost_basis=0`, `notes=""` | Inserts a holding (symbol upper-cased). The account must exist. |
| `update_holding` | `holding_id`, `quantity=0`, `cost_basis=0` | Updates quantity and/or cost basis. |
| `delete_holding` | `holding_id` | Deletes the holding. |
| `add_dividend` | `symbol`, `amount`, `date`, `holding_id=""` | Inserts a dividend with `source: 'manual'`. |

### Write tools and app side effects

The write tools insert and update documents directly. They **do not** run the
extra bookkeeping the web routes perform. In particular, **no MCP tool changes
an account balance as a side effect.** That is also true of the web app's own
transaction routes: the stored `balance` is a base figure and the ledger
computes current balances by adding transactions on top. The table lists
where the MCP tool and the equivalent web route differ.

| Tool | Equivalent web route | Mirrors the app? | Differences |
|---|---|---|---|
| `add_transaction` | `POST /transactions` | Partly | The web route stores the primary account's ID when none is given. MCP stores `null`, which the ledger and history treat as the primary account, but the dashboard's Month to Date card matches the primary account's ID exactly and skips it; pass `account_id` explicitly to avoid that. The web route also calls `_auto_link_transaction()` so a description matching a loan's `payment_match` is tagged as a loan payment. MCP does not, but the next visit to the dashboard, ledger, debts or goals page runs `_apply_auto_links()` and links unlinked expense transactions anyway. |
| `update_transaction` | `POST /transactions/edit/<id>` | Yes | The web form cannot change the account; MCP can. |
| `delete_transaction` | `DELETE /transactions/<id>` | No | The web route also records the description and date in `skipped_transactions` so the projected recurring occurrence the transaction realized does not reappear. MCP only deletes, so the projection for that date can come back. |
| `add_recurring` / `update_recurring` | `/budget/add`, `/budget/edit/<id>` | Mostly | MCP rejects `biweekly` (every 14 days), which the web forms offer, and does not verify that `account_id` exists. |
| `delete_recurring` | `POST /budget/delete/<id>` | Yes | |
| `override_recurring_amount` | `POST /projected/update-amount` | **No** | The web route stores the override's `date` as an ISO string, and `calculate_yearly_outlook()` looks overrides up by that string. MCP stores a datetime, so the override is saved but does not affect the ledger projection. |
| `add_transfer` | `POST /transfers/add` | **No** | The web route also inserts two ledger transactions (`Transfer to: ...` on the source, `Transfer from: ...` on the destination), which is what moves money between accounts in the ledger. MCP only records the `transfers` document, so the transfer appears in the history but does not change either account's ledger balance. It also does not reject same-account transfers. |
| `add_recurring_transfer` | `POST /transfers/recurring/add` | Mostly | MCP does not check that the accounts exist or differ, or that the amount is positive. |
| `delete_recurring_transfer` | `POST /transfers/recurring/delete/<id>` | Yes | |
| `add_account` / `update_account` / `update_account_balance` | `/accounts/add`, `/accounts/edit/<id>`, `/accounts/update-balance/<id>` | Yes | Primary demotion matches the web forms. |
| `add_asset` / `update_asset` | `/assets/add`, `/assets/edit/<id>` | Partly | Only a subset of fields. |
| `add_debt` | `POST /debts/add` | Partly | Only a subset of fields. |
| `update_debt_balance` / `make_debt_payment` | none | **Different model** | The app treats `balance` as the starting balance and derives the current balance from linked ledger payments. These tools edit the starting balance and create no ledger transaction, so if payments are also linked the payment is counted twice. To record a real payment the app's way, add an expense transaction whose description matches the debt's `payment_match`. |
| `set_budget_target` | `/budget/targets`, `/budget/targets/template` | Yes | |
| `add_goal` / `update_goal_amount` / `delete_goal` | `/goals/add`, `/goals/contribute/<id>`, `/goals/delete/<id>` | Partly | Manual goals only; `update_goal_amount` sets rather than increments. |
| `add_holding` / `update_holding` / `delete_holding` | `/portfolio/add`, `/portfolio/update/<id>`, `/portfolio/delete/<id>` | Yes | |
| `add_dividend` | `POST /portfolio/dividend/add` | Yes | |

### Report tools

| Tool | Arguments | What it does |
|---|---|---|
| `generate_weekly_advisor_report` | none | Calls `agent.reports.generate_weekly_report()` and returns `Weekly report generated. Insights: N chars.` |
| `generate_monthly_advisor_report` | none | Calls `agent.reports.generate_monthly_report()` and returns `Monthly report generated. Insights: N chars.` |

Both run **synchronously** in the MCP request and block until the LLM
finishes, which can take many minutes on a CPU-only model. Make sure your
client's request timeout is long enough. `Insights: 0 chars` means the LLM
call failed (the report is still stored, with no insights); check the MCP
server logs. There is no MCP tool for the portfolio report, and MCP-triggered
runs do not show up in the web page's in-progress banner. The finished report
appears on `/reports`. See [advisor-agent.md](advisor-agent.md).

## Related docs

- [architecture.md](architecture.md): how the processes and collections fit together
- [advisor-agent.md](advisor-agent.md): the report generator behind the report tools
- [deployment.md](deployment.md): running the MCP server in Docker or Kubernetes
- [ledger-projections.md](ledger-projections.md): the authoritative projection logic
