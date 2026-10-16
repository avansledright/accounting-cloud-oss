# Feature tour

A page-by-page walk through the app, in sidebar order. Several areas have
their own deep-dive docs and are only summarised here:

| Area | Doc |
|---|---|
| Budget page, targets, recurring items | [budget.md](budget.md) |
| Ledger projection engine (`calculate_yearly_outlook`) | [ledger-projections.md](ledger-projections.md) |
| Portfolio, holdings, live prices, dividends, investment projections | [portfolio.md](portfolio.md) |
| Credit card statements, categorisation, savings insights | [credit-cards.md](credit-cards.md) |
| Tax year contribution room and card perks (`/benefits`) | [benefits.md](benefits.md) |
| Home move planner (`/tools/home-move`) | [home-move.md](home-move.md) |
| Advisor reports | [advisor-agent.md](advisor-agent.md) |
| MCP server | [mcp-server.md](mcp-server.md) |

All routes live in `app.py`. Page routes render a Jinja template from
`templates/`; form posts redirect back to the page. JSON endpoints used by
page JavaScript send the CSRF token in a header (see `getHeaders()` in
`static/main.js`).

The examples on this page use invented figures.

## Setup wizard (`/setup`)

On a fresh install (`needs_first_run_setup()`: no accounts, and setup neither
completed nor skipped) the dashboard redirects to `/setup/welcome`. The steps
are `welcome`, `account`, `income`, `bills`, `extras` and `done`
(`setup_step()`, `templates/setup.html`). They create records through the same
helpers as the normal pages (`_create_account`, `_create_recurring_item`,
`_create_debt`). State lives in the `app_settings` singleton (`_id: 'app'`):
`setup_completed`, `setup_skipped`, `household_name`, `checklist_dismissed`.

- Income and bill rows take a frequency and the next pay/due date. Monthly
  items start today (their day of month carries the schedule); weekly and
  bi-weekly items are re-anchored on their next occurrence on or after today;
  yearly and bi-monthly items keep the due date's month. "Twice a month" is a
  monthly item with two days, e.g. `1,15`. So nothing is projected before the
  balance entered for today.
- Re-saving the account step updates the existing primary account.
- `POST /setup/demo` loads `demo_data.load_demo_data()` (empty database only),
  `POST /setup/skip` skips, and `POST /setup/restart` (Admin) re-opens the
  wizard without touching data.

A walkthrough for users is in [getting-started.md](getting-started.md).

## Dashboard (`/`)

