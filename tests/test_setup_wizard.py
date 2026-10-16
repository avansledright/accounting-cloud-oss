"""First-run setup wizard, demo-data mode and the getting-started checklist.

app.py connects to MongoDB at import time, so these tests swap in mongomock
before importing it. Skipped when mongomock is not installed
(`pip install -r requirements-dev.txt`).
"""
from datetime import date, datetime, timedelta

import pytest

mongomock = pytest.importorskip('mongomock')


@pytest.fixture(scope='module')
def app_module():
    import pymongo
    real = pymongo.MongoClient
    pymongo.MongoClient = mongomock.MongoClient
    try:
        import app as app_module
    finally:
        pymongo.MongoClient = real
    app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    return app_module


@pytest.fixture()
def db(app_module):
    for name in app_module.db.list_collection_names():
        app_module.db[name].delete_many({})
    return app_module.db


@pytest.fixture()
def client(app_module, db):
    return app_module.app.test_client()


def _walk_to_account(client, balance='2500.00'):
    client.post('/setup/welcome', data={'household_name': 'Test Household'})
    return client.post('/setup/account', data={
        'name': 'Main Checking', 'institution': 'Example Bank',
        'account_type': 'checking', 'balance': balance})


def test_fresh_install_redirects_dashboard_to_setup(client):
    r = client.get('/')
    assert r.status_code == 302
    assert r.headers['Location'].endswith('/setup/welcome')
    page = client.get('/setup/welcome')
    assert page.status_code == 200
    assert b'Load demo data instead' in page.data


def test_existing_install_with_accounts_is_not_redirected(client, db):
    db.accounts.insert_one({'name': 'Old', 'account_type': 'checking', 'balance': 1.0, 'is_primary': True})
    assert client.get('/').status_code == 200


def test_skip_stops_the_redirect(client, db):
    r = client.post('/setup/skip')
    assert r.status_code == 302
    assert db.app_settings.find_one({'_id': 'app'})['setup_skipped'] is True
    assert client.get('/').status_code == 200


def test_account_step_creates_primary_account_with_datetime(client, db):
    r = _walk_to_account(client)
    assert r.headers['Location'].endswith('/setup/income')
    accounts = list(db.accounts.find())
    assert len(accounts) == 1
    acct = accounts[0]
    assert acct['is_primary'] is True
    assert acct['balance'] == 2500.0
    assert acct['institution'] == 'Example Bank'
    assert type(acct['balance_as_of_date']) is datetime  # never datetime.date (BSON)
    assert db.app_settings.find_one({'_id': 'app'})['household_name'] == 'Test Household'


def test_account_step_again_updates_instead_of_duplicating(client, db):
    _walk_to_account(client)
    client.post('/setup/account', data={'name': 'Renamed', 'institution': 'Other Bank',
                                        'account_type': 'checking', 'balance': '99'})
    accounts = list(db.accounts.find())
    assert len(accounts) == 1
    assert accounts[0]['name'] == 'Renamed' and accounts[0]['balance'] == 99.0


def test_account_step_rejects_non_cash_type(client, db):
    client.post('/setup/welcome', data={})
    r = client.post('/setup/account', data={'name': 'X', 'account_type': '401k', 'balance': '1'})
    assert r.status_code == 400
    assert db.accounts.count_documents({}) == 0


def test_income_step_creates_recurring_income_on_primary(client, db):
    _walk_to_account(client)
    primary = db.accounts.find_one({'is_primary': True})
    payday = (date.today() - timedelta(days=3)).isoformat()   # a recent payday
    r = client.post('/setup/income', data={
        'income_description': ['Paycheck', 'Side gig', ''],
        'income_amount': ['2000', '150', ''],
        'income_category': ['Salary', 'Freelance', 'Salary'],
        'income_frequency': ['biweekly', 'twice_monthly', 'biweekly'],
        'income_date': [payday, date.today().replace(day=1).isoformat(), ''],
        'income_second_day': ['', '15', ''],
        'income_auto_pay': ['', '', ''],
    })
    assert r.status_code == 302 and r.headers['Location'].endswith('/setup/bills')
    items = {i['description']: i for i in db.recurring_transactions.find()}
    assert set(items) == {'Paycheck', 'Side gig'}
    pay = items['Paycheck']
    assert pay['transaction_type'] == 'income'
    assert pay['frequency'] == 'biweekly'
    assert pay['account_id'] == primary['_id']
    assert type(pay['start_date']) is datetime
    # Re-anchored on the next payday on or after today, keeping the 14-day cadence.
    today = datetime.combine(date.today(), datetime.min.time())
    assert pay['start_date'] >= today
    assert (pay['start_date'] - datetime.fromisoformat(payday)).days % 14 == 0
    side = items['Side gig']
    assert side['frequency'] == 'monthly' and side['day_of_month'] == '1,15'
    assert side['category'] == 'Freelance'


