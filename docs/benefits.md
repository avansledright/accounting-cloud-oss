# Tax Year and Card Perks (`/benefits`)

The `/benefits` page answers one question before December 31: is any money
being left on the table? It does two related jobs:

1. **Contribution limits.** HSA, 401(k) and IRA accounts are matched to an IRS
   annual limit, year-to-date contributions are measured, and the pace needed
   to max out before the deadline is shown, along with employer-match status.
2. **Card perks.** Recurring statement credits on premium credit cards are
   tracked per reset period, so unused credits can be flagged before they
   expire, and each card's annual fee is compared with the credits actually
   used.

All calculation lives in `benefits.py` as pure functions over plain dicts.
Routes and persistence live in `app.py` (search for
`TAX-ADVANTAGED BENEFITS & CARD PERKS`).

Related: [credit-cards.md](credit-cards.md) (where `cc_cards` and
`cc_transactions` come from), [portfolio.md](portfolio.md) (the investment
accounts themselves).

## Collections

| Collection | Shape | Purpose |
|---|---|---|
| `benefit_settings` | singleton | HSA coverage tier, catch-up flags, salary, pay periods, per-year limit overrides |
| `benefit_accounts` | one per linked account | Limit bucket, per-paycheck election, employer match formula |
| `contribution_overrides` | one per (account, year) | Authoritative YTD figure from a paystub plus its as-of date |
| `cc_cards` | one per card | Annual fee, fee month, `perks` (credits), `benefits` (non-credit perks), `template_key` |
| `cc_perk_usage` | one per (card, perk, period) | Dollars redeemed, entered by hand |

## Routes

| Route | Function | Purpose |
|---|---|---|
| `GET /benefits?year=YYYY` | `benefits_page` | The page; year defaults to the current year (accepted range 2000-2100) |
| `POST /benefits/settings` | `benefits_settings_save` | Coverage tier, catch-up flags, salary, pay periods, limit overrides for the selected year |
| `POST /benefits/account-config` | `benefits_account_config` | Link an account to a bucket; per-paycheck amount; match formula |
| `POST /benefits/contribution` | `benefits_contribution_save` | Save or clear (`clear=1`) a paystub YTD figure |
| `POST /benefits/card` | `benefits_card_save` | Card annual fee and fee month |
| `POST /benefits/perk` | `benefits_perk_save` | Add, edit or delete (`delete=1`) one credit |
| `POST /benefits/perk-template` | `benefits_perk_template` | Load a template onto a card (replaces its credits, benefits and annual fee) |
| `POST /benefits/card-benefit` | `benefits_card_benefit` | Add, edit or delete one non-credit benefit |
| `POST /benefits/perk-usage` | `benefits_perk_usage` | Record dollars used for one perk period; `amount_used <= 0` deletes the record |

The dashboard also calls `_benefit_context(today.year)` and shows up to four
high or medium severity alerts in its "Tax Year & Perks" card.

---

## Part 1: Contribution limits

### Seed limits and overrides

`benefits.CONTRIBUTION_LIMITS` holds IRS figures by calendar year:

| Year | 401(k) base | 401(k) catch-up (50+) | IRA base | IRA catch-up (50+) | HSA self | HSA family | HSA catch-up (55+) |
|---|---|---|---|---|---|---|---|
| 2025 | 23,500 | 7,500 | 7,000 | 1,000 | 4,300 | 8,550 | 1,000 |
| 2026 | 24,500 | 8,000 | 7,500 | 1,100 | 4,400 | 8,750 | 1,000 |

These are **seed defaults only**. Every bucket can be overridden per year from
the settings form on the page; overrides are stored in
`benefit_settings.limit_overrides` as `{"2027": {"401k": 25000.0, ...}}`. A
blank input clears that override. When the IRS publishes new limits, enter them
in the UI; no code change or deploy is needed. If a year has neither seed data
nor an override, `limit_for()` returns `None` and the bucket shows no limit.

`limit_for(kind, year, settings)` resolves a limit in this order:

1. A per-year override, used as-is (catch-up is **not** added on top; enter the
   full figure you want).
2. Otherwise the seed table:
   - `hsa`: the `self` or `family` figure per `hsa_coverage`, plus `catchup_55`
     if `age_55_plus`.
   - `401k` and `ira`: `base`, plus `catchup_50` if `age_50_plus`.

