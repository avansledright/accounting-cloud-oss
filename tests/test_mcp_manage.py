"""The MCP's edit/delete/link tools do what the web app's forms do."""
from datetime import datetime

from bson import ObjectId

from agent.tools import manage as M
from agent.tools import writes as W
from agent.tools.transactions import search_cc_transactions


def _accounts(db):
    primary = db.accounts.insert_one({'name': 'Checking', 'account_type': 'checking',
                                      'balance': 1000.0, 'is_primary': True}).inserted_id
    savings = db.accounts.insert_one({'name': 'Savings', 'account_type': 'savings',
                                      'balance': 500.0}).inserted_id
    return primary, savings


def _debt(db, **over):
    doc = {'name': 'Student Loan', 'debt_type': 'student_loan', 'balance': 10000.0,
           'interest_rate': 4.0, 'minimum_payment': 250.0, 'creditor': 'Servicer',
           'notes': None, 'payment_match': []}
    doc.update(over)
    return db.debts.insert_one(doc).inserted_id


# ------------------------------------------------------------------- debts
def test_update_debt_changes_only_what_is_passed(db):
    did = _debt(db)
    out = M.update_debt(str(did), minimum_payment=275.5)
    d = db.debts.find_one({'_id': did})
    assert out == "Updated minimum_payment."
    assert (d['minimum_payment'], d['interest_rate'], d['balance'], d['name']) == (
        275.5, 4.0, 10000.0, 'Student Loan')


def test_update_debt_zero_is_a_value_and_none_clears(db):
    did = _debt(db, deferment_end_date=datetime(2027, 1, 1), subsidized=True)
    M.update_debt(str(did), interest_rate=0, creditor='none', deferment_end_date='none',
                  subsidized='false', payment_match='Loan Pmt, Loan Payment\nAUTOPAY')
    d = db.debts.find_one({'_id': did})
    assert d['interest_rate'] == 0 and d['creditor'] is None
    assert d['deferment_end_date'] is None and d['subsidized'] is False
    assert d['payment_match'] == ['Loan Pmt', 'Loan Payment', 'AUTOPAY']
    assert isinstance(M.update_debt(str(did), deferment_end_date='2027-06-01'), str)
    assert db.debts.find_one({'_id': did})['deferment_end_date'] == datetime(2027, 6, 1)


def test_update_debt_rejects_bad_input_and_writes_nothing(db):
    did = _debt(db)
    for kwargs in ({'debt_type': 'mortgage'}, {'interest_rate': 400}, {'minimum_payment': -5},
                   {'subsidized': 'maybe'}, {'deferment_end_date': 'next year'}):
        assert M.update_debt(str(did), name='Renamed', **kwargs).startswith('Error')
    assert db.debts.find_one({'_id': did})['name'] == 'Student Loan'
    assert M.update_debt(str(did)) == "Nothing to update."
    assert M.update_debt(str(ObjectId()), name='x') == "Debt not found."
    assert M.update_debt('nope', name='x').startswith('Error')


def test_delete_debt_unlinks_its_payments_like_web(db):
    did = _debt(db)
    tid = db.transactions.insert_one({'description': 'Loan Pmt', 'amount': 250.0,
                                      'date': datetime(2026, 5, 1), 'transaction_type': 'expense',
                                      'debt_id': did}).inserted_id
    assert "1 ledger payment(s) unlinked" in M.delete_debt(str(did))
    assert db.debts.count_documents({}) == 0
    assert 'debt_id' not in db.transactions.find_one({'_id': tid})


def test_link_and_unlink_a_payment_moves_the_balance(db):
    did = _debt(db, interest_rate=0)
    aid = db.assets.insert_one({'name': 'Car', 'asset_type': 'vehicle', 'estimated_value': 9000,
                                'liability_balance': 5000.0, 'interest_rate': 0}).inserted_id
    tid = db.transactions.insert_one({'description': 'Pmt', 'amount': 500.0,
                                      'date': datetime(2026, 5, 1),
                                      'transaction_type': 'expense'}).inserted_id
    assert "payment on Student Loan" in M.link_transaction_to_loan(str(tid), f"debt:{did}")
    assert "owed now $9,500.00" in W.list_debts()
    assert "principal $500.00" in M.loan_payments(f"debt:{did}")
    # moving it to the asset's loan clears the debt link, as the ledger's picker does
    M.link_transaction_to_loan(str(tid), f"asset:{aid}")
    t = db.transactions.find_one({'_id': tid})
    assert t['asset_liability_id'] == aid and 'debt_id' not in t
    assert "owed now $10,000.00" in W.list_debts() and "owed now=$4,500.00" in W.list_assets()
    M.link_transaction_to_loan(str(tid))
    assert 'asset_liability_id' not in db.transactions.find_one({'_id': tid})
    assert M.link_transaction_to_loan(str(tid), 'card:123').startswith('Error')
    assert M.link_transaction_to_loan(str(tid), f"debt:{ObjectId()}") == "Loan not found."
    assert "No ledger payments" in M.loan_payments(f"asset:{aid}")


