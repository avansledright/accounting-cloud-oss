"""Tax-advantaged contribution tracking and credit card perk utilization.

Two related jobs live here, both about not leaving money on the table before
December 31:

1. **Contribution limits** — HSA / 401(k) / IRA accounts are matched to an IRS
   annual limit, their year-to-date contributions are measured, and the pace
   needed to max out by the deadline is computed.
2. **Card perks** — recurring statement credits (Amex Platinum, Chase Sapphire
   Preferred, Citi) are tracked per reset period so unused credits can be
   flagged before they expire, and annual-fee break-even is shown.

Pure calculation only: every function takes plain dicts and returns plain
dicts. Route wiring and persistence live in app.py.
"""

from datetime import datetime, timedelta
import calendar as _calendar
import re

# ---------------------------------------------------------------------------
# IRS limits
# ---------------------------------------------------------------------------
# Published figures by calendar year. These are seed defaults only — every
# number can be overridden per-year from the Benefits page (stored in the
# `benefit_settings` doc) so a limit change doesn't require a deploy.
CONTRIBUTION_LIMITS = {
    2025: {
        '401k': {'base': 23500.0, 'catchup_50': 7500.0},
        'ira': {'base': 7000.0, 'catchup_50': 1000.0},
        'hsa': {'self': 4300.0, 'family': 8550.0, 'catchup_55': 1000.0},
    },
    2026: {
        '401k': {'base': 24500.0, 'catchup_50': 8000.0},
        'ira': {'base': 7500.0, 'catchup_50': 1100.0},
        'hsa': {'self': 4400.0, 'family': 8750.0, 'catchup_55': 1000.0},
    },
}

# Which limit bucket an account type falls into by default. Traditional and
# Roth IRAs deliberately share the 'ira' bucket — the IRS limit is combined
# across both. Brokerage/deferred comp have no statutory limit.
DEFAULT_LIMIT_KIND = {
    '401k': '401k',
    'ira': 'ira',
    'roth_ira': 'ira',
    'hsa': 'hsa',
}

LIMIT_KINDS = [
    ('401k', '401(k) elective deferral'),
    ('ira', 'IRA (Traditional + Roth combined)'),
    ('hsa', 'HSA'),
]

LIMIT_LABELS = dict(LIMIT_KINDS)

# A taxable brokerage earmarked for retirement has no IRS cap, but leaving it
# out entirely understates what is actually being saved — the page would read
# "$0 contributed" for someone steadily funding a brokerage. It is tracked as
# an uncapped bucket: contributions counted, no limit, no deadline, no
# urgency, and never mixed into the capped totals.
UNCAPPED_KIND = 'taxable_retirement'

TRACKED_KINDS = LIMIT_KINDS + [
    (UNCAPPED_KIND, 'Retirement savings — taxable (no IRS limit)'),
]

TRACKED_LABELS = dict(TRACKED_KINDS)

DEFAULT_SETTINGS = {
    'hsa_coverage': 'self',        # 'self' | 'family'
    'age_50_plus': False,          # 401(k)/IRA catch-up eligible
    'age_55_plus': False,          # HSA catch-up eligible
    'annual_salary': None,         # for employer-match math
    'pay_periods_per_year': 26,
    'limit_overrides': {},         # {'2026': {'401k': 24500.0, ...}}
}


def get_settings(raw):
    """Merge a stored benefit_settings doc over the defaults."""
    s = dict(DEFAULT_SETTINGS)
    for k, v in (raw or {}).items():
        if k in s:
            s[k] = v
    if s['hsa_coverage'] not in ('self', 'family'):
        s['hsa_coverage'] = 'self'
    try:
        s['pay_periods_per_year'] = max(1, int(s['pay_periods_per_year'] or 26))
    except (TypeError, ValueError):
        s['pay_periods_per_year'] = 26
    return s


def limit_for(kind, year, settings):
    """The dollar limit for one bucket in one year, honoring overrides.

    Returns None when the year has no published figures and no override.
    """
    override = (settings.get('limit_overrides') or {}).get(str(year), {}).get(kind)
    if override is not None:
        try:
            return float(override)
        except (TypeError, ValueError):
            pass

    table = CONTRIBUTION_LIMITS.get(year)
    if not table or kind not in table:
        return None
    row = table[kind]

    if kind == 'hsa':
        total = row['self' if settings.get('hsa_coverage') == 'self' else 'family']
        if settings.get('age_55_plus'):
            total += row.get('catchup_55', 0.0)
        return total

    total = row['base']
    if settings.get('age_50_plus'):
        total += row.get('catchup_50', 0.0)
    return total


def contribution_deadline(kind, year):
    """Last day a contribution can be applied to `year` for this bucket.

    401(k) deferrals must come out of payroll by Dec 31; IRA and HSA
    contributions can be made up until the tax filing deadline.
    """
    if kind == '401k':
        return datetime(year, 12, 31, 23, 59, 59)
    return datetime(year + 1, 4, 15, 23, 59, 59)


# ---------------------------------------------------------------------------
# Year-to-date contributions
# ---------------------------------------------------------------------------

def _as_datetime(value):
    """Normalize a Mongo date field (datetime or date) to a datetime."""
    if isinstance(value, datetime):
        return value
    if hasattr(value, 'year') and hasattr(value, 'month'):
        return datetime(value.year, value.month, value.day)
    return None


