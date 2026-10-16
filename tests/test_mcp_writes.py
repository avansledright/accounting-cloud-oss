"""MCP write tools do what the web app does for the same action."""
from datetime import date, datetime

from bson import ObjectId

from agent.tools import writes as W


def _accounts(db):
    primary = db.accounts.insert_one({'name': 'Checking', 'account_type': 'checking',
                                      'balance': 1000.0, 'is_primary': True}).inserted_id
    savings = db.accounts.insert_one({'name': 'Savings', 'account_type': 'savings',
                                      'balance': 500.0}).inserted_id
    return primary, savings


def _projected(outlook, recurring_id):
    return [t for m in outlook for t in m['transactions']
            if t.get('recurring_id') == str(recurring_id) and t['type'] == 'projected']


# ---------------------------------------------------------------- 1. overrides
def test_override_stored_as_date_string_and_applied_by_outlook(app_module, db):
    primary, _ = _accounts(db)
    nxt = date.today().replace(day=1)
    nxt = date(nxt.year + (nxt.month == 12), nxt.month % 12 + 1, 1)
    rid = db.recurring_transactions.insert_one({
        'description': 'Electric', 'amount': 100.0, 'frequency': 'monthly',
        'day_of_month': '1', 'start_date': datetime(2020, 1, 1),
        'transaction_type': 'expense', 'account_id': None}).inserted_id

    W.override_recurring_amount(str(rid), nxt.isoformat(), 140.0)

    row = db.projected_overrides.find_one({'recurring_id': rid})
    assert row['date'] == nxt.isoformat()  # same format as /projected/update-amount
    hit = [t for t in _projected(app_module.calculate_yearly_outlook(1000.0, str(primary)), rid)
           if t['date'] == nxt.isoformat()]
    assert hit and hit[0]['amount'] == -140.0 and hit[0]['has_override']


def test_outlook_tolerates_legacy_datetime_override(app_module, db):
    primary, _ = _accounts(db)
    nxt = date.today().replace(day=1)
    nxt = date(nxt.year + (nxt.month == 12), nxt.month % 12 + 1, 1)
    rid = db.recurring_transactions.insert_one({
        'description': 'Water', 'amount': 50.0, 'frequency': 'monthly',
        'day_of_month': '1', 'start_date': datetime(2020, 1, 1),
        'transaction_type': 'expense', 'account_id': None}).inserted_id
    # What the old MCP tool wrote
    db.projected_overrides.insert_one({'recurring_id': rid, 'amount': 75.0,
                                       'date': datetime(nxt.year, nxt.month, 1)})
    hit = [t for t in _projected(app_module.calculate_yearly_outlook(1000.0, str(primary)), rid)
           if t['date'] == nxt.isoformat()]
    assert hit and hit[0]['amount'] == -75.0


# ---------------------------------------------------------------- 2. transfers
def test_add_transfer_posts_paired_ledger_rows_like_web(db):
    primary, savings = _accounts(db)
    W.add_transfer(str(primary), str(savings), 250.0, '2026-05-01', 'Rainy day')

    t = db.transfers.find_one()
    assert t['from_account_id'] == primary and t['to_account_id'] == savings
    rows = {r['description']: r for r in db.transactions.find()}
    out, inc = rows['Transfer to: Rainy day'], rows['Transfer from: Rainy day']
    assert (out['transaction_type'], out['account_id'], out['amount']) == ('expense', primary, 250.0)
    assert (inc['transaction_type'], inc['account_id'], inc['amount']) == ('income', savings, 250.0)
    assert out['date'] == inc['date'] == datetime(2026, 5, 1)


def test_add_transfer_rejects_same_account(db):
    primary, _ = _accounts(db)
    assert W.add_transfer(str(primary), str(primary), 10.0, '2026-05-01').startswith('Error')
    assert db.transfers.count_documents({}) == 0


# ---------------------------------------------------------------- 3. delete
def test_delete_transaction_records_skip(db):
    _accounts(db)
    tid = db.transactions.insert_one({'description': 'Rent', 'amount': 1500.0,
                                      'date': datetime(2026, 6, 1),
                                      'transaction_type': 'expense'}).inserted_id
    assert W.delete_transaction(str(tid)) == 'Deleted.'
    assert db.transactions.count_documents({}) == 0
    assert db.skipped_transactions.find_one({'description': 'Rent', 'date': '2026-06-01'})


# ---------------------------------------------------------------- 4. account default
def test_add_transaction_defaults_to_primary_account(db):
    primary, savings = _accounts(db)
    W.add_transaction('Coffee', 4.5, '2026-06-02', 'expense')
    W.add_transaction('Interest', 1.25, '2026-06-02', 'income', str(savings))
    rows = {r['description']: r for r in db.transactions.find()}
    assert rows['Coffee']['account_id'] == primary
    assert rows['Interest']['account_id'] == savings
    assert isinstance(rows['Coffee']['date'], datetime)


# ---------------------------------------------------------------- 5. debts
def test_make_debt_payment_logs_linked_transaction_not_balance(app_module, db):
    primary, _ = _accounts(db)
    did = db.debts.insert_one({'name': 'Car', 'debt_type': 'personal_loan',
                               'balance': 10000.0, 'interest_rate': 0.0,
                               'minimum_payment': 300.0}).inserted_id

    W.make_debt_payment(str(did), 400.0, '2026-06-03')

    assert db.debts.find_one({'_id': did})['balance'] == 10000.0  # starting balance untouched
    txn = db.transactions.find_one({'debt_id': did})
    assert txn['amount'] == 400.0 and txn['transaction_type'] == 'expense'
    assert txn['account_id'] == primary
    assert txn['description'] == app_module.EXTRA_DEBT_PAYMENT_DESC
    # The web app's amortization sees it as a payment against the starting balance
    debt = app_module._decorate_debt(db.debts.find_one({'_id': did}))
    assert debt['current_balance'] == 9600.0


def test_update_debt_balance_sets_starting_balance(db):
    did = db.debts.insert_one({'name': 'Loan', 'debt_type': 'other', 'balance': 1.0}).inserted_id
    assert 'Starting balance' in W.update_debt_balance(str(did), 5000.0)
    assert db.debts.find_one({'_id': did})['balance'] == 5000.0


# ---------------------------------------------------------------- frequencies
def test_recurring_tools_accept_biweekly(db):
    assert not W.add_recurring('Pay', 2000.0, 'biweekly', '2026-01-02', 'income').startswith('Error')
    assert db.recurring_transactions.find_one()['frequency'] == 'biweekly'
