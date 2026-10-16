# Home Move Planner

The home move planner (`/tools/home-move`) estimates "sell my home for X, buy
another for Y" end to end: net proceeds from the sale, cash to close on the
purchase, the new monthly payment, a loan amortization summary and, for
adjustable-rate loans, payment scenarios under the rate caps. It can also run in
reverse and find the purchase price that hits a target monthly payment.

The calculation lives in `home_move.py` (pure functions plus a rate fetcher).
The routes, prefill logic and saved scenarios live in `app.py` under
`HOME MOVE PLANNER`.

Related: [features.md](features.md) (assets and their mortgages, which feed the
prefill), [architecture.md](architecture.md).

> **Closing customs are local.** Who pays transfer taxes and title insurance,
> what those cost, and whether property tax is billed in arrears differ by
> state, county and even city. The defaults below are placeholders, not advice.
> Set `seller_transfer_tax_per_1000`, `buyer_transfer_tax_per_1000`,
> `seller_title_insurance`, `lender_title_policy`, `tax_proration_pct` and the
> fee inputs for your own area, ideally from a real title or escrow quote.

## Routes

| Route | Function | Purpose |
|---|---|---|
| `GET /tools/home-move` | `home_move_page` | Form and results. All inputs are query parameters, so a result URL is shareable and bookmarkable. `?scenario=<id>` loads a saved scenario; `?refresh_rates=1` bypasses the rate cache. |
| `POST /tools/home-move/save` | `home_move_save` | Saves the current inputs as a named scenario, then redirects to it |
| `POST /tools/home-move/delete/<scenario_id>` | `home_move_delete` | Deletes a saved scenario |

A result is computed only when at least one of `purchase_price`, `sale_price`
or `target_payment` is non-zero.

## Request flow

1. `_home_move_current_homes()` loads every asset with
   `asset_type: 'real_estate'`. For each it computes the current mortgage payoff
   (the amortized liability balance via `_decorate_asset_liability`), the
   current monthly payment (`monthly_payment` plus escrow from
   `_calculate_monthly_escrow`), the estimated value and the annual property
   tax.
2. `fetch_mortgage_rates()` returns current rates (cached; see below).
3. Inputs come from the query string, or from the saved scenario's `params`.
   `parse_params()` walks `DEFAULTS`, parses each value (commas, `$` and `%` are
   stripped; blanks fall back to the default), and forces an unknown
   `loan_type` back to `fixed30`.
4. **Prefill:** if `sale_price` is zero and at least one real-estate asset
   exists, the first one fills `sale_price`, `mortgage_payoff`,
   `current_monthly_housing` and `current_tax_annual`. The form also has a
   "Prefill from" dropdown listing every property, which fills the same fields
   client-side.
5. Monthly income for the housing-ratio check is the sum of every active
   recurring income item (queried with `ACTIVE_RECURRING`, so archived lines
   are excluded) converted to a monthly amount with `calc_monthly_amount()`
   (`_home_move_monthly_income`).