# ---------------------------------------------------------------- accounts
def test_delete_account_refuses_while_in_use(db):
    primary, savings = _accounts(db)
    W.add_transfer(str(primary), str(savings), 50.0, '2026-05-01')
    out = M.delete_account(str(savings))
    assert out.startswith("Not deleted") and "1 transfers" in out and "1 ledger transactions" in out
    assert db.accounts.count_documents({}) == 2
    spare = db.accounts.insert_one({'name': 'Old', 'account_type': 'savings'}).inserted_id
    assert M.delete_account(str(spare)) == "Deleted account Old."
    assert M.delete_account(str(primary)).startswith("Not deleted")
    assert M.delete_account(str(savings), force=True) == "Deleted account Savings."


def test_update_account_details(db):
    primary, _ = _accounts(db)
    M.update_account_details(str(primary), account_type='savings', notes='Joint',
                             balance_as_of_date='2026-05-01')
    a = db.accounts.find_one({'_id': primary})
    assert (a['account_type'], a['notes'], a['balance_as_of_date']) == (
        'savings', 'Joint', datetime(2026, 5, 1))
    assert M.update_account_details(str(primary), account_type='crypto').startswith('Error')


# ------------------------------------------------------------------ assets
def test_update_asset_details_and_delete(db):
    aid = db.assets.insert_one({'name': 'House', 'asset_type': 'real_estate',
                                'estimated_value': 300000, 'liability_balance': 200000.0,
                                'interest_rate': 5.0, 'hoa_monthly': 40.0}).inserted_id
    tid = db.transactions.insert_one({'description': 'Mortgage', 'amount': 1500.0,
                                      'date': datetime(2026, 5, 1), 'transaction_type': 'expense',
                                      'asset_liability_id': aid}).inserted_id
    M.update_asset_details(str(aid), name='Home', property_tax_annual=6000, hoa_monthly=0,
                           payment_match='Mortgage', loan_term_months=360,
                           payment_start_date='2025-01-01')
    a = db.assets.find_one({'_id': aid})
    assert (a['name'], a['property_tax_annual'], a['hoa_monthly'], a['loan_term_months']) == (
        'Home', 6000.0, None, 360)
    assert a['payment_match'] == ['Mortgage'] and a['payment_start_date'] == datetime(2025, 1, 1)
    assert a['estimated_value'] == 300000
    assert M.update_asset_details(str(aid), payment_frequency='daily').startswith('Error')
    assert "1 ledger payment(s) unlinked" in M.delete_asset(str(aid))
    assert 'asset_liability_id' not in db.transactions.find_one({'_id': tid})


# ------------------------------------------------------------------- goals
def test_goal_edit_and_contribute(db):
    gid = db.savings_goals.insert_one({'name': 'Trip', 'target_amount': 2000.0,
                                       'current_amount': 100.0, 'target_date': None}).inserted_id
    M.update_goal(str(gid), name='Japan', target_amount=3000, target_date='2027-04-01',
                  category='travel')
    assert M.contribute_to_goal(str(gid), 250) == "Japan: $350.00 of $3,000.00."
    g = db.savings_goals.find_one({'_id': gid})
    assert (g['target_date'], g['category']) == (datetime(2027, 4, 1), 'travel')
    assert M.update_goal(str(gid), category='boats').startswith('Error')


# --------------------------------------------------------------- recurring
def test_update_recurring_details(db):
    rid = db.recurring_transactions.insert_one({
        'description': 'Gym', 'amount': 40.0, 'frequency': 'monthly',
        'start_date': datetime(2025, 1, 1), 'end_date': None, 'transaction_type': 'expense',
        'auto_pay': False}).inserted_id
    M.update_recurring_details(str(rid), end_date='2026-12-31', auto_pay='true',
                               payment_method='Card')
    r = db.recurring_transactions.find_one({'_id': rid})
    assert (r['end_date'], r['auto_pay'], r['payment_method']) == (
        datetime(2026, 12, 31), True, 'Card')
    M.update_recurring_details(str(rid), end_date='none')
    assert db.recurring_transactions.find_one({'_id': rid})['end_date'] is None
    assert M.update_recurring_details(str(rid), start_date='none').startswith('Error')
    assert M.update_recurring_details(str(rid), auto_pay='sometimes').startswith('Error')


