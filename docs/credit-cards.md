# Credit Card Statements

The credit-card section (`/credit-cards`) imports statement CSVs from your card
issuers, categorises every charge, and gives you spending breakdowns, duplicate
and subscription-overlap detection, optional AI savings tips, and a
missing-statement check. Card records created here are also what the perk
tracker on [benefits.md](benefits.md) works on.

Code lives in `app.py` (routes and pipeline) and `models.py` (categories and
keyword rules). Related: [budget.md](budget.md) (the same category list),
[architecture.md](architecture.md), [deployment.md](deployment.md) (volumes and
environment variables).

## Collections

| Collection | Purpose |
|---|---|
| `cc_transactions` | One document per imported row: `date`, `description`, `amount`, `category`, `card_issuer`, `card_name`, `original_category`, `statement_date` (`YYYY-MM`), optional `categorized_by: 'llm'`, optional `payment_plan_id` |
| `cc_cards` | One document per card: `name`, `issuer`, `created_at`, plus the perk fields described in [benefits.md](benefits.md) |
| `payment_plans` | No-interest installment plans billed to a card |
| `app_migrations` | Flags for the one-time data migrations |

Transactions are linked to a card **by name** (`card_name`), not by id.

## Cards

- `POST /credit-cards/cards/add` (`cc_card_add`): name plus issuer from
  `CARD_ISSUERS` (`chase`, `citi`, `amex`, `other`). Duplicate names are ignored.
- `POST /credit-cards/cards/delete` (`cc_card_delete`): refused while any
  transaction still references the card's name.
- `ensure_cc_cards_seeded()` runs on every page load and creates a card record
  for any `(card_name, card_issuer)` pair found in `cc_transactions` that has no
  card yet.
- An upload that names a card not yet in `cc_cards` creates it.

## Importing a statement

Route: `POST /credit-cards/upload` (`cc_upload`). Form fields: `file`, and
either `card_id` (the card picker) or the legacy `card_issuer` + `card_name`.

### Format detection

The CSV is decoded as `utf-8-sig` and read with `csv.DictReader`. The format is
detected from the header row; if nothing matches, the card's issuer is used as
the format.

| Format | Detected when | Date column | Amount handling |
|---|---|---|---|
| `chase` | a `Post Date` header exists | `Transaction Date` | Chase exports purchases as negative; the sign is flipped so charges are positive. Chase's own `Category` is kept in `original_category` and mapped through `CHASE_CATEGORY_MAP`. |
| `citi` | a `Debit` or `Credit` header exists | `Date` | `Debit` becomes a positive charge, `Credit` a negative amount; rows with neither are skipped |
| `amex` | exactly three headers, one of them `Amount` | `Date` | `Amount` as-is (charges positive) |
| generic | fallback | `Date` or `date` | `Amount` or `amount` as-is (charges positive) |

All formats expect dates as `MM/DD/YYYY`. Rows that fail to parse are skipped
silently.

### Row rules

Applied to each parsed row, in order:

1. **Payments dropped.** If the upper-cased description contains any of
   `PAYMENT_KEYWORDS` (`PAYMENT`, `AUTOPAY`, `AUTO-PAY`, `THANK YOU`, `PYMT`),
   the row is a bill payment, which is neither spending nor a credit, and is
   discarded.
2. **Credits stored negative.** If the amount is negative, or the description
   contains any of `CREDIT_KEYWORDS` (`CREDIT`, `REFUND`, `REBATE`,
   `CASHBACK REWARD`), the amount is forced negative and the category set to
   `Credit`. Credits therefore net against spending, and setting the category
   here keeps the later categorisation steps (which only touch `Other`) from
   overriding it.
3. **Zero rows skipped.** Rows that round to $0.00 are statement noise.
4. **Keyword categorisation** for anything still `Other` (see below).

### Statement month

Each row's `YYYY-MM` is counted, and the month with the most rows becomes the
upload's `statement_date`, stamped on every row. An empty file falls back to the
current month. The statement list on the page, the delete-statement action and
the default month filter all group by `(card_name, statement_date)`.