def test_income_step_reports_errors_and_writes_nothing(client, db):
    _walk_to_account(client)
    r = client.post('/setup/income', data={
        'income_description': ['Paycheck'], 'income_amount': ['abc'],
        'income_category': ['Salary'], 'income_frequency': ['monthly'],
        'income_date': [''], 'income_second_day': [''], 'income_auto_pay': [''],
    })
    assert r.status_code == 400
    assert b'enter an amount' in r.data
    assert db.recurring_transactions.count_documents({}) == 0


def test_income_step_requires_an_account(client):
    r = client.get('/setup/income')
    assert r.status_code == 302 and r.headers['Location'].endswith('/setup/account')


def test_bills_step_and_projection(client, db, app_module):
    _walk_to_account(client, balance='1000')
    due = date.today() + timedelta(days=5)
    yearly_due = date.today() - timedelta(days=30)   # last renewal: next one is in ~11 months
    r = client.post('/setup/bills', data={
        'bills_description': ['Rent', 'Insurance', ''],
        'bills_amount': ['1200', '600', ''],
        'bills_category': ['Housing', 'Insurance', 'Utilities'],
        'bills_frequency': ['monthly', 'yearly', 'monthly'],
        'bills_date': [due.isoformat(), yearly_due.isoformat(), ''],
        'bills_second_day': ['', '', ''],
        'bills_auto_pay': ['on', '', ''],
    })
    assert r.headers['Location'].endswith('/setup/extras')
    rent = db.recurring_transactions.find_one({'description': 'Rent'})
    assert rent['transaction_type'] == 'expense' and rent['category'] == 'Housing'
    assert rent['day_of_month'] == str(due.day) and rent['auto_pay'] is True
    ins = db.recurring_transactions.find_one({'description': 'Insurance'})
    assert ins['frequency'] == 'yearly' and ins['start_date'].date() > date.today()

    # The ledger projection picks the rent up on the primary account.
    primary = db.accounts.find_one({'is_primary': True})
    outlook = app_module.calculate_yearly_outlook(start_balance=1000, account_filter=str(primary['_id']),
                                                  max_months_back=0)
    descs = [t['description'] for m in outlook[:2] for t in m['transactions']]
    assert 'Rent' in descs


def test_add_more_stays_on_step(client, db):
    _walk_to_account(client)
    r = client.post('/setup/bills', data={
        'bills_description': ['Gym'], 'bills_amount': ['40'], 'bills_category': ['Personal'],
        'bills_frequency': ['monthly'], 'bills_date': [date.today().isoformat()],
        'bills_second_day': [''], 'bills_auto_pay': [''], 'action': 'add_more'})
    assert r.headers['Location'].endswith('/setup/bills')
    assert b'Gym' in client.get('/setup/bills').data


def test_extras_step_creates_account_and_debt(client, db):
    _walk_to_account(client)
    client.post('/setup/extras', data={'kind': 'account', 'name': 'Savings', 'account_type': 'savings',
                                       'institution': 'Example Online Bank', 'balance': '5000'})
    client.post('/setup/extras', data={'kind': 'account', 'name': 'Roth', 'account_type': 'roth_ira',
                                       'balance': '100', 'estimated_return_rate': '7'})
    client.post('/setup/extras', data={'kind': 'debt', 'name': 'Student Loan', 'debt_type': 'student_loan',
                                       'balance': '9000', 'interest_rate': '4.5', 'minimum_payment': '120'})
    assert db.accounts.count_documents({'is_primary': True}) == 1   # extras never steal primary
    roth = db.accounts.find_one({'name': 'Roth'})
    assert roth['estimated_return_rate'] == 7.0
    debt = db.debts.find_one({'name': 'Student Loan'})
    assert debt['balance'] == 9000.0 and debt['minimum_payment'] == 120.0
    r = client.post('/setup/extras', data={})
    assert r.headers['Location'].endswith('/setup/done')