def test_skip_removes_the_occurrence_from_the_outlook(app_module, db):
    primary, savings = _accounts(db)
    today = datetime.now()
    nxt = datetime(today.year + (today.month == 12), today.month % 12 + 1, 1)
    day = nxt.strftime('%Y-%m-%d')
    rid = db.recurring_transactions.insert_one({
        'description': 'Electric', 'amount': 100.0, 'frequency': 'monthly',
        'day_of_month': '1', 'start_date': datetime(2020, 1, 1),
        'transaction_type': 'expense', 'account_id': None}).inserted_id

    def projected():
        return [t for m in app_module.calculate_yearly_outlook(1000.0, str(primary))
                for t in m['transactions']
                if t.get('recurring_id') == str(rid) and t['date'] == day]

    assert projected()
    assert M.skip_projected('Electric', day) == f"Skipped 'Electric' on {day}."
    assert not projected() and f"{day}  Electric" in M.list_skipped(day)
    assert M.unskip_projected('Electric', day).startswith('Restored')
    assert projected()
    # a transfer is skipped on both accounts, like the web ledger
    M.skip_projected('Transfer to: Rainy day', day)
    assert {s['description'] for s in db.skipped_transactions.find()} == {
        'Transfer to: Rainy day', 'Transfer from: Rainy day'}
    M.unskip_projected('Transfer from: Rainy day', day)
    assert db.skipped_transactions.count_documents({}) == 0
    assert M.skip_projected('Electric', 'soon').startswith('Error')


# --------------------------------------------------------------- transfers
def test_delete_transfer_reverses_the_ledger_rows(db):
    primary, savings = _accounts(db)
    W.add_transfer(str(primary), str(savings), 250.0, '2026-05-01', 'Rainy day')
    listing = M.list_transfers()
    assert "Checking -> Savings" in listing and "$250.00" in listing
    transfer_id = listing.split(' | ')[0]
    assert M.delete_transfer(transfer_id) == "Deleted the transfer and 2 ledger row(s)."
    assert db.transfers.count_documents({}) == 0 and db.transactions.count_documents({}) == 0
    assert M.delete_transfer(transfer_id) == "Transfer not found."


def test_update_recurring_transfer(db):
    primary, savings = _accounts(db)
    W.add_recurring_transfer(str(primary), str(savings), 100.0, 'monthly', '2026-01-01')
    rt = db.recurring_transfers.find_one()
    M.update_recurring_transfer(str(rt['_id']), amount=150, end_date='2027-01-01',
                                description='Savings sweep')
    rt = db.recurring_transfers.find_one()
    assert (rt['amount'], rt['end_date'], rt['description']) == (
        150.0, datetime(2027, 1, 1), 'Savings sweep')
    assert M.update_recurring_transfer(str(rt['_id']), to_account_id=str(primary)).startswith('Error')
    assert M.update_recurring_transfer(str(rt['_id']), amount=0).startswith('Error')
    assert db.recurring_transfers.find_one()['to_account_id'] == savings


# ------------------------------------------------- dividends, budget, worth
def test_dividend_list_and_delete(db):
    W.add_dividend('vti', 12.5, '2026-03-28')
    row = M.list_dividends('VTI')
    assert "VTI: $12.50" in row
    assert M.delete_dividend(row.split(' | ')[0]) == "Deleted."
    assert M.list_dividends() == "No dividends."


def test_reset_month_budget_targets_keeps_the_defaults(db):
    W.set_budget_target('Dining', 300)
    W.set_budget_target('Dining', 450, '2026-06')
    W.set_budget_target('Travel', 900, '2026-06')
    W.set_budget_target('Travel', 100, '2026-07')
    assert M.reset_month_budget_targets('2026-06') == "Cleared 2 override(s) for 2026-06."
    assert db.monthly_budget_targets.count_documents({}) == 1
    assert db.budget_targets.count_documents({}) == 1
    assert M.reset_month_budget_targets('June').startswith('Error')