### Settings (`benefit_settings`)

| Field | Default | Meaning |
|---|---|---|
| `hsa_coverage` | `self` | `self` or `family` HSA tier |
| `age_50_plus` | false | Adds the 401(k) and IRA catch-up |
| `age_55_plus` | false | Adds the HSA catch-up |
| `annual_salary` | none | Needed for employer-match math |
| `pay_periods_per_year` | 26 | Used to count remaining paychecks |
| `limit_overrides` | `{}` | Per-year, per-bucket overrides |

`get_settings()` merges the stored doc over `DEFAULT_SETTINGS` and sanitises
values.

### Limit buckets

| Bucket key | Label | Default account types |
|---|---|---|
| `401k` | 401(k) elective deferral | `401k` |
| `ira` | IRA (Traditional + Roth combined) | `ira`, `roth_ira` |
| `hsa` | HSA | `hsa` |
| `taxable_retirement` | Retirement savings, taxable (no IRS limit) | none (opt-in) |

Traditional and Roth IRAs deliberately **share one `ira` bucket**, because the
IRS limit is combined across both. Contributions from every account in a bucket
are summed against a single limit.

`taxable_retirement` (`UNCAPPED_KIND`) is for a brokerage account earmarked for
retirement. Its contributions are counted and shown, but it has no limit, no
deadline and no urgency, and it is never mixed into capped totals.

### Linking accounts (`benefit_accounts`)

Any account whose type has a default bucket, plus any investment account (e.g.
`brokerage`, `deferred_comp`), appears in the linking table. Per account you
can set:

| Field | Meaning |
|---|---|
| `limit_kind` | One of the bucket keys above, or `none` to exclude the account. Blank falls back to the type default. |
| `per_paycheck` | Your payroll election in dollars per paycheck. Used to project what the current schedule will still add this year. |
| `match_rate_pct` | Employer match rate, e.g. `50` for 50 cents per dollar (defaults to 100 if blank) |
| `match_limit_pct` | Percent of salary the employer matches up to, e.g. `6` |

### Measuring contributions

`auto_contributions()` counts money moving **into** the account during the
calendar year:

- `transfers` whose `to_account_id` is the account, and
- positive `transactions` booked against the account.

A verified transfer is stored both as a `transfers` doc and as mirror
`transactions` docs with no id linking them. To avoid double counting, a
transaction is skipped if a transfer with the same date and amount was already
counted, or if its description starts with `Transfer from:`.

Payroll deductions (401(k), HSA through a cafeteria plan) never touch the
ledger, so they are invisible to this count. That is what overrides are for.

### Paystub overrides (`contribution_overrides`)

Enter the YTD figure from your latest paystub and the paystub date (`as_of`).
For that account and year:

```
contributed = ytd_amount + (auto-detected activity strictly after as_of)
```

The entry's `source` becomes `manual` or `manual+auto`. Without an override it
is `auto`. Post `clear=1` to delete the override.

### Group figures

`build_contribution_groups()` returns one group per bucket in use:

| Field | Meaning |
|---|---|
| `limit`, `contributed`, `remaining`, `over`, `pct` | Progress against the limit |
| `deadline` | `contribution_deadline()`: Dec 31 for `401k` (payroll must run in the year); Apr 15 of the following year for `ira` and `hsa` |
| `days_left` | Days until the deadline |
| `periods_left` | 401(k) only: `pay_periods_per_year x (fraction of the year left)` |
| `per_period_needed` | `remaining / periods_left` |
| `scheduled_remaining` | Sum of `per_paycheck` across the bucket's accounts x `periods_left` |
| `entries` | Per-account detail, including the five most recent detected contributions |

### Employer match status

`employer_match_status()` returns `None` unless a positive `annual_salary` is set
and some 401(k) account has `match_limit_pct`. Otherwise:

```
needed_contribution = salary x match_limit_pct / 100
shortfall           = max(0, needed_contribution - 401k contributed)
match_at_risk       = shortfall x match_rate_pct / 100
captured            = shortfall <= 0
```

This compares **year-to-date** contributions with the full-year amount needed,
so early in the year it will usually show a shortfall even when your election
is on track.