### Raw CSV archive

`save_raw_cc_statement()` writes the uploaded bytes to
`CC_STATEMENT_DIR/<card-slug>-<YYYY-MM>.csv`. The directory defaults to
`data/cc_statements` next to `app.py`; the provided `docker-compose.yml` sets
`CC_STATEMENT_DIR=/app/data/cc_statements` on a named volume. The statements
table links to the file when it exists, served by
`GET /credit-cards/statements/raw/<filename>` (`cc_statement_raw`). A second
upload for the same card and month overwrites the file.

### Deduplication

A row is inserted only if no existing `cc_transactions` document has the same
`date`, `description`, `amount` and `card_name`. Re-uploading an overlapping
export is therefore safe. After insertion the user is redirected to the
statement month with an "N transactions imported" message, and the background
LLM pass is started.

### Deleting a statement

`POST /credit-cards/delete-statement` (`cc_delete_statement`) deletes every row
with the given `card_name` and `statement_date`. The raw CSV file is kept.

## Categorisation pipeline

Categories are the unified `CATEGORIES` list from `models.py` (20 entries,
shared with the budget). `CC_CATEGORIES` is `CATEGORIES + ['Credit']`.

A row's category is decided by the first step that produces something other
than `Other`:

| Step | Where | Applies to |
|---|---|---|
| 1. Credit rule | import | negative / credit-keyword rows become `Credit` |
| 2. `CHASE_CATEGORY_MAP` | import | Chase exports only: maps Chase's own category (`Food & Drink` to `Dining`, `Bills & Utilities` to `Utilities`, and so on). Unmapped Chase categories become `Other`. |
| 3. `categorize_transaction()` | import | `MERCHANT_OVERRIDES` first, then `MERCHANT_CATEGORY_RULES` |
| 4. LLM pass (optional) | background thread | rows still at `Other` |
| 5. Manual correction | UI | any row |

### Keyword rules

`categorize_transaction(description)` upper-cases the raw description and:

1. Walks `MERCHANT_OVERRIDES`, an **ordered list** of `(substring, category)`
   pairs. The first match wins. Overrides exist for cases where a broad keyword
   would otherwise win wrongly, e.g. `COSTCO GAS` to `Gas & Fuel` before
   `COSTCO` to `Groceries`, or `SUBWAY TRANSIT` before the `SUBWAY` sandwich
   rule.
2. Walks `MERCHANT_CATEGORY_RULES`, a dict of `category: [substrings]`. **Dict
   order is precedence**: the first category with any matching substring wins.
3. Returns `Other`.

Matching is plain substring matching on the raw descriptor, including processor
prefixes like `SQ *` and `TST*` (some rules key off exactly those).

### Customising the rules

Edit `models.py`:

- Add a keyword to the right category in `MERCHANT_CATEGORY_RULES`. Use a
  distinctive substring of the descriptor as it appears on your statement.
- If a keyword collides with a broader rule that sits earlier in the dict, add
  an entry to `MERCHANT_OVERRIDES` instead (or as well). Each override beats
  every keyword rule, so add them from how your own statements actually read,
  not from intuition.
- Map an issuer's own category in `CHASE_CATEGORY_MAP` if you use Chase exports.
- Adjust `PAYMENT_KEYWORDS` and `CREDIT_KEYWORDS` if your issuer words payments
  or refunds differently.

New rules apply to future imports. Existing rows still at `Other` are picked up
by the next LLM pass; rows already categorised are not re-run (the one-off
`recategorize_existing_cc_other` migration has already been recorded). Fix those
by hand or with a small script.

### Optional LLM pass

Rows the keyword rules leave at `Other` can be sent to a local LLM. The LLM is
**optional**: with it disabled or unreachable, everything still works and
unresolved rows stay at `Other`.

Any server exposing the Ollama-compatible `POST /api/chat` endpoint works: a
plain local Ollama, or a gateway in front of one.

