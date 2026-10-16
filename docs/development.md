# Development

How to run the app from source, load demo data, run the tests, and follow the
project's conventions when adding a feature. For production setups see
[deployment.md](deployment.md); for how the pieces fit together see
[architecture.md](architecture.md).

## Prerequisites

- **Python 3.12 or newer.** CI tests on 3.12; the Docker image uses
  `python:3.14-slim`.
- **MongoDB 7.0** (the version in `docker-compose.yml`), either in Docker or
  installed locally.
- Optional: an Ollama-compatible LLM server for credit card categorisation and
  advisor reports. Everything else works without one.

## Local setup

```bash
git clone <your fork> accounting-cloud
cd accounting-cloud
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # set SECRET_KEY; everything else is optional
```

### Start MongoDB

The compose `db` service does **not** publish a port to the host; only the
`web` and `mcp` containers can reach it on the compose network. So
`docker compose up -d db` starts MongoDB, but an app running on your host
cannot connect to it on `localhost:27017`.

For development on the host, run a standalone container with the port
published:

```bash
docker run -d --name accounting-mongo -p 27017:27017 \
  -v accounting-mongo-data:/data/db mongo:7.0
```

The default `MONGO_URI=mongodb://localhost:27017/` then works. Alternatively,
add a `docker-compose.override.yml` that publishes `127.0.0.1:27017:27017` on
the `db` service and use `docker compose up -d db`.

### Run the app

```bash
flask --app app run --debug
```

The dev server listens on `http://127.0.0.1:5000`. `--debug` enables the
reloader and debugger.

`flask run` loads `.env` (and `.flaskenv`) automatically because
`python-dotenv` is installed. `app.py` itself does not call `load_dotenv()`,
so `python app.py` does **not** read `.env`; export the variables in your
shell first. `python app.py` serves on `0.0.0.0:5000` and turns on debug only
when `FLASK_DEBUG=true`.

`app.py` connects to MongoDB at import time and starts background threads
(the stock price refresher and, if `ADVISOR_ENABLED=true`, the report
scheduler), so the database must be reachable before the app starts serving
requests.

To work on the MCP server, run `python mcp_server.py` in another terminal with
`MCP_API_TOKEN` exported (see [mcp-server.md](mcp-server.md)).

### Seed demo data

`scripts/seed_demo.py` fills an empty database with obviously fake data
(accounts at invented institutions such as "Demo Bank", a fictional
household, recurring items, recurring transfers, debts, assets, holdings, goals, budget targets,
credit cards and card transactions) so every page has something to show:

```bash
python scripts/seed_demo.py
# or, against the compose stack:
docker compose exec web python scripts/seed_demo.py
```

It reads `MONGO_URI` and `MONGO_DB_NAME` like the app. The data set itself
lives in `demo_data.py` (`load_demo_data(db, force=False)`), which is also
what the setup wizard's **Load demo data instead** button calls.

- **Refuses on a non-empty database.** If the `accounts` collection has any
  documents, it exits with an error.
- **`--force`** first runs `delete_many({})` on every collection it seeds
  (`accounts`, `transactions`, `recurring_transactions`,
  `recurring_transfers`, `debts`, `assets`, `holdings`, `savings_goals`,
  `budget_targets`, `cc_cards`, `cc_transactions`), then seeds. Other
  collections are left as they are. **Never point it at a database holding
  real data.**

## Tests

```bash
pip install -r requirements-dev.txt   # requirements.txt + pytest + mongomock
pytest tests/
```