def _parse_iso(value):
    """Parse a 'YYYY-MM-DD' string, or return None."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip())
    except ValueError:
        return None


def auto_contributions(account_id, year, transfers, transactions, since=None):
    """Sum observed contributions into one account for a calendar year.

    Counts money moving *in*: transfers whose destination is the account, and
    positive ledger transactions booked against it. `since`, when given,
    restricts the sum to activity strictly after that moment — used to add
    post-paystub activity on top of a manual year-to-date figure.
    """
    start = datetime(year, 1, 1)
    end = datetime(year + 1, 1, 1)
    floor = max(start, since) if since else start

    total = 0.0
    rows = []
    # A verified transfer is stored twice — as a `transfers` doc and as a
    # mirror `transactions` doc on each side ("Transfer from: <description>")
    # — with no id linking them. Counting both doubles every contribution
    # that arrives by transfer, which is most of them.
    counted = set()
    for t in transfers:
        if str(t.get('to_account_id')) != str(account_id):
            continue
        when = _as_datetime(t.get('date'))
        if not when or when < floor or when >= end or (since and when <= since):
            continue
        amount = float(t.get('amount') or 0)
        total += amount
        counted.add((when.date(), round(amount, 2)))
        rows.append({'date': when, 'description': t.get('description') or 'Transfer',
                     'amount': amount, 'source': 'transfer'})

    for t in transactions:
        if str(t.get('account_id')) != str(account_id):
            continue
        when = _as_datetime(t.get('date'))
        if not when or when < floor or when >= end or (since and when <= since):
            continue
        amount = float(t.get('amount') or 0)
        if amount <= 0:
            continue
        description = t.get('description') or 'Deposit'
        # Drop the mirror of a transfer already counted, and any transfer
        # mirror at all — the `transfers` collection is the record for those.
        if (when.date(), round(amount, 2)) in counted:
            continue
        if description.startswith('Transfer from:'):
            continue
        total += amount
        rows.append({'date': when, 'description': description,
                     'amount': amount, 'source': 'transaction'})

    rows.sort(key=lambda r: r['date'], reverse=True)
    return total, rows


def build_contribution_groups(accounts, configs, overrides, settings, year,
                              transfers, transactions, today=None):
    """Assemble per-limit-bucket progress for the benefits page.

    accounts   — account docs (any type; non-tax-advantaged ones are skipped)
    configs    — {account_id_str: benefit_accounts doc} linking accounts to buckets
    overrides  — {(account_id_str, year): contribution_overrides doc}
    Returns a list of group dicts, one per limit bucket in use.
    """
    today = today or datetime.now()
    buckets = {}

    for acct in accounts:
        aid = str(acct['_id'])
        cfg = configs.get(aid) or {}
        kind = cfg.get('limit_kind') or DEFAULT_LIMIT_KIND.get(acct.get('account_type'))
        if cfg.get('limit_kind') == 'none':
            continue
        if not kind or kind not in TRACKED_LABELS:
            continue

        ov = overrides.get((aid, year))
        since = _as_datetime(ov.get('as_of')) if ov else None
        auto, rows = auto_contributions(aid, year, transfers, transactions, since=since)

        if ov:
            base = float(ov.get('ytd_amount') or 0)
            contributed = base + auto
            source = 'manual+auto' if auto else 'manual'
        else:
            base, contributed, source = 0.0, auto, 'auto'

        per_period = cfg.get('per_paycheck')
        try:
            per_period = float(per_period) if per_period else None
        except (TypeError, ValueError):
            per_period = None

        entry = {
            'account': acct,
            'account_id': aid,
            'config': cfg,
            'override': ov,
            'manual_base': base,
            'manual_as_of': since,
            'auto_amount': auto,
            'contributed': contributed,
            'source': source,
            'recent': rows[:5],
            'per_paycheck': per_period,
        }
        buckets.setdefault(kind, []).append(entry)

    groups = []
    for kind, _label in TRACKED_KINDS:
        entries = buckets.get(kind)
        if not entries:
            continue
        uncapped = kind == UNCAPPED_KIND
        limit = None if uncapped else limit_for(kind, year, settings)
        contributed = sum(e['contributed'] for e in entries)
        remaining = max(0.0, (limit - contributed)) if limit else 0.0
        deadline = None if uncapped else contribution_deadline(kind, year)
        days_left = max(0, (deadline - today).days) if deadline else None

        periods_left = None
        if kind == '401k' and settings.get('pay_periods_per_year'):
            # Payroll deferrals only land on remaining paychecks this year.
            per_year = settings['pay_periods_per_year']
            year_end = datetime(year, 12, 31)
            if today < year_end:
                fraction = (year_end - max(today, datetime(year, 1, 1))).days / 365.0
                periods_left = max(0, int(per_year * fraction))

        groups.append({
            'kind': kind,
            'uncapped': uncapped,
            'label': TRACKED_LABELS[kind],
            'limit': limit,
            'contributed': contributed,
            'remaining': remaining,
            'over': max(0.0, contributed - limit) if limit else 0.0,
            'pct': min(100.0, (contributed / limit * 100.0)) if limit else 0.0,
            'deadline': deadline,
            'days_left': days_left,
            'periods_left': periods_left,
            'per_period_needed': (remaining / periods_left) if periods_left else None,
            'scheduled_remaining': _scheduled_remaining(entries, periods_left),
            'entries': entries,
        })
    return groups


def _scheduled_remaining(entries, periods_left):
    """Dollars the known payroll schedule will still add before year end."""
    if not periods_left:
        return None
    total = sum((e['per_paycheck'] or 0) for e in entries)
    return total * periods_left if total else None


def employer_match_status(groups, settings):
    """Whether 401(k) deferrals are capturing the full employer match.

    Returns None unless both a salary and a match formula are configured.
    """
    salary = settings.get('annual_salary')
    group = next((g for g in groups if g['kind'] == '401k'), None)
    if not salary or not group:
        return None
    try:
        salary = float(salary)
    except (TypeError, ValueError):
        return None
    if salary <= 0:
        return None

    cfg = next((e['config'] for e in group['entries'] if e['config'].get('match_limit_pct')), None)
    if not cfg:
        return None
    try:
        match_limit_pct = float(cfg.get('match_limit_pct') or 0)
        match_rate = float(cfg.get('match_rate_pct') or 100)
    except (TypeError, ValueError):
        return None
    if match_limit_pct <= 0:
        return None

    needed = salary * match_limit_pct / 100.0
    contributed = group['contributed']
    shortfall = max(0.0, needed - contributed)
    return {
        'salary': salary,
        'match_limit_pct': match_limit_pct,
        'match_rate_pct': match_rate,
        'needed_contribution': needed,
        'contributed': contributed,
        'shortfall': shortfall,
        'match_at_risk': shortfall * match_rate / 100.0,
        'captured': shortfall <= 0,
    }


# ---------------------------------------------------------------------------
# Credit card perks
# ---------------------------------------------------------------------------

PERK_CADENCES = [
    ('monthly', 'Monthly'),
    ('quarterly', 'Quarterly'),
    ('semiannual', 'Semi-annual'),
    ('annual', 'Annual'),
]

CADENCE_PERIODS_PER_YEAR = {'monthly': 12, 'quarterly': 4, 'semiannual': 2, 'annual': 1}

# A credit either resets on the calendar (Jan 1) or on the cardmember year
# (the month the account was opened / the annual fee posts). Getting this wrong
# means showing a deadline that is off by months, so it is tracked per credit.
# Issuers run monthly and quarterly credits on the calendar either way, so the
# basis only changes anything for 'semiannual' and 'annual'.
RESET_BASES = [
    ('calendar', 'Calendar year'),
    ('anniversary', 'Cardmember year'),
]

ANNIVERSARY_CADENCES = {'semiannual', 'annual'}

# Much of what a premium card's fee buys is not a statement credit — lounge
# access, elite status, insurance, earning rates. These have no reset period
# and nothing to forfeit, so they are held as reference only: listed beside the
# card, deliberately excluded from the captured / forfeited / net math, and
# never alerted on. Mixing them into the break-even would make a card look like
# it pays for itself on benefits you cannot spend.
BENEFIT_CATEGORIES = [
    ('lounge', 'Lounge access'),
    ('status', 'Elite status'),
    ('earning', 'Earning & redemption'),
    ('protection', 'Insurance & protection'),
    ('other', 'Other'),
]

BENEFIT_CATEGORY_LABELS = dict(BENEFIT_CATEGORIES)


def _perk_match_patterns(card, perk):
    """Merchant patterns for a perk, falling back to its template.

    Cards loaded from a template before patterns existed — or before a
    pattern was improved — still get detection, because the lookup falls
    back to the template entry with the same name. That keeps stored perk
    ids (and the usage rows pointing at them) untouched.
    """
    if perk.get('match'):
        return [p.upper() for p in perk['match']], [p.upper() for p in perk.get('match_exclude') or []]

    tpl = PERK_TEMPLATES.get(card.get('template_key') or suggest_template(card) or '')
    if not tpl:
        return [], []
    name = (perk.get('name') or '').strip().lower()
    for candidate in tpl['perks']:
        if candidate['name'].strip().lower() == name:
            return ([p.upper() for p in candidate.get('match') or []],
                    [p.upper() for p in candidate.get('match_exclude') or []])
    return [], []


def detect_perk_usage(card, transactions, year, today=None):
    """Infer which credits were used, from qualifying charges on the statements.

    Issuers post a statement credit as its own line, but a CSV export often
    carries only the charges. The charge is the better signal anyway: a
    $9.99 Uber One charge in March means March's Uber One credit had
    something to apply to. So for each perk carrying `match` patterns, the
    qualifying spend in a period is summed and capped at the credit amount.

    Returns {(perk_id, period_key): amount}. This is evidence of qualifying
    spend, not proof the issuer paid — a manual entry always overrides it.
    """
    today = today or datetime.now()
    try:
        anniv_month = int(card.get('fee_month') or 1)
    except (TypeError, ValueError):
        anniv_month = 1
    if not 1 <= anniv_month <= 12:
        anniv_month = 1

    card_name = card.get('name')
    rows = []
    for t in transactions:
        if t.get('card_name') != card_name:
            continue
        when = _as_datetime(t.get('date'))
        amount = float(t.get('amount') or 0)
        # Only charges count. Credits and refunds are negative here.
        if not when or amount <= 0 or when.year != year:
            continue
        rows.append((when, amount, (t.get('description') or '').upper()))

    detected = {}
    for perk in card.get('perks') or []:
        patterns, excludes = _perk_match_patterns(card, perk)
        if not patterns:
            continue
        cadence = perk.get('cadence') if perk.get('cadence') in CADENCE_PERIODS_PER_YEAR else 'annual'
        basis = perk.get('reset_basis') if perk.get('reset_basis') in dict(RESET_BASES) else 'calendar'
        overrides = perk.get('amount_by_period') or {}
        try:
            base = float(perk.get('amount') or 0)
        except (TypeError, ValueError):
            base = 0.0
        ends_on = _parse_iso(perk.get('ends_on'))

        for when, amount, description in rows:
            if ends_on and when > ends_on:
                continue
            if not any(p in description for p in patterns):
                continue
            if any(p in description for p in excludes):
                continue
            key = period_key(cadence, when, basis, anniv_month)
            detected[(perk['id'], key)] = detected.get((perk['id'], key), 0.0) + amount

    # Cap each period at what the credit is actually worth.
    capped = {}
    perks_by_id = {p['id']: p for p in (card.get('perks') or []) if p.get('id')}
    for (perk_id, key), spend in detected.items():
        perk = perks_by_id.get(perk_id)
        if not perk:
            continue
        try:
            base = float(perk.get('amount') or 0)
        except (TypeError, ValueError):
            base = 0.0
        limit = amount_for_period(base, perk.get('amount_by_period') or {}, key)
        capped[(perk_id, key)] = min(spend, limit)
    return capped


# "ANNUAL FEE" (Citi) and "ANNUAL MEMBERSHIP FEE" (Amex, Chase) statement lines.
ANNUAL_FEE_PATTERN = re.compile(r'\bANNUAL\s+(?:MEMBERSHIP\s+)?FEE\b')


def detect_annual_fee(card, transactions):
    """(fee, month) from the most recent annual-fee charge on this card.

    The fee charge is the authoritative record of what the card costs and
    when the cardmember year turns over, so the page reads it rather than
    asking. Returns (None, None) if no fee charge is found. Callers only use
    this where the card has no value of its own — a hand-entered fee or
    month always wins.
    """
    latest = None
    for t in transactions:
        if t.get('card_name') != card.get('name'):
            continue
        try:
            amount = float(t.get('amount') or 0)
        except (TypeError, ValueError):
            continue
        # Charges only; a fee reversal is a negative credit line.
        if amount <= 0 or not ANNUAL_FEE_PATTERN.search((t.get('description') or '').upper()):
            continue
        when = _as_datetime(t.get('date'))
        if when and (latest is None or when > latest[0]):
            latest = (when, amount)
    if not latest:
        return None, None
    return latest[1], latest[0].month


def suggest_template(card):
    """Best-guess template key for a card, matched on its name and issuer.

    A card with no credits yet is useless, and the templates only apply when
    someone explicitly loads one — so the page offers the likely match inline
    rather than leaving an empty card and a buried menu. Like the others, the
    Citi template keys on the product name, not the issuer: every Citi card
    carries issuer 'citi', and a Double Cash, Custom Cash or Premier must not
    be offered the AAdvantage Executive's $695 fee and credits.
    """
    text = f"{card.get('name') or ''} {card.get('issuer') or ''}".lower()
    if 'platinum' in text and ('amex' in text or 'american express' in text):
        return 'amex_platinum'
    if 'sapphire' in text:
        return 'chase_sapphire_preferred'
    if 'aadvantage' in text or 'american airlines' in text or ('executive' in text and 'citi' in text):
        return 'citi_aadvantage_executive'
    return None


def build_card_benefits(card):
    """Group a card's non-credit benefits by category, in display order."""
    grouped = {}
    for item in card.get('benefits') or []:
        category = item.get('category')
        if category not in BENEFIT_CATEGORY_LABELS:
            category = 'other'
        grouped.setdefault(category, []).append({
            'id': item.get('id'),
            'name': item.get('name'),
            'category': category,
            'notes': item.get('notes'),
        })
    return [{'key': key, 'label': label, 'items': grouped[key]}
            for key, label in BENEFIT_CATEGORIES if key in grouped]


