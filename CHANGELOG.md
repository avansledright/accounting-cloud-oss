# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- First-run setup wizard (`/setup`): welcome and household name, primary
  account, recurring income, recurring bills, optional accounts and debts, and
  a summary with the first projected months. A fresh install's dashboard
  redirects to it; it can be skipped, and re-run from Admin.
- "Load demo data instead" on the wizard; the demo data set moved to
  `demo_data.py` (the `scripts/seed_demo.py` CLI is unchanged).
- Dashboard "Getting started" checklist that ticks off from real data, and
  guidance on empty Ledger, Budget, Cards and Net Worth pages.
- `docs/getting-started.md` walkthrough; mongomock route tests
  (`requirements-dev.txt`).
- `recurrence.py`: pure `occurrence_dates()`, `calc_monthly_amount()` (moved
  from `app.py`) and `to_date()`, shared by the ledger outlook, the calendar,
  the budget and account pages and the agent/MCP tools, so every frequency
  (weekly, biweekly, monthly, every-other-month `bi-monthly`, yearly) means
  the same thing everywhere.
- Home move planner: `target_basis` (`note` or `year1`) for the reverse
  solve, so a buydown's first-year payment can be the target;
  `monthly.by_year` and `monthly.first_year_total`; a buydown comparison table
  (none, 1-0, 2-1) via `compare_buydowns()`.
- Benefits: a card's missing annual fee and fee month are filled from its
  latest `ANNUAL FEE` / `ANNUAL MEMBERSHIP FEE` statement charge
  (`detect_annual_fee()`), in memory only; hand-entered values win.
- Payroll: `under_21` flag on household employees.
- MCP `make_debt_payment` takes optional `date`, `account_id` and
  `description`.
- Docker image installs `tzdata` and sets `ENV TZ=UTC` (overridable); the
  Kubernetes ConfigMap has a commented `TZ` example.
- App-level tests on a shared mongomock fixture in `tests/conftest.py`.

### Changed
- MCP write tools now match the web app: `add_transaction` defaults to the
  primary account's ID; `delete_transaction` records a `skipped_transactions`
  entry; `override_recurring_amount` stores the `YYYY-MM-DD` string the
  outlook reads; `add_transfer` also posts the paired `Transfer to:` /
  `Transfer from:` ledger rows and rejects same-account and non-positive
  transfers; recurring tools accept `biweekly`; `update_debt_balance` sets the
  starting balance; `make_debt_payment` logs a linked ledger expense instead
  of editing the balance.
- MCP read tools: `recurring_items` uses `calc_monthly_amount()`;
  `cash_flow_outlook` and `negative_balance_months` count the ledger's
  occurrence dates and default to the primary account; `portfolio_summary`
  reports live market value, cost basis and unrealized gain/loss (holdings
  without a quote are excluded and called out).
- Payroll follows Pub 926 more closely: a child under 21 and a parent are
  exempt from FICA and FUTA, an unrelated employee under 18 from FICA only;
  the run that crosses the FICA threshold catches up Social Security and
  Medicare on the year's earlier wages, and Schedule H counts the employee's
  full-year wages once FICA applies.
- Card template suggestion: the Citi AAdvantage Executive template is no
  longer offered to every Citi card.

### Fixed
- The calendar shows weekly items and drops occurrences after `end_date`.
- `/history?year=YYYY` works in January and February.
- A malformed or out-of-range account `?horizon=` falls back to 120 months.
- Home move monthly income excludes archived recurring items.
- Deleting a transfer removes both ledger rows whether their `account_id` is
  stored as an `ObjectId` or a string.
- The outlook still applies per-date overrides written with a datetime by
  older MCP versions.
- Buydown labels read `2-1` / `1-0` instead of `2-1-0`.

## [0.1.0] - 2026-10-15

First public, self-hostable release, extracted from a privately run household
finance app.

### Added
- Budget with recurring income/bills, recurring transfers as budget lines, and
  two-layer category targets (default template + per-month overrides).
- Ledger with a rolling 12-month projection engine: per-account projections,
  low-balance tracking, cash-flow warnings, loan payoff simulation and
  automatic archiving of paid-off loan payments, and a what-if simulator.
- Accounts, transfers, debts (payoff planner, payment plans), assets with
  amortization and escrow simulation, goals (manual and live-tracked), net
  worth history and projection, analytics, calendar and history views.
- Credit card statement import (Chase, Citi, Amex and generic CSV) with
  keyword categorization and an optional two-vote LLM pass via any
  Ollama-compatible endpoint.
- Portfolio with live prices and dividends (Yahoo Finance, Alpha Vantage
  fallback), brokerage CSV import, derived return rates and investment
  projections.
- Benefits page: contribution limits by tax year, employer match, and credit
  card perk tracking with researched templates.
- Home-move / mortgage planner with live Freddie Mac rates, configurable local
  closing costs, ARM and buydown scenarios.
- Household-employer payroll (W-2, Schedule H, EFW2 export and validator).
- Optional MCP server (stateless streamable HTTP, bearer-token auth) with read
  and write tools, and optional LLM advisor reports.
- Docker Compose stack, generic Kubernetes manifests, demo data seeder, unit
  tests and GitHub Actions CI.

### Changed (relative to the private original)
- All hosts, models, keys and paths are configurable through environment
  variables (see `.env.example`); no deployment-specific defaults remain.
- Advisor household profile is loaded from `ADVISOR_PROFILE_FILE`; the report
  scheduler only runs when `ADVISOR_ENABLED=true`.
- Home-move transfer taxes, payroll state settings and escrow disbursement
  months are inputs/config instead of fixed regional values.
- Docker image runs gunicorn as a non-root user.