Part of the suite covers pure modules. App-level tests swap in
[mongomock](https://github.com/mongomock/mongomock) before importing `app.py`
(which connects to MongoDB at import time), so no database is needed; they
are skipped if mongomock is not installed. `tests/conftest.py` provides the
shared fixtures: `app_module` (imports `app.py` once per session with
`pymongo.MongoClient` patched to mongomock and the report scheduler disabled,
with CSRF off), `db` (the app's mongomock database, emptied before each test;
the agent/MCP tools see the same database through `agent.db`) and `client`
(a Flask test client).

| File | Covers |
|---|---|
| `tests/test_pure_modules.py` | `home_move` (payments, transfer taxes, PMI), `benefits`, `payroll_validator` |
| `tests/test_prompts.py` | `agent.prompts`: the generic default profile, and brace escaping for a profile file |
| `tests/test_setup_wizard.py` | First-run redirect, each setup wizard step's records, skip/done/restart, demo-data mode, the getting-started checklist and empty-state pages (mongomock) |
| `tests/test_home_move.py` | `home_move` payment math, temporary buydowns and labels, the reverse price solve with `target_basis`, `compare_buydowns` |
| `tests/test_card_perks.py` | `benefits.detect_annual_fee` (fee and anniversary month from the statement fee charge), `build_card_perks` never overriding hand-entered values, the benefits page reading a fee charge from a previous year (mongomock), and `suggest_template` matching |
| `tests/test_agent_read_tools.py` | `recurrence` math, and the agent/MCP read tools: `recurring_items` monthly equivalents, the cash flow outlook's occurrence counts, `portfolio_summary` live values (mongomock, prices mocked) |
| `tests/test_mcp_writes.py` | MCP write tools against the web app's behaviour: primary-account default, delete skips, override date format, transfers with ledger rows, `biweekly`, debt starting balance and `make_debt_payment` (mongomock) |
| `tests/test_payroll.py` | Pub 926 FICA/FUTA exemptions, the FICA threshold catch-up and Schedule H line-1 wages (mongomock) |
| `tests/test_web_fixes.py` | Transfer delete, `/history?year=`, the account `?horizon=` fallback, calendar weekly items and end dates, home-move income excluding archived items (mongomock) |
| `tests/test_deploy_config.py` | The Dockerfile installs `tzdata` and sets an overridable `TZ`; the Kubernetes ConfigMap documents `TZ` |
| `tests/conftest.py` | Puts the project root on `sys.path`; shared mongomock fixtures (`app_module`, `db`, `client`) |

Keep new business logic in importable modules with no database dependency
(as `benefits.py`, `home_move.py`, `recurrence.py` and `payroll_validator.py` are) so it can be
unit tested here.

### Continuous integration

`.github/workflows/ci.yml` has two jobs:

1. **test**: Python 3.12, `pip install -r requirements-dev.txt`,
   `python -m compileall` over every module, then `pytest -q tests`.
2. **docker-smoke**: copies `.env.example` to `.env` with a generated
   `SECRET_KEY` and a test `MCP_API_TOKEN`, runs
   `docker compose --profile mcp up -d --build --wait`, then:
   - `curl -fsS http://127.0.0.1:5052/health`,
   - checks that `/` redirects to the setup wizard on an empty database and
     that the main pages return 200,
   - runs `scripts/seed_demo.py` and checks the main pages again,
   - checks that the MCP server returns 401 without the token and that an
     authenticated `tools/call` to `account_balances` returns the demo
     account.

Route-level behaviour is covered by this smoke test rather than unit tests.
If you add a page, add its path to the smoke test loops.

## Project conventions

These come from comments in the code and from bugs that have bitten before.

### Store dates as `datetime.datetime`, never `datetime.date`

BSON has no date-only type; pymongo refuses to encode `datetime.date`. Always
store `datetime.datetime`:

- Parse form dates with `datetime.fromisoformat(...)` (or `_parse_form_date()`
  in `app.py`) and store the result as is.
- Do **not** call `.date()` on a value before storing it.
- When **reading**, handle both types, because some code paths and older
  records differ. The pattern used throughout `app.py`:

```python
d = doc['date']
if isinstance(d, datetime):
    d = d.date()
```

`agent/tools/writes.py` states the same rule in its module docstring.

A few fields intentionally hold ISO date strings (for example
`skipped_transactions.date` and `projected_overrides.date`, which are matched
against the projection's `date.isoformat()` keys). Match the existing type of
a field when you write to it.

### Existing records may lack new fields

MongoDB has no schema, so records created before a field existed will not
have it. Jinja dot access on a dict raises `UndefinedError` for a missing key,
so before passing a document to a template, fill defaults:

```python
account.setdefault('balance_as_of_date', None)
account.setdefault('management_type', None)
```

See `account_detail()`, `edit_asset()` and `_decorate_goal()` for examples.
In Python, read optional fields with `.get(..., default)`.

### Account types have one source of truth

`INVESTMENT_ACCOUNT_TYPES` and `CASH_ACCOUNT_TYPES` in `models.py` define the
groupings. Never re-spell the list inline. The `inject_account_type_sets`
context processor exposes them to every template as
`investment_account_types` and `cash_account_types`, so templates and page
JavaScript use the same values (for example
`accounts | selectattr('account_type', 'in', investment_account_types)`).

### Query active recurring items with `ACTIVE_RECURRING`

When a loan is paid off, `_sync_payoff_archive()` marks its budget lines
`archived: true`. Any code that totals or projects recurring transactions must
exclude them:

```python
db.recurring_transactions.find({'transaction_type': 'expense', **ACTIVE_RECURRING})
```

`ACTIVE_RECURRING = {'archived': {'$ne': True}}` is defined in `app.py`; the
agent tools use the same filter inline.

### Card perk templates are researched, not recalled

`benefits.PERK_TEMPLATES` drive real spending decisions and go stale quickly.
Each template is researched against the issuer's own product pages and
carries a `checked` date that the page displays. Never fill or update them
from memory. Credits with no fixed reset (rolling-window or purchase-gated
offers) are deliberately left out, because modelling them as periodic creates
false expiry alerts. See [benefits.md](benefits.md).

### Derive from the data before asking the user

If the statements or ledger already contain a fact, compute it instead of
adding a form field. Existing examples: perk usage is inferred from
qualifying credit card charges (`benefits.detect_perk_usage`), loan payments
link themselves by description (`_apply_auto_links`), and contribution
totals are derived from transfers and transactions
(`benefits.auto_contributions`). Manual entry should be an override, not the
primary input.

### Keep pure logic out of `app.py`

`app.py` holds routes and database access. Calculations that do not need the
database belong in a separate module (`benefits.py`, `home_move.py`,
`recurrence.py`, `payroll_validator.py`, `payroll_forms.py`) so they can be tested and reused.

### One process, in-memory state

The stock price cache (`STOCK_CACHE`), report job state (`_REPORT_JOBS`) and
background threads live in process memory. The Docker image runs gunicorn
with one worker and eight threads for this reason. Do not add state that
assumes multiple workers share memory, and guard shared dicts with a lock as
`_REPORT_JOBS_LOCK` does.

## Adding a feature: checklist

1. **Model.** If the feature stores a new kind of document, add a class to
   `models.py` following the existing pattern (`_id` as `ObjectId`, foreign
   keys converted with `ObjectId(...)`, dates as `datetime`). Add any shared
   constant lists there too.
2. **Pure logic.** Put calculations in a module without database access and
   add tests under `tests/`.
3. **Routes.** Add routes to `app.py` near related ones. Validate IDs with
   `safe_objectid()`, parse optional numbers with `_form_float()` /
   `_form_int()`, and redirect after a form POST. JSON endpoints called from
   page JavaScript need the CSRF header (`getHeaders()` in `static/main.js`).
4. **Old records.** `setdefault` every new field before rendering, and use
   `.get()` in Python.
5. **Recurring items.** If you read `recurring_transactions`, filter with
   `ACTIVE_RECURRING`.
6. **Template.** Extend `base.html`; add a sidebar link there if it is a new
   page.
7. **Export and import.** Nothing to do: `/export` enumerates every
   collection with `list_collection_names()` and `/import` restores whatever
   collections the file contains, so a new collection is backed up and
   restored automatically. Add a CSV export only if users need one.
8. **MCP and advisor.** If AI clients should see the data, add a read
   function in `agent/tools/` and register it with `@mcp.tool()` in
   `mcp_server.py`. Write tools go in `agent/tools/writes.py`; mirror any side
   effects the web route performs, or document the difference in
   [mcp-server.md](mcp-server.md).
9. **Smoke test.** Add the page path to the loops in
   `.github/workflows/ci.yml`.
10. **Docs.** Update the relevant page in `docs/` (and `.env.example` for new
    environment variables).

## Code layout

```
app.py                    Flask app: config, all routes and business logic
models.py                 Data classes and shared constants (categories, account
                          types, goal types, payroll defaults, ...)
benefits.py               Contribution limits and card perk calculations (pure)
home_move.py              Home move planner calculations (pure)
recurrence.py             Recurring-item schedule and monthly-equivalent math (pure)
payroll_forms.py          IRS PDF filling and EFW2 file generation
payroll_validator.py      Local EFW2 validation (pure)
mcp_server.py             Standalone MCP server (read, write and report tools)
agent/
  db.py                   Database proxy shared by the agent code
  prompts.py              Household profile and prompt templates
  reports.py              Weekly, monthly and portfolio report generation
  scheduler.py            Background report scheduler (ADVISOR_ENABLED)
  household_profile.example.md
  tools/                  Read tools (accounts, transactions, budget, cashflow,
                          portfolio, networth, debts) and writes.py
templates/                Jinja2 templates; base.html holds the sidebar
static/style.css          Stylesheet
static/main.js            Shared page JavaScript (transactions, overrides, ...)
forms/                    IRS form field maps and README; drop fillable PDFs here
demo_data.py              Demo data set (setup wizard button and seed script)
scripts/seed_demo.py      Demo data loader (CLI)
scripts/backup-accounting.sh  JSON backup via /export
tests/                    pytest suite (pure modules + mongomock route tests)
deploy/kubernetes/        Example Kubernetes manifests
Dockerfile, docker-compose.yml, .env.example
```

## Related docs

- [architecture.md](architecture.md)
- [features.md](features.md)
- [deployment.md](deployment.md)
- [mcp-server.md](mcp-server.md)
- [advisor-agent.md](advisor-agent.md)
