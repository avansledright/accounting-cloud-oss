import calendar
from datetime import datetime, date, timedelta

from dateutil.relativedelta import relativedelta
from agent.db import db
from recurrence import occurrence_dates, to_date


def _active_in(item, month_start, month_end):
    """Started by month end and not ended before month start (as the web outlook)."""
    if to_date(item['start_date']) > month_end:
        return False
    return not (item.get('end_date') and to_date(item['end_date']) < month_start)


def _generate_outlook(account_filter=None):
    """Generate 12-month cash flow outlook. Returns list of monthly summaries."""
    today = date.today()

    # Get primary account
    primary_account = db.accounts.find_one({'is_primary': True})
    primary_account_id = str(primary_account['_id']) if primary_account else None

    # Default to the primary account, like the web ledger
    if not account_filter:
        account_filter = primary_account_id

    # Determine start balance
    if account_filter:
        acct = db.accounts.find_one({'_id': __import__('bson').ObjectId(account_filter)})
        start_balance = acct.get('balance', 0) if acct else 0
    else:
        start_balance = 0

    transactions = list(db.transactions.find())
    recurring = list(db.recurring_transactions.find({'archived': {'$ne': True}}))  # skip paid-off loan lines
    recurring_xfers = list(db.recurring_transfers.find())

    running_balance = float(start_balance)
    months = []

    for i in range(12):
        month_date = today + relativedelta(months=i)
        month_start = date(month_date.year, month_date.month, 1)
        month_end = date(
            month_date.year, month_date.month,
            calendar.monthrange(month_date.year, month_date.month)[1],
        )

        income = 0.0
        expenses = 0.0

        # Recurring transactions
        for rt in recurring:
            if not _active_in(rt, month_start, month_end):
                continue

            # Account filter (no account_id means the primary account)
            rt_acct = str(rt['account_id']) if rt.get('account_id') else primary_account_id
            if account_filter and rt_acct != account_filter:
                continue

            # Same occurrence dates the web ledger projects (weekly, biweekly,
            # split-day monthly, every-other-month bi-monthly, yearly)
            occurrences = len(occurrence_dates(rt, month_start, month_end))
            amount = rt.get('amount', 0)
            if rt.get('transaction_type') == 'income':
                income += amount * occurrences
            else:
                expenses += amount * occurrences

        # Recurring transfers (affect source and destination)
        for xfer in recurring_xfers:
            if not account_filter or not _active_in(xfer, month_start, month_end):
                continue
            from_id = str(xfer.get('from_account_id', ''))
            to_id = str(xfer.get('to_account_id', ''))
            if account_filter not in (from_id, to_id):
                continue
            moved = xfer.get('amount', 0) * len(occurrence_dates(xfer, month_start, month_end))
            if account_filter == from_id:
                expenses += moved
            else:
                income += moved

        net = income - expenses
        running_balance += net
        months.append({
            'month': month_date.strftime('%Y-%m'),
            'income': income,
            'expenses': expenses,
            'net': net,
            'end_balance': running_balance,
        })

    # Track min balance
    min_balance = float(start_balance)
    for m in months:
        if m['end_balance'] < min_balance:
            min_balance = m['end_balance']

    return months, start_balance, min_balance


def get_cash_flow_outlook(account_id: str = "") -> str:
    """Get a 12-month cash flow outlook showing projected income, expenses, and ending balance per month.

    Args:
        account_id: Specific account ID to project. Leave empty for primary account.
    """
    acct_filter = account_id if account_id else None
    months, start_balance, min_balance = _generate_outlook(acct_filter)

    lines = [f"12-month cash flow outlook (starting balance: ${start_balance:,.2f}):"]
    for m in months:
        lines.append(
            f"  {m['month']}: +${m['income']:,.2f} -${m['expenses']:,.2f} = "
            f"${m['net']:+,.2f} → end ${m['end_balance']:,.2f}"
        )
    lines.append(f"\nLowest projected balance: ${min_balance:,.2f}")
    return "\n".join(lines)


def get_negative_balance_months(account_id: str = "") -> str:
    """Check which months the account balance is projected to dip below zero.

    Args:
        account_id: Specific account ID to check. Leave empty for primary account.
    """
    acct_filter = account_id if account_id else None
    months, start_balance, min_balance = _generate_outlook(acct_filter)

    negative = [m for m in months if m['end_balance'] < 0]
    if not negative:
        return f"No negative balance months projected. Lowest point: ${min_balance:,.2f}"

    lines = ["Months with projected negative balance:"]
    for m in negative:
        lines.append(f"  {m['month']}: ${m['end_balance']:,.2f}")
    return "\n".join(lines)
