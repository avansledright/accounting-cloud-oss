"""Recurring-item schedule math, shared by the web app and the agent/MCP tools.

Pure functions (no DB, no Flask), so the MCP server can import them without
pulling in app.py. The ledger outlook, the calendar, the budget and account
pages and the advisor tools all go through here, so a frequency means the same
thing everywhere:

- ``weekly``     every 7 days on the start date's weekday
- ``biweekly``   every 14 days anchored on the start date
- ``monthly``    every month, on each day listed in ``day_of_month`` ('15,30'
                 is a semi-monthly payday: two occurrences a month)
- ``bi-monthly`` every OTHER month (the start month, then +2, +4, ...), on the
                 listed days
- ``yearly``     once a year in the start date's month, on the listed days
"""
import calendar
from datetime import date, datetime, timedelta


def calc_monthly_amount(amount, frequency, day_of_month):
    """Calculate monthly amount from a recurring transaction's frequency and day(s)."""
    days_count = len(str(day_of_month or '1').split(',')) if day_of_month else 1
    if frequency == 'weekly':
        return amount * 52 / 12
    elif frequency == 'biweekly':
        return amount * 26 / 12
    elif frequency == 'monthly':
        return amount * days_count
    elif frequency == 'bi-monthly':
        return (amount * days_count) / 2
    elif frequency == 'yearly':
        return (amount * days_count) / 12
    else:
        return amount * days_count


def to_date(value):
    """A stored start/end date (datetime, date or ISO string) as a date."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.fromisoformat(value).date()


def occurrence_dates(item, month_start, month_end):
    """Dates a recurring transaction or transfer falls on within one month.

    Only applies the start date; callers decide how to treat ``end_date``
    (the ledger outlook drops an item only once a whole month is past it).
    """
    start = to_date(item['start_date'])
    frequency = item.get('frequency')
    dates = []
    if frequency == 'weekly':
        # Every occurrence of the start date's weekday within the month
        d = month_start
        while d <= month_end:
            if d.weekday() == start.weekday() and d >= start:
                dates.append(d)
            d += timedelta(days=1)
    elif frequency == 'biweekly':
        # Every 14 days anchored on start_date
        d = start
        if d < month_start:
            d += timedelta(days=((month_start - d).days + 13) // 14 * 14)
        while d <= month_end:
            dates.append(d)
            d += timedelta(days=14)
    else:
        applies = False
        if frequency == 'monthly':
            applies = True
        elif frequency == 'yearly':
            applies = start.month == month_start.month
        elif frequency == 'bi-monthly':
            month_diff = (month_start.year - start.year) * 12 + (month_start.month - start.month)
            applies = month_diff >= 0 and month_diff % 2 == 0

        if applies:
            max_day = calendar.monthrange(month_start.year, month_start.month)[1]
            days = []
            if item.get('day_of_month'):
                for day_str in str(item['day_of_month']).split(','):
                    try:
                        days.append(min(int(day_str.strip()), max_day))
                    except ValueError:
                        pass
            if not days:
                days = [1]
            for day in days:
                dates.append(date(month_start.year, month_start.month, day))

    # Filter out dates before the start date
    return [d for d in dates if d >= start]
