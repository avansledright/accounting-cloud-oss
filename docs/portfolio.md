# Portfolio, Live Prices and Investment Projections

This page covers everything that deals with securities: holdings, the live
price service, dividends, the brokerage CSV import, the derived return rate for
self-managed accounts, and the growth projections shown on account detail and
net worth pages. All of it lives in `app.py`; the `Holding` and `Account` data
classes are in `models.py`.

Related: [architecture.md](architecture.md) (process model and collections),
[ledger-projections.md](ledger-projections.md) (cash-account projections, which
are a separate engine), [benefits.md](benefits.md) (contribution limits for the
same investment accounts).

## Accounts and management type

Investment accounts are any account whose `account_type` is in
`INVESTMENT_ACCOUNT_TYPES` (`models.py`):

```
ira, roth_ira, brokerage, 401k, hsa, deferred_comp
```

Each account also carries an optional `management_type`:

| Value | Meaning |
|---|---|
| `self_managed` | You hold individual positions. Holdings are tracked on `/portfolio`, and the account's `estimated_return_rate` is derived automatically from those holdings. |
| `managed` | A robo-advisor, target-date fund or employer plan you only see as a balance. No holdings; you set `estimated_return_rate` by hand. |
| unset | Treated like `managed` for return-rate purposes. |

Records created before `management_type` existed will not have the key;
`account_detail` calls `setdefault('management_type', None)` before rendering.

## Holdings

Stored in the `holdings` collection, one document per (symbol, account):

| Field | Notes |
|---|---|
| `account_id` | `ObjectId` of the investment account, or `None` for an unassigned holding |
| `symbol` | Upper-cased ticker. Must match `SYMBOL_PATTERN` (`^[A-Z0-9]{1,10}$`), checked by `validate_symbol()` |
| `quantity` | Share count (float) |
| `cost_basis` | Optional total cost basis |
| `date_added`, `notes` | Informational |

Routes:

| Route | Function | Behaviour |
|---|---|---|
| `GET /portfolio?account=<id\|all>` | `portfolio_page` | Holdings table, live values, dividend calendar and history |
| `POST /portfolio/add` | `add_holding` | If a holding already exists for the same symbol and account, the quantity is **added** (`$inc`); otherwise a new `Holding` is inserted |
| `POST /portfolio/update/<holding_id>` | `update_holding` | Sets `quantity` and/or `cost_basis`. Accepts form data or JSON; returns JSON when `Accept: application/json` |
| `POST /portfolio/delete/<holding_id>` | `delete_holding` | Deletes one holding |
| `POST /portfolio/delete-all` | `delete_all_holdings` | Deletes every holding, or only those of `account_id` if given; clears the price cache |
| `POST /portfolio/import-csv` | `import_holdings_csv` | Brokerage export import (see below) |
| `GET /api/stock/<symbol>` | `api_stock_data` | Returns the cached/live data dict for one symbol as JSON |

The portfolio page lists all self-managed investment accounts for assignment,
plus managed ones in the filter (managed accounts simply have no holdings).

## Live price data

### Sources

| Function | Role |
|---|---|
| `fetch_yahoo(symbol)` | **Primary.** Calls the Yahoo Finance chart endpoint (`query1.finance.yahoo.com/v8/finance/chart/<symbol>?range=1y&interval=1d&events=div`). No API key. |
| `fetch_alpha_vantage(symbol)` | **Fallback.** Only used when `ALPHA_VANTAGE_KEY` is set. Calls `GLOBAL_QUOTE` for the price and `TIME_SERIES_MONTHLY_ADJUSTED` for dividends paid in the last 12 months. |
| `fetch_symbol_data(symbol)` | Tries Yahoo, then Alpha Vantage. If both fail, returns a placeholder with `price: 0` and `valid: False` so the page still renders. |
| `get_live_data(symbols)` | Cached front door used by every caller. |

### What `fetch_yahoo` returns

