"""Web-app route fixes: transfer delete, history year, horizon, calendar, home-move income."""
from datetime import date, datetime
from unittest import mock

from bson import ObjectId


def _primary(db, **extra):
    return db.accounts.insert_one({'name': 'Checking', 'account_type': 'checking',
                                   'balance': 1000.0, 'is_primary': True, **extra}).inserted_id


# ---------------------------------------------------------------- 8. delete_transfer
def test_delete_transfer_removes_paired_ledger_rows(client, db):
    primary = _primary(db)
    savings = db.accounts.insert_one({'name': 'Savings', 'account_type': 'savings',
                                      'balance': 0.0}).inserted_id
    client.post('/transfers/add', data={'from_account_id': str(primary),
                                        'to_account_id': str(savings),
                                        'amount': '75', 'date': '2026-04-10',
                                        'description': 'Move'})
    assert db.transactions.count_documents({}) == 2
    tid = db.transfers.find_one()['_id']

    client.post(f'/transfers/delete/{tid}')

    assert db.transfers.count_documents({}) == 0
    assert db.transactions.count_documents({}) == 0


def test_delete_transfer_also_matches_legacy_string_account_ids(client, db):
    a, b = ObjectId(), ObjectId()
    when = datetime(2026, 4, 10)
    tid = db.transfers.insert_one({'from_account_id': a, 'to_account_id': b, 'amount': 5.0,
                                   'date': when, 'description': 'Old'}).inserted_id
    db.transactions.insert_many([
        {'description': 'Transfer to: Old', 'date': when, 'account_id': str(a),
         'amount': 5.0, 'transaction_type': 'expense'},
        {'description': 'Transfer from: Old', 'date': when, 'account_id': str(b),
         'amount': 5.0, 'transaction_type': 'income'},
    ])
    client.post(f'/transfers/delete/{tid}')
    assert db.transactions.count_documents({}) == 0


# ---------------------------------------------------------------- 9. /history?year=
class _January(date):
    @classmethod
    def today(cls):
        return cls(2027, 1, 20)


def test_history_honours_year_param_in_january(app_module, client, db):
    _primary(db)
    db.transactions.insert_one({'description': 'Old groceries', 'amount': 42.0,
                                'date': datetime(2026, 6, 3), 'transaction_type': 'expense'})
    with mock.patch.object(app_module, 'date', _January):
        past = client.get('/history?year=2026')
        current = client.get('/history')
    assert past.status_code == 200
    assert b'Old groceries' in past.data
    assert current.status_code == 200
    assert b'Old groceries' not in current.data


# ---------------------------------------------------------------- 10. horizon
def test_bad_horizon_falls_back_to_default(client, db):
    acct = db.accounts.insert_one({'name': 'IRA', 'account_type': 'ira', 'balance': 10000.0,
                                   'estimated_return_rate': 7.0}).inserted_id
    for bad in ('abc', '-5', '99999', ''):
        resp = client.get(f'/accounts/{acct}?horizon={bad}')
        assert resp.status_code == 200, bad
    assert client.get(f'/accounts/{acct}?horizon=60').status_code == 200


# ---------------------------------------------------------------- 11. calendar
def test_weekly_items_appear_on_calendar(client, db):
    db.recurring_transactions.insert_one({
        'description': 'Lawn care', 'amount': 40.0, 'frequency': 'weekly',
        'start_date': datetime(2026, 9, 2), 'transaction_type': 'expense'})  # a Wednesday
    events = client.get('/api/calendar/events?year=2026&month=9').get_json()
    assert [e['date'] for e in events if e['title'] == 'Lawn care'] == [
        '2026-09-02', '2026-09-09', '2026-09-16', '2026-09-23', '2026-09-30']


def test_calendar_respects_end_date(client, db):
    db.recurring_transactions.insert_one({
        'description': 'Lessons', 'amount': 20.0, 'frequency': 'weekly',
        'start_date': datetime(2026, 9, 2), 'end_date': datetime(2026, 9, 15),
        'transaction_type': 'expense'})
    events = client.get('/api/calendar/events?year=2026&month=9').get_json()
    assert [e['date'] for e in events] == ['2026-09-02', '2026-09-09']


# ---------------------------------------------------------------- 12. home move income
def test_home_move_income_skips_archived_items(app_module, db):
    db.recurring_transactions.insert_many([
        {'description': 'Salary', 'amount': 5000.0, 'frequency': 'monthly', 'day_of_month': '1',
         'transaction_type': 'income', 'start_date': datetime(2025, 1, 1)},
        {'description': 'Old side gig', 'amount': 900.0, 'frequency': 'monthly',
         'transaction_type': 'income', 'start_date': datetime(2025, 1, 1), 'archived': True},
    ])
    assert app_module._home_move_monthly_income() == 5000.0