def test_done_marks_setup_complete_and_dashboard_shows_ledger(client, db):
    _walk_to_account(client, balance='3210.55')
    client.post('/setup/income', data={
        'income_description': ['Paycheck'], 'income_amount': ['2000'], 'income_category': ['Salary'],
        'income_frequency': ['monthly'], 'income_date': [date.today().isoformat()],
        'income_second_day': [''], 'income_auto_pay': ['']})
    done = client.get('/setup/done')
    assert done.status_code == 200
    assert b'Projected balance' in done.data
    s = db.app_settings.find_one({'_id': 'app'})
    assert s['setup_completed'] is True and type(s['setup_completed_at']) is datetime

    dash = client.get('/')
    assert dash.status_code == 200
    assert b'Main Checking' in dash.data and b'3210.55' in dash.data
    assert b'Getting started' in dash.data
    assert b'Test Household' in dash.data
    ledger = client.get('/ledger')
    assert ledger.status_code == 200 and b'Paycheck' in ledger.data


def test_checklist_ticks_from_data_and_dismisses(client, db, app_module):
    db.app_settings.insert_one({'_id': 'app', 'setup_completed': True})
    with app_module.app.test_request_context():
        items = {i['label']: i['done'] for i in app_module.getting_started_checklist()}
    assert not any(items.values())
    db.accounts.insert_one({'name': 'A', 'account_type': 'checking', 'balance': 0, 'is_primary': True})
    db.budget_targets.insert_one({'category': 'Groceries', 'target': 500.0})
    db.networth_snapshots.insert_one({'date': datetime.now(), 'net_worth': 0})
    with app_module.app.test_request_context():
        items = {i['label']: i['done'] for i in app_module.getting_started_checklist()}
    assert items['Add your primary account'] and items['Set budget targets'] and items['Take a net worth snapshot']
    assert not items['Add recurring income']

    assert b'Getting started' in client.get('/').data
    client.post('/setup/checklist/dismiss')
    assert b'Getting started' not in client.get('/').data
    client.post('/setup/checklist/dismiss', data={'show': '1'})
    assert b'Getting started' in client.get('/').data


def test_demo_mode_loads_demo_data(client, db):
    r = client.post('/setup/demo')
    assert r.status_code == 302
    assert db.accounts.find_one({'is_primary': True})['name'] == 'Demo Checking'
    assert db.recurring_transactions.count_documents({}) > 0
    assert db.app_settings.find_one({'_id': 'app'})['setup_completed'] is True
    dash = client.get('/')
    assert dash.status_code == 200 and b'Demo Checking' in dash.data


def test_demo_mode_refuses_when_accounts_exist(client, db):
    _walk_to_account(client)
    r = client.post('/setup/demo')
    assert r.status_code == 400
    assert db.accounts.count_documents({}) == 1


def test_restart_reopens_wizard_without_losing_data(client, db):
    _walk_to_account(client)
    client.get('/setup/done')
    r = client.post('/setup/restart')
    assert r.headers['Location'].endswith('/setup/welcome')
    assert db.accounts.count_documents({}) == 1
    assert not db.app_settings.find_one({'_id': 'app'}).get('setup_completed')
    # Accounts exist, so the dashboard does not force the wizard again.
    assert client.get('/').status_code == 200


def test_main_pages_render_on_empty_database(client, db):
    db.app_settings.insert_one({'_id': 'app', 'setup_skipped': True})
    for path in ('/', '/ledger', '/budget', '/accounts', '/debts', '/goals', '/transfers',
                 '/portfolio', '/networth', '/credit-cards', '/history', '/admin', '/assets'):
        assert client.get(path).status_code == 200, path
    # Pages that used to render blank tables explain what to do instead.
    assert b'No primary account yet' in client.get('/ledger').data
    assert b'Start with your recurring items' in client.get('/budget').data
    assert b'Import your first statement' in client.get('/credit-cards').data
    assert b'No snapshots yet' in client.get('/networth').data
    assert b'Re-run setup wizard' in client.get('/admin').data