### Contribution alerts

`build_alerts()` produces, per capped bucket:

| Condition | Severity |
|---|---|
| Over the limit | high ("Excess contributions are taxable until withdrawn") |
| Maxed out | info |
| Room left, payroll schedule covers it | by days to deadline: high <= 14, med <= 45, otherwise info |
| Room left and `scheduled_remaining < remaining` | high if <= 90 days left, otherwise med |

Uncapped buckets get one informational line. An uncaptured employer match is
always high.

---

## Part 2: Card perks

### Data model

A card in `cc_cards` (created on the credit-cards page, see
[credit-cards.md](credit-cards.md)) gains these fields on the benefits page:

| Field | Meaning |
|---|---|
| `annual_fee` | Dollars per year |
| `fee_month` | Month (1-12) the annual fee posts. Anchors the cardmember year. Defaults to January when unset. |
| `perks` | List of statement credits (below) |
| `benefits` | List of non-credit benefits (below) |
| `template_key` | Which template was loaded, so detection can fall back to its merchant patterns |

Each **perk** (credit):

| Field | Meaning |
|---|---|
| `id` | Random hex id, stable across edits; usage rows point at it |
| `name`, `notes` | Display |
| `amount` | Dollars per period |
| `cadence` | `monthly`, `quarterly`, `semiannual` or `annual` |
| `reset_basis` | `calendar` or `anniversary` (cardmember year) |
| `amount_by_period` | Optional per-period overrides keyed by the period suffix (`'12'` for December, `'Q2'`, `'H1'`) |
| `ends_on` | Optional `YYYY-MM-DD` hard sunset for promos |
| `match`, `match_exclude` | Optional merchant substrings used to detect usage (template-only today) |

### Reset periods

`period_key()` produces a stable key for the window a date falls in:

| Cadence | Calendar key | Cardmember-year key |
|---|---|---|
| monthly | `2026-03` | same as calendar |
| quarterly | `2026-Q2` | same as calendar |
| semiannual | `2026-H1` | `2026A09-H1` |
| annual | `2026` | `2026A09` |

`reset_basis` only matters for `semiannual` and `annual` (issuers run monthly
and quarterly credits on the calendar either way). An anniversary key embeds
the year the cardmember year began and its anchor month (`A09` = September,
from `fee_month`), so a stored key stays interpretable even if `fee_month` is
later corrected. `period_bounds()` turns a key back into start and end dates;
`periods_in_year()` lists every period overlapping a calendar year, which for
anniversary credits includes the cardmember year that started last year.

### Varying amounts and sunsets

- **`amount_by_period`**: for credits that are not the same size every period.
  The Amex Uber Cash template is `$15` monthly with `{'12': 35.0}`, i.e. $35 in
  December, for $200 a year. `amount_for_period()` applies it.
- **`ends_on`**: periods that start after this date are **dropped**, not
  reported as forfeited. Detection also ignores charges after it. This keeps the
  page from nagging about money that no longer exists.

### Detecting usage from statements

`detect_perk_usage(card, transactions, year)` derives usage from qualifying
**charges** in `cc_transactions` rather than asking you to tick boxes:

1. Takes positive-amount rows for the card (matched by `card_name`) in the year.
2. For each perk with match patterns, sums charges whose upper-cased
   description contains any `match` substring and none of the `match_exclude`
   substrings, grouped by period.
3. Caps each period at the credit's value for that period.

The charge is used as the signal because many CSV exports omit the statement
credit line. This is evidence of qualifying spend, not proof the issuer paid.
Detected cells are shown dotted; clicking one confirms it.

Patterns come from the perk's own `match` list, or, failing that, from the
template entry with the same name (looked up via `template_key` or
`suggest_template()`). This means cards loaded before patterns were added, or
before a pattern was improved, still get detection without rewriting stored perk
ids.

### Manual usage (`cc_perk_usage`)

`POST /benefits/perk-usage` with `card_id`, `perk_id`, `period` and
`amount_used` upserts one document keyed by **card + perk + period**. A manual
entry always overrides detection for that period. An amount of zero or less
deletes the record and returns the cell to detection. Deleting a perk deletes
its usage rows.

### Card figures

`build_card_perks()` computes per card:

