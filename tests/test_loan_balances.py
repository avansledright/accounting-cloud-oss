"""The advisor/MCP read tools report what is owed today, the same figure the web
app shows, not the starting balance stored on the loan record.

A tool that shows the starting balance as "the balance" invites the reader to
correct it to the statement's payoff figure, and the web app then subtracts
every linked payment a second time.

All figures here are made up.
"""
from datetime import datetime

import pytest

import loans
from agent.tools import debts, networth, writes

CAR_PAYMENTS = [datetime(2026, m, 15) for m in range(3, 11)]


@pytest.fixture
def car(db):
    aid = db.assets.insert_one({
        'name': 'Family car', 'asset_type': 'vehicle', 'estimated_value': 18000.0,
        'liability_balance': 20000.0, 'interest_rate': 6.0, 'monthly_payment': 400.0,
    }).inserted_id
    db.transactions.insert_many([
        {'date': d, 'amount': 400.0, 'description': 'Car Payment',
         'transaction_type': 'expense', 'asset_liability_id': aid} for d in CAR_PAYMENTS])
    return aid


@pytest.fixture
def student_loan(db):
    did = db.debts.insert_one({'name': 'Student loan', 'debt_type': 'student_loan',
                               'balance': 10000.0, 'interest_rate': 0.0,
                               'minimum_payment': 500.0}).inserted_id
    db.transactions.insert_many([
        {'date': datetime(2026, m, 1), 'amount': 500.0, 'description': 'Student Loans',
         'transaction_type': 'expense', 'debt_id': did} for m in (9, 10)])
    return did


def test_linked_payments_come_off_principal_after_interest(car, db):
    owed = loans.current_asset_liability(db.assets.find_one({'_id': car}),
                                         loans.payments_by_link(db, 'asset_liability_id'))
    # Eight $400 payments at 6%: roughly $100 of each is interest at the start,
    # so the balance drops by about $2,400, not by the $3,200 paid.
    assert 17500 < owed < 17700


def test_tools_agree_with_the_web_app(app_module, car, student_loan, db):
    web_asset = app_module._asset_liability_total()
    web_debt = app_module._standalone_debt_total()
    assert web_debt == pytest.approx(9000.0)

    assert f"loan owed now=${web_asset:,.2f} (starting $20,000.00, 6.0%)" in writes.list_assets()
    assert "owed now $9,000.00 (starting $10,000.00)" in writes.list_debts()
    assert "Student loan (student_loan): $9,000.00 owed" in debts.get_debts()
    assert "Total debt: $9,000.00" in debts.get_total_debt()

    nw = networth.get_current_net_worth()
    assert f"Family car loan: ${web_asset:,.2f}" in nw
    assert f"Total liabilities: ${web_asset + web_debt:,.2f}" in nw
    assert "20,000.00" not in nw


def test_asset_without_a_loan_owes_nothing(db):
    db.assets.insert_one({'name': 'Old truck', 'asset_type': 'vehicle', 'estimated_value': 5900.0,
                          'liability_balance': None})
    assert "Old truck (vehicle): value=$5,900.00" in writes.list_assets()
    assert "loan owed" not in writes.list_assets()


def test_update_asset_sets_the_loan_rate(car, db):
    assert writes.update_asset(str(car), interest_rate=5.25) == "Updated."
    assert db.assets.find_one({'_id': car})['interest_rate'] == 5.25
    assert "5.25%" in writes.list_assets()
    assert writes.update_asset(str(car), interest_rate=525).startswith("Error")
    assert db.assets.find_one({'_id': car})['interest_rate'] == 5.25
