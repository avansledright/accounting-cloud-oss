"""Loan balance math, shared by the web app and the agent/MCP tools.

A loan's stored balance is its *starting* balance: a debt's ``balance`` or an
asset's ``liability_balance``. The current balance is found by walking the
ledger payments linked to it (``debt_id`` / ``asset_liability_id``) in date
order and taking off the principal part of each one (payment minus accrued
interest minus escrow).

Anything that reports what is owed today must go through ``current_debt_balance``
or ``current_asset_liability``, never the stored field, or it reports the
balance from the day the loan was entered.

A loan with a *buydown* (the seller, builder or lender funds a reserve that
pays part of each payment for a while) is entered at its note rate, with the
credit recorded in ``buydown``. The borrower's ledger payment is smaller than
the contractual one by that credit, so the credit is added back before the
payment is split; otherwise the principal part comes out wrong every month.

No Flask here; functions that read payments take the database as an argument.
"""
import re
from datetime import date, datetime


def as_date(value):
    """Coerce a stored date (datetime/date/ISO string) to a date for arithmetic."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value).date()
        except ValueError:
            return None
    return None


_BUYDOWN_LINE = re.compile(
    r'^\$?\s*(?P<credit>\d[\d,]*(?:\.\d+)?)\s*(?:(?:until|through|thru|to)?\s*(?P<until>\d{4}-\d{2}-\d{2}))?$',
    re.IGNORECASE)


def parse_buydown(raw):
    """Parse the buydown field into steps: [{'credit': float, 'until': datetime|None}].

    One step per line (or separated by ';'): the monthly credit, then optionally
    the last date it applies, e.g. ``200.00 until 2027-12-31``. A step with no
    date applies from then on. A 2-1 buydown is two lines. Empty input is no
    buydown. Raises ValueError on a line it can't read.
    """
    steps = []
    for line in re.split(r'[;\n]', raw or ''):
        line = line.strip()
        if not line:
            continue
        m = _BUYDOWN_LINE.match(line)
        if not m:
            raise ValueError(
                f"Can't read buydown step {line!r}. Use: amount, then optionally "
                f"'until YYYY-MM-DD', e.g. 200.00 until 2027-12-31")
        credit = float(m.group('credit').replace(',', ''))
        try:
            until = datetime.fromisoformat(m.group('until')) if m.group('until') else None
        except ValueError:
            raise ValueError(f"Buydown step {line!r} has a date that doesn't exist.")
        if credit > 0:
            steps.append({'credit': credit, 'until': until})
    return _sorted_steps(steps)


def _sorted_steps(steps):
    """Dated steps in date order, then any open-ended one."""
    return sorted(steps or [], key=lambda st: (st.get('until') is None,
                                               as_date(st.get('until')) or date.max))


def format_buydown(steps):
    """Inverse of parse_buydown, for the edit form, CSV export and tool output."""
    lines = []
    for st in _sorted_steps(steps):
        until = as_date(st.get('until'))
        lines.append(f"{st.get('credit') or 0:.2f}" + (f" until {until.isoformat()}" if until else ''))
    return '\n'.join(lines)


def buydown_credit(steps, on_date=None):
    """Monthly buydown credit in effect on `on_date` (a date; None = today).
    0 when there is no buydown or every step has ended."""
    if not steps:
        return 0.0
    on_date = as_date(on_date) or date.today()
    for st in _sorted_steps(steps):
        until = as_date(st.get('until'))
        if until is None or on_date <= until:
            return float(st.get('credit') or 0)
    return 0.0


def buydown_for_payment(steps, pdate, days, frequency='monthly'):
    """Buydown credit that came with one payment made `days` after the previous one.

    The reserve pays a fixed amount per scheduled payment, so this counts whole
    payment periods rather than prorating by the day: a normal payment gets one
    credit (exactly the statement's figure, whether the month had 28 or 31
    days), an extra payment a few days later gets none, and a payment covering
    a skipped month gets two. Biweekly schedules get 12/26 of the monthly credit."""
    credit_m = buydown_credit(steps, pdate) if steps else 0.0
    if not credit_m:
        return 0.0
    if frequency == 'biweekly':
        period, per_period = 14, credit_m * 12 / 26
    else:
        period, per_period = 30.44, credit_m
    return per_period * int(days / period + 0.5)


def amortize(starting_balance, annual_rate, monthly_escrow, payments, frequency='monthly',
             buydown=None):
    """Walk linked payments in date order, splitting each into interest, escrow,
    and principal. Interest accrues on elapsed days since the previous payment;
    escrow is prorated the same way. Returns totals plus a per-payment breakdown.

    `buydown` is the loan's buydown steps (see parse_buydown). The credit in
    effect on a payment's date is money the reserve paid alongside it, so it
    counts toward the payment before the split (see buydown_for_payment)."""
    rate = (annual_rate or 0) / 100.0
    escrow_m = monthly_escrow or 0
    default_days = 14 if frequency == 'biweekly' else 30.44
    remaining = starting_balance or 0
    prev_date = None
    total_principal = total_interest = total_escrow = total_credit = 0.0
    rows = []
    for p in sorted(payments, key=lambda x: as_date(x['date']) or date.min):
        pdate = as_date(p['date'])
        if prev_date is not None and pdate is not None:
            days = max(0.0, (pdate - prev_date).days)
        else:
            days = default_days
        interest = remaining * rate * (days / 365.0) if rate else 0.0
        escrow = escrow_m * (days / 30.44) if escrow_m else 0.0
        credit = buydown_for_payment(buydown, pdate, days, frequency)
        principal = (p.get('amount') or 0) + credit - interest - escrow
        if principal < 0:
            principal = 0.0
        if principal > remaining:
            principal = remaining
        remaining -= principal
        total_principal += principal
        total_interest += interest
        total_escrow += escrow
        total_credit += credit
        rows.append({
            '_id': p.get('_id'),
            'date': p['date'],
            'description': p.get('description'),
            'account_name': p.get('account_name'),
            'amount': p.get('amount') or 0,
            'interest': round(interest, 2),
            'escrow': round(escrow, 2),
            'credit': round(credit, 2),
            'principal': round(principal, 2),
            'balance_after': round(remaining, 2),
        })
        prev_date = pdate
    return {
        'current_balance': max(0.0, round(remaining, 2)),
        'principal_paid': round(total_principal, 2),
        'interest_paid': round(total_interest, 2),
        'escrow_paid': round(total_escrow, 2),
        'credit_applied': round(total_credit, 2),
        'rows': rows,
    }


def monthly_escrow(asset):
    """Sum monthly escrow: property_tax/12 + insurance/12 + pmi + hoa."""
    total = 0
    if asset.get('property_tax_annual'):
        total += asset['property_tax_annual'] / 12
    if asset.get('homeowners_insurance_annual'):
        total += asset['homeowners_insurance_annual'] / 12
    if asset.get('pmi_monthly'):
        total += asset['pmi_monthly']
    if asset.get('hoa_monthly'):
        total += asset['hoa_monthly']
    return total


def payments_by_link(db, field):
    """Map of linked-loan id (str) -> list of payment dicts, for the given link field
    ('debt_id' or 'asset_liability_id')."""
    out = {}
    acct_names = {str(a['_id']): a['name'] for a in db.accounts.find()}
    for t in db.transactions.find({field: {'$exists': True, '$ne': None}}):
        key = str(t[field])
        out.setdefault(key, []).append({
            '_id': str(t['_id']),
            'date': t.get('date'),
            'amount': t.get('amount') or 0,
            'description': t.get('description'),
            'account_name': acct_names.get(str(t.get('account_id')), '—'),
        })
    return out


def amortize_debt(debt, payments):
    return amortize(debt.get('balance', 0) or 0, debt.get('interest_rate', 0), 0,
                    payments, 'monthly')


def amortize_asset_liability(asset, payments):
    return amortize(asset.get('liability_balance', 0) or 0, asset.get('interest_rate', 0),
                    monthly_escrow(asset), payments,
                    asset.get('payment_frequency', 'monthly') or 'monthly',
                    buydown=asset.get('buydown'))


def current_debt_balance(debt, payments_map):
    """What is owed on a standalone debt today. payments_map: payments_by_link(db, 'debt_id')."""
    return amortize_debt(debt, payments_map.get(str(debt['_id']), []))['current_balance']


def current_asset_liability(asset, payments_map):
    """What is owed on an asset's loan today. payments_map:
    payments_by_link(db, 'asset_liability_id')."""
    if not (asset.get('liability_balance') or 0) > 0:
        return 0.0
    return amortize_asset_liability(asset, payments_map.get(str(asset['_id']), []))['current_balance']