6. If `solve_for == 'payment'` and `target_payment > 0`,
   `solve_price_for_payment()` runs; otherwise `calculate_home_move()`. When
   the reverse solve runs with `target_basis == 'year1'`,
   `compare_buydowns()` also runs (see
   [Comparing buydowns](#comparing-buydowns-compare_buydowns)).

## The calculation (`calculate_home_move`)

### Closing date

`closing_date` (ISO) or, if blank or invalid, today + 60 days. The sale and the
purchase are assumed to close the same day.

### Sale side

| Line | Formula |
|---|---|
| Agent commission | `sale_price x commission_pct / 100` |
| Transfer taxes (seller) | `sale_price / 1000 x seller_transfer_tax_per_1000` |
| Owner's title insurance | `seller_title_insurance`, or `estimate_owner_title_policy(sale_price)` when blank |
| Survey | `seller_survey` |
| Property tax proration credit to buyer | `tax_proration_credit` if given, else `current_tax_annual x tax_proration_pct / 100 x days_elapsed / 365`, where `days_elapsed` is the day of the year of the closing date |
| Misc | `seller_misc` |

`net_proceeds = sale_price - mortgage_payoff - total seller costs`. Equity
(`sale_price - mortgage_payoff`) and seller costs as a percentage of the price
are also reported.

`estimate_owner_title_policy()` is a rough tiered approximation (about $1,700
for the first $250,000 plus $2.25 per $1,000 above that). Title premiums are
state-regulated; override it with a quote.

The proration models areas where property tax is **billed in arrears**: the
seller credits the buyer for the current year's taxes accrued up to closing.
Where taxes are paid in advance, set `tax_proration_pct` to `0` (or enter the
actual credit in `tax_proration_credit`).

### Purchase side

- **Rate.** `rate` if entered, else the live rate for the loan type
  (`LOAN_TYPES[loan_type]['rate_key']`: `rate30`, `rate15` or `arm`).
- **Property tax.** `tax_mode = 'rate'`: `purchase_price x tax_rate_pct / 100`;
  `'amount'`: `tax_annual`.
- **Insurance.** Same pattern with `insurance_mode`, `insurance_rate_pct`,
  `insurance_annual`.
- **Down payment.** See "Down-payment modes" below.
- `loan = purchase_price - down`; LTV and down-payment percentage are reported.

Buyer closing costs:

| Line | Formula |
|---|---|
| Lender origination / underwriting | `lender_origination` |
| Discount points | `loan x points_pct / 100` |
| Rate buydown escrow | temporary buydown cost, only when `buydown_payer == 'buyer'` |
| Appraisal | `appraisal` |
| Credit report, flood cert, tax service | `lender_misc` |
| Lender's title policy | `lender_title_policy` |
| Title settlement / closing fee | `closing_escrow_fee` |
| Recording fees | `recording_fees` |
| Buyer transfer taxes | `purchase_price / 1000 x buyer_transfer_tax_per_1000` |
| Inspection | `inspection` |
| Misc | `buyer_misc` |

Prepaids:

| Line | Formula |
|---|---|
| Prepaid interest | `loan x rate / 100 / 365 x days from closing to the first of next month` |
| Homeowner's insurance | `annual insurance / 12 x prepaid_insurance_months` |
| Initial escrow reserve | `(monthly tax + monthly insurance) x escrow_reserve_months` |

```
cash_to_close  = down + closing costs + prepaids - seller_credit
cash_available = net_proceeds + extra_cash
cash_after     = cash_available - cash_to_close     (negative => shortfall)
```

### Down-payment modes (`down_mode`)

| Mode | Down payment |
|---|---|
| `pct` (default) | `purchase_price x down_payment_pct / 100` |
| `amount` | `down_payment_amount`, capped at the price |
| `proceeds` | Every available dollar: `cash_available - closing costs`, capped at the price |

`proceeds` mode has a circular dependency: discount points, prepaid interest and
a buyer-paid buydown all depend on the loan size, which depends on the down
payment. The code solves it by **fixed-point iteration**: start from
`cash_available - fixed costs`, then five times recompute
`down = cash_available - fixed costs - loan_dependent_costs(price - down)`. It
converges within a couple of passes in practice.

### Monthly payment

```
P&I  = payment_for(loan, rate, term)            (standard amortizing payment)
PMI  = loan x pmi_rate_pct / 100 / 12           only when LTV > 80%
PITI = P&I + tax/12 + insurance/12 + PMI + hoa_monthly
```

`monthly.by_year` lists the total payment year by year, each entry
`{label, rate, total}`: with a buydown, `Year 1` (and `Year 2` for a 2-1) at
the reduced rate, then `Year 2+` or `Year 3+` at the note rate; without one, a
single `Every year` entry. `monthly.first_year_total` is the first entry's
total (equal to `monthly.total` when there is no buydown).

The result compares PITI with `current_monthly_housing` (monthly and annual
difference) and, when budgeted income exists, reports a housing ratio
(PITI / monthly income). The page flags above 28% and warns above 36%.

PMI is a flat percentage of the original loan and is not dropped when LTV later
falls below 80%.

### Loan types

| Key | Label | Term | Fixed years | Live rate used |
|---|---|---|---|---|
| `fixed30` | 30-year fixed | 360 | - | `rate30` |
| `fixed15` | 15-year fixed | 180 | - | `rate15` |
| `arm5` | 5/1 ARM (30-yr) | 360 | 5 | `arm` |
| `arm7` | 7/1 ARM (30-yr) | 360 | 7 | `arm` |
| `arm10` | 10/1 ARM (30-yr) | 360 | 10 | `arm` |

### Amortization

`_yearly_schedule()` amortizes month by month and returns one row per year
(principal, interest, ending balance, rate), total interest and, for ARMs, the
payment and balance at the adjustment. The result also includes total paid,
payoff year (`closing year + term in years`) and the balance after year 5. The
page shows the first ten years with a toggle for the rest.

### ARM scenarios and caps

For an ARM, the intro rate holds for the fixed period. Then:

| Scenario | Rate after the fixed period |
|---|---|
| Expected | `arm_expected_rate`, or intro + 1.0 when blank; capped at intro + `arm_lifetime_cap` |
| Worst first adjustment | intro + `min(arm_first_cap, arm_lifetime_cap)` |
| Lifetime max | intro + `arm_lifetime_cap` |

At the adjustment the remaining balance is re-amortized over the remaining term.
The main amortization table uses the **expected** scenario. The worst-case
payments are reported alongside it with the balance at the first adjustment.

Each scenario applies a single rate change at the end of the fixed period. The
lifetime-max payment is computed as if the rate jumped straight to the cap at
that point; `arm_periodic_cap` is shown on the page as context but is not used
to step the rate up year by year.

### Temporary buydowns

`temporary_buydown()` models a 2-1 or 1-0 style buydown: the borrower pays P&I
at a reduced rate for year 1 (`note rate - buydown_year1`) and optionally year 2
(`note rate - buydown_year2`), while an escrow funded at closing covers the
difference. The loan still amortizes at the note rate.

```
cost = sum over buydown years of (full payment - reduced payment) x 12
```

The label is the year-1 and year-2 reductions, e.g. `2-1` or `1-0`. With
`buydown_payer = 'buyer'` the cost is added to closing costs (and included in
the `proceeds` fixed point). With `'seller'` it is a seller concession and does
not affect cash to close. The monthly figures reported as PITI are at the note
rate; the per-year reduced payments are listed with the buydown.

## Reverse solve: price for a payment (`solve_price_for_payment`)

With `solve_for = 'payment'` and a `target_payment` (total monthly PITI plus
HOA), the planner finds the highest purchase price whose payment does not exceed
the target, holding every other input fixed (rate, loan type, down-payment mode,
buydown, sale side and so on).

`target_basis` chooses which payment the target refers to:

| `target_basis` | Payment compared with the target |
|---|---|
| `note` (default) | `monthly.total`, the note-rate payment (year 3+ under a 2-1 buydown) |
| `year1` | `monthly.first_year_total`, the first-year payment under the buydown |

Total payment rises monotonically with price, so the function bisects between
$0 and `max_price` ($20,000,000) for up to 60 iterations or until the bracket is
narrower than $50, then rounds **down** to the nearest $1,000 so the result stays
under the target. If even `max_price` cannot reach the target, or the result is
zero, it returns `(None, None)` and the page falls back to a normal calculation
with whatever `purchase_price` was supplied.

With `target_basis = 'note'` a temporary buydown does not increase the solved
price. With `'year1'` the reduced first-year payment is matched, so a buydown
raises the affordable price; the page then states the payment the loan steps
up to once the buydown ends (and the year-2 payment for a 2-1), and notes that
lenders qualify a temporary buydown at the full note-rate payment.

### Comparing buydowns (`compare_buydowns`)

`compare_buydowns(p, rates, monthly_income)` re-runs the reverse solve with
`target_basis = 'year1'` for each entry in `BUYDOWN_OPTIONS` (none, 1-0 and
2-1), keeping every other input as entered. Each row has the label, the
maximum price, the loan, `by_year`, the note-rate total, the buydown cost and
cash to close; an option the solver cannot satisfy is left out. The page shows
it as a table (buydown, max price, year 1, year 2 and year 3+ payments,
buydown cost) when `solve_for = 'payment'` and `target_basis = 'year1'`.

## Live rates (`fetch_mortgage_rates`)

| Item | Value |
|---|---|
| Source | Freddie Mac Primary Mortgage Market Survey (PMMS) via FRED's public CSV endpoint (`fred.stlouisfed.org/graph/fredgraph.csv?id=<series>`). No API key. |
| Series | `MORTGAGE30US` (30-year fixed), `MORTGAGE15US` (15-year fixed) |
| Cache | In-process, 12 hours (`_RATE_TTL`), guarded by a lock. `?refresh_rates=1` forces a refetch. |
| Timeout | 8 seconds per request |
| Fallback | If the fetch fails or returns no 30-year value: `FALLBACK_RATES = {'rate30': 6.50, 'rate15': 5.85}`, flagged `live: False`, source "fallback defaults". If only the 15-year value is missing, the 15-year fallback is used alongside the live 30-year rate. |
| ARM rate | PMMS no longer publishes ARM rates, so `arm = rate30 + ARM_SPREAD_VS_30YR` (`-0.45`), and the result carries an `arm_note` saying it is an estimate |

`_fred_latest()` takes the most recent row with a numeric value (FRED marks
missing weeks with `.`). The returned dict carries `rate30`, `rate15`, `arm`,
`as_of` (the survey date), `source`, `live` and `arm_note`; the page shows the
rates, their date and a refresh link.

Update `FALLBACK_RATES` and `ARM_SPREAD_VS_30YR` in `home_move.py` if you run
the app without outbound internet access or when market spreads move.

## Saved scenarios (`home_move_scenarios`)

`POST /tools/home-move/save` re-parses the form, solves for price first if in
reverse mode, computes the result, and inserts:

| Field | Meaning |
|---|---|
| `name` | `scenario_name` from the form, or `Sell $X -> Buy $Y` |
| `params` | The full parsed input dict |
| `summary` | e.g. `$3,250/mo · cash to close $48,000` |
| `created_at` | Timestamp |

Saved scenarios appear as chips at the top of the page, newest first, each with
its summary as a tooltip and a delete button. Opening one re-runs the
calculation with **today's** live rate unless the scenario had an explicit
`rate`, so results can drift from when it was saved.

## Inputs (`DEFAULTS`)

Every input in `home_move.DEFAULTS`, with its default. `None` means "compute or
look up automatically".

### Current home / sale

| Input | Default | Meaning |
|---|---|---|
| `sale_price` | 0 | Expected sale price (prefilled from the first real-estate asset) |
| `mortgage_payoff` | 0 | Principal owed at closing (prefilled from the asset's amortized balance) |
| `current_monthly_housing` | 0 | Current P&I plus escrow, used only for comparison |
| `current_tax_annual` | 0 | Most recent annual tax bill on the current home; drives the proration credit |
| `commission_pct` | 5.0 | Total agent commission (listing plus buyer agent), percent of sale price |
| `seller_title_insurance` | None | Owner's title policy cost; None estimates it from price |
| `seller_survey` | 550 | Survey cost |
| `seller_misc` | 500 | Local certificates, lien release recording, wires, courier |
| `tax_proration_pct` | 100 | Percent of the last annual bill credited to the buyer, prorated to closing. Set 0 where taxes are paid in advance |
| `seller_transfer_tax_per_1000` | 1.0 | Seller-side transfer / stamp taxes per $1,000 of price. **Set for your area** |
| `tax_proration_credit` | None | Explicit proration credit; None computes it from bill x pct x days elapsed |
| `closing_date` | None | ISO date; None means 60 days from today |

### New home / purchase

| Input | Default | Meaning |
|---|---|---|
| `purchase_price` | 0 | Price of the new home |
| `solve_for` | `price` | `price` (normal) or `payment` (reverse: solve price from target payment) |
| `target_payment` | 0 | Desired total monthly payment (PITI plus HOA) when `solve_for = payment` |
| `target_basis` | `note` | Which payment `target_payment` means: `note` (note-rate payment, after any buydown) or `year1` (first-year payment with the buydown). Any other value becomes `note` |
| `down_mode` | `pct` | `pct`, `amount` or `proceeds` |
| `down_payment_pct` | 20.0 | Down payment percent in `pct` mode |
| `down_payment_amount` | 0 | Down payment dollars in `amount` mode |
| `extra_cash` | 0 | Additional cash from savings toward the deal |
| `loan_type` | `fixed30` | `fixed30`, `fixed15`, `arm5`, `arm7`, `arm10` |
| `rate` | None | Note rate in percent; None uses the live rate for the loan type |
| `points_pct` | 0.0 | Discount points, percent of loan |
| `buydown_year1` | 0.0 | Temporary buydown rate reduction in year 1 (e.g. 2 for a 2-1) |
| `buydown_year2` | 0.0 | Rate reduction in year 2 (e.g. 1 for a 2-1) |
| `buydown_payer` | `buyer` | `buyer` (added to closing costs) or `seller` (seller concession) |
| `arm_first_cap` | 2.0 | Maximum rate change at the first adjustment, percentage points |
| `arm_periodic_cap` | 1.0 | Maximum change at each later adjustment (displayed only; see above) |
| `arm_lifetime_cap` | 5.0 | Maximum increase over the intro rate, percentage points |
| `arm_expected_rate` | None | Expected post-adjustment rate; None means intro + 1.0 |
| `tax_mode` | `rate` | `rate` (percent of price) or `amount` |
| `tax_rate_pct` | 1.1 | Effective property tax rate, percent of price per year. **Set for your area** |
| `tax_annual` | 0 | Annual property tax in `amount` mode |
| `insurance_mode` | `rate` | `rate` or `amount` |
| `insurance_rate_pct` | 0.5 | Homeowner's insurance, percent of price per year |
| `insurance_annual` | 0 | Annual insurance in `amount` mode |
| `pmi_rate_pct` | 0.6 | PMI, percent of loan per year, applied when LTV > 80% |
| `hoa_monthly` | 0 | HOA dues per month |
| `lender_origination` | 1500 | Lender origination / underwriting fee |
| `appraisal` | 600 | Appraisal |
| `lender_misc` | 150 | Credit report, flood certification, tax service |
| `lender_title_policy` | 750 | Lender's title policy. **Set for your area** |
| `closing_escrow_fee` | 1200 | Title company settlement / closing fee, buyer share |
| `recording_fees` | 250 | Recording fees |
| `inspection` | 500 | Home inspection |
| `buyer_misc` | 300 | Other buyer costs |
| `escrow_reserve_months` | 3 | Initial escrow cushion, months of tax plus insurance (integer) |
| `prepaid_insurance_months` | 12 | Months of homeowner's insurance paid at closing (integer) |
| `seller_credit` | 0 | Concessions negotiated from the seller of the new home |
| `buyer_transfer_tax_per_1000` | 0.0 | Buyer-side transfer / stamp taxes per $1,000 of price (charged to the buyer in some places). **Set for your area** |

## Limitations

- The sale and purchase close on the same day; there is no bridge loan, rent-back
  or carrying-cost model.
- Seller proration ignores any unpaid prior-year tax balance.
- Capital gains on the sale are not estimated.
- Fees are flat inputs, not percentages, except where noted.
- The model is a planning estimate. Confirm everything with a lender's Loan
  Estimate and a title company's settlement statement.
