# Architecture

This document describes how Accounting Cloud is put together: which processes run, what each module does, how a request flows through the app, and every MongoDB collection the code reads or writes. It is aimed at self-hosters who want to understand what they are running and at contributors who are about to change it.

Related documents: [budget.md](budget.md), [ledger-projections.md](ledger-projections.md), [portfolio.md](portfolio.md), [benefits.md](benefits.md), [credit-cards.md](credit-cards.md), [home-move.md](home-move.md), [features.md](features.md), [mcp-server.md](mcp-server.md), [advisor-agent.md](advisor-agent.md), [deployment.md](deployment.md), [development.md](development.md).

## Process layout

```
                 +-----------------------------------------------+
  browser  --->  | gunicorn (1 worker, 8 threads)                |
  (via proxy)    |   app:app  (Flask)                            |
                 |     + daemon thread: stock refresh @ 17:00    |
                 |     + daemon thread: advisor report scheduler |
                 |       (only when ADVISOR_ENABLED=true)        |
                 |     + short-lived daemon threads: on-demand   |
                 |       report jobs, LLM categorization         |
                 +-----------------------+-----------------------+
                                         |
                                         v
                                  +-------------+
                                  |  MongoDB    |
                                  +-------------+
                                         ^
                                         |
                 +-----------------------+-----------------------+
  MCP client --> | python mcp_server.py   (optional, separate)   |
  (Bearer token) |   FastMCP over streamable HTTP / SSE          |
                 |   uses agent/ tools on the same database      |
                 +-----------------------------------------------+
```

### Web process

The `Dockerfile` runs:

```
gunicorn --bind 0.0.0.0:5000 --workers 1 --threads 8 --timeout 120 --access-logfile - app:app
```

There is exactly **one worker on purpose**. Several pieces of state live in process memory and would be duplicated or inconsistent across workers:

| In-process state | Where | Purpose |
|---|---|---|
| `STOCK_CACHE` | `app.py` | 5-minute price/dividend cache (`STOCK_CACHE_TTL = 300`) |
| `_REPORT_JOBS` + `_REPORT_JOBS_LOCK` | `app.py` | Which advisor reports are currently generating (shown on `/reports`, polled by `/api/reports/status`) |
| `_CC_LLM_RUNNING` + `_CC_LLM_LOCK` | `app.py` | Guarantees only one LLM categorization pass runs at a time |
| Background scheduler threads | `app.py`, `agent/scheduler.py` | Started once per process at import time |

Concurrency comes from gunicorn threads. If you scale to more workers or replicas, each one starts its own scheduler threads and keeps its own caches.

### Background threads started at import

Importing `app.py` (which is what gunicorn does) starts these daemon threads:

1. **Advisor report scheduler** — `agent_scheduler.schedule_reports()` is called right after `agent_db.init(db)` near the top of `app.py`. It returns immediately unless `ADVISOR_ENABLED=true`. When enabled, it first runs `_check_and_generate()` (generates any missing current-period weekly, monthly and portfolio reports), then sleeps until the earlier of next Monday 06:00 or the 1st of next month 06:00 and checks again. See [advisor-agent.md](advisor-agent.md).
2. **Stock refresh** — `schedule_stock_refresh()` is called at the very bottom of `app.py`. The loop sleeps until the next 17:00 local time, resets `STOCK_CACHE['timestamp']`, and calls `get_live_data()` for every distinct symbol in `holdings`. See [portfolio.md](portfolio.md).

Short-lived daemon threads are also spawned on demand:

- `_run_report_async(report_type)` — from `POST /api/reports/generate`. One job per report type at a time; an error stays visible for 60 seconds.
- `_categorize_leftovers_async()` — after a credit-card upload and on every `/credit-cards` page load, when `CC_LLM_ENABLED` is true. See [credit-cards.md](credit-cards.md).

### Optional MCP server process

`mcp_server.py` is a separate, standalone process (the `mcp` profile in `docker-compose.yml`, command `python mcp_server.py`). It does **not** import `app.py`. It opens its own `MongoClient` with the same `MONGO_URI` / `MONGO_DB_NAME`, calls `agent_db.init(db)`, and registers read tools from `agent/tools/*` and write tools from `agent/tools/writes.py`. Every request must carry `Authorization: Bearer <MCP_API_TOKEN>` (`BearerAuthASGI`); with no token configured it refuses everything. See [mcp-server.md](mcp-server.md).

Because the MCP server does not import `app.py`, it does not start the stock refresh or report scheduler threads, and its tools reimplement some calculations rather than calling the web app's functions (for example, `agent/tools/cashflow.py` has its own simplified outlook rather than `calculate_yearly_outlook()`).

### The `agent/` package

Shared by both processes:

| Module | Used by | Role |
|---|---|---|
| `agent/db.py` | both | `_DBProxy` so modules can `from agent.db import db` before the real database exists; `init(app_db)` binds it |
| `agent/reports.py` | both | `generate_weekly_report()`, `generate_monthly_report()`, `generate_portfolio_report()`; calls an Ollama-compatible endpoint (`AGENT_OLLAMA_*`) and upserts into `reports` |
| `agent/scheduler.py` | web | `schedule_reports()` background loop |
| `agent/prompts.py` | reports | Prompt templates; optional household profile from `ADVISOR_PROFILE_FILE` |
| `agent/tools/*.py` | MCP server, reports | Read tools (accounts, transactions, budget, cashflow, portfolio, networth, debts) and write tools (`writes.py`) |

## Module map