| Variable | Default | Purpose |
|---|---|---|
| `CC_LLM_ENABLED` | `true` | Set to anything other than `true` to disable the categorisation pass entirely |
| `OLLAMA_URL` | `http://localhost:11434` | Base URL of the Ollama-compatible server |
| `OLLAMA_MODEL` | `granite4.2:3b` | Model name passed in each request |
| `OLLAMA_API_KEY` | empty | Sent as `Authorization: Bearer <key>` when set; leave blank for an unauthenticated Ollama |
| `CC_LLM_TIMEOUT` | `60` | Per-request timeout in seconds |

How it works (`_categorize_leftovers_async`, `categorize_with_llm`, `_llm_vote`):

- **Background thread.** Started after every upload and on every
  `/credit-cards` page load, as a daemon thread. A lock ensures only one run at
  a time; a second trigger while one is running is a no-op, since the next run
  picks up whatever is still `Other`. Uploads never wait for the model.
- **One merchant per request.** The worker takes the distinct descriptions still
  at `Other` and asks about each separately. Batching several merchants into one
  prompt made small models collapse the whole batch into one category.
- **`clean_descriptor()`.** Before prompting, processor prefixes (`SQ *`,
  `TST*`, `PAYPAL *`, ...), trailing state codes, phone numbers, store numbers
  and long reference numbers are stripped, e.g.
  `SQ *EXAMPLE CAFE Springfield OH` becomes `EXAMPLE CAFE Springfield`. This is
  only used for the LLM; keyword rules match the raw string.
- **`think: False`.** Reasoning is disabled. With reasoning on, a small model
  spent close to a minute per description; off, it answers in about a second.
- **Constrained output.** Each request passes a JSON schema whose `category`
  field is an enum of `CC_CATEGORIES`, with `temperature: 0`, so an invented
  category is impossible.
- **Two-vote agreement gate.** Two differently worded prompts
  (`CC_LLM_VOTE_PROMPTS`) are sent. The answer is accepted only if both votes
  agree on something other than `Other`. Otherwise the row stays at `Other`.
  This trades recall for precision deliberately: an `Other` row is visibly unset
  and invites a correction, whereas a wrong label silently skews the charts.
- Accepted answers update every `Other` row with that description and set
  `categorized_by: 'llm'`. Such rows show an "AI" badge in the table.
- A non-200 response is logged as a warning, so a dead or unauthenticated
  endpoint is visible in the logs.

The full category list is always sent; trimming it was found to make the model
pick a neighbouring category instead of abstaining.

### Manual recategorisation

Each row in the table has a category dropdown. Changing it calls
`POST /credit-cards/recategorize` (`cc_recategorize`) with JSON
`{transaction_id, category}`. The category must be in `CC_CATEGORIES`. A manual
change also removes `categorized_by`, clearing the AI badge.

## The page

`GET /credit-cards` (`credit_cards_page`) accepts:

| Parameter | Meaning |
|---|---|
| `card` | Card name or `all` |
| `month` | Statement month `YYYY-MM`; defaults to the most recent |
| `category` | Category or `all` |
| `start_date`, `end_date` | ISO dates; a date range overrides the month filter and filters on transaction `date` |

It shows total spending, transaction count, average and top category (charges
only, credits excluded), a category breakdown, the transaction table with
category and payment-plan dropdowns, the statement list with raw-CSV links, and
the missing-statement banner.

`GET /api/cc/spending-by-category` returns `{labels, data}` for the chart, with
the same filters, positive amounts only.

## Savings insights

`GET /api/cc/savings-insights` (`cc_savings_insights`) takes the same filters
plus `scope` (`current` restricts to the selected month) and returns:

| Key | Source | What it finds |
|---|---|---|
| `duplicates` | `detect_duplicate_charges()` | Merchants (normalised by `normalize_merchant()`, which drops suffixes like INC/LLC, store and reference numbers, truncated to 25 characters) with **three or more** charges in one statement month. Top 20 by total. |
| `subscriptions` | `detect_subscription_overlap()` | Groups in `SUBSCRIPTION_GROUPS` where **two or more** different services were charged. Always looks across all months (the month filter is ignored). |
| `ai_tips` | `get_ollama_savings_tips()` | Three to five tips from the LLM, or an empty list |

