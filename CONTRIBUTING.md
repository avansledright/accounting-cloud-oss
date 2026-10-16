# Contributing

Thanks for your interest. Bug reports, fixes, docs and features are all welcome.

## Before you start

- For anything bigger than a small fix, open an issue first so we can agree on
  the approach.
- **Never include real financial data** in issues, pull requests, tests,
  fixtures or screenshots: no statements, account numbers, balances, names or
  addresses. Use `scripts/seed_demo.py` or obviously fake values.

## Development setup

See [docs/development.md](docs/development.md). Short version:

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
docker run -d --name acct-mongo -p 127.0.0.1:27017:27017 mongo:7.0
cp .env.example .env            # set SECRET_KEY
flask --app app run --debug     # http://127.0.0.1:5000
python scripts/seed_demo.py     # optional demo data
pytest tests
```

## Conventions

These matter more than style; please read them before changing data code.

- **Dates in MongoDB are `datetime.datetime`, never `datetime.date`.** BSON has
  no date-only type. Do not call `.date()` on a value before storing it. Code
  that *reads* dates should accept both, since older records may differ.
- **Existing records lack new fields.** When you add a field to a model, old
  documents won't have it, and Jinja dot-access on a missing dict key raises.
  Use `doc.setdefault('field', None)` before passing documents to templates.
- **Account types:** use `INVESTMENT_ACCOUNT_TYPES` / `CASH_ACCOUNT_TYPES` from
  `models.py` (injected into every template). Never re-spell the lists inline.
- **Archived recurring items** must be excluded from projections: query with
  `ACTIVE_RECURRING`.
- **Card perk templates** (`benefits.PERK_TEMPLATES`) must be researched from
  the issuer's own pages, with the `checked` date updated. Never fill them
  from memory; they drive real spending decisions and go stale quickly.
- **IRS figures** (`benefits.CONTRIBUTION_LIMITS`, payroll defaults) are seed
  defaults that users can override in the UI. Cite the source when you update
  them.
- **Derive from the data before asking the user.** If a value can be read from
  statements or transactions (perk usage, for example), compute it rather than
  adding another form field.
- Keep pure calculation logic in importable modules (`benefits.py`,
  `home_move.py`) so it can be unit tested without MongoDB.
- Update the relevant page in `docs/` when you change behaviour or add an
  environment variable (and `.env.example` + the README table).

## Pull requests

- Keep PRs focused; one change per PR.
- Make sure `pytest tests` passes and you have clicked through the pages you
  touched. CI also builds the Docker image and smoke-tests every main page.
- Describe what changed and how you tested it.

By contributing you agree that your contributions are licensed under the
project's [MIT License](LICENSE).