| Path | Role |
|---|---|
| `app.py` | The Flask app: configuration, middleware, every route, and nearly all business logic (ledger outlook, budget, debts and loan amortization, goals, net worth, credit cards, payroll, benefits routes, export/import). About 8,000 lines. |
| `models.py` | Plain data classes (`Transaction`, `RecurringTransaction`, `ProjectedOverride`, `Account`, `Asset`, `SavingsGoal`, `Transfer`, `RecurringTransfer`, `Debt`, `PaymentPlan`, `NetWorthSnapshot`, `Holding`, `HouseholdEmployee`, `PayrollEntry`, `Dividend`) and shared constants (`CATEGORIES`, `INCOME_CATEGORIES`, `ACCOUNT_TYPES`, `INVESTMENT_ACCOUNT_TYPES`, `CASH_ACCOUNT_TYPES`, `DEBT_TYPES`, `ASSET_TYPES`, `LIABILITY_TYPES`, `GOAL_TYPES`, `CARD_ISSUERS`, `CC_CATEGORIES`, merchant categorization rules, `PAYROLL_DEFAULTS`, `PAY_FREQUENCIES`). Routes insert `Model(...).__dict__`, so the stored document has exactly the constructor's attributes; `to_dict()` is only used for JSON responses. |
| `benefits.py` | Pure functions for tax-year contribution room (IRS limit seeds in `CONTRIBUTION_LIMITS`), employer match, credit-card statement-credit periods, perk templates and alerts. No database access; `app.py` loads the documents and passes them in. See [benefits.md](benefits.md). |
| `home_move.py` | Pure "sell one home, buy another" calculator (`parse_params`, `calculate_home_move`, `solve_price_for_payment`) plus a Freddie Mac PMMS rate fetch via FRED. See [home-move.md](home-move.md). |
| `payroll_forms.py` | Fills IRS fillable W-2 and Schedule H PDFs and builds EFW2 files for household-employer payroll. PDF templates are resolved through `set_template_resolver()` (the app supplies `_template_from_db`, reading `form_templates`) with a fallback to the `forms/` directory. |
| `payroll_validator.py` | Local EFW2 validator (`validate_efw2`, `summarize`) based on SSA Pub 42-007. |
| `mcp_server.py` | Standalone MCP server (see above). |
| `agent/` | Advisor reports, scheduler, and the tool functions used by the MCP server (see above). |
| `templates/` | Jinja2 templates, one per page plus `base.html` (navigation, CSRF meta tag). |
| `static/main.js` | Client-side behavior: month expand/collapse and auto-expanding months with a negative low point, forecast-card click-to-scroll, cash-flow warning toggle/dismiss, nav toggle, and a `fetch` wrapper that adds the `X-CSRFToken` header. |
| `static/style.css` | Stylesheet. |
| `forms/` | Field maps for the IRS PDFs (`w2_fieldmap.json`, `schedule_h_fieldmap.json`); fillable PDFs can be dropped here or uploaded through the app. |
| `scripts/seed_demo.py` | Seeds a demo dataset. `scripts/backup-accounting.sh` is a backup helper. |
| `tests/` | pytest suite for the pure modules and prompts. |

## Request flow

