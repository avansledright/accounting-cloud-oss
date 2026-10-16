"""Advisor/MCP read tools use the web app's recurrence math and report honest values."""
import calendar
from datetime import date, datetime
from unittest import mock

import pytest
from dateutil.relativedelta import relativedelta

from recurrence import calc_monthly_amount, occurrence_dates
from agent.tools import budget, cashflow, portfolio


# ---------------------------------------------------------------- 6. frequency math
def test_calc_monthly_amount_matches_web_definitions():
    assert calc_monthly_amount(1500.0, 'biweekly', None) == pytest.approx(1500.0 * 26 / 12)
    assert calc_monthly_amount(4000.0, 'monthly', '15,30') == pytest.approx(8000.0)
    assert calc_monthly_amount(120.0, 'bi-monthly', '5') == pytest.approx(60.0)  # every other month
    assert calc_monthly_amount(100.0, 'weekly', None) == pytest.approx(100 * 52 / 12)
    assert calc_monthly_amount(1200.0, 'yearly', None) == pytest.approx(100.0)


def test_recurring_items_monthly_equivalents(db):
    db.recurring_transactions.insert_many([
        {'description': 'Salary A', 'amount': 4000.0, 'frequency': 'monthly',
         'day_of_month': '15,30', 'start_date': datetime(2025, 1, 1), 'transaction_type': 'income'},
        {'description': 'Salary B', 'amount': 1500.0, 'frequency': 'biweekly',
         'start_date': datetime(2025, 1, 3), 'transaction_type': 'income'},
        {'description': 'Water', 'amount': 120.0, 'frequency': 'bi-monthly', 'day_of_month': '5',
         'start_date': datetime(2025, 1, 5), 'transaction_type': 'expense'},
    ])
    out = budget.get_recurring_items()
    assert 'Salary A: $4,000.00/monthly (~$8,000.00/mo)' in out
    assert 'Salary B: $1,500.00/biweekly (~$3,250.00/mo)' in out
    assert 'Water: $120.00/bi-monthly (~$60.00/mo)' in out


def test_cashflow_counts_the_occurrences_the_web_ledger_projects(app_module, db):
    primary = db.accounts.insert_one({'name': 'Checking', 'account_type': 'checking',
                                      'balance': 0.0, 'is_primary': True}).inserted_id
    db.recurring_transactions.insert_many([
        # account_id None means the primary account
        {'description': 'Salary B', 'amount': 100.0, 'frequency': 'biweekly',
         'start_date': datetime(2025, 1, 3), 'transaction_type': 'income', 'account_id': None},
        {'description': 'Water', 'amount': 30.0, 'frequency': 'bi-monthly', 'day_of_month': '5',
         'start_date': datetime(2025, 1, 5), 'transaction_type': 'expense', 'account_id': primary},
    ])
    months, _, _ = cashflow._generate_outlook()

    today = date.today()
    for i, m in enumerate(months):
        md = today + relativedelta(months=i)
        start = date(md.year, md.month, 1)
        end = date(md.year, md.month, calendar.monthrange(md.year, md.month)[1])
        pays = len(occurrence_dates({'start_date': datetime(2025, 1, 3), 'frequency': 'biweekly'}, start, end))
        water = 1 if ((md.year - 2025) * 12 + md.month - 1) % 2 == 0 else 0
        assert m['income'] == pytest.approx(100.0 * pays), m['month']
        assert m['expenses'] == pytest.approx(30.0 * water), m['month']
    # Two or three paydays a month, never the flat "1" the tool used to assume
    assert {round(m['income']) for m in months} <= {200, 300}


def test_cashflow_matches_web_outlook_projection(app_module, db):
    primary = db.accounts.insert_one({'name': 'Checking', 'account_type': 'checking',
                                      'balance': 0.0, 'is_primary': True}).inserted_id
    db.recurring_transactions.insert_one({
        'description': 'Gym', 'amount': 10.0, 'frequency': 'weekly',
        'start_date': datetime(2025, 1, 6), 'transaction_type': 'expense', 'account_id': None})
    months, _, _ = cashflow._generate_outlook(str(primary))
    web = app_module.calculate_yearly_outlook(0.0, str(primary), max_months_back=0)
    web_by_month = {}
    for m in web:
        n = sum(1 for t in m['transactions'] if t['description'] == 'Gym')
        web_by_month[datetime.strptime(m['month'], '%B %Y').strftime('%Y-%m')] = n * 10.0
    for m in months[1:]:  # the current month is partly actuals on the web side
        assert m['expenses'] == pytest.approx(web_by_month[m['month']]), m['month']


# ---------------------------------------------------------------- 7. portfolio
def test_portfolio_summary_reports_live_value_separately_from_cost(db):
    acct = db.accounts.insert_one({'name': 'Brokerage', 'account_type': 'brokerage'}).inserted_id
    db.holdings.insert_many([
        {'account_id': acct, 'symbol': 'VTI', 'quantity': 10.0, 'cost_basis': 2000.0},
        {'account_id': acct, 'symbol': 'XYZ', 'quantity': 5.0, 'cost_basis': 100.0},
    ])
    prices = {'VTI': 300.0, 'XYZ': None}
    with mock.patch.object(portfolio, 'fetch_live_price', side_effect=prices.get):
        out = portfolio.get_portfolio_summary()
    assert 'Market value (live): $3,000.00 (1 holding(s) without a live price excluded)' in out
    assert 'Total cost basis: $2,100.00' in out
    assert 'Unrealized gain/loss: $+1,000.00 (+50.0%)' in out


def test_portfolio_summary_without_prices_does_not_call_cost_market_value(db):
    acct = db.accounts.insert_one({'name': 'Brokerage', 'account_type': 'brokerage'}).inserted_id
    db.holdings.insert_one({'account_id': acct, 'symbol': 'VTI', 'quantity': 1.0, 'cost_basis': 200.0})
    with mock.patch.object(portfolio, 'fetch_live_price', return_value=None):
        out = portfolio.get_portfolio_summary()
    assert 'Market value: unavailable' in out
    assert 'Total cost basis: $200.00' in out
