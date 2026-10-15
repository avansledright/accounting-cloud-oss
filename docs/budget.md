# Budget

The budget is built from **recurring items** (bills, paychecks, and transfers that repeat on a schedule) plus **category targets** (how much you intend to spend per category each month). The same recurring items drive the ledger projections described in [ledger-projections.md](ledger-projections.md), so the budget page and the 12-month outlook always agree on what is planned.

All logic lives in `app.py`; the data shapes are in [architecture.md](architecture.md#data-model).

## Budget items: `recurring_transactions`

Each document in `recurring_transactions` is one budget item (model `RecurringTransaction` in `models.py`).

| Field | Notes |
|---|---|
| `description` | Name shown everywhere. Also used to match actual ledger transactions to projections and to match loan `payment_match` rules, so keep it identical to how the real transaction is recorded |
| `amount` | Positive amount per occurrence |
| `transaction_type` | `income` or `expense` |
| `frequency` | See below |
| `day_of_month` | Comma-separated list of days, for example `"1"` or `"1,15"` |
| `start_date` / `end_date` | Window in which the item is active (`end_date` optional) |
| `category` | Usually one of `CATEGORIES` (expenses) or `INCOME_CATEGORIES` (income) |
| `account_id` | Account the item posts to in projections; empty means the primary account |
| `auto_pay`, `payment_method`, `account_number`, `notes` | Informational |
| `archived`, `archived_reason`, `archived_loan_ref`, `archived_at` | Set automatically when the loan this item pays is paid off (see [Archived items](#archived-items-and-active_recurring)) |

### Frequency values

| Value | Meaning in projections | Uses `day_of_month`? |
|---|---|---|
| `weekly` | Every week on the weekday of `start_date` | No |
| `biweekly` | Every 14 days counted from `start_date` | No |
| `monthly` | Every month on each listed day | Yes |
| `bi-monthly` | Every **other** month (months where the distance from `start_date`'s month is even), on each listed day | Yes |
| `yearly` | Once a year in `start_date`'s month, on each listed day | Yes |

`day_of_month` values larger than the month length are clamped to the last day (31 becomes 30 in April). If the field is empty or unparseable, the 1st is used. Occurrences before `start_date` are dropped.

The add-item form on `/budget` offers all five frequencies and requires `day_of_month`. Note that the MCP write tools (`add_recurring`, `update_recurring`, `add_recurring_transfer` in `agent/tools/writes.py`) only accept `weekly`, `monthly`, `bi-monthly` and `yearly`.

### Monthly equivalent: `calc_monthly_amount()`

`calc_monthly_amount(amount, frequency, day_of_month)` converts one item to a monthly figure. It is used by the budget page, `seed_targets_from_recurring`, the transfers page, account detail pages, investment projections, goal estimates (`_monthly_expense_estimate`, `_monthly_income_estimate`) and the debt plan surplus.

Let `days` be the number of comma-separated entries in `day_of_month` (1 when empty):

| Frequency | Monthly amount |
|---|---|
| `weekly` | `amount * 52 / 12` |
| `biweekly` | `amount * 26 / 12` |
| `monthly` | `amount * days` |
| `bi-monthly` | `amount * days / 2` |
| `yearly` | `amount * days / 12` |
| anything else | `amount * days` |

Example: a paycheck of 2,000.00 with `frequency=monthly` and `day_of_month="1,15"` counts as 4,000.00 a month. The same paycheck entered as `biweekly` counts as 4,333.33 (26 checks a year).

## Recurring transfers as synthetic budget items

Recurring transfers (`recurring_transfers`, managed on `/transfers`) are not budget items in the database, but `budget_page()` turns each one into a synthetic item so money you move to savings or investments shows up in the plan:

- Category is chosen from the **destination** account's type: `savings` gives `Savings`; any type in `INVESTMENT_ACCOUNT_TYPES` gives `Investments`; anything else gives `Other`.
- The synthetic item is always an `expense`, with `auto_pay=True`, `is_transfer=True`, `account_id` set to the source account, and `notes` set to `"<from name> → <to name>"`.
- Its monthly amount (from `calc_monthly_amount`) is added to the category's planned total and to the monthly expense sum.
- In the item list, the Edit button for a transfer links to `/transfers` instead of the budget edit form, and there is no delete button for it on the budget page.

Income items are also credited to savings and investment categories: when an `income` item's `account_id` points at a `savings` account, its monthly amount is added to the `Savings` planned total; when it points at an investment account, to `Investments`. The item is still counted in its own category and in monthly income.

## Category targets: two layers

Targets say how much you plan to spend per category each month. There are two collections:

| Layer | Collection | Key | Written by |
|---|---|---|---|
| Default template | `budget_targets` | `category` | `POST /budget/targets/template` (`set_default_targets`), `POST /budget/targets/seed` (`seed_targets_from_recurring`), MCP `set_budget_target` with no month |
| Per-month override | `monthly_budget_targets` | `(month, category)`, `month` as `YYYY-MM` | `POST /budget/targets` (`set_budget_targets`), MCP `set_budget_target` with a month |

`monthly_budget_targets` has a **unique compound index on `(month, category)`**, created when `app.py` is imported:

```python
db.monthly_budget_targets.create_index([('month', 1), ('category', 1)], unique=True)
```

Resolution in `budget_page()` for the selected month:

```python
default_targets = {t['category']: t['target'] for t in db.budget_targets.find()}
overrides = {t['category']: t['target'] for t in db.monthly_budget_targets.find({'month': selected_month})}
budget_targets = {**default_targets, **overrides}
```

So an override wins for its month; every other category and month falls back to the template.

Editing targets on the page:

- **Save for YYYY-MM** posts every `target_<Category>` field to `/budget/targets` with the month. Non-empty values upsert an override; an empty value deletes that month's override so the category falls back to the template.
- **Save as Default Template** posts the same fields to `/budget/targets/template`. Non-empty values upsert the template; empty values delete the template entry.
- **Reset to Template** (shown only when the month has overrides) posts to `/budget/targets/reset`, which deletes every override for that month.
- Inputs whose value differs from the template get the `overridden` CSS class.

### Seeding targets from recurring items

`POST /budget/targets/seed` (`seed_targets_from_recurring`) is offered only while the template is empty. It sums `calc_monthly_amount()` of every active (non-archived) **expense** recurring transaction per category (missing category counts as `Other`) and upserts each sum, rounded to cents, into `budget_targets`. Recurring transfers are not included.

## Summary cards

The three summary cards and the annual overview on `/budget` mix two sources on purpose:

| Card | Source |
|---|---|
| Monthly Income | Sum of `calc_monthly_amount()` over active **income** recurring items |
| Monthly Expenses | Sum of **all resolved budget targets** for the month (template plus overrides) |
| Monthly Net | Income minus the target total |
| Annual Income / Expenses / Net | The monthly figures times 12 |

Expenses use targets because targets cover discretionary spending (groceries, dining) that has no recurring item, while income is predictable enough to take from the recurring items. Savings and Investments targets are part of the expense total, so money set aside reduces the net.

The page still computes recurring expense totals (`monthly_expenses`, including synthetic transfers) for the per-category "Planned" column, but they do not feed the summary cards.

## Category breakdown table

For each entry in `CATEGORIES`, the table shows:

| Column | Source |
|---|---|
| Planned | Monthly amounts of recurring items in that category, including synthetic transfers and the Savings/Investments income credit described above |
| CC Actual | Sum of positive `cc_transactions.amount` in that category for the selected statement month (`statement_date == selected month`) |
| Target | Resolved target, editable |
| Status | Compares the actual (or the planned amount when there is no card spending yet) against the target. For `Savings` and `Investments`, reaching the target is "On Track"; for every other category, staying at or below it is "On Track", above it is "Over Budget". No target shows "No Target" |

The month picker lists every distinct `statement_date` found in `cc_transactions` plus the current month. See [credit-cards.md](credit-cards.md) for how statements are imported and categorized.

## Categories

`models.py` defines the lists; nothing should re-spell them inline.

- **`CATEGORIES`** (20 entries) is the unified spending list shared by budget items and credit-card transactions: Dining, Groceries, Gas & Fuel, Travel, Shopping, Entertainment, Subscriptions, Utilities, Phone, Healthcare, Transportation, Home, Housing, Insurance, Debt Payments, Personal, Fees & Interest, Savings, Investments, Other.
- **`EXPENSE_CATEGORIES`** is an alias of `CATEGORIES`.
- **`CC_CATEGORIES`** is `CATEGORIES + ['Credit']`; `Credit` marks merchant credits and refunds on card statements.
- **`INCOME_CATEGORIES`**: Salary, Freelance, Investments, Rental Income, Other Income.

The item list groups budget items under each name in `CATEGORIES + INCOME_CATEGORIES`, followed by an "Uncategorized" section for items with no category. The add form's category dropdown lists `CATEGORIES`; the edit form (`edit_budget.html`) also receives `INCOME_CATEGORIES`.

## Archived items and `ACTIVE_RECURRING`

When the ledger shows that a loan has actually been paid off, the budget lines that pay it are archived automatically by `_sync_payoff_archive()`; when the loan reopens they are restored. The full mechanism is described in [ledger-projections.md](ledger-projections.md#archiving-paid-off-loan-lines). For the budget this means:

- An archived item has `archived: true`, `archived_reason: 'paid_off'`, `archived_loan_ref` (`debt:<id>` or `asset:<id>`), and `archived_at` (the payoff date).
- Every query that should only see live items filters with

  ```python
  ACTIVE_RECURRING = {'archived': {'$ne': True}}
  ```

  This includes the budget page, the ledger outlook, the simulator, account detail pages, investment projections, the dashboard's upcoming bills, the calendar, analytics, goal estimates and the debt plan surplus. The agent tools use the same filter literally. When you add a new query over `recurring_transactions`, include it unless you really want archived items.
- `/budget` lists archived items in a collapsed "Archived" section with the loan name and payoff date; they can still be deleted there. They do not count toward any total.
- `budget_page()` calls `_apply_auto_links()` first, so archiving is up to date whenever the page loads.

## The auto-managed "Extra Debt Payment" line

The debt payoff plan (`POST /debts/plan`, `save_debt_plan`) can maintain a recurring expense described `Extra Debt Payment` (category `Debt Payments`, monthly on the 1st, notes "Auto-managed by the debt payoff plan") so the extra payment is reserved in the budget and outlook. Its amount is updated when the plan is saved and the line is deleted when the plan stops requesting it. `_projected_monthly_surplus()` excludes this description so reserving the extra does not shrink the surplus that funds it. See [features.md](features.md).

## Routes

| Method and path | Function | What it does |
|---|---|---|
| `GET /budget?month=YYYY-MM` | `budget_page` | Budget page for a month (default current month) |
| `POST /budget/add` | `add_budget_item` | Create a recurring transaction from the form |
| `GET /budget/edit/<item_id>` | `edit_budget_item` | Edit form |
| `POST /budget/edit/<item_id>` | `edit_budget_item` | Replace the item's editable fields |
| `POST /budget/delete/<item_id>` | `delete_budget_item` | Delete (JSON response with `Accept: application/json`) |
| `POST /budget/targets` | `set_budget_targets` | Save per-month overrides |
| `POST /budget/targets/template` | `set_default_targets` | Save the default template |
| `POST /budget/targets/reset` | `reset_month_targets` | Remove all overrides for a month |
| `POST /budget/targets/seed` | `seed_targets_from_recurring` | Fill the template from recurring expenses |
| `GET /recurring_transactions` | `handle_recurring_transactions` | JSON list of all recurring transactions (archived included) |
| `POST /recurring_transactions` | `handle_recurring_transactions` | JSON create (description, amount, frequency, start_date, transaction_type, end_date, day_of_month) |
| `GET /export/csv/recurring` | `export_recurring_csv` | CSV of description, amount, frequency, start_date, end_date, transaction_type |
| `POST /import/csv/recurring` | `import_recurring_csv` | CSV import of the same columns (`clear=true` drops the collection first) |

Recurring transfers are managed on `/transfers` (`/transfers/recurring/add`, `/transfers/recurring/edit/<id>`, `/transfers/recurring/delete/<id>`).

Related MCP tools: `budget_status` and `recurring_items` (backed by `get_budget_status` and `get_recurring_items` in `agent/tools/budget.py`), and `set_budget_target`, `add_recurring`, `update_recurring`, `delete_recurring` (backed by `agent/tools/writes.py`). Note that `get_budget_status` compares targets against card spending by transaction `date`, while the budget page uses `statement_date`, and that the agent tools compute monthly equivalents themselves (treating `bi-monthly` as twice a month) instead of calling `calc_monthly_amount()`. See [mcp-server.md](mcp-server.md).