### Subscription groups

`SUBSCRIPTION_GROUPS` in `models.py` maps a group name to service keywords:
Video Streaming, Music Streaming, Cloud Storage, Password Manager, VPN, Fitness,
News/Media and Food Delivery. Each charge counts toward the first keyword it
matches in a group. For every overlapping group the response lists each service
with its average charge and a group total. Add groups or keywords there to
customise.

### AI spending analysis

`get_ollama_savings_tips()` first pings `OLLAMA_URL/api/tags` with a 3-second
timeout and returns nothing if the server is unreachable. Otherwise it
aggregates spending by category and statement month plus the top 20
descriptions by total, sends them to `OLLAMA_MODEL` with `format: 'json'` and
`think: False` (90-second timeout), and returns up to five
`{title, detail, potential_savings}` objects. Any error yields an empty list.
This uses the same `OLLAMA_*` settings as categorisation.

## Payment plans

A payment plan models a no-interest installment arrangement (for example a
medical bill) that is charged to a card each month.

| Route | Function |
|---|---|
| `POST /debts/payment-plans/add` | `add_payment_plan`: vendor, original amount, monthly payment (both > 0), optional start date, card name, category, notes |
| `POST /debts/payment-plans/edit/<plan_id>` | `edit_payment_plan`, including `status` (`active` or `closed`) |
| `POST /debts/payment-plans/delete/<plan_id>` | `delete_payment_plan`; also unlinks its transactions |
| `GET /debts/payment-plans/<plan_id>` | `payment_plan_detail`: plan plus linked transactions |
| `POST /credit-cards/link-plan` | `cc_link_plan`: JSON `{transaction_id, plan_id}` links a card transaction to a plan; an empty `plan_id` unlinks it |

On the credit-card table each row has a dropdown of active plans. Progress is
computed by `_decorate_payment_plan()` from the linked transactions:

- `paid` = sum of linked amounts, `payments_made` = number linked
- `num_payments` = `ceil(original_amount / monthly_payment)`
- `remaining`, `payments_remaining`, `progress_pct`
- `end_date_estimate` = start date + (`num_payments` - 1) months

Plans appear on the debts page and, when active, on the dashboard.

## Missing-statement check

`compute_missing_statements()` aggregates `cc_transactions` per card and, for
each card with any statement history, lists every month from its earliest
`statement_date` through the last closed calendar month (`_last_closed_month()`,
i.e. the previous month) that is not covered.

A month counts as covered if it is either a `statement_date` label **or** a
month in which the card has transactions dated. This matters because an upload
is stamped with a single majority month: a CSV spanning Aug 1 to Sep 16 labels
every row `2026-09`, and August would otherwise appear missing.

A card you no longer use will keep reporting gaps until it is deleted (which
requires deleting its statements first).

## One-time migrations on page load

`credit_cards_page` calls these on every request. Each is guarded by a flag
document in `app_migrations`, so it does real work once per database and is a
single `find_one` afterwards. They exist to bring rows imported by older
versions in line with current import rules.

| Function | Flag `_id` | Effect |
|---|---|---|
| `ensure_cc_cards_seeded()` | none (idempotent) | Creates missing `cc_cards` records from transaction card names |
| `normalize_existing_cc_credits()` | `cc_credits_negative_v1` | Flips positive rows whose description matches `CREDIT_KEYWORDS` (and not `PAYMENT_KEYWORDS`) to negative |
| `mark_existing_cc_credits_category()` | `cc_credits_category_v1` | Sets `category: 'Credit'` on every negative row |
| `categorize_existing_cc_phone()` | `cc_phone_category_v1` | Retags rows matching the `Phone` keyword rules, never overriding `Credit` |
| `recategorize_existing_cc_other()` | `cc_recategorize_other_v1` | Replays the keyword rules over rows still at `Other`; nothing set by hand or by the Chase map is touched |

After these, `_categorize_leftovers_async()` is started (a no-op when the LLM is
disabled, already running, or there is nothing at `Other`).

To re-run a migration, delete its flag document from `app_migrations`.