| Key | Meaning |
|---|---|
| `price` | `meta.regularMarketPrice` |
| `dividend` | Sum of all dividend events in the 1-year window, per share (trailing annual dividend) |
| `div_months` | Sorted list of calendar months (1-12) in which a dividend was paid; drives the dividend calendar |
| `div_history` | List of `{date, amount_per_share}` for each dividend event, oldest first; drives dividend auto-recording |
| `trailing_return` | Percent change from the first to the last daily close in the 1-year window (price return only, dividends not reinvested) |
| `valid`, `source` | `True`, `'yahoo'` |

`fetch_alpha_vantage` returns `price`, `dividend`, `div_months`, `valid` and
`source: 'alphavantage'` only. It does **not** return `div_history` or
`trailing_return`, so when Yahoo is down dividends are not auto-recorded and
self-managed return rates are not refreshed.

Requests to Yahoo send a browser `User-Agent` (`STOCK_HEADERS`) and use a
10-second timeout.

### Cache

`STOCK_CACHE = {'data': {...}, 'timestamp': ...}` with
`STOCK_CACHE_TTL = 300` (5 minutes). `get_live_data(symbols)` returns cached
values only if the cache is younger than the TTL **and** every requested symbol
is present; otherwise it fetches every requested symbol, merges the results
into the cache, and resets the timestamp. Setting `STOCK_CACHE['timestamp'] = 0`
forces the next call to refetch; the CSV import and delete-all routes do this.

The cache is in process memory, which is one reason the container runs a single
gunicorn worker (see [deployment.md](deployment.md)).

### Daily background refresh

`schedule_stock_refresh()` is called once at module import and starts a daemon
thread. The thread sleeps until the next 17:00 (server local time; in the
default container that is UTC unless you set `TZ`), then collects every distinct
symbol in `holdings`, zeroes the cache timestamp, and calls `get_live_data()`.
Failures are logged and the loop continues to the next day.

## Dividends

Stored in the `dividends` collection: `{symbol, amount, date, source}` where
`amount` is the total dollars received (not per share) and `source` is `'api'`
or `'manual'`.

**Automatic recording.** Each time `/portfolio` renders, for every symbol in
the live data and every entry in its `div_history`, the page checks for an
existing `source: 'api'` record for the same symbol on the same day. If none
exists, it inserts one with
`amount = amount_per_share x (total quantity of that symbol across the holdings shown)`.
Because Yahoo's window is one year, this backfills the past 12 months the first
time a symbol is added.

**Manual entry.** `POST /portfolio/dividend/add` (`add_dividend`) records a
`source: 'manual'` dividend with a symbol, amount and date.
`POST /portfolio/dividend/delete/<dividend_id>` removes one.

The page shows:

- The 50 most recent dividend records.
- YTD dividends, summed from those records.
- Projected annual and monthly dividend income: `dividend x quantity` per holding.
- A 12-month dividend calendar: each holding's projected annual dividend split
  evenly across its `div_months`.

## Brokerage CSV import

Route: `POST /portfolio/import-csv` (`import_holdings_csv`). Form fields: `file`
(must end in `.csv`) and optional `account_id`.

1. **Encoding.** Decoded as `utf-8-sig`, so a byte-order mark from Excel or a
   brokerage export is stripped.
2. **Header auto-detection.** Brokerage exports often start with several lines
   of account information. The importer scans for the first line that contains
   the text `symbol` (case-insensitive) and a comma, and treats it as the header
   row. Everything above it is discarded.
3. **Column matching** (case-insensitive, per header):

   | Field | Header rule |
   |---|---|
   | Symbol | header is exactly `symbol`, `ticker` or `stock` |
   | Quantity | header contains `quantity`, `qty` or `shares` (so `Qty (Quantity)` works) |
   | Cost basis | header contains both `cost` and `basis` |

4. **Dollar parsing.** `parse_dollar()` strips `$`, commas and quotes, and
   treats `--`, `-` and empty as missing.
5. **Footer and non-stock rows.** There is no explicit footer detection: any
   row without a valid symbol (per `validate_symbol`) or a positive quantity is
   skipped. In practice this drops cash lines, "Account Total" rows and
   disclaimer text at the bottom of the export.
6. **Upsert.** An existing holding for the same symbol and account has its
   quantity **replaced** (not added to) and its cost basis replaced if the file
   has one. New symbols are inserted.