1. **Reverse proxy** (recommended; the app has no login of its own — see [deployment.md](deployment.md)) forwards the request, optionally with an `X-Script-Name` header.
2. **`ReverseProxyMiddleware`** (WSGI) fixes up `SCRIPT_NAME` / `PATH_INFO` for sub-path hosting (below).
3. **Flask-WTF `CSRFProtect`** validates the token on every state-changing request.
4. **Route function** in `app.py` reads/writes MongoDB through the module-level `db` handle. Many page routes first run idempotent maintenance passes; for example `/`, `/ledger`, `/budget`, `/debts` and `/goals` call `_apply_auto_links()`, which links loan payments and archives or restores paid-off loan budget lines (see [ledger-projections.md](ledger-projections.md#archiving-paid-off-loan-lines)), and `/credit-cards` runs the guarded one-time migrations.
5. **Template rendering** with two context processors injected into every template: `inject_account_type_sets()` (`investment_account_types`, `cash_account_types`) and `inject_escrow_schedule()` (`escrow_tax_months_label`, `escrow_insurance_month_label`). The `usd` filter formats numbers as `1,234.56`.
6. **`set_security_headers()`** (`after_request`) adds the security headers.

Most form routes answer with a redirect. Several also return JSON when the request sends `Accept: application/json` (for example `/transactions/skip`, `/projected/update-amount`, `/budget/delete/<id>`, `/accounts/update-balance/<id>`).

### ReverseProxyMiddleware, APPLICATION_ROOT and X-Script-Name

The app can be served under a sub-path such as `/accounting`:

- `APPLICATION_ROOT` (environment variable, default `/`) sets the configured base path.
- `ReverseProxyMiddleware.__call__` prefers the `X-Script-Name` request header (`HTTP_X_SCRIPT_NAME`); if it is absent it falls back to `APPLICATION_ROOT`.
- When the resulting prefix is not `/`, it sets `SCRIPT_NAME` to the prefix and strips the prefix from the front of `PATH_INFO` if present. `url_for()` then generates prefixed links automatically.

So a proxy can either strip the prefix and send `X-Script-Name: /accounting`, or pass the full path through with `APPLICATION_ROOT=/accounting` set on the app.

### CSRF

- `CSRFProtect(app)` is enabled globally; there are no exempt routes.
- `SECRET_KEY` comes from the environment. If it is unset, a random key is generated at startup, which invalidates every outstanding token on restart and breaks forms across replicas. Always set it.
- `WTF_CSRF_TIME_LIMIT = 3600`: tokens expire after one hour.
- HTML forms include a hidden `csrf_token` field. `base.html` exposes the token in `<meta name="csrf-token">`, and `static/main.js` sends it as `X-CSRFToken` on `fetch` calls (JSON endpoints such as `/transactions/link-loan`, `/credit-cards/recategorize`, `/dashboard/layout`).
- A failed check is handled by `handle_csrf_error()`, which returns `400` with `{"error": "CSRF token missing or invalid"}`.

### Security headers

`set_security_headers()` sets on every response:

| Header | Value |
|---|---|
| `X-Content-Type-Options` | `nosniff` |
| `X-Frame-Options` | `SAMEORIGIN` |
| `X-XSS-Protection` | `1; mode=block` |
| `Referrer-Policy` | `strict-origin-when-cross-origin` |
| `Content-Security-Policy` | `default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'self'` |

`MAX_CONTENT_LENGTH` is 16 MB, which caps every upload (statement CSVs, holdings CSVs, PDF templates, and `/import` backups).

### `/health`

`GET /health` runs `db.command('ping')`:

- Success: `200` with `{"status": "healthy", "timestamp": "<UTC ISO>", "database": "connected"}`.
- Failure: `503` with `{"status": "unhealthy", "error": "Database connection failed", "database": "disconnected"}`.

It is suitable for Kubernetes liveness/readiness probes; `docker-compose.yml` uses it as the container healthcheck.

### `/export` and `/import` (JSON backup)

- **`GET /export`** (`export_data`) enumerates `db.list_collection_names()` and dumps every document of every collection, so new collections are included automatically. Output is MongoDB Extended JSON produced by `bson.json_util.dumps` (ObjectIds as `{"$oid": ...}`, datetimes as `{"$date": ...}`), wrapped with `"_meta": {"version": 3, "exported_at": ...}`.
- **`POST /import`** (`import_data`) takes a multipart `file`, parses it with `bson.json_util.loads` so ObjectIds and datetimes are restored exactly, drops `_meta`, and then for **every collection present in the file** drops the collection and inserts the file's documents. Collections not present in the file are left alone. It redirects to `/ledger`.

Things to know:

- Import is destructive per collection, and dropping a collection also drops its indexes. The only index the app creates, the unique `(month, category)` index on `monthly_budget_targets`, is re-created by `create_index` the next time `app.py` is imported (that is, on restart).
- The backup contains everything, including household-employee SSNs and addresses (`household_employees`), employer EIN (`payroll_settings`) and uploaded PDF templates (`form_templates`, stored as binary). Treat export files as sensitive.
- Backups larger than 16 MB are rejected by `MAX_CONTENT_LENGTH`.

There are also per-collection CSV exports and imports (`/export/csv/transactions`, `/export/csv/recurring`, `/export/csv/holdings`, `/export/csv/dividends`, `/export/csv/debts`, `/export/csv/assets`, and `/import/csv/transactions`, `/import/csv/recurring`, `/import/csv/debts`, `/import/csv/assets`). The CSV imports accept `clear=true` (form field or query argument) to drop the collection first; they carry fewer fields than the JSON backup (for example, imported transactions have no `account_id`, and imported recurring items have no `day_of_month` or `category`).

## Route overview

| Area | Routes | Doc |
|---|---|---|
| Dashboard | `/`, `POST /dashboard/layout` | [features.md](features.md) |
| Ledger and projections | `/ledger`, `/history`, `/simulation`, `/transactions` (GET/POST), `/transactions/<id>` (DELETE), `/transactions/edit/<id>`, `/transactions/verify`, `/transactions/skip`, `/transactions/link-loan`, `/projected/update-amount` | [ledger-projections.md](ledger-projections.md) |
| Budget | `/budget`, `/budget/add`, `/budget/edit/<id>`, `/budget/delete/<id>`, `/budget/targets`, `/budget/targets/template`, `/budget/targets/reset`, `/budget/targets/seed`, `/recurring_transactions` | [budget.md](budget.md) |
| Accounts | `/accounts`, `/accounts/add`, `/accounts/edit/<id>`, `/accounts/delete/<id>`, `/accounts/update-balance/<id>`, `/accounts/<id>`, `/api/accounts` | [features.md](features.md) |
| Transfers | `/transfers`, `/transfers/add`, `/transfers/delete/<id>`, `/transfers/recurring/add`, `/transfers/recurring/edit/<id>`, `/transfers/recurring/delete/<id>` | [features.md](features.md) |
| Portfolio | `/portfolio`, `/portfolio/add`, `/portfolio/update/<id>`, `/portfolio/delete/<id>`, `/portfolio/delete-all`, `/portfolio/import-csv`, `/portfolio/dividend/add`, `/portfolio/dividend/delete/<id>`, `/api/stock/<symbol>` | [portfolio.md](portfolio.md) |
| Assets, debts, loans | `/assets...`, `/assets/<id>/amortization`, `/debts...`, `/debts/asset/<id>...`, `/debts/payment-plans...`, `/debts/plan`, `/debts/plan/log-payment`, `/debts/calculate`, `/debts/auto-link`, `/debts/<id>/payoff` | [features.md](features.md) |
| Goals, net worth, analytics, calendar | `/goals...`, `/networth`, `/networth/snapshot`, `/api/networth/history`, `/analytics`, `/api/analytics/*`, `/calendar`, `/api/calendar/events` | [features.md](features.md) |
| Credit cards | `/credit-cards`, `/credit-cards/upload`, `/credit-cards/cards/*`, `/credit-cards/delete-statement`, `/credit-cards/recategorize`, `/credit-cards/link-plan`, `/credit-cards/statements/raw/<file>`, `/api/cc/*` | [credit-cards.md](credit-cards.md) |
| Benefits and perks | `/benefits`, `/benefits/settings`, `/benefits/account-config`, `/benefits/contribution`, `/benefits/card`, `/benefits/perk`, `/benefits/perk-template`, `/benefits/card-benefit`, `/benefits/perk-usage` | [benefits.md](benefits.md) |
| Home move | `/tools/home-move`, `/tools/home-move/save`, `/tools/home-move/delete/<id>` | [home-move.md](home-move.md) |
| Payroll | `/payroll`, `/payroll/settings`, `/payroll/employees/...`, `/payroll/run`, `/payroll/preview`, `/payroll/entries/<id>/delete`, `/payroll/w2/...`, `/payroll/schedule-h/...`, `/payroll/validate/<year>`, `/payroll/efw2/<year>`, `/payroll/year-end/<year>/zip`, `/payroll/forms/...` | [features.md](features.md) |
| Advisor reports | `/reports`, `/reports/<id>`, `POST /api/reports/generate`, `/api/reports/status` | [advisor-agent.md](advisor-agent.md) |
| Admin and backup | `/admin`, `/export`, `/import`, `/export/csv/*`, `/import/csv/*`, `/health` | this document |

## Data model

### Conventions

**Dates are stored as `datetime.datetime`, never `datetime.date`.** BSON has no date-only type, and PyMongo refuses to encode `datetime.date`. Every form date is parsed with `datetime.fromisoformat(...)` (or `_parse_form_date()`) and stored as a datetime at midnight; code must not call `.date()` before storing. When reading, the code tolerates both: it uses `isinstance(value, datetime)` checks, `_as_date()`, or `datetime.fromisoformat()` for values that are ISO strings. A few fields are deliberately strings (noted below), such as `skipped_transactions.date`, `projected_overrides.date`, `cc_transactions.statement_date` and `monthly_budget_targets.month`.

**Older documents lack newer fields.** MongoDB does not backfill. Jinja dot-access on a dict raises `UndefinedError` for a missing key, so routes call `dict.setdefault('field', None)` before rendering (for example `account_detail` sets `estimated_return_rate`, `balance_as_of_date`, `management_type`; `edit_asset` sets the escrow fields and `payment_match`; `budget_page` sets `archived_at`, `archived_reason`, `account_id`, `is_transfer`). Any new field you add needs the same treatment wherever it reaches a template.

**IDs.** `_id` is an `ObjectId` unless noted. Reference fields (`account_id`, `debt_id`, `card_id`, ...) are normally stored as `ObjectId`; a few are stored as strings, noted per field. Comparisons in Python usually go through `str(...)` on both sides.

**Types in the tables.** `str`, `float`, `int`, `bool`, `datetime`, `ObjectId`, `list`, `dict`. "or null" means the field is written as `None` when not provided.

### Collection index

| Collection | Purpose |
|---|---|
| [`accounts`](#accounts) | Bank, savings and investment accounts |
| [`transactions`](#transactions) | Actual ledger transactions |
| [`recurring_transactions`](#recurring_transactions) | Budget items: recurring bills and income |
| [`recurring_transfers`](#recurring_transfers) | Recurring account-to-account transfers |
| [`transfers`](#transfers) | One-time transfers (history) |
| [`skipped_transactions`](#skipped_transactions) | Projected occurrences the user skipped |
| [`projected_overrides`](#projected_overrides) | Per-occurrence amount overrides for projections |
| [`budget_targets`](#budget_targets) | Default monthly target per category |
| [`monthly_budget_targets`](#monthly_budget_targets) | Per-month target overrides |
| [`debts`](#debts) | Standalone debts |
| [`debt_plan`](#debt_plan) | Singleton payoff-plan settings |
| [`payment_plans`](#payment_plans) | No-interest installment plans billed to a credit card |
| [`assets`](#assets) | Real estate, vehicles and other assets, with an optional attached loan |
| [`holdings`](#holdings) | Stock/ETF positions |
| [`dividends`](#dividends) | Dividend payments received |
| [`savings_goals`](#savings_goals) | Goals of several types |
| [`networth_snapshots`](#networth_snapshots) | Point-in-time net worth |
| [`cc_cards`](#cc_cards) | Credit cards, their fees, credits and benefits |
| [`cc_transactions`](#cc_transactions) | Imported credit-card statement lines |
| [`cc_perk_usage`](#cc_perk_usage) | Statement credit redeemed per card, perk and period |
| [`benefit_settings`](#benefit_settings) | Singleton tax-year benefit settings |
| [`benefit_accounts`](#benefit_accounts) | Per-account contribution-limit configuration |
| [`contribution_overrides`](#contribution_overrides) | Paystub year-to-date contribution figures |
| [`reports`](#reports) | Advisor reports |
| [`home_move_scenarios`](#home_move_scenarios) | Saved home-move planner scenarios |
| [`household_employees`](#household_employees) | Household employees (payroll) |
| [`payroll_entries`](#payroll_entries) | Payroll runs |
| [`payroll_settings`](#payroll_settings) | Singleton payroll tax settings |
| [`form_templates`](#form_templates) | Uploaded IRS fillable PDF templates |
| [`dashboard_prefs`](#dashboard_prefs) | Singleton dashboard card layout |
| [`app_settings`](#app_settings) | Singleton first-run setup state and household name |
| [`app_migrations`](#app_migrations) | Markers for one-time data migrations |

### accounts

Written by `add_account`, `edit_account`, `update_account_balance`, `derive_account_return_rate`, and MCP `add_account` / `update_account` / `update_account_balance`. Model: `Account`.

| Field | Type | Meaning |
|---|---|---|
| `name` | str | Display name |
| `account_type` | str | One of `ACCOUNT_TYPES`: `checking`, `savings`, `ira`, `roth_ira`, `brokerage`, `401k`, `hsa`, `deferred_comp`, `other`. Grouping uses `CASH_ACCOUNT_TYPES` (`checking`, `savings`) and `INVESTMENT_ACCOUNT_TYPES` (`ira`, `roth_ira`, `brokerage`, `401k`, `hsa`, `deferred_comp`) |
| `institution` | str | Bank or broker name |
| `balance` | float | Stored balance. The ledger treats it as the base to which every recorded transaction for the account is applied (see [ledger-projections.md](ledger-projections.md#starting-balance)) |
| `account_number` | str or null | Free text (for example last four digits) |
| `notes` | str or null | |
| `is_primary` | bool | Exactly one account should be primary; saving one as primary clears the flag on all others. Transactions and recurring items with no `account_id` belong to the primary account |
| `estimated_return_rate` | float or null | Annual percent (3.0 means 3%). Auto-derived for self-managed investment accounts by `derive_account_return_rate()` |
| `balance_as_of_date` | datetime or null | When the balance was last confirmed; stamped by `/accounts/update-balance/<id>` |
| `management_type` | str or null | `managed` or `self_managed`. Only self-managed accounts have holdings |

### transactions

Actual (realized) ledger entries. Written by `handle_transactions` (JSON POST), `verify_transaction`, `add_transfer` (two rows per transfer), `edit_transaction`, `import_transactions_csv`, `log_debt_payment`, `payroll_run`, the loan link routes, `_auto_link_transaction` / `_apply_auto_links`, and MCP `add_transaction` / `update_transaction`. Model: `Transaction`.

| Field | Type | Meaning |
|---|---|---|
| `description` | str | Free text. Also the key for matching against projections, skips and loan `payment_match` rules |
| `amount` | float | Always a positive magnitude |
| `date` | datetime | Transaction date (string ISO dates in very old records are tolerated on read) |
| `transaction_type` | str | `income` or `expense` |
| `account_id` | ObjectId or null | Owning account; null means the primary account. Rows from `/import/csv/transactions` have no `account_id` key at all |
| `debt_id` | ObjectId | Present only when the row is a payment toward a standalone debt. Set by auto-linking, `/debts/<id>/link-transaction`, `/transactions/link-loan`, or `log_debt_payment`; removed (`$unset`) when unlinked or when the debt is deleted |
| `asset_liability_id` | ObjectId | Present only when the row is a payment toward an asset's loan. Mutually exclusive with `debt_id` |
| `category` | str | Optional. Read by `/api/analytics/spending-by-category` (defaulting to `Other`); nothing in the web app writes it |

Transfers create a pair of rows with descriptions `Transfer to: <description>` (expense, source account) and `Transfer from: <description>` (income, destination account). Payroll runs create one expense row described `Payroll: <name> (<start> – <end>)` for the net pay.

### recurring_transactions

Budget items. Written by `add_budget_item`, `edit_budget_item`, `handle_recurring_transactions` (JSON POST), `import_recurring_csv`, `save_debt_plan` (the auto-managed `Extra Debt Payment` line), `_sync_payoff_archive`, and MCP `add_recurring` / `update_recurring`. Model: `RecurringTransaction`. See [budget.md](budget.md).

| Field | Type | Meaning |
|---|---|---|
| `description` | str | Name, matched against actual transactions for dedup and against loan `payment_match` patterns |
| `amount` | float | Positive amount per occurrence |
| `frequency` | str | `weekly`, `biweekly`, `monthly`, `bi-monthly` (every other month), `yearly` |
| `start_date` | datetime | First possible occurrence; also the anchor for weekly weekday, biweekly cadence, yearly month and bi-monthly parity |
| `end_date` | datetime or null | Last month the item can occur |
| `transaction_type` | str | `income` or `expense` |
| `day_of_month` | str or null | Comma-separated days such as `"1"` or `"1,15"`; clamped to the month's last day; ignored for weekly and biweekly |
| `category` | str or null | Usually one of `CATEGORIES` or `INCOME_CATEGORIES` |
| `notes` | str or null | |
| `account_number` | str or null | Free text |
| `auto_pay` | bool | Display only (budget, calendar) |
| `payment_method` | str or null | Free text |
| `account_id` | ObjectId or null | Account the projection posts to; null means primary |
| `archived` | bool | Present and `true` only when archived. Queries use `ACTIVE_RECURRING = {'archived': {'$ne': True}}` |
| `archived_reason` | str | `paid_off` (the only reason the code sets) |
| `archived_loan_ref` | str | `debt:<id>` or `asset:<id>` of the loan that was paid off |
| `archived_at` | datetime | The paid-off date (the payment that brought the balance to zero), or today if unknown |

The four `archived*` fields are added and removed together by `_sync_payoff_archive()`; they are not part of the model.

### recurring_transfers

Written by `add_recurring_transfer`, `edit_recurring_transfer`, MCP `add_recurring_transfer`. Model: `RecurringTransfer`.

| Field | Type | Meaning |
|---|---|---|
| `from_account_id` | ObjectId | Source account |
| `to_account_id` | ObjectId | Destination account |
| `amount` | float | Positive amount per occurrence |
| `frequency` | str | Same values as recurring transactions |
| `start_date` | datetime | Anchor, as for recurring transactions |
| `day_of_month` | str or null | `"6"` or `"6,21"` |
| `end_date` | datetime or null | |
| `description` | str | Default `Recurring Transfer`. Projections are described `Transfer to: <description>` / `Transfer from: <description>` |
| `notes` | str or null | |

Recurring transfers are never archived.

### transfers

One-time transfer history. Written by `add_transfer`, `verify_transaction` (when confirming a projected transfer), MCP `add_transfer`. Model: `Transfer`.

| Field | Type | Meaning |
|---|---|---|
| `from_account_id` | ObjectId | Source account |
| `to_account_id` | ObjectId | Destination account |
| `amount` | float | |
| `date` | datetime | |
| `description` | str | Default `Account Transfer` |
| `notes` | str or null | |

The web routes also insert the two matching `transactions` rows; the MCP `add_transfer` tool inserts only the `transfers` document. `benefits.py` reads transfers into retirement accounts as contributions.

### skipped_transactions

Written by `skip_transaction` and `delete_transaction` (deleting an actual transaction records a skip so its projection does not reappear).

| Field | Type | Meaning |
|---|---|---|
| `description` | str | Projection description (for transfers, both `Transfer to:` and `Transfer from:` variants are recorded) |
| `date` | str | `YYYY-MM-DD` string |

### projected_overrides

Written by `update_projected_amount` (web) and MCP `override_recurring_amount`. Model: `ProjectedOverride`.

| Field | Type | Meaning |
|---|---|---|
| `recurring_id` | ObjectId | The recurring transaction or recurring transfer |
| `date` | str | `YYYY-MM-DD` of the occurrence. The outlook looks overrides up by this string; the MCP tool writes a datetime here instead |
| `amount` | float | Replacement amount for that one occurrence |

### budget_targets

Default template. Written by `set_default_targets`, `seed_targets_from_recurring`, MCP `set_budget_target` (empty month).

| Field | Type | Meaning |
|---|---|---|
| `category` | str | Category name |
| `target` | float | Monthly target |

### monthly_budget_targets

Per-month overrides. Written by `set_budget_targets`, `reset_month_targets`, MCP `set_budget_target` (with month). Has a **unique index on `(month, category)`**, created at import time in `app.py`.

| Field | Type | Meaning |
|---|---|---|
| `month` | str | `YYYY-MM` |
| `category` | str | |
| `target` | float | Overrides the template for that month |

### debts

Standalone debts (credit cards, personal and student loans, medical). Written by `add_debt`, `edit_debt`, `import_debts_csv`, `_seed_default_matches`, MCP `add_debt` / `update_debt_balance` / `make_debt_payment`. Model: `Debt`.

| Field | Type | Meaning |
|---|---|---|
| `name` | str | |
| `debt_type` | str | One of `DEBT_TYPES`: `credit_card`, `personal_loan`, `student_loan`, `medical`, `other` |
| `balance` | float | **Starting** balance. The current balance is computed by `_decorate_debt()` by amortizing linked payments against it |
| `interest_rate` | float | APR percent |
| `minimum_payment` | float | |
| `creditor` | str or null | |
| `account_number` | str or null | |
| `notes` | str or null | |
| `deferment_end_date` | datetime or null | Student-loan deferment end; deferred debts are skipped by the payoff plan |
| `subsidized` | bool | No interest accrues during deferment (payoff schedule) |
| `payment_match` | list of str | Ledger descriptions that auto-link as payments (case-insensitive exact match after trimming) |

### debt_plan

Singleton, written by `save_debt_plan`.

| Field | Type | Meaning |
|---|---|---|
| `strategy` | str | `avalanche` (highest rate first) or `snowball` (lowest balance first) |
| `auto_extra` | bool | Use the projected monthly surplus as the extra payment |
| `extra_payment` | float | Manual extra payment when `auto_extra` is false |
| `cash_account_id` | str or null | Account (as a string id) the extra payment comes from |
| `recurring_item_id` | ObjectId or null | The auto-managed `Extra Debt Payment` recurring transaction, if any |
| `updated` | datetime | |

### payment_plans

Written by `add_payment_plan`, `edit_payment_plan`. Model: `PaymentPlan`. Progress comes from `cc_transactions` rows whose `payment_plan_id` points here.

| Field | Type | Meaning |
|---|---|---|
| `vendor` | str | |
| `original_amount` | float | |
| `monthly_payment` | float | |
| `start_date` | datetime or null | |
| `card_name` | str or null | Card the plan bills to |
| `category` | str or null | |
| `notes` | str or null | |
| `status` | str | `active` or `closed` |

### assets

Written by `add_asset`, `edit_asset`, `import_assets_csv`, `_seed_default_matches`, MCP `add_asset` / `update_asset`. Model: `Asset`.

| Field | Type | Meaning |
|---|---|---|
| `name` | str | |
| `asset_type` | str | `real_estate`, `vehicle`, `other` |
| `estimated_value` | float | |
| `purchase_date` | datetime or null | |
| `purchase_price` | float or null | |
| `liability_balance` | float or null | Starting balance of the attached loan; assets with `liability_balance > 0` are treated as loans |
| `liability_type` | str or null | `mortgage`, `auto_loan`, `heloc`, `other` |
| `address` | str or null | |
| `notes` | str or null | |
| `interest_rate` | float or null | APR percent |
| `loan_term_months` | int or null | Used to estimate the original balance on the amortization page |
| `monthly_payment` | float or null | Principal and interest per month (halved per period for biweekly) |
| `payment_start_date` | datetime or null | |
| `payment_frequency` | str | `monthly` or `biweekly` |
| `property_tax_annual` | float or null | Escrow component |
| `homeowners_insurance_annual` | float or null | Escrow component |
| `pmi_monthly` | float or null | Included in `_calculate_monthly_escrow()` |
| `hoa_monthly` | float or null | Included in `_calculate_monthly_escrow()` |
| `escrow_balance` | float or null | Starting balance for the escrow simulation |
| `payment_match` | list of str | Ledger descriptions that auto-link as loan payments |

Older asset documents may lack the escrow fields, `payment_frequency`, `payment_start_date` and `payment_match`; CSV and MCP inserts also omit some of them.

### holdings

Written by `add_holding`, `update_holding`, `import_holdings_csv`, MCP holding tools. Model: `Holding`. See [portfolio.md](portfolio.md).

| Field | Type | Meaning |
|---|---|---|
| `account_id` | ObjectId or null | Investment account |
| `symbol` | str | Uppercase ticker (validated `^[A-Z0-9]{1,10}$`) |
| `quantity` | float | Shares |
| `cost_basis` | float or null | |
| `date_added` | datetime | UTC |
| `notes` | str or null | |

### dividends

Written by `portfolio_page` (auto-inserted from Yahoo dividend history), `add_dividend`, MCP `add_dividend`.

| Field | Type | Meaning |
|---|---|---|
| `symbol` | str | |
| `amount` | float | Total received (per-share amount times quantity held) |
| `date` | datetime | |
| `source` | str | `api` or `manual` |
| `holding_id` | ObjectId or null | Set by the model and the MCP tool when given; the web routes do not write it |

### savings_goals

Written by `add_goal`, `edit_goal`, `contribute_to_goal`, MCP goal tools. Model: `SavingsGoal`.

| Field | Type | Meaning |
|---|---|---|
| `name` | str | |
| `goal_type` | str | `manual`, `account`, `emergency`, `retirement`, `networth`, `debt_free`, `debt_payoff`. Missing on older or MCP-created goals; treated as `manual` |
| `target_amount` | float | Ignored for `emergency` (computed from months of expenses) and debt goals (computed from the baseline) |
| `current_amount` | float | Only meaningful for `manual`; live types compute progress from data |
| `target_date` | datetime or null | |
| `category` | str or null | One of `GOAL_CATEGORIES` keys |
| `notes` | str or null | |
| `created_date` | datetime | |
| `linked_account_ids` | list of str | Account ids (strings) for `account`, `emergency`, `retirement` |
| `linked_debt_id` | str or null | Debt id (string) for `debt_payoff` |
| `baseline_amount` | float or null | Starting debt for debt goals, captured at creation |
| `months_target` | float or null | Months of expenses for `emergency` |

### networth_snapshots

Written by `take_networth_snapshot`. Model: `NetWorthSnapshot`.

| Field | Type | Meaning |
|---|---|---|
| `date` | datetime | |
| `total_assets` | float | Account balances plus asset values |
| `total_liabilities` | float | Amortized asset loans plus standalone debts |
| `net_worth` | float | |
| `breakdown` | dict | `{accounts, assets, asset_liabilities, standalone_debts, liabilities}` |

### cc_cards

Written by `cc_card_add`, `ensure_cc_cards_seeded`, `cc_upload` (auto-create), and the benefits routes (`benefits_card_save`, `benefits_perk_save`, `benefits_perk_template`, `benefits_card_benefit`). See [credit-cards.md](credit-cards.md) and [benefits.md](benefits.md).

| Field | Type | Meaning |
|---|---|---|
| `name` | str | Unique by convention; joins to `cc_transactions.card_name` |
| `issuer` | str | One of `CARD_ISSUERS`: `chase`, `citi`, `amex`, `other` |
| `created_at` | datetime | |
| `annual_fee` | float | Set from `/benefits/card` or a template |
| `fee_month` | int or null | Month the fee posts; anchors cardmember-year resets |
| `perks` | list of dict | Statement credits: `{id, name, amount, cadence, reset_basis, ends_on, notes}` plus, from templates, `amount_by_period` (dict of period suffix to amount), `match` and `match_exclude` (merchant patterns for usage detection) |
| `benefits` | list of dict | Non-credit benefits: `{id, name, category, notes}`. Reference only, never scored |
| `template_key` | str | Key into `benefits.PERK_TEMPLATES` the card was loaded from |

### cc_transactions

Written by `cc_upload`, `cc_recategorize`, `cc_link_plan`, `delete_payment_plan`, `cc_delete_statement`, the LLM worker in `_categorize_leftovers_async`, and the one-time migrations.

| Field | Type | Meaning |
|---|---|---|
| `date` | datetime | Transaction date |
| `description` | str | Merchant descriptor as exported |
| `amount` | float | Positive for charges, negative for merchant credits and refunds. Card payments are dropped at import |
| `category` | str | One of `CC_CATEGORIES` (`CATEGORIES` plus `Credit`) |
| `card_issuer` | str | Issuer selected or detected at upload |
| `card_name` | str | Card display name |
| `original_category` | str | The bank's own category (Chase exports only; empty otherwise) |
| `statement_date` | str | `YYYY-MM`, the majority month of the uploaded file |
| `categorized_by` | str | `llm` when the LLM pass set the category; removed on manual recategorization; absent on older rows |
| `payment_plan_id` | ObjectId | Present when linked to a payment plan |

Duplicate rows (same `date`, `description`, `amount`, `card_name`) are skipped at upload.

### cc_perk_usage

Written by `benefits_perk_usage`; rows for a perk are deleted when the perk is deleted.

| Field | Type | Meaning |
|---|---|---|
| `card_id` | ObjectId | |
| `perk_id` | str | `perks[].id` on the card |
| `period` | str | Period key from `benefits.period_key()`, for example `2026-03`, `2026-Q2`, `2026-H1`, `2026`, or anniversary keys like `2026A09` / `2026A09-H2` |
| `amount_used` | float | Dollars redeemed in that period |
| `updated_at` | datetime | |

### benefit_settings

Singleton, written by `benefits_settings_save`; merged over `benefits.DEFAULT_SETTINGS` by `benefits.get_settings()`.

| Field | Type | Meaning |
|---|---|---|
| `hsa_coverage` | str | `self` or `family` |
| `age_50_plus` | bool | 401(k)/IRA catch-up eligibility |
| `age_55_plus` | bool | HSA catch-up eligibility |
| `annual_salary` | float or null | For employer-match math |
| `pay_periods_per_year` | int | Default 26 |
| `limit_overrides` | dict | `{"<year>": {"401k": float, "ira": float, "hsa": float}}` |
| `updated_at` | datetime | |

### benefit_accounts

Written by `benefits_account_config`, one document per account.

| Field | Type | Meaning |
|---|---|---|
| `account_id` | ObjectId | |
| `limit_kind` | str or null | `401k`, `ira`, `hsa`, `taxable_retirement` (uncapped), `none`, or null (fall back to the account type default) |
| `per_paycheck` | float or null | Payroll election per paycheck |
| `match_rate_pct` | float or null | Employer match rate |
| `match_limit_pct` | float or null | Salary percentage the match applies up to |
| `updated_at` | datetime | |

### contribution_overrides

Written by `benefits_contribution_save`, one per account and year.

| Field | Type | Meaning |
|---|---|---|
| `account_id` | ObjectId | |
| `year` | int | Tax year |
| `ytd_amount` | float | Authoritative year-to-date figure from a paystub |
| `as_of` | datetime | Activity detected after this date is added on top |
| `note` | str or null | |
| `updated_at` | datetime | |

### reports

Written by `agent/reports.py` with `replace_one(..., upsert=True)` keyed on `(type, period)`. See [advisor-agent.md](advisor-agent.md).

| Field | Type | Meaning |
|---|---|---|
| `type` | str | `weekly`, `monthly`, or `portfolio` |
| `period` | str | ISO week such as `2026-W08` (weekly) or `YYYY-MM` (monthly, portfolio) |
| `date` | datetime | Generation time |
| `data` | dict | The figures gathered for the report |
| `insights` | str | Markdown text from the LLM, rendered to HTML on `/reports` |

### home_move_scenarios

Written by `home_move_save`.

| Field | Type | Meaning |
|---|---|---|
| `name` | str | User-supplied or generated (`Sell $X → Buy $Y`) |
| `params` | dict | Output of `home_move.parse_params()`; reloaded when the scenario is opened |
| `summary` | str | Monthly payment and cash to close |
| `created_at` | datetime | |

### household_employees

Written by `payroll_employee_form`. Model: `HouseholdEmployee`.

| Field | Type | Meaning |
|---|---|---|
| `name` | str | |
| `ssn` | str or null | Sensitive |
| `address` | str or null | Sensitive |
| `hire_date`, `termination_date` | datetime or null | |
| `pay_type` | str | `salary` or `hourly` |
| `pay_rate` | float | Gross per period (salary) or hourly rate |
| `pay_frequency` | str | `weekly`, `biweekly`, `semimonthly`, `monthly` |
| `funding_account_id` | ObjectId or null | Account net pay is drawn from |
| `fit_per_period`, `sit_per_period` | float | Flat federal and state withholding per period |
| `state` | str or null | Two-letter code; overrides the payroll setting |
| `relationship` | str or null | Affects FICA exemptions |
| `lives_with_employer`, `under_18` | bool | |
| `notes` | str or null | |
| `active` | bool | |

### payroll_entries

Written by `payroll_run`; deleted by `payroll_entry_delete` (which also deletes the linked ledger transaction). Model: `PayrollEntry`.

| Field | Type | Meaning |
|---|---|---|
| `employee_id` | ObjectId | |
| `pay_date`, `period_start`, `period_end` | datetime | |
| `gross_wages` | float | |
| `hours`, `rate` | float or null | Hourly runs |
| `ee_ss`, `ee_medicare`, `ee_addl_medicare`, `ee_fit`, `ee_sit` | float | Employee withholdings |
| `er_ss`, `er_medicare`, `er_futa`, `er_suta` | float | Employer taxes |
| `net_pay` | float | |
| `funding_account_id` | ObjectId or null | |
| `transaction_id` | ObjectId or null | Ledger row created for the net pay |
| `state` | str | |
| `notes` | str or null | Exemption and threshold notes joined with `; ` |

### payroll_settings

Singleton with `_id: "default"`, seeded from `PAYROLL_DEFAULTS` by `get_payroll_settings()` and written by `payroll_settings_page`.

| Field | Type | Meaning |
|---|---|---|
| `_id` | str | Always `default` |
| `employer_name`, `employer_ein`, `employer_address` | str | |
| `ss_rate`, `medicare_rate`, `additional_medicare_rate` | float | |
| `additional_medicare_threshold`, `ss_wage_base`, `fica_annual_threshold` | float | |
| `futa_rate`, `futa_wage_base`, `futa_quarterly_threshold` | float | |
| `state` | str | Default two-letter state |
| `state_configs` | dict | `{"<STATE>": {name, suta_rate, suta_wage_base, suta_quarterly_threshold, sit_withhold_required}}` |

### form_templates

Written by `payroll_forms_upload`; read by `_template_from_db()`.

| Field | Type | Meaning |
|---|---|---|
| `_id` | str | `fw2_<year>.pdf` or `f1040sh_<year>.pdf` |
| `content` | binary | The fillable PDF |
| `uploaded_at` | datetime | |
| `field_count` | int | Number of form fields found |

### dashboard_prefs

Singleton written by `save_dashboard_layout` (deleted on reset).

| Field | Type | Meaning |
|---|---|---|
| `cards` | list of dict | Ordered `{key, span (1-4), visible}` entries; keys from `DASHBOARD_CARD_DEFS` |
| `updated` | datetime | |

### app_settings

Singleton with `_id: 'app'`, written by the setup wizard (`/setup/*`) and the
dashboard checklist.

| Field | Type | Meaning |
|---|---|---|
| `setup_completed` | bool | Wizard reached its last step (or demo data was loaded); stops the first-run redirect |
| `setup_completed_at`, `setup_started_at` | datetime | |
| `setup_skipped` | bool | "Skip setup" was chosen; also stops the redirect |
| `household_name` | string or null | Shown on the dashboard |
| `checklist_dismissed` | bool | Hides the dashboard getting-started checklist |
| `demo_data` | bool | Set when the wizard loaded the demo data set |
| `updated_at` | datetime | |

The dashboard redirects to the wizard only while neither flag is set **and**
there are no accounts, so existing databases never see it.

### app_migrations

Markers so one-time migrations run once. Each document has a string `_id` and `run_at` (datetime). Current ids, all written from the `/credit-cards` page load:

| `_id` | Migration |
|---|---|
| `cc_credits_negative_v1` | `normalize_existing_cc_credits()`: flip stored credits and refunds to negative amounts |
| `cc_credits_category_v1` | `mark_existing_cc_credits_category()`: tag negative rows `Credit` |
| `cc_phone_category_v1` | `categorize_existing_cc_phone()`: apply the Phone keyword rules |
| `cc_recategorize_other_v1` | `recategorize_existing_cc_other()`: re-run keyword rules over rows still at `Other` |

To add a migration, follow the same pattern: check for a marker document, do idempotent work, insert the marker.