def uses_anniversary(cadence, basis):
    return basis == 'anniversary' and cadence in ANNIVERSARY_CADENCES


def period_suffix(key):
    """The within-year part of a period key ('03', 'Q2', 'H1'), or '' if annual.

    Used to look up a per-period amount override, for credits that are not the
    same size every period — Amex's Uber Cash is $15 a month but $35 in
    December, and a flat amount would under-count the year by $20.
    """
    head = key.split('-', 1)
    return head[1] if len(head) > 1 else ''


def amount_for_period(perk_amount, overrides, key):
    """Dollar value of one period, honoring any per-period override."""
    if not overrides:
        return perk_amount
    raw = overrides.get(period_suffix(key))
    if raw is None:
        return perk_amount
    try:
        return float(raw)
    except (TypeError, ValueError):
        return perk_amount


def _anniversary_year_start(when, anniv_month):
    """Year in which the cardmember year containing `when` began."""
    return when.year if when.month >= anniv_month else when.year - 1


def period_key(cadence, when, basis='calendar', anniv_month=1):
    """Stable identifier for the reset window `when` falls in.

    Anniversary keys carry an 'A' marker and the month they are anchored to
    (e.g. '2026A09', '2026A09-H2') so a stored key stays interpretable even if
    the card's anniversary month is later corrected.
    """
    if uses_anniversary(cadence, basis):
        y = _anniversary_year_start(when, anniv_month)
        base = f'{y}A{anniv_month:02d}'
        if cadence == 'annual':
            return base
        months_in = (when.year - y) * 12 + when.month - anniv_month
        return f'{base}-H{1 if months_in < 6 else 2}'

    y = when.year
    if cadence == 'monthly':
        return f'{y}-{when.month:02d}'
    if cadence == 'quarterly':
        return f'{y}-Q{(when.month - 1) // 3 + 1}'
    if cadence == 'semiannual':
        return f'{y}-H{1 if when.month <= 6 else 2}'
    return str(y)