The home page (`index()`, `templates/dashboard.html`) is a high-signal
snapshot. Before rendering it runs `_apply_auto_links()` so loan payments are
attached (see [Debts](#debts-debts)).

A **Getting started** checklist (`getting_started_checklist()`) sits at the
top until every item is done or it is hidden (`POST /setup/checklist/dismiss`;
Admin can show it again). Items tick off from data: a primary account,
recurring income, recurring bills, budget targets, a recorded transaction, an
imported card statement, and a net worth snapshot.

The summary strip shows the primary account balance, cash total
(`CASH_ACCOUNT_TYPES`), investment total (`INVESTMENT_ACCOUNT_TYPES`), net
worth with its change against the closest net worth snapshot at least 30 days
old, and total liabilities (amortised asset loans plus standalone debts).

Below it is a grid of cards defined in `DASHBOARD_CARD_DEFS`:

| Key | Card | Content |
|---|---|---|
| `upcoming_bills` | Upcoming Bills | Active recurring expenses (`ACTIVE_RECURRING`) falling in the next 14 days, by `day_of_month` or, for `biweekly` items, every 14 days from `start_date`. |
| `month_to_date` | Month to Date | Income and expense transactions on the primary account since the 1st. |
| `payment_plans` | Payment Plans | The three most-progressed active payment plans. |
| `outlook` | Next 3 Months | Current month plus three from `calculate_yearly_outlook()` on the primary account, with a cash flow warning for any month whose low point goes negative. |
| `goals` | Savings Goals | The three goals closest to their target, computed live by `_decorate_goal()`. |
| `advisor_brief` | Advisor Brief | The first 280 characters of the most recent advisor report. |
| `benefit_alerts` | Tax Year & Perks | Up to four high or medium severity alerts from `_benefit_context()`. See [benefits.md](benefits.md). |

Cards with nothing to show (no plans, no goals, no reports, no alerts) are
hidden automatically.

**Customising the layout.** Click "Customize" to drag cards into a new order,
drag a card's right edge to change its width (1 to 4 columns), or toggle its
visibility. The layout is saved with `POST /dashboard/layout` as JSON
(`{"cards": [{"key": ..., "span": ..., "visible": ...}]}`) into the singleton
`dashboard_prefs` document. Posting `{"reset": true}` deletes it and restores
the defaults. `_dashboard_layout()` merges the saved list with the registry:
unknown keys are dropped and cards added in later versions are appended with
their default width, so old preferences never break the page.

## Ledger (`/ledger`)

The ledger (`ledger_page()`, `templates/index.html`) is a month-by-month
statement of actual transactions merged with projected occurrences of
recurring items and recurring transfers, with a running balance, the month's
low point, a forecast strip of upcoming months, and a cash flow warning
banner when any month goes negative. The projection itself is documented in
[ledger-projections.md](ledger-projections.md).

**Account filter.** The dropdown picks one account or "All Accounts".
`resolve_account_filter()` stores the choice in the session, so it sticks as
you move between the ledger, history and simulation. With no stored choice
the **primary account** is the default. Opening an account's detail page also
makes it the sticky selection.

**Balances.** Each account's stored `balance` is a base figure. The ledger
shows a *calculated* balance (base plus every transaction on that account) and
a *confirmed* balance (base plus transactions dated today or earlier).
Transactions with no `account_id` count toward the primary account.

**Working with transactions:**

| Action | Route | Notes |
|---|---|---|
| Add a transaction | `POST /transactions` (JSON) | Floating add form. Defaults to the primary account. Runs `_auto_link_transaction()` so a description matching a loan's `payment_match` is tagged as a payment. |
| Verify a projected row | `GET/POST /transactions/verify` | Turns a projected occurrence into a real transaction (amount and date editable). For projected transfers it creates a `transfers` record plus both ledger sides. |
| Skip a projected row | `POST /transactions/skip` | Stores `(description, date)` in `skipped_transactions` so the occurrence disappears. Skipping one side of a transfer skips its mirror too. |
| Change one occurrence's amount | `POST /projected/update-amount` | Upserts a `projected_overrides` document keyed by recurring ID and ISO date. |
| Edit a transaction | `GET/POST /transactions/edit/<id>` | Description, amount, date, type. |
| Delete a transaction | `DELETE /transactions/<id>` | Also records a skip so the projection it realised does not come back. |
| Link a row to a loan | `POST /transactions/link-loan` (JSON) | Sets `debt_id` or `asset_liability_id` (`loan_ref` of `debt:<id>` or `asset:<id>`; empty unlinks), then re-syncs paid-off archiving. |
| One-time transfer | `POST /transfers/add` | The "Transfer Between Accounts" form on the ledger. See [Transfers](#transfers-transfers). |

**Search.** Any of `search`, `date_from`, `date_to`, `amount_min`,
`amount_max` or `type` switches the page to a flat list of matching actual
transactions across all accounts.

`GET /transactions` and `GET /recurring_transactions` return raw JSON lists;
`POST /recurring_transactions` creates a minimal recurring item from JSON.
The budget page is the normal way to manage recurring items
([budget.md](budget.md)).

## Accounts (`/accounts`, `/accounts/<id>`)

Accounts are the containers for balances: bank accounts, retirement plans,
brokerage accounts.

**Types.** `ACCOUNT_TYPES` in `models.py` lists every type (`checking`,
`savings`, `ira`, `roth_ira`, `brokerage`, `401k`, `hsa`, `deferred_comp`,
`other`). Two lists group them and are the **single source of truth**:

- `INVESTMENT_ACCOUNT_TYPES` (`ira`, `roth_ira`, `brokerage`, `401k`, `hsa`,
  `deferred_comp`): get return-rate fields, investment projections and
  portfolio treatment.
- `CASH_ACCOUNT_TYPES` (`checking`, `savings`): spendable cash for the ledger
  and outlook.

The `inject_account_type_sets` context processor exposes both to every
template as `investment_account_types` and `cash_account_types`, so Jinja and
page JavaScript use the same values.

**Fields.** Name, type, institution, balance, account number, notes,
`is_primary`, and for investment accounts `estimated_return_rate` (annual
percent) and `management_type` (`managed` or `self_managed`).

- **Primary account.** Exactly one account should have `is_primary: true`.
  It is the ledger's default view, the account that transactions without an
  `account_id` belong to, and the default for recurring items without one.
  Marking an account primary on add or edit clears the flag everywhere else.
  If no primary account exists, the ledger falls back to the
  `STARTING_BALANCE` environment variable.
- **`balance_as_of_date`.** When the balance was last confirmed. Set from the
  add and edit forms, and stamped automatically by the quick balance update
  (`POST /accounts/update-balance/<id>`, form or JSON).

**Pages and routes:**

| Route | Purpose |
|---|---|
| `GET /accounts` | Accounts grouped as Cash, Investments and Other, with a sub-nav strip of section anchors and summary totals. |
| `POST /accounts/add` | Create an account. |
| `GET/POST /accounts/edit/<id>` | Edit every field. |
| `POST /accounts/update-balance/<id>` | Set the balance and stamp `balance_as_of_date`. |
| `POST /accounts/delete/<id>` | Delete an account (does not touch its transactions). |
| `GET /accounts/<id>` | Detail page with breadcrumbs. Lists recurring items and recurring transfers that touch the account. Cash accounts show an inline 12-month outlook. Investment accounts show a projection chart with a 5, 10, 20 or 30 year horizon (`?horizon=` in months). Self-managed accounts derive their return rate from holdings on each visit (`derive_account_return_rate`). |
| `GET /api/accounts` | JSON list of accounts for dropdowns. |

Investment projections and return-rate derivation are covered in
[portfolio.md](portfolio.md).

## Transfers (`/transfers`)

`transfers_page()` shows the last 50 one-time transfers and every recurring
transfer with its monthly equivalent.

**One-time transfers** (`POST /transfers/add`, from the ledger or this page)
create a `transfers` record and two ledger transactions: an expense
`Transfer to: <description>` on the source account and an income
`Transfer from: <description>` on the destination. Those transactions are
what move the money in the ledger. Same-account and non-positive transfers
are rejected. `POST /transfers/delete/<id>` removes the record and attempts
to remove both ledger sides.

**Recurring transfers** (`recurring_transfers`) have a from and to account,
amount, frequency (`weekly`, `biweekly`, `monthly`, `bi-monthly`, `yearly`),
start date, optional `day_of_month` (for example `"6"` or `"6,21"`), optional
end date, description and notes. They project on both accounts in the ledger
and appear on the budget page as synthetic items categorised by the
destination account type (Savings, Investments). See
[budget.md](budget.md) and [ledger-projections.md](ledger-projections.md).

| Route | Purpose |
|---|---|
| `POST /transfers/recurring/add` | Create. |
| `GET/POST /transfers/recurring/edit/<id>` | Edit. |
| `POST /transfers/recurring/delete/<id>` | Delete. |

Transfers into tax-advantaged accounts also count as contributions on the
Benefits page ([benefits.md](benefits.md)).

## Debts (`/debts`)

The Debt Payoff Planner (`debts_page()`) covers standalone debts, loans
attached to assets, and no-interest payment plans.

### Debt records

A debt has a name, `debt_type` (from `DEBT_TYPES`: `credit_card`,
`personal_loan`, `student_loan`, `medical`, `other`), balance, APR, minimum
payment, creditor, account number, notes, and:

- **`deferment_end_date` and `subsidized`** for student loans. While a loan is
  deferred it is left out of the payoff plan. The per-debt payoff schedule
  (`/debts/<id>/payoff`) starts payments after deferment ends and, unless the
  loan is subsidised, accrues monthly interest on the balance during the
  deferment.
- **`payment_match`**: a list of ledger descriptions that count as payments
  to this debt, entered as comma or newline separated text.

### Balances and automatic payment linking

The entered `balance` is the **starting balance**. The current balance comes
from ledger transactions linked to the debt (`debt_id` on the transaction):
`_amortize()` walks the linked payments in date order and reduces the balance
by each payment's principal, after interest accrued on the days since the
previous payment. Asset loans do the same with `asset_liability_id`, and also
subtract prorated escrow.

Linking is automatic. `_apply_auto_links()` runs on the dashboard, ledger,
debts and goals pages and tags every unlinked expense transaction whose
description equals (case-insensitively, trimmed) a `payment_match` entry on a
debt or asset. New transactions are linked as they are added. You can also
link and unlink by hand on the detail pages, or trigger a pass with
`POST /debts/auto-link`. `DEFAULT_PAYMENT_MATCHES` in `app.py` can seed
patterns by loan name; it is empty by default.

When the linked payments bring a loan to zero, `_sync_payoff_archive()`
archives its matching budget lines (`archived: true`,
`archived_reason: 'paid_off'`) so they drop out of the budget, estimates,
calendar and bills. If the loan reopens they are restored. Paid-off loans
move to a collapsible "Paid Off" section. Details in
[ledger-projections.md](ledger-projections.md).

### Payoff plan

The plan (`_get_debt_plan()`, stored as the singleton `debt_plan` document)
turns an avalanche or snowball simulation into a budget-aware strategy:

- **Strategy.** `avalanche` (highest APR first) or `snowball` (lowest balance
  first), applied by `_order_debts()`.
- **Extra payment.** With `auto_extra` on, the extra is the projected monthly
  surplus: recurring income minus recurring expenses
  (`_projected_monthly_surplus()`, which excludes the plan's own line to avoid
  a feedback loop). Otherwise it is a fixed `extra_payment`.
- **Simulation.** `_simulate_payoff()` pays every minimum, then rolls the
  extra plus freed-up minimums into the first unpaid debt, month by month (50
  year cap). Debts with no minimum get `max($25, 2% of balance)`. It covers
  standalone debts not in deferment and asset loans, at their current
  amortised balances (`_unified_debts()`).
- **Output.** Target debt, debt-free date, months and interest saved against
  minimums only, and a payoff order with an ETA per debt.
- **Budget line.** Saving the plan (`POST /debts/plan`) with "create
  recurring" maintains a monthly `Extra Debt Payment` recurring expense
  (category Debt Payments) on the chosen cash account, so the extra is
  reserved in the budget and outlook.
- **Logging a payment.** `POST /debts/plan/log-payment` inserts a real
  `Extra Debt Payment` transaction linked to the current target. Because it
  shares the recurring line's description, the ledger dedups this month's
  projected occurrence.

The "Payoff Calculator" widget (`POST /debts/calculate`, JSON
`{"extra_payment": 200}`) runs both strategies side by side without saving
anything.

### Payment plans

Payment plans are no-interest installment purchases billed to a credit card
(vendor, original amount, monthly payment, start date, optional card and
category, status `active` or `closed`). Progress comes from credit card
transactions linked to the plan (`payment_plan_id`, set from the credit card
page with `POST /credit-cards/link-plan`). `_decorate_payment_plan()` computes
paid, remaining, payments made and remaining, and an estimated end date.

### Routes

| Route | Purpose |
|---|---|
| `GET /debts` | Planner page. |
| `POST /debts/add`, `GET/POST /debts/edit/<id>`, `POST /debts/delete/<id>` | Manage debts (delete also unlinks payments). |
| `GET /debts/<id>` | Debt detail: linked payments with principal and interest split, link candidates (unlinked expenses from the last 18 months). |
| `POST /debts/<id>/link-transaction`, `POST /debts/<id>/unlink-transaction` | Manual linking. |
| `GET /debts/<id>/payoff` | Month-by-month payoff schedule at the minimum payment. |
| `GET /debts/asset/<id>` | Asset loan detail, with escrow split when escrow is set. |
| `POST /debts/asset/<id>/link-transaction`, `.../unlink-transaction` | Manual linking for asset loans. |
| `POST /debts/auto-link` | Run the auto-link pass now. |
| `POST /debts/plan`, `POST /debts/plan/log-payment`, `POST /debts/calculate` | Payoff plan. |
| `POST /debts/payment-plans/add`, `.../edit/<id>`, `.../delete/<id>`, `GET /debts/payment-plans/<id>` | Payment plans. |

## Assets and liabilities (`/assets`)

Assets are things you own outside of accounts: real estate, vehicles, other
(`ASSET_TYPES`). Each has an estimated value, purchase date and price,
address and notes, and optionally an attached loan:

| Field | Meaning |
|---|---|
| `liability_balance`, `liability_type` | Loan balance and type (`LIABILITY_TYPES`: `mortgage`, `auto_loan`, `heloc`, `other`). |
| `interest_rate`, `loan_term_months`, `monthly_payment` | Loan terms. |
| `payment_start_date`, `payment_frequency` | When payments start; `monthly` or `biweekly`. |
| `property_tax_annual`, `homeowners_insurance_annual`, `pmi_monthly`, `hoa_monthly`, `escrow_balance` | Escrow inputs. |
| `payment_match` | Ledger descriptions that auto-link as loan payments, as for debts. |

The assets page groups assets by type and shows equity (value minus loan).
Asset loans appear on the Debts page under "Asset Liabilities" with amortised
current balances.

**Amortization page** (`GET /assets/<id>/amortization`):

- A forward payment schedule from today (or from a future
  `payment_start_date`) with principal, interest, escrow and remaining balance
  per period.
- Monthly escrow = property tax / 12 + insurance / 12 + PMI + HOA
  (`_calculate_monthly_escrow`), broken down by component.
- A monthly versus bi-weekly comparison (`_calculate_payment_comparison`):
  paying half the monthly amount every two weeks, with interest and months
  saved.
- A mortgage position summary estimated from the loan term and start date.
- An **escrow account simulation** for the next 36 periods: deposits each
  period, property tax disbursed in `ESCROW_PROPERTY_TAX_MONTHS` (default
  June and December, split evenly) and insurance in `ESCROW_INSURANCE_MONTH`
  (default January), flagging any shortage. Both are environment variables
  (comma-separated month numbers).

| Route | Purpose |
|---|---|
| `GET /assets` | Assets page. |
| `POST /assets/add`, `GET/POST /assets/edit/<id>`, `POST /assets/delete/<id>` | Manage assets (delete also unlinks loan payments). |
| `GET /assets/<id>/amortization` | Amortization and escrow. |

The [home move planner](home-move.md) reads current homes from real estate
assets.

## Goals (`/goals`)

Goals (`savings_goals`) have a name, target amount, optional target date,
category (`GOAL_CATEGORIES`), notes, and a **goal type** (`GOAL_TYPES`) that
decides how progress is measured. `_decorate_goal()` computes progress at
display time:

| Type | Progress | Target |
|---|---|---|
| `manual` | The stored `current_amount`, increased with the Contribute box (`POST /goals/contribute/<id>`). | As entered. |
| `account` | Live combined balance of the linked accounts. | As entered. |
| `emergency` | Combined balance of linked accounts, or all checking and savings if none are linked; also shows months covered. | `months_target` times the estimated monthly expenses from recurring expense items. |
| `retirement` | Live combined balance of the linked accounts, with a projected ETA: the first month their combined investment projection reaches the target (`_retirement_eta`, up to 50 years). | As entered. |
| `networth` | Live net worth (accounts plus assets minus amortised loans and debts). | As entered. |
| `debt_free` | Amount of standalone debt paid off since the baseline. Shows the payoff plan's strategy, target debt and debt-free date. Excludes asset-backed loans. | `baseline_amount`, captured as total standalone debt when the goal is created. |
| `debt_payoff` | Amount paid off on one linked debt since the baseline. | `baseline_amount`, captured as that debt's current balance at creation. |

Types in `LIVE_GOAL_TYPES` ignore the stored `current_amount`. The page's
"Total Saved / Target" summary only counts `SAVINGS_GOAL_TYPES` (`manual`,
`account`, `emergency`, `retirement`) so debt and net worth milestones do not
dwarf the totals. The goals page runs `_apply_auto_links()` first so debt
goals use current balances.

| Route | Purpose |
|---|---|
| `GET /goals` | Goals page. |
| `POST /goals/add`, `GET/POST /goals/edit/<id>`, `POST /goals/delete/<id>` | Manage goals. |
| `POST /goals/contribute/<id>` | Add to a manual goal (form or JSON). |

## Net worth (`/networth`)

The Net Worth Tracker shows current assets (all account balances plus asset
values), liabilities (amortised asset loans plus standalone debts) and net
worth.

- **Snapshots.** "Record Snapshot" (`POST /networth/snapshot`) stores a
  `networth_snapshots` document with the date, totals and a breakdown
  (accounts, assets, asset liabilities, standalone debts). Snapshots are
  manual only; nothing records them on a schedule. The dashboard's net worth
  change and the advisor's net worth history both read them, so record one
  regularly (for example monthly).
- **History.** A chart and table of the last 24 snapshots.
  `GET /api/networth/history` returns every snapshot as JSON for the chart.
- **Projection.** A 24-month forward projection: investment accounts with an
  `estimated_return_rate` are compounded with `calculate_investment_projection()`
  (including recurring deposits and transfers into them); everything else
  (cash, assets, liabilities, investment accounts without a rate) is held
  flat.

## Analytics (`/analytics`)

Spending Analytics renders charts from two JSON endpoints:

| Route | Returns |
|---|---|
| `GET /api/analytics/spending-by-category?month=YYYY-MM` | Expense totals per category for the month: ledger expense transactions (by their `category` field, else Other) plus active recurring expenses expected that month (monthly items times the number of listed days, biweekly occurrences, yearly items in their month). |
| `GET /api/analytics/monthly-trend` | Ledger income and expenses for each of the last six months. |

Credit card spending analytics live on the credit card page; see
[credit-cards.md](credit-cards.md).

## Calendar (`/calendar`)

The Bill Calendar is a month grid filled by
`GET /api/calendar/events?year=YYYY&month=M`, which expands active recurring
items into dated events (title, amount, type, category, auto-pay flag):
monthly items on each listed day (clamped to the month's last day, day 1 if
none), bi-monthly items every other month from the start month, yearly items
in their start month, and biweekly items every 14 days. Start and end dates
are respected. Clicking a day shows its events.

## History (`/history`)

`history_page()` is a read-only view of closed months: actual transactions
only, no projections, grouped by month with income, expenses and net. For the
current year it covers January through two months ago (the previous month is
still on the ledger). `?year=YYYY` shows all twelve months of a past year. The
account filter is shared with the ledger.

## Simulation (`/simulation`)

The what-if simulator (`simulation_page()`) re-runs the ledger projection
with temporary changes and compares it with the baseline. **Nothing is
written to the database**; the scenario lives entirely in query parameters,
so a scenario URL can be bookmarked.

- **Change or disable items.** Every active recurring item and recurring
  transfer is listed. `amt_<id>` overrides an amount and `off_<id>` disables
  an item.
- **Add hypothetical items.** Parallel `new_desc`, `new_amount`, `new_type`,
  `new_freq` and `new_day` parameters add items that start in the anchor
  month on the account in view.
- **Mode.** `sim_mode=ledger` (default) starts from the account balance and
  includes actual transactions, skips and per-date overrides.
  `sim_mode=budget` uses recurring items only, starting from $0 unless a
  balance is given: a pure "what does my budget do" run.
- **Window.** `sim_start=YYYY-MM-DD` anchors the 12-month window on another
  month; `sim_start_balance` sets the starting balance.

Both runs call `calculate_yearly_outlook(..., sim=...)`. The page shows the
scenario ledger, a month-by-month comparison, and the change in ending
balance and lowest balance.

## Payroll (`/payroll`)

Payroll supports a **household employer** (for example, someone paying a
caregiver or housekeeper directly): pay runs with withholding, year-end W-2s,
Schedule H and the SSA electronic W-2 file.

> **Tax figures must be verified every year.** `PAYROLL_DEFAULTS` in
> `models.py` seeds Social Security and Medicare rates, the Social Security
> wage base, the household-employee FICA threshold, Additional Medicare, and
> FUTA rate and wage base, with comments to verify against SSA and IRS
> Publication 926. They change yearly. Review them in Payroll Settings at the
> start of each year. This feature is a calculator and form filler, not tax
> advice.

**Settings** (`GET/POST /payroll/settings`, singleton `payroll_settings`
document with `_id: 'default'`): employer name, EIN and address, federal
rates and thresholds, and the **state**. The state code, state name, SUTA
rate, SUTA wage base, SUTA quarterly threshold and whether state income tax
withholding is required are set in the UI and stored per state in
`state_configs`. They start blank on purpose: your state agency assigns the
SUTA rate.

**Employees** (`household_employees`; `/payroll/employees/new`,
`/payroll/employees/<id>/edit`, `/payroll/employees/<id>/delete`): name, SSN,
address, hire and termination dates, `pay_type` (`salary` per period or
`hourly`), pay rate, `pay_frequency` (`PAY_FREQUENCIES`), funding account,
flat federal and state withholding per period, state override, relationship,
`lives_with_employer`, `under_18`, active flag.

**Pay runs** (`GET/POST /payroll/run`; live preview via
`POST /payroll/preview`): `calculate_payroll()` computes employee Social
Security (up to the wage base), Medicare, Additional Medicare (over the
threshold), flat federal and state withholding, and employer Social Security,
Medicare, FUTA and SUTA (each up to its wage base), plus net pay. FICA only
applies once the employee's year-to-date cash wages reach
`fica_annual_threshold`, and relationship or age exemptions from Publication
926 are applied with notes asking you to verify them. Saving a run stores a
`payroll_entries` document and, if a funding account is set, posts the net pay
to the ledger as an expense (`Payroll: <name> (<period>)`). Employer taxes are
tracked on the entry; paying them is a separate transaction.
`POST /payroll/entries/<id>/delete` removes an entry and its ledger
transaction. The payroll page shows recent runs and year-to-date totals per
employee.

**Year-end:**

| Route | Output |
|---|---|
| `GET /payroll/w2/<employee_id>/<year>` | W-2 figures for one employee, on screen. |
| `GET /payroll/w2/<employee_id>/<year>/pdf` | Filled official W-2 PDF. |
| `GET /payroll/schedule-h/<year>` | Schedule H figures, on screen. |
| `GET /payroll/schedule-h/<year>/pdf` | Filled Schedule H PDF. |
| `GET /payroll/efw2/<year>` | `W2_EFW2_<year>.txt` in SSA's EFW2 format, for upload to SSA Business Services Online. |
| `GET /payroll/validate/<year>` | Runs `payroll_validator.validate_efw2()` against the generated file and lists errors and warnings. |
| `GET /payroll/year-end/<year>/zip` | One bundle: EFW2 file, validation report, filled W-2s, filled Schedule H and a README of filing steps. If validation finds errors it shows the validation page instead, unless `?force=1`. |

**Validator.** `payroll_validator.py` implements a subset of SSA Publication
42-007 rules (record layout, SSN and EIN format, state codes, totals) that a
household filing commonly trips. It is a fast local check, **not** a
replacement for SSA's AccuWage; run AccuWage before uploading.

**IRS form templates.** The app fills the official IRS fillable PDFs, which
you supply. `forms/README.md` explains the details:

- Name them `fw2_<year>.pdf` and `f1040sh_<year>.pdf` and drop them in
  `forms/` (ignored by git), or upload them at `/payroll/forms/inspect`
  (`POST /payroll/forms/upload`), which stores them in the `form_templates`
  collection so they survive container redeploys. Uploaded templates take
  precedence over files on disk. `POST /payroll/forms/delete/<key>` removes
  one.
- `/payroll/forms/inspect` lists every field found in each PDF. If the IRS
  renames fields, add `forms/w2_fieldmap_<year>.json` or
  `forms/schedule_h_fieldmap_<year>.json`; otherwise the default
  `w2_fieldmap.json` and `schedule_h_fieldmap.json` are used.

Employee SSNs are stored in MongoDB in plain text and are included in JSON
exports. Protect the database and backups accordingly.

## Admin (`/admin`)

`admin_page()` has **Re-run setup wizard** and **Show getting-started
checklist** buttons, document counts for the main collections, and **data
health checks**: holdings pointing at missing accounts, dividends pointing at
missing holdings, and budget items pointing at missing accounts.

### Full JSON backup and restore

- **Export** (`GET /export`): every collection in the database, enumerated
  with `list_collection_names()`, serialised as MongoDB Extended JSON
  (`bson.json_util`) so ObjectIds and dates round-trip exactly. A `_meta`
  block records the format version and export time. Because collections are
  enumerated dynamically, data from new features is included automatically.
  `scripts/backup-accounting.sh` downloads this on a schedule and keeps the
  newest N copies (see [deployment.md](deployment.md)).
- **Import** (`POST /import`, multipart field `file`): parses the file with
  `bson.json_util.loads` and, for **every collection present in the file,
  drops it and inserts the backup's documents**. Collections not in the file
  are left alone. The page asks for confirmation because this replaces your
  data.

The export contains everything, including uploaded IRS PDF templates, payroll
SSNs and reports. Store it as carefully as the database.

### CSV export and import

| Route | Columns |
|---|---|
| `GET /export/csv/transactions` | `date, description, amount, transaction_type, account_id` |
| `GET /export/csv/recurring` | `description, amount, frequency, start_date, end_date, transaction_type` |
| `GET /export/csv/holdings` | `symbol, quantity, cost_basis, account, date_added` |
| `GET /export/csv/dividends` | `symbol, amount, date, source` |
| `GET /export/csv/debts` | `name, debt_type, balance, interest_rate, minimum_payment, creditor, account_number, notes, deferment_end_date, subsidized` |
| `GET /export/csv/assets` | Asset fields including loan and escrow columns |
| `POST /import/csv/transactions` | Reads `date, description, amount, transaction_type`; runs payment auto-linking. |
| `POST /import/csv/recurring` | Reads `description, amount, frequency, start_date, end_date, transaction_type`. |
| `POST /import/csv/debts` | Reads the debt export columns. |
| `POST /import/csv/assets` | Reads the asset export columns. |

Each CSV import accepts `clear=true` (form field or query string) to **drop
the collection first**. CSV round trips are lossy: transaction imports ignore
`account_id` (rows land on the primary account), and the recurring export
omits category, `day_of_month` and account. Use the JSON backup for a full
restore. Holdings have their own brokerage CSV import on the portfolio page
([portfolio.md](portfolio.md)).

## Reports (`/reports`)

The AI Advisor page shows the latest weekly briefing, monthly review and
portfolio review, a history of past reports (`/reports/<id>`), and a
"Regenerate now" form (`POST /api/reports/generate`) that runs generation in
the background and polls `GET /api/reports/status` until it finishes. How
reports are built, scheduled and configured is in
[advisor-agent.md](advisor-agent.md).

## Health check

`GET /health` pings MongoDB and returns `200 {"status": "healthy", ...}` or
`503` when the database is unreachable. Docker Compose, Kubernetes probes and
CI use it ([deployment.md](deployment.md)).