| Figure | Meaning |
|---|---|
| Cell `state` | `used`, `partial`, `missed` (period ended), `open` (current), `future` |
| `captured` | Dollars used across all displayed periods |
| `forfeited` | Unused value of periods that have ended |
| `available_now` | Unused value of current periods |
| `potential` | Full-year value of all credits |
| `net` | `captured - annual_fee` |
| `breakeven_pct` | `captured / annual_fee`, capped at 100 |

### Non-credit benefits

`cc_cards.benefits` holds perks that are not statement credits: lounge access,
elite status, earning rates, insurance and other notes, grouped by
`BENEFIT_CATEGORIES` (`lounge`, `status`, `earning`, `protection`, `other`).

These are **reference only**. They have no reset period and nothing to forfeit,
so they **never** contribute to `captured`, `forfeited`, `available_now`,
`potential` or `net`, and they never generate alerts. Scoring them against the
fee would make a card look like it pays for itself on benefits you cannot spend.
If you contribute code here, keep them out of every figure.

### Templates (`PERK_TEMPLATES`)

`benefits.PERK_TEMPLATES` ships starter sets for three cards:
`amex_platinum`, `chase_sapphire_preferred` and `citi_aadvantage_executive`.
Each has a `label`, `annual_fee`, `checked` date, `source`, a `perks` list and a
`benefits` list. `TEMPLATE_TOTALS` precomputes each template's annual credit
value (honouring `amount_by_period`).

**Templates go stale fast.** Issuers add, drop and reprice credits several times
a year, change fees, and move credits between calendar and cardmember years.
The figures shipped in the repo were researched against the issuers' own
product pages and carry the month they were checked; the page shows that date
next to every template. Treat a loaded template as a draft to reconcile against
your own card's terms, not as a source of truth.

Rules for contributors:

- **Re-check every template against the issuer's current product and benefit
  pages** before changing it, and update `checked` and `source`.
- **Never fill or "fix" a template from memory** or from a model's recollection.
  These numbers drive real spending decisions.
- Business and consumer versions of a card have different credits; do not mix
  them.
- Network promotions (for example Mastercard World Elite offers on the Citi
  card) are modelled as ordinary credits with `ends_on`.

**Loading.** `POST /benefits/perk-template` replaces the card's `perks`,
`benefits` and `annual_fee` with the template, assigns fresh ids, defaults
`reset_basis` to `calendar`, and records `template_key`. Existing usage rows for
the old perk ids are orphaned.

**Inline suggestion.** A card with no credits shows a one-click "Load ...
credits" button for its likely template, chosen by `suggest_template()` from the
card's name and issuer (`platinum` + `amex`/`american express`, `sapphire`, or
`aadvantage`/`executive`/`citi`). The button sits right on the empty card rather
than behind a menu, so an empty card reads as "not loaded yet" rather than as
missing data. The full template picker is under "Card settings & add a credit".

### Why some credits are excluded

Credits with **no fixed reset** are deliberately left out of the templates:

- Global Entry / TSA PreCheck: a rolling window (about four years) from last
  use, with no calendar or anniversary anchor.
- Purchase-gated offers (e.g. a credit that only triggers with one specific
  large purchase).
- Waived memberships and one-time trials, which are not money back.
- Targeted, activation-based offers (Amex Offers, Chase Offers) with individual
  expiry dates.

Modelling any of these as periodic would produce false "expiring" alerts every
period. Where useful they are listed as non-credit `benefits` instead.

### Annual fee and fee month

`annual_fee` and `fee_month` are set on the card's settings form
(`POST /benefits/card`), and `annual_fee` is also set when a template is loaded.
Set `fee_month` to the month the fee actually posts on your statement (search
your imported transactions for `ANNUAL FEE`, which the keyword rules categorise
as `Fees & Interest`); anniversary-basis credits are anchored to it.

### Perk alerts

Per card, for each credit's current period with money outstanding:

| Days left in period | Alert |
|---|---|
| <= 7 | high, own line |
| 8-21 | med, own line |
| > 21 | rolled into one info line per card ("$X across N credits still open") |

Plus, if `captured < annual_fee`, a fee-gap alert: med when 120 days or fewer
remain in the calendar year, otherwise info. All alerts are sorted high, med,
info.