def _add_months(when, months):
    total = when.month - 1 + months
    return datetime(when.year + total // 12, total % 12 + 1, 1)


def period_bounds(cadence, key):
    """(start, end_exclusive) datetimes for a period key.

    The key is self-describing — anniversary keys carry their anchor month —
    so no extra card context is needed to place a period on the calendar.
    """
    if 'A' in key:
        head, _, half = key.partition('-')
        y_str, _, m_str = head.partition('A')
        start = datetime(int(y_str), int(m_str), 1)
        if not half:
            return start, _add_months(start, 12)
        if half == 'H1':
            return start, _add_months(start, 6)
        return _add_months(start, 6), _add_months(start, 12)

    if cadence == 'annual':
        y = int(key)
        return datetime(y, 1, 1), datetime(y + 1, 1, 1)
    y_str, rest = key.split('-', 1)
    y = int(y_str)
    if cadence == 'monthly':
        m = int(rest)
        end_y, end_m = (y + 1, 1) if m == 12 else (y, m + 1)
        return datetime(y, m, 1), datetime(end_y, end_m, 1)
    if cadence == 'quarterly':
        q = int(rest[1:])
        start_m = (q - 1) * 3 + 1
        end_y, end_m = (y + 1, 1) if q == 4 else (y, start_m + 3)
        return datetime(y, start_m, 1), datetime(end_y, end_m, 1)
    h = int(rest[1:])
    if h == 1:
        return datetime(y, 1, 1), datetime(y, 7, 1)
    return datetime(y, 7, 1), datetime(y + 1, 1, 1)


def periods_in_year(cadence, year, basis='calendar', anniv_month=1):
    """Every period key for a cadence that overlaps a calendar year, in order.

    Anniversary periods straddle New Year, so a calendar-year view of one
    shows the cardmember year that started last year as well as this year's.
    """
    if uses_anniversary(cadence, basis):
        keys, seen = [], set()
        for probe_year in (year - 1, year):
            for month in ([anniv_month] if cadence == 'annual'
                          else [anniv_month, (anniv_month + 5) % 12 + 1]):
                probe = datetime(probe_year if month >= anniv_month else probe_year + 1, month, 1)
                key = period_key(cadence, probe, basis, anniv_month)
                if key in seen:
                    continue
                start, end = period_bounds(cadence, key)
                if end > datetime(year, 1, 1) and start < datetime(year + 1, 1, 1):
                    keys.append((start, key))
                    seen.add(key)
        return [k for _s, k in sorted(keys)]

    if cadence == 'annual':
        return [str(year)]
    if cadence == 'monthly':
        return [f'{year}-{m:02d}' for m in range(1, 13)]
    if cadence == 'quarterly':
        return [f'{year}-Q{q}' for q in range(1, 5)]
    return [f'{year}-H1', f'{year}-H2']


def period_label(cadence, key):
    """Short cell label. Anniversary periods show the month they open."""
    if 'A' in key:
        head, _, half = key.partition('-')
        y_str, _, m_str = head.partition('A')
        label = f"{_calendar.month_abbr[int(m_str)]} '{y_str[-2:]}"
        return f'{label} {half}' if half else label
    if cadence == 'monthly':
        y, m = key.split('-')
        return _calendar.month_abbr[int(m)]
    if cadence in ('quarterly', 'semiannual'):
        return key.split('-')[1]
    return key


def build_card_perks(cards, usage_docs, year, today=None, transactions=None,
                     fee_transactions=None):
    """Per-card perk utilization, forfeited value, and annual-fee break-even.

    usage_docs       — cc_perk_usage docs: {card_id, perk_id, period, amount_used}
    transactions     — cc_transactions for the year, used to infer usage from
                       qualifying charges where no manual entry exists.
    fee_transactions — annual-fee charges from any year (the latest fee may
                       predate this year); defaults to `transactions`.
    """
    today = today or datetime.now()
    transactions = transactions or []
    if fee_transactions is None:
        fee_transactions = transactions
    used = {}
    for u in usage_docs:
        used[(str(u.get('card_id')), u.get('perk_id'), u.get('period'))] = float(u.get('amount_used') or 0)

    results = []
    for card in cards:
        cid = str(card['_id'])
        try:
            annual_fee = float(card.get('annual_fee') or 0)
        except (TypeError, ValueError):
            annual_fee = 0.0

        # Fill a missing fee / fee month from the statement's fee charge. Only
        # in memory: a value the user entered is never replaced or written.
        fee_detected = month_detected = False
        if not annual_fee or not card.get('fee_month'):
            found_fee, found_month = detect_annual_fee(card, fee_transactions)
            card = dict(card)
            if found_fee and not annual_fee:
                annual_fee = card['annual_fee'] = found_fee
                fee_detected = True
            if found_month and not card.get('fee_month'):
                card['fee_month'] = found_month
                month_detected = True

        # The cardmember year is anchored to the month the annual fee posts.
        try:
            anniv_month = int(card.get('fee_month') or 1)
        except (TypeError, ValueError):
            anniv_month = 1
        if not 1 <= anniv_month <= 12:
            anniv_month = 1

        # Inferred from statement charges; a manual entry always overrides.
        auto = detect_perk_usage(card, transactions, year, today=today)

        perks, captured, forfeited, available = [], 0.0, 0.0, 0.0
        for perk in card.get('perks') or []:
            cadence = perk.get('cadence') if perk.get('cadence') in CADENCE_PERIODS_PER_YEAR else 'annual'
            basis = perk.get('reset_basis') if perk.get('reset_basis') in dict(RESET_BASES) else 'calendar'
            try:
                amount = float(perk.get('amount') or 0)
            except (TypeError, ValueError):
                amount = 0.0

            overrides = perk.get('amount_by_period') or {}
            # Several card credits are promos with a hard sunset (Grubhub,
            # Instacart, the Chase DoorDash promo). Past that date the benefit
            # simply does not exist, so its periods are dropped rather than
            # reported as forfeited — otherwise the page nags about money that
            # was never available.
            ends_on = _as_datetime(perk.get('ends_on')) or _parse_iso(perk.get('ends_on'))

            cells = []
            for key in periods_in_year(cadence, year, basis, anniv_month):
                start, end = period_bounds(cadence, key)
                if ends_on and start > ends_on:
                    continue
                cell_amount = amount_for_period(amount, overrides, key)
                manual = used.get((cid, perk.get('id'), key))
                inferred = auto.get((perk.get('id'), key), 0.0)
                got = manual if manual is not None else inferred
                is_auto = manual is None and inferred > 0
                if today >= end:
                    state = 'used' if got >= cell_amount - 0.01 else ('partial' if got > 0 else 'missed')
                elif today >= start:
                    state = 'used' if got >= cell_amount - 0.01 else ('partial' if got > 0 else 'open')
                else:
                    state = 'future'

                cells.append({
                    'period': key,
                    'label': period_label(cadence, key),
                    'amount': cell_amount,
                    'used': got,
                    'auto': is_auto,
                    'state': state,
                    'current': start <= today < end,
                    'ends': end - timedelta(days=1),
                    'days_left': max(0, (end - today).days) if start <= today < end else None,
                })
                captured += got
                if state in ('missed', 'partial') and today >= end:
                    forfeited += max(0.0, cell_amount - got)
                elif state in ('open', 'partial') and start <= today < end:
                    available += max(0.0, cell_amount - got)

            # A calendar year's cells are exactly one year of the credit, so
            # summing them picks up per-period overrides. Anniversary periods
            # straddle New Year and can show two rows for one yearly credit,
            # so those are counted once per cardmember year instead.
            if uses_anniversary(cadence, basis):
                annual_value = amount * CADENCE_PERIODS_PER_YEAR[cadence]
            else:
                annual_value = sum(c['amount'] for c in cells)

            perks.append({
                'id': perk.get('id'),
                'name': perk.get('name'),
                'amount': amount,
                'amount_by_period': overrides,
                'varies': bool(overrides),
                'reset_basis': basis,
                'cadence': cadence,
                'notes': perk.get('notes'),
                'ends_on': ends_on,
                'annual_value': annual_value,
                'cells': cells,
                'current': next((c for c in cells if c['current']), None),
            })

        potential = sum(p['annual_value'] for p in perks)
        card_benefits = build_card_benefits(card)
        results.append({
            'card': card,
            'card_id': cid,
            'annual_fee': annual_fee,
            'fee_month': card.get('fee_month'),
            'fee_detected': fee_detected,
            'fee_month_detected': month_detected,
            'perks': perks,
            # Reference only — intentionally absent from every figure below.
            'benefits': card_benefits,
            'benefit_count': sum(len(g['items']) for g in card_benefits),
            'suggested_template': suggest_template(card) if not perks else None,
            'captured': captured,
            'forfeited': forfeited,
            'available_now': available,
            'potential': potential,
            'net': captured - annual_fee,
            'breakeven_pct': min(100.0, captured / annual_fee * 100.0) if annual_fee else None,
        })
    results.sort(key=lambda r: (-r['annual_fee'], r['card'].get('name') or ''))
    return results


# Starter templates for popular premium cards, researched against the issuers'
# own product pages in September 2026 and cross-checked against the points
# press. Issuers reshuffle these constantly — Amex alone dropped the Saks
# credit in July 2026 and CLEAR repriced — so `checked` records when each set
# was last verified, and the page shows it. Treat a loaded template as a draft
# to reconcile against your own card's terms, not a source of truth.
#
# Deliberately excluded, because they are not fixed recurring credits and
# would produce false "expiring" alerts:
#   - Global Entry / TSA PreCheck (both cards): a rolling ~4-year window from
#     last use, with no calendar or anniversary anchor.
#   - Amex SoulCycle at-home bike credit: gated on a specific large purchase.
#   - Chase DashPass membership and the Apple TV year: waived fees and a
#     one-time trial, not money back.
PERK_TEMPLATES = {
    'amex_platinum': {
        'label': 'Amex Platinum',
        'annual_fee': 895.0,
        'checked': 'September 2026',
        'source': 'Amex consumer Platinum fact sheet + benefit detail pages',
        # Audited for completeness against Amex's own marketed "$3,500+ in
        # annual value": these 11 credits total $2,994.40, and the remaining
        # ~$506 is lounge access, which Amex itemizes at $850+. The union of
        # five independent enumerations adds only Global Entry/TSA and
        # SoulCycle, both excluded above on purpose. Do not re-add the Dell,
        # wireless, Indeed or Adobe credits — those are Business Platinum.
        'perks': [
            # $15/mo with a $20 bonus in December — $200 a year, not $180.
            {'name': 'Uber Cash', 'amount': 15.0, 'cadence': 'monthly',
             'amount_by_period': {'12': 35.0},
             # "UBER ONE" is its own credit below, so it must not also feed this one.
             'match': ['UBER'], 'match_exclude': ['UBER ONE'],
             'notes': '$35 in December. Add the card to your Uber account; no rollover'},
            {'name': 'Digital Entertainment', 'amount': 25.0, 'cadence': 'monthly',
             'match': ['HULU', 'DISNEY', 'ESPN', 'PARAMOUNT', 'PEACOCK', 'NYTIMES',
                       'NEW YORK TIMES', 'WSJ', 'WALL STREET JOURNAL', 'YOUTUBE'],
             'notes': 'Enrollment required. Disney+, ESPN+, Hulu, Paramount+, Peacock, NYT, WSJ, YouTube'},
            {'name': 'Walmart+ membership', 'amount': 12.95, 'cadence': 'monthly',
             'match': ['WALMART+', 'WALMART PLUS', 'WM PLUS'],
             'notes': 'Monthly plan only, not the annual one. Plus applicable taxes'},
            {'name': 'Resy dining', 'amount': 100.0, 'cadence': 'quarterly',
             'notes': 'Enrollment required. US Resy-partner restaurants'},
            {'name': 'lululemon', 'amount': 75.0, 'cadence': 'quarterly',
             'match': ['LULULEMON'],
             'notes': 'Enrollment required. Outlets excluded'},
            {'name': 'Hotel credit (FHR / The Hotel Collection)', 'amount': 300.0,
             'cadence': 'semiannual',
             'notes': 'Prepaid through Amex Travel; THC needs a 2-night minimum'},
            {'name': 'Airline fee credit', 'amount': 200.0, 'cadence': 'annual',
             'notes': 'Enrollment required; pick one airline. Incidentals only, not airfare'},
            {'name': 'Uber One membership', 'amount': 120.0, 'cadence': 'annual',
             'match': ['UBER ONE'],
             'notes': 'Enrollment required; auto-renewing US membership'},
            {'name': 'CLEAR Plus', 'amount': 219.0, 'cadence': 'annual',
             'match': ['CLEAR'],
             'notes': 'Subject to auto-renewal'},
            {'name': 'Equinox', 'amount': 300.0, 'cadence': 'annual',
             'match': ['EQUINOX'],
             'notes': 'Enroll before the purchase. Club membership or Equinox+ digital'},
            {'name': 'Oura Ring', 'amount': 200.0, 'cadence': 'annual',
             'match': ['OURA'],
             'notes': 'Ring hardware from ouraring.com'},
        ],
        'benefits': [
            {'category': 'lounge', 'name': 'Global Lounge Collection',
             'notes': '1,550+ lounges: Centurion, Priority Pass Select (enroll), Plaza Premium, '
                      'Escape, Airspace. 10 Delta Sky Club visits a year with a Delta flight. '
                      'Amex self-values this at $850+, which is what closes the gap between the '
                      'credits below and its "$3,500+ in annual value" claim. Centurion entry is '
                      'within 5 hours of departure since Jul 8, 2026 and guests must be on the '
                      'same flight; Lufthansa lounges drop out Oct 1, 2026'},
            {'category': 'status', 'name': 'Hotel elite status',
             'notes': 'Marriott Bonvoy Gold, Hilton Honors Gold, Leading Hotels Leaders Club '
                      'Sterling. Enrollment required'},
            {'category': 'status', 'name': 'Rental car elite status',
             'notes': 'Hertz, Avis, National'},
            {'category': 'earning', 'name': '5x on flights and prepaid hotels',
             'notes': 'Flights booked direct or via Amex Travel, and prepaid hotels via Amex '
                      'Travel, up to $500k a year. 1x everything else'},
            {'category': 'protection', 'name': 'Travel and purchase protection',
             'notes': 'Trip cancellation/interruption, trip delay, car rental loss & damage up '
                      'to $75k, baggage, cell phone, purchase protection, extended warranty, '
                      'Global Assist. No foreign transaction fees'},
            {'category': 'other', 'name': 'Fine Hotels + Resorts perks',
             'notes': 'Roughly $100 property credit, room upgrade, breakfast for two, '
                      'guaranteed 4pm late checkout. Separate from the hotel credit'},
            {'category': 'other', 'name': 'Authorized users, $195/yr each',
             'notes': 'Each AU gets their own Global Entry / TSA PreCheck credit — the only '
                      'credit that multiplies per user — plus full lounge access and hotel and '
                      'car rental status. Every other credit is shared once per account, and '
                      'AUs do not get the $200 Uber Cash'},
            {'category': 'other', 'name': 'Amex Offers',
             'notes': 'Targeted and activation-based with individual expiry dates, not a fixed '
                      'recurring credit — which is why they are not tracked above'},
        ],
    },
    'chase_sapphire_preferred': {
        'label': 'Chase Sapphire Preferred',
        'annual_fee': 95.0,
        'checked': 'September 2026',
        'source': 'chase.com product page + June 2026 refresh release',
        'perks': [
            # Doubled to $100 and moved to a single credit in the June 2026
            # refresh; runs on the cardmember year, not the calendar.
            {'name': 'Chase Travel hotel credit', 'amount': 100.0, 'cadence': 'annual',
             'reset_basis': 'anniversary',
             'notes': 'Prepaid hotel through Chase Travel. Resets on your account anniversary'},
            {'name': 'DoorDash non-restaurant promo', 'amount': 10.0, 'cadence': 'monthly',
             'match': ['DOORDASH', 'DD *'],
             'ends_on': '2027-12-31',
             'notes': 'Grocery/convenience/retail only, needs DashPass. An in-app '
                      'checkout discount, not a posted statement credit'},
        ],
        'benefits': [
            {'category': 'earning', 'name': 'Points multipliers',
             'notes': '5x Chase Travel; 3x dining, gas & EV charging, vacation rentals, '
                      'streaming, online grocery; 2x other travel; 1x else'},
            {'category': 'earning', 'name': 'Promotional multipliers',
             'notes': '5x Lyft through 9/30/2027; 5x Peloton over $150 through 12/31/2027'},
            {'category': 'earning', 'name': 'Transfer partners',
             'notes': '1:1 to 13+ airline and hotel programs; points worth 1.25c via Chase '
                      'Travel. World of Hyatt drops to 4:3 on Oct 1, 2026'},
            {'category': 'protection', 'name': 'Travel and purchase protection',
             'notes': 'Primary auto rental CDW up to $60k, trip cancellation/interruption up '
                      'to $10k per traveler, trip delay up to $500, baggage delay $100/day for '
                      '5 days, lost luggage $3k, travel accident $500k, emergency evacuation '
                      '$100k, purchase protection, extended warranty. No foreign transaction fees'},
            {'category': 'other', 'name': 'DashPass membership',
             'notes': 'Waived delivery fees — a subscription, not money back. Through '
                      '12/31/2027 if activated by 12/31/2026'},
            {'category': 'other', 'name': 'Apple TV, 12 months',
             'notes': 'One-time, roughly $156. Must be activated by 12/31/2026'},
            {'category': 'other', 'name': 'Visa Signature benefits',
             'notes': 'GigSky eSIM 1GB / 15 days once a year plus 20% off other plans; Sofar '
                      'Sounds presale. Visa has no recurring statement-credit program'},
            {'category': 'other', 'name': 'No lounge access or elite status',
             'notes': 'Those are Sapphire Reserve benefits, not Preferred'},
        ],
    },
    'citi_aadvantage_executive': {
        'label': 'Citi / AAdvantage Executive',
        'annual_fee': 695.0,
        'checked': 'September 2026',
        'source': 'creditcards.aa.com + Aug 2026 refresh coverage',
        'perks': [
            # Refreshed Aug 23, 2026: fee $595 -> $695, rebranded World Legend.
            # Accounts opened before that keep $595 until their next renewal,
            # so check which fee you are actually paying.
            {'name': 'AA Vacations credit', 'amount': 250.0, 'cadence': 'semiannual',
             'match': ['AA VACATIONS', 'AMERICAN AIRLINES VAC'],
             'notes': 'Package at AAVacations.com; standalone AA.com airfare excluded. '
                      'The first half does not roll into the second'},
            {'name': 'Inflight & Admirals Club credit', 'amount': 100.0, 'cadence': 'annual',
             'notes': 'Food and drink on AA flights, and purchases in Admirals Club'},
            {'name': 'Avis / Budget car rental', 'amount': 120.0, 'cadence': 'annual',
             'match': ['AVIS', 'BUDGET RENT'],
             'notes': 'Prepaid bookings made direct at Avis.com or Budget.com; OTAs excluded'},
            {'name': 'Lyft credit', 'amount': 15.0, 'cadence': 'monthly',
             'match': ['LYFT'],
             'notes': 'Only earned in months with 3+ eligible rides; posts after the 3rd. '
                      'Link the card in the Lyft app'},
            {'name': 'Grubhub credit', 'amount': 10.0, 'cadence': 'monthly',
             'match': ['GRUBHUB'],
             'ends_on': '2027-08-31',
             'notes': 'Being retired — gone for accounts opened on/after 8/23/2026, and '
                      'sunsets 8/31/2027 for older accounts'},
            # Mastercard network promos rather than Citi credits, so they are
            # easy to miss when reconciling a statement. All have hard end
            # dates. The network Lyft credit is omitted because the Citi-issued
            # $15/mo above supersedes it on this card.
            {'name': 'Instacart credit (Mastercard)', 'amount': 10.0, 'cadence': 'monthly',
             'match': ['INSTACART'],
             'ends_on': '2027-01-31',
             'notes': 'Off your second eligible order each month. Network promo'},
            {'name': 'Peacock credit (Mastercard)', 'amount': 3.0, 'cadence': 'monthly',
             'match': ['PEACOCK'],
             'ends_on': '2027-12-31',
             'notes': 'Subscribe direct through Peacock with the card. $5 on Premium Plus '
                      'per some sources — verify your tier. Network promo'},
            {'name': 'Transit credit (Mastercard)', 'amount': 2.50, 'cadence': 'monthly',
             'notes': 'After $10+ of tap-to-pay transit spend at participating systems. '
                      'Network promo'},
        ],
        'benefits': [
            {'category': 'lounge', 'name': 'Admirals Club membership',
             'notes': 'Full membership for you, and for authorized users per Citi'},
            {'category': 'status', 'name': 'Avis President\'s Club'},
            {'category': 'status', 'name': 'Omni Hotels Champion',
             'notes': 'Plus a free-night award after a qualifying 2+ night stay'},
            {'category': 'earning', 'name': 'Loyalty Point milestone bonuses',
             'notes': 'Up to 40,000 LP, at 50k / 90k / 165k / 240k Loyalty Points'},
            {'category': 'protection', 'name': 'Travel protection',
             'notes': 'Trip cancellation/interruption, trip delay, lost baggage, car rental '
                      'insurance. No foreign transaction fees'},
            {'category': 'other', 'name': 'AA travel perks',
             'notes': 'First checked bag free on domestic AA itineraries; priority check-in, '
                      'security and boarding'},
            {'category': 'other', 'name': 'Authorized users cost $175 each',
             'notes': 'The Aug 2026 refresh ended the 3-free-authorized-users benefit'},
        ],
    },
}


def template_annual_total(key):
    """A template's full-year credit value, honoring per-period overrides."""
    tpl = PERK_TEMPLATES.get(key)
    if not tpl:
        return 0.0
    total = 0.0
    for perk in tpl['perks']:
        cadence = perk['cadence']
        overrides = perk.get('amount_by_period') or {}
        base = float(perk.get('amount') or 0)
        if overrides:
            # Walk a representative year so December-style overrides count.
            total += sum(amount_for_period(base, overrides, k)
                         for k in periods_in_year(cadence, datetime.now().year))
        else:
            total += base * CADENCE_PERIODS_PER_YEAR[cadence]
    return total


# Precomputed so templates can show "worth $X a year" without doing the math
# in Jinja.
TEMPLATE_TOTALS = {key: template_annual_total(key) for key in PERK_TEMPLATES}


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------

def _urgency(days_left):
    if days_left <= 14:
        return 'high'
    if days_left <= 45:
        return 'med'
    return 'info'


def build_alerts(groups, card_perks, match, year, today=None):
    """Flatten contributions and perks into a ranked alert list.

    Severity order is high → med → info; the dashboard card shows the top few.
    """
    today = today or datetime.now()
    alerts = []

    for g in groups:
        if g.get('uncapped'):
            # No cap and no deadline, so nothing can be forfeited — this is
            # informational only and must never read as urgent.
            alerts.append({
                'severity': 'info',
                'title': f"{g['label']}: {'$'}{g['contributed']:,.0f} contributed in {year}",
                'detail': 'No IRS limit applies to this account.',
                'kind': 'contribution',
            })
            continue
        if not g['limit']:
            continue
        if g['over'] > 0:
            alerts.append({
                'severity': 'high',
                'title': f"{g['label']} over the limit",
                'detail': f"${g['over']:,.0f} above the ${g['limit']:,.0f} cap for {year}. "
                          f"Excess contributions are taxable until withdrawn.",
                'kind': 'contribution',
            })
            continue
        if g['remaining'] <= 0:
            alerts.append({
                'severity': 'info',
                'title': f"{g['label']} maxed out",
                'detail': f"${g['contributed']:,.0f} of ${g['limit']:,.0f} for {year}.",
                'kind': 'contribution',
            })
            continue

        detail = (f"${g['remaining']:,.0f} of room left before "
                  f"{g['deadline'].strftime('%b %-d, %Y')} ({g['days_left']} days).")
        if g['per_period_needed']:
            detail += f" ~${g['per_period_needed']:,.0f} per remaining paycheck."
        severity = _urgency(g['days_left'])
        if g['scheduled_remaining'] is not None and g['scheduled_remaining'] < g['remaining']:
            gap = g['remaining'] - g['scheduled_remaining']
            detail += (f" Current payroll election only adds ${g['scheduled_remaining']:,.0f} "
                       f"— ${gap:,.0f} short.")
            severity = 'high' if g['days_left'] <= 90 else 'med'
        alerts.append({
            'severity': severity,
            'title': f"{g['label']}: ${g['remaining']:,.0f} left to contribute",
            'detail': detail,
            'kind': 'contribution',
        })

    if match and not match['captured']:
        alerts.append({
            'severity': 'high',
            'title': f"Employer match short by ${match['match_at_risk']:,.0f}",
            'detail': (f"Contributing ${match['contributed']:,.0f}; "
                       f"${match['needed_contribution']:,.0f} "
                       f"({match['match_limit_pct']:g}% of salary) is needed for the full match."),
            'kind': 'match',
        })

    for card in card_perks:
        name = card['card'].get('name') or 'Card'
        # Credits close to their reset get their own line; everything else is
        # rolled into one summary so a card with a dozen perks can't bury the
        # contribution alerts.
        not_urgent_value, not_urgent_count = 0.0, 0
        for perk in card['perks']:
            cell = perk['current']
            if not cell or cell['state'] in ('used', 'future'):
                continue
            outstanding = cell['amount'] - cell['used']
            if outstanding <= 0.01:
                continue
            days = cell['days_left'] or 0
            if days > 21:
                not_urgent_value += outstanding
                not_urgent_count += 1
                continue
            alerts.append({
                'severity': 'high' if days <= 7 else 'med',
                'title': f"{name} — {perk['name']}: ${outstanding:,.0f} unused",
                'detail': f"Resets {cell['ends'].strftime('%b %-d')} — {days} days left.",
                'kind': 'perk',
            })
        if not_urgent_count:
            alerts.append({
                'severity': 'info',
                'title': (f"{name}: ${not_urgent_value:,.0f} across {not_urgent_count} "
                          f"credit{'' if not_urgent_count == 1 else 's'} still open"),
                'detail': 'More than three weeks before these reset.',
                'kind': 'perk',
            })
        if card['annual_fee'] and card['captured'] < card['annual_fee']:
            gap = card['annual_fee'] - card['captured']
            remaining_days = (datetime(year, 12, 31) - today).days
            alerts.append({
                'severity': 'med' if remaining_days <= 120 else 'info',
                'title': f"{name}: ${gap:,.0f} short of covering the annual fee",
                'detail': (f"${card['captured']:,.0f} of credits used against a "
                           f"${card['annual_fee']:,.0f} fee; ${card['available_now']:,.0f} "
                           f"is available right now."),
                'kind': 'fee',
            })

    order = {'high': 0, 'med': 1, 'info': 2}
    alerts.sort(key=lambda a: order.get(a['severity'], 3))
    return alerts