7. The price cache is cleared. Parse errors are logged and the user is
   redirected back to `/portfolio` without a message.

Note the difference from `/portfolio/add`, which increments quantity. The CSV
import is meant to be re-run with a fresh export to sync positions.

## Derived return rate for self-managed accounts

`derive_account_return_rate(account_id_str)`:

1. Loads the account's holdings and their live data.
2. Computes each holding's market value (`price x quantity`) and the total.
3. Computes the value-weighted average of `trailing_return` across holdings
   that have one.
4. If at least one holding had return data, rounds to two decimals, **writes it
   to `accounts.estimated_return_rate`**, and returns it. Otherwise returns
   `None` and leaves the stored rate alone.

It runs automatically:

- On `/accounts/<account_id>` for an investment account with
  `management_type == 'self_managed'`.
- On `/portfolio`, once for each self-managed account that appears among the
  displayed holdings.

Keep in mind that a trailing one-year price return can be very large or
negative, and it then becomes the compounding rate for long projections. For
managed accounts you set `estimated_return_rate` yourself on the account form.

## Investment projections

### `calculate_investment_projection(account, months=120)`

Projects one account forward month by month.

Inputs:

- Starting value: `account['balance']`.
- Annual rate: `account['estimated_return_rate']` (percent), converted to a
  monthly rate as `annual / 12 / 100` (simple division, not
  `(1 + r)^(1/12) - 1`).
- Monthly deposit, the sum of:
  - every active (`ACTIVE_RECURRING`, so not archived) recurring **income**
    transaction whose `account_id` is this account, and
  - every recurring **transfer** whose `to_account_id` is this account,

  each converted to a monthly amount by `calc_monthly_amount()` (weekly x 52/12,
  biweekly x 26/12, monthly x number of listed days, bi-monthly / 2, yearly / 12).

Each month after month 0:

```
growth   = balance * monthly_rate
balance += growth + monthly_deposit
```

So deposits land at the end of the month and start compounding the following
month. Recurring transfers **out** of the account and recurring expenses are not
subtracted.

Returns a list of `months + 1` points:

| Key | Meaning |
|---|---|
| `month_label` | e.g. `Mar 2027` |
| `month_index` | 0 = today |
| `projected_value` | Balance at that month |
| `total_deposits` | Cumulative deposits so far |
| `total_growth` | `projected_value - starting balance - total_deposits` |

### Account detail chart

On `/accounts/<account_id>`, if the account is an investment account and has a
non-zero `estimated_return_rate`, the page builds a projection over
`?horizon=` months (default 120). A value that is not an integer, or is
outside 1–600 months, falls back to 120 rather than raising an error. The
template offers a
**Projection Horizon** selector that reloads the page with:

| Option | Query string |
|---|---|
| 5 years | `?horizon=60` |
| 10 years (default) | `?horizon=120` |
| 20 years | `?horizon=240` |
| 30 years | `?horizon=360` |

Any other month count from 1 to 600 can be passed by hand. The page also lists recurring
items that touch the account (income and inbound transfers as deposits,
outbound transfers as withdrawals), with their monthly equivalents.

### Net worth 24-month projection

`/networth` (`networth_page`) shows a 24-month forward line when at least one
investment account has an `estimated_return_rate`. For each month `i` from 0 to
24:

- Every investment account with a rate contributes the last point of
  `calculate_investment_projection(account, months=i)`.
- Investment accounts without a rate contribute their current balance.
- Non-investment account balances, asset values and all liabilities are held
  **constant** at today's values.

`projected_net_worth = static cash balances + asset values + projected investments - liabilities`.

The projection therefore shows only investment growth and recurring
contributions; it does not model debt paydown, cash-flow changes or asset
appreciation. For cash-account forecasting see
[ledger-projections.md](ledger-projections.md).

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `ALPHA_VANTAGE_KEY` | empty | Enables the Alpha Vantage fallback. Without it, a Yahoo failure leaves the symbol at price 0 / invalid until the next fetch. |
| `TZ` | container default | Affects when the 17:00 refresh runs. |

No key is needed for Yahoo Finance. Both providers are unofficial or
rate-limited free tiers; expect occasional gaps.