def test_net_worth_snapshot_uses_what_is_owed_now(db):
    _accounts(db)
    did = _debt(db, interest_rate=0)
    db.assets.insert_one({'name': 'Car', 'asset_type': 'vehicle', 'estimated_value': 9000.0})
    db.transactions.insert_one({'description': 'Pmt', 'amount': 1000.0, 'date': datetime(2026, 5, 1),
                                'transaction_type': 'expense', 'debt_id': did})
    assert "net worth $1,500.00" in M.take_net_worth_snapshot()
    snap = db.networth_snapshots.find_one()
    assert snap['total_assets'] == 10500.0 and snap['total_liabilities'] == 9000.0
    assert snap['breakdown']['standalone_debts'] == 9000.0 and isinstance(snap['date'], datetime)


# ------------------------------------------- payment plans and card charges
def test_payment_plan_lifecycle(db):
    out = M.add_payment_plan('Dentist', 1200, 100, '2026-01-15', card_name='Visa')
    pid = db.payment_plans.find_one()['_id']
    assert str(pid) in out
    charge = db.cc_transactions.insert_one({'description': 'DENTIST PLAN', 'amount': 100.0,
                                            'date': datetime(2026, 2, 15), 'card_name': 'Visa',
                                            'category': 'Other',
                                            'categorized_by': 'llm'}).inserted_id
    assert str(charge) in search_cc_transactions()       # the ID the tools below need
    assert "Dentist plan" in M.link_cc_transaction_to_plan(str(charge), str(pid))
    assert "$1,100.00 left of $1,200.00" in M.list_payment_plans()
    assert M.recategorize_cc_transaction(str(charge), 'Healthcare').endswith('now Healthcare.')
    row = db.cc_transactions.find_one({'_id': charge})
    assert row['category'] == 'Healthcare' and 'categorized_by' not in row
    assert M.recategorize_cc_transaction(str(charge), 'Teeth').startswith('Error')
    M.update_payment_plan(str(pid), monthly_payment=150, status='closed')
    plan = db.payment_plans.find_one()
    assert (plan['monthly_payment'], plan['status'], plan['vendor']) == (150.0, 'closed', 'Dentist')
    assert M.update_payment_plan(str(pid), status='paused').startswith('Error')
    assert M.delete_payment_plan(str(pid)) == "Deleted; 1 card charge(s) unlinked."
    assert 'payment_plan_id' not in db.cc_transactions.find_one({'_id': charge})
    assert M.add_payment_plan('', 0, 0).startswith('Error')


# ----------------------------------------------------------------- reports
def test_reports_list_and_read(db):
    rid = db.reports.insert_one({'type': 'scheduled', 'task_name': 'Rebalance check',
                                 'date': datetime(2026, 5, 2), 'period': '2026-05-02',
                                 'summary_md': 'All within band.', 'insights': None}).inserted_id
    db.reports.insert_one({'type': 'weekly', 'date': datetime(2026, 5, 4), 'period': '2026-W19',
                           'insights': 'Spending is on track.'})
    listing = M.list_reports().splitlines()
    assert listing[0].endswith('weekly 2026-W19') and str(rid) in listing[1]
    assert "All within band." in M.get_report(str(rid))
    assert M.get_report(str(ObjectId())) == "Report not found."


# ------------------------------------------------- the server's tool surface
def test_mcp_server_registers_the_new_tools(app_module):
    import asyncio
    from unittest import mock

    import mongomock
    with mock.patch('pymongo.MongoClient', mongomock.MongoClient):
        import mcp_server
    try:
        names = {t.name for t in asyncio.run(mcp_server.mcp.list_tools())}
        assert {'update_debt', 'delete_debt', 'delete_account', 'delete_asset',
                'update_asset_details', 'update_goal', 'contribute_to_goal',
                'skip_projected', 'unskip_projected', 'list_transfers', 'delete_transfer',
                'update_recurring_transfer', 'link_transaction_to_loan', 'loan_payments',
                'list_dividends', 'delete_dividend', 'reset_month_budget_targets',
                'take_net_worth_snapshot', 'list_payment_plans', 'add_payment_plan',
                'recategorize_cc_transaction', 'list_reports', 'get_report'} <= names
        assert mcp_server._combine("Nothing to update.", "Nothing to update.") == "Nothing to update."
        assert mcp_server._combine("Updated.", "Nothing to update.") == "Updated."
        assert mcp_server._combine("Updated.", "Error: bad date.") == "Updated. Error: bad date."
    finally:
        import agent.db as agent_db
        agent_db.init(app_module.db)     # the server module pointed the tools at its own db
