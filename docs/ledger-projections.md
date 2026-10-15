# Ledger and projections

The ledger combines what has happened (actual transactions) with what is scheduled to happen (projected occurrences of recurring items and recurring transfers) into a month-by-month running balance. The engine is `calculate_yearly_outlook()` in `app.py`. It powers the ledger page, the dashboard's cash-flow card, the inline outlook on cash account pages, and the what-if simulator.

See [budget.md](budget.md) for how recurring items are defined and [architecture.md](architecture.md#data-model) for the collections involved.

## Where the outlook is used

| Caller | Route | Arguments | Window |
|---|---|---|---|
| `ledger_page` | `/ledger` | start = account balance (or the total of all accounts for "All Accounts"), `account_filter` = selected account or `None`, `max_months_back=1` | Previous month, current month, and 12 more |
| `index` (dashboard) | `/` | start = primary account balance, filter = primary account, `max_months_back=0` | Current month and 12 more; the dashboard uses the first 4 |
| `account_detail` | `/accounts/<id>` (non-investment accounts) | start = that account's balance, filter = that account, `max_months_back=1` | Previous month, current month, and 12 more |
| `simulation_page` | `/simulation` | start = chosen start balance, filter = selected account or `None`, `max_months_back=0`, `sim=...` | Anchor month and 12 more |

The ledger's account selection is sticky: `resolve_account_filter()` stores an explicit `?account=` value in the session and falls back to the stored value, then the primary account, then `all`. Visiting an account detail page also stores that account as the selection.

## `calculate_yearly_outlook()`

```python
def calculate_yearly_outlook(start_balance=0.0, account_filter=None, max_months_back=None, sim=None)
```

| Argument | Meaning |
|---|---|
| `start_balance` | Balance the walk starts from (see [Starting balance](#starting-balance)) |
| `account_filter` | `None` (or `'all'`) for every account; otherwise an account id string. Only rows belonging to that account are included |
| `max_months_back` | How many months before the anchor month to show. `None` shows from January of the anchor year |
| `sim` | Optional in-memory what-if overlay; never written to the database (see [What-if simulation](#what-if-simulation)) |

It returns a list with one dict per month:

```python
{
    "month": "October 2026",        # month_start.strftime("%B %Y")
    "balance": 1234.56,             # running balance after the month's last row
    "min_balance": -12.34,          # lowest running balance within the month
    "transactions": [ ... ],        # rows in date order, each with running_balance
}
```

### Month window

The anchor is `sim['start_date']` if given, otherwise today.

- Months back: `-min(max_months_back, anchor.month - 1)` when `max_months_back` is set, otherwise `-(anchor.month - 1)` (back to January). Because of the `anchor.month - 1` cap, the window never reaches into the previous calendar year: in January, `max_months_back=1` shows no previous month.
- Months forward: always 12, inclusive. So the window covers the anchor month plus 12 following months, plus any months back.

### Starting balance

Before walking the window, every **actual** transaction dated before the first visible month (that passes the account filter) is applied to `start_balance`: income adds, expenses subtract. Projected occurrences from earlier months are not counted.

Callers pass the account's stored `balance`. Together with the step above, this means the stored balance behaves as an **opening balance**: every recorded transaction for the account, whatever its date, is applied on top of it. The ledger's per-account "calculated balance" follows the same rule (`balance` plus all transaction adjustments). Keep this in mind when you correct an account's balance to match the bank: transactions already recorded will still be applied to the new value.

### Rows for each month

For each month in the window the function builds `month_transactions` from three sources.

**1. Actual transactions** dated within the month. Each row:

| Key | Value |
|---|---|
| `_id` | Transaction id |
| `type` | `actual` |
| `amount` | Positive for income, negative for expenses |
| `account_id`, `account_name` | Owning account; a missing `account_id` means the primary account |
| `loan_ref`, `loan_label` | For expenses linked to a loan: `debt:<id>` / `asset:<id>` and the loan name (shown as a purple badge) |

**2. Projected recurring transactions** (from `recurring_transactions` filtered with `ACTIVE_RECURRING`, plus any `sim['additions']`). An item is considered for the month unless its `start_date` is after the month end or its `end_date` is before the month start. Occurrence dates:

- `weekly`: every day in the month with the same weekday as `start_date`, on or after `start_date`.
- `biweekly`: every 14 days from `start_date`, fast-forwarded into the month.
- `monthly`, `yearly` (only in `start_date`'s month), `bi-monthly` (only when the month distance from `start_date` is even): each day in `day_of_month`, clamped to the month length, defaulting to the 1st.
- Dates before `start_date` are dropped.

Each projected row carries `type: 'projected'`, `recurring_id`, `amount` (positive income, negative expense), `base_amount` (the unoverridden amount), `has_override`, `is_transfer: False`, and the account (the item's `account_id`, or the primary account).

**3. Projected recurring transfers** (from `recurring_transfers`). Dates are computed exactly as above. For every occurrence, up to two rows are emitted:

- Source side: description `Transfer to: <description>`, negative amount, `account_id` = `from_account_id`, shown when there is no filter or the filter is the source account.
- Destination side: description `Transfer from: <description>`, positive amount, `account_id` = `to_account_id`, shown when there is no filter or the filter is the destination account.

Both carry `is_transfer: True`, `transfer_from_id` and `transfer_to_id`. In the unfiltered view both sides appear and net to zero, which is correct for an all-accounts total.

### Per-occurrence overrides: `projected_overrides`

Before the walk, every document in `projected_overrides` is loaded into a dict keyed by `(str(recurring_id), date)`, where `date` is the `YYYY-MM-DD` string. When a projected row's `(recurring_id, date)` matches, its amount becomes the override (`has_override: True`) while `base_amount` keeps the regular amount. Overrides apply to recurring transactions and recurring transfers alike.

On the ledger, clicking a projected amount edits it in place and posts to `POST /projected/update-amount` (`update_projected_amount`) with `recurring_id`, `date` and `amount`, which upserts the override.

### Matching actuals to projections (dedup)

Once a payment actually happens, its projection must disappear, otherwise it would be counted twice. After sorting the month's rows by date, the function matches actual rows to projected rows **with the same description**:

1. **Pass 1, nearest within tolerance.** For each actual (carried actuals from the previous month first, then this month's, each in date order), find the nearest not-yet-cancelled projection with the same description. If it is within `DEDUP_TOLERANCE_DAYS = 7` days, cancel it. Otherwise, if the actual is dated in this month, keep it as a leftover.
2. **Pass 2, past projections.** Each leftover cancels the first still-unmatched projection with the same description that is dated today or earlier. A past actual never cancels a projection that is still upcoming, so when a biweekly draft shifts within a month, both the already-paid date and the new upcoming date stay visible.
3. **Carry forward.** A leftover that cancelled nothing is carried into the next month (`carried_actuals`), where it takes part only in pass 1. This covers a bill due on the 1st but paid on the 30th of the previous month. Carried actuals that still match nothing are dropped.

Because matching is by exact description, record actual transactions with the same description as the budget item (the "Verify" action does this for you, see below). Transfers recorded through the app use the `Transfer to:` / `Transfer from:` descriptions, so they dedup against projected transfers automatically.

### Skips: `skipped_transactions`

A skipped occurrence is stored as `{description, date}` with `date` as a `YYYY-MM-DD` string. Skips are written by:

- `POST /transactions/skip` (`skip_transaction`). For a transfer description it also records the mirror side (`Transfer to:` and `Transfer from:`).
- `DELETE /transactions/<id>` (`delete_transaction`), which records a skip for the deleted transaction's description and date so a projection it had realized does not reappear.

In the outlook, skips are counted per description per month (by the `YYYY-MM` prefix of the stored date). While building the filtered list, each surviving projected row whose description has a remaining skip count is dropped and the count decremented. Skips are therefore matched by description and month, not by exact date: skipping one date of a twice-monthly item removes the first remaining occurrence of that description in that month.

### Running balance and low point

The filtered rows are walked in date order. Projections cancelled by dedup and skipped projections are dropped, and projected loan payments are passed through the loan payoff simulation (next section). Every remaining row adds its amount to the running balance and records `running_balance` on the row.

`min_balance` starts at the balance carried in from the previous month (before the month's first row) and is lowered whenever a row takes the running balance lower. It is the "low point" used by:

- **Cash-flow warning banner** (`templates/index.html`): lists every month whose `min_balance` is below zero, with the low point and whether the month recovers by its end. It can be collapsed or dismissed (`static/main.js`).
- **Forecast cards** (`forecast-strip` in `index.html` and `simulation.html`): one card per month showing the ending balance, colored negative or positive, with a "Low" warning when the month dips below zero but ends positive. Clicking a card scrolls to and expands that month (`static/main.js`).
- **Low Point column** in the ledger month table, the dashboard outlook card and the account detail outlook. Months with a negative low point are auto-expanded on page load.
- **Dashboard banner**: `index()` takes the first four months (current plus next three) of the primary account's outlook and shows a warning when any has `min_balance < 0`.

### Ledger actions on rows

| Action | Route | Effect |
|---|---|---|
| Verify a projection | `GET/POST /transactions/verify` (`verify_transaction`) | Prefills a form from the projected row and records an actual transaction with the same description. For a projected transfer it creates a `transfers` document and the `Transfer to:` / `Transfer from:` pair |
| Skip a projection | `POST /transactions/skip` | Records a skip |
| Edit a projected amount | `POST /projected/update-amount` | Upserts a per-occurrence override |
| Link an actual to a loan | `POST /transactions/link-loan` (JSON `{transaction_id, loan_ref}`) | Sets `debt_id` or `asset_liability_id` (or clears both), then runs `_sync_payoff_archive()` |
| Edit or delete an actual | `/transactions/edit/<id>`, `DELETE /transactions/<id>` | Delete also records a skip |

`/ledger` also supports a filtered search mode (`search`, `date_from`, `date_to`, `amount_min`, `amount_max`, `type`) that lists matching actual transactions instead of the outlook. `/history` shows closed months (January through two months ago, or all of a past year) from actual transactions only.

## Loan payoff simulation

A loan's budget line (for example a "Car Payment" item that pays the "Demo Car Loan") would otherwise be projected forever. The outlook instead runs each projected loan payment against the loan's actual remaining balance, caps the occurrence that pays it off, and drops the ones after it.

### Linking payments to loans: `payment_match`

Standalone debts (`debts`) and asset loans (`assets` with `liability_balance > 0`) have a `payment_match` list of ledger descriptions, entered comma- or newline-separated on the debt or asset form and parsed by `_parse_payment_match()`.

- `_payment_match_index()` maps each normalized pattern (`_normalize_desc()`: trimmed, lowercased) to `('debt' | 'asset', id)`.
- `_auto_link_transaction()` tags a newly created transaction with `debt_id` or `asset_liability_id` when its description matches. It runs on `POST /transactions`, `/transactions/verify` and the transactions CSV import.
- `_apply_auto_links()` tags every unlinked expense whose description matches, then calls `_sync_payoff_archive()`. It runs on `/`, `/ledger`, `/budget`, `/debts`, `/goals`, and `POST /debts/auto-link`.
- `DEFAULT_PAYMENT_MATCHES` (empty by default) can seed patterns by loan name; `_seed_default_matches()` only fills loans that have none.
- Payments can also be linked or unlinked by hand from the debt and asset-loan detail pages and from the ledger row (`/transactions/link-loan`).

### Amortizing actual payments: `_amortize()`

```python
_amortize(starting_balance, annual_rate, monthly_escrow, payments, frequency='monthly')
```

The stored balance (`debts.balance`, `assets.liability_balance`) is the **starting** balance. `_amortize()` walks the linked payments in date order and splits each into interest, escrow and principal:

- `days` = days since the previous payment; for the first payment, a default of 30.44 days (14 for biweekly).
- `interest = remaining * rate * days / 365` (simple daily interest).
- `escrow = monthly_escrow * days / 30.44`.
- `principal = payment - interest - escrow`, floored at 0 and capped at the remaining balance.

It returns `current_balance`, `principal_paid`, `interest_paid`, `escrow_paid`, and per-payment `rows` with `balance_after`. `_decorate_debt()` uses it with no escrow; `_decorate_asset_liability()` passes `_calculate_monthly_escrow(asset)` (property tax / 12 + insurance / 12 + PMI + HOA) and the asset's `payment_frequency`. `_payoff_status()` reports a loan as paid off when its starting balance was positive and the current balance is at or below `PAID_OFF_EPSILON` (half a cent); the paid-off date is the first payment whose `balance_after` reached that threshold.

### Simulation state: `_loan_projection_state()`

`_loan_statuses()` decorates every debt and asset loan and returns `{ref, name, balance, rate, escrow, frequency, last_payment, paid_off, paid_off_date, patterns}`, where `balance` is the amortized current balance and `last_payment` the date of the latest linked payment.

`_loan_projection_state()` turns this into a dict from normalized `payment_match` pattern to a mutable state `{ref, name, balance, rate, escrow, frequency, last_payment}`. A pattern shared by two different loans is removed, since there is no way to tell which balance the payment reduces. The state is rebuilt on every outlook call.

### Applying projected payments: `_apply_projected_loan_payment()`

During the walk, a projected row is sent through the simulation when all of these hold:

- the month is on or after the first day of the **current** month (`loan_sim_floor`, based on today even when a simulation anchor is set),
- it is not a transfer,
- its amount is negative (an expense),
- its normalized description is a key in the loan state.

`_apply_projected_loan_payment(loan, txn)` then:

1. Returns `False` if the simulated balance is already at or below `PAID_OFF_EPSILON`; the outlook drops the row.
2. Computes days since `loan['last_payment']` (default 30.44, or 14 for biweekly), then interest and escrow the same way `_amortize()` does, and the payoff amount `balance + interest + escrow`.
3. Sets `txn['loan_label']` to the loan name.
4. If the projected payment is at least the payoff amount, it **caps** the row's amount to `-payoff_amount`, sets `txn['loan_payoff'] = True`, and sets the simulated balance to 0. The ledger shows this row with a green "Payoff · <loan>" badge.
5. Otherwise it reduces the simulated balance by `payment - interest - escrow` (never negative).
6. Records the row's date as `last_payment` and returns `True`.

Every later occurrence for that loan hits step 1 and is dropped. Because the simulation starts from the actual balance, a projected payoff that does not happen (a smaller real payment realizes that occurrence through dedup instead) leaves a balance behind, and the regular payment simply resumes the following month.

### Archiving paid-off loan lines

`_sync_payoff_archive()` keeps the budget honest once a loan is actually paid off, not just projected to be:

- For each loan with `payment_match` patterns, it builds a case-insensitive regex that matches expense recurring transactions whose description is exactly one of the patterns (surrounding whitespace allowed).
- **Paid off**: every matching non-archived line gets `archived: True`, `archived_reason: 'paid_off'`, `archived_loan_ref: '<debt|asset>:<id>'`, and `archived_at` set to the paid-off date (or today if unknown).
- **Not paid off** (for example a payment was unlinked or edited, or the starting balance was corrected upward): lines that this function archived for **this** loan (`archived_reason == 'paid_off'` and the same `archived_loan_ref`) have the four fields removed, restoring them.
- It returns `(archived, restored)` counts and is idempotent.

It runs from `_apply_auto_links()` and after every manual link or unlink (`/transactions/link-loan`, `/debts/<id>/link-transaction`, `/debts/<id>/unlink-transaction`, `/debts/asset/<id>/link-transaction`, `/debts/asset/<id>/unlink-transaction`).

Archived lines are excluded from the outlook, budget, calendar, upcoming bills and estimates through `ACTIVE_RECURRING = {'archived': {'$ne': True}}`. Every query over `recurring_transactions` that should only see live items must include it. See [budget.md](budget.md#archived-items-and-active_recurring).

## Escrow simulation configuration

The amortization page (`GET /assets/<id>/amortization`, `amortization_page`) simulates the escrow account for the first 36 payment periods of a loan whose asset has escrow components. Two environment variables choose the disbursement months, because counties and insurers differ:

| Variable | Default | Meaning |
|---|---|---|
| `ESCROW_PROPERTY_TAX_MONTHS` | `6,12` | Comma-separated months in which property tax is paid. The annual `property_tax_annual` is split evenly across them |
| `ESCROW_INSURANCE_MONTH` | `1` | Month in which the annual `homeowners_insurance_annual` premium is paid. Only the first value is used |

Both are parsed by `_parse_months()`: values outside 1-12 are ignored, and an empty or invalid value falls back to the default. `inject_escrow_schedule()` exposes them to templates as `escrow_tax_months_label` (for example `Jun & Dec`) and `escrow_insurance_month_label`.

In the simulation, each period deposits the per-period escrow (`_calculate_monthly_escrow(asset)`, halved for biweekly loans) starting from `escrow_balance`. In the first period that falls in a property-tax month, one installment is disbursed; in the first period of the insurance month, the annual premium is disbursed. Rows where the balance goes below zero are flagged as a shortage.

The amortization schedule itself starts from `liability_balance` treated as the current balance and projects from `max(payment_start_date, today)`, using the configured payment and frequency. This is separate from the payment-walk in `_amortize()` used by the debts pages and the loan payoff simulation.

## What-if simulation

`GET /simulation` (`simulation_page`) is a non-destructive simulator: it runs the outlook twice in memory, once as a baseline and once with your changes, and never writes to the database.

All changes are query parameters, so a scenario is just a URL:

| Parameter | Meaning |
|---|---|
| `account` | Account to simulate (sticky, like the ledger) or `all` |
| `amt_<id>` | New amount for a recurring transaction or recurring transfer |
| `off_<id>` | Any non-empty value turns that item off |
| `new_desc`, `new_amount`, `new_type`, `new_freq`, `new_day` | Parallel lists describing hypothetical new items (type `income` or `expense`, frequency default `monthly`, day default `1`) |
| `sim_mode` | `ledger` (default) or `budget` |
| `sim_start` | `YYYY-MM-DD`; anchors the 12-month window on that month instead of today |
| `sim_start_balance` | Overrides the starting balance |

The route builds the `sim` dict passed to `calculate_yearly_outlook()`:

| Key | Effect |
|---|---|
| `overrides` | `{recurring_id: amount}`; replaces the base amount of that item for every occurrence (per-date `projected_overrides` still take precedence in ledger mode) |
| `disabled` | Set of recurring ids skipped entirely |
| `additions` | Hypothetical recurring items (ids `sim-new-<n>`), starting on the first of the anchor month and attached to the account in view |
| `start_date` | Window anchor |
| `budget_only` | When true, actual transactions, skips and per-date overrides are ignored, so the projection is driven only by recurring items |

Starting balance: `sim_start_balance` if given; otherwise `0` in budget mode; otherwise the selected account's balance (or the total of all accounts). The baseline uses the same `start_date` and `budget_only` settings without the changes. The page shows the scenario's forecast cards and month table, a month-by-month comparison (`sim_balance`, `base_balance`, `delta`), and a summary of ending balance and lowest low point for both runs.

The loan payoff simulation still runs inside the simulator and always starts from the loans' actual balances.

## Inline 12-month outlook on cash account pages

`GET /accounts/<id>` (`account_detail`) computes `calculate_yearly_outlook(account['balance'], account_id, max_months_back=1)` for any account whose type is not in `INVESTMENT_ACCOUNT_TYPES` (so checking, savings and `other`). The "12-Month Outlook" table shows, per month, the ending balance, the low point (with a warning marker when negative), and income and expense totals summed from the month's rows in the template. A "Full Ledger" link opens `/ledger?account=<id>`. Investment accounts get the growth projection instead (see [portfolio.md](portfolio.md)).

## Agent and MCP outlook

The MCP tools `cash_flow_outlook` and `negative_balance_months` use `_generate_outlook()` in `agent/tools/cashflow.py`, a separate, simplified monthly estimate. It does not use actual transactions, dedup, skips, overrides or the loan payoff simulation, and it counts occurrences per month with fixed multipliers. Expect its numbers to differ from the ledger. See [mcp-server.md](mcp-server.md).
