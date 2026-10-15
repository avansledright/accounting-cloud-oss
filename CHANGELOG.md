# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

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
