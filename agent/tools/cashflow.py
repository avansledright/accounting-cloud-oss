import calendar
from datetime import datetime, date, timedelta

from dateutil.relativedelta import relativedelta
from agent.db import db


def _generate_outlook(account_filter=None):
    """Generate 12-month cash flow outlook. Returns list of monthly summaries."""
    today = date.today()

    # Get primary account
    primary_account = db.accounts.find_one({'is_primary': True})
    primary_account_id = str(primary_account['_id']) if primary_account else None

    # Determine start balance
    if account_filter:
        acct = db.accounts.find_one({'_id': __import__('bson').ObjectId(account_filter)})
        start_balance = acct.get('balance', 0) if acct else 0
    elif primary_account:
        start_balance = primary_account.get('balance', 0)
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
            rt_start = rt['start_date']
            if isinstance(rt_start, datetime):
                rt_start = rt_start.date()
            if rt_start > month_end:
                continue
            if rt.get('end_date'):
                rt_end = rt['end_date']
                if isinstance(rt_end, datetime):
                    rt_end = rt_end.date()
                if rt_end < month_start:
                    continue

            # Account filter
            rt_acct = str(rt.get('account_id', '')) or primary_account_id
            if account_filter and rt_acct != account_filter:
                continue

            amount = rt.get('amount', 0)
            freq = rt.get('frequency', 'monthly')
            if freq == 'weekly':
                occurrences = 4
            elif freq == 'bi-monthly':
                occurrences = 2
            elif freq == 'yearly':
                if month_date.month == rt_start.month:
                    occurrences = 1
                else:
                    occurrences = 0
            elif freq == 'monthly':
                # day_of_month may list several days, e.g. "1,15" for a semi-monthly payday
                occurrences = len([d for d in str(rt.get('day_of_month') or '').split(',') if d.strip()]) or 1
            else:
                occurrences = 1

            if rt.get('transaction_type') == 'income':
                income += amount * occurrences
            else:
                expenses += amount * occurrences

        # Recurring transfers (affect source and destination)
        for xfer in recurring_xfers:
            amount = xfer.get('amount', 0)
            from_id = str(xfer.get('from_account_id', ''))
            to_id = str(xfer.get('to_account_id', ''))
            if account_filter:
                if account_filter == from_id:
                    expenses += amount
                elif account_filter == to_id:
                    income += amount

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
