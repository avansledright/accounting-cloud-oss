"""Scheduled tasks: schedule math, atomic claim/catch-up, rebalance math, AI off, MCP + web.

Every account, fund weight and amount here is invented.
"""
import json
import threading
import time
from datetime import datetime

import pytest
from bson import ObjectId

import agent.scheduled_tasks as S


# ---------------------------------------------------------------- fixtures
@pytest.fixture(autouse=True)
def _no_llm(monkeypatch):
    """No network: AI is off (the default) and the narrative runs inline."""
    for var in ('OLLAMA_URL', 'AGENT_OLLAMA_HOST', 'ADVISOR_ENABLED'):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(S, 'NARRATIVE_ASYNC', False)


@pytest.fixture
def ai_on(monkeypatch):
    """Pretend a provider is configured, without calling one."""
    monkeypatch.setattr(S, 'ai_available', lambda: True)


@pytest.fixture
def prices(monkeypatch):
    table = {}
    monkeypatch.setattr(S, 'fetch_live_price', lambda sym: table.get(sym))
    return table


def _acct(db, name, account_type):
    return db.accounts.insert_one({'name': name, 'account_type': account_type,
                                   'institution': 'Sample Brokerage', 'balance': 0.0}).inserted_id


def _hold(db, account_id, symbol, qty, cost=None):
    db.holdings.insert_one({'account_id': account_id, 'symbol': symbol, 'quantity': float(qty),
                            'cost_basis': cost, 'date_added': datetime(2025, 1, 1)})


def _rebalance_task(db, accounts, now=datetime(2026, 3, 1), **cfg):
    tid, err = S.create_task({
        'name': 'Yearly rebalance check', 'task_type': 'rebalance_check',
        'schedule': {'frequency': 'yearly', 'month': 3, 'day': 10, 'hour': 8},
        'config': {'accounts': accounts, **cfg},
    }, now=now)
    assert err is None, err
    return tid


def _reminder_task(db, now=datetime(2026, 3, 1), **sched):
    sched = sched or {'frequency': 'yearly', 'month': 3, 'day': 10, 'hour': 8}
    tid, err = S.create_task({'name': 'Review insurance cover', 'task_type': 'reminder',
                              'schedule': sched,
                              'config': {'message': 'Yearly review', 'checklist': ['Home', 'Car']}},
                             now=now)
    assert err is None, err
    return tid


# ---------------------------------------------------------------- compute_next_run
YEARLY_MAR10 = {'frequency': 'yearly', 'month': 3, 'day': 10, 'hour': 8}


@pytest.mark.parametrize('after, expected', [
    (datetime(2026, 3, 19, 12), datetime(2027, 3, 10, 8)),   # already passed → next year
    (datetime(2026, 3, 10, 7, 59), datetime(2026, 3, 10, 8)),
    (datetime(2026, 3, 10, 8), datetime(2027, 3, 10, 8)),     # strictly after
    (datetime(2026, 12, 31, 23), datetime(2027, 3, 10, 8)),    # year rollover
    (datetime(2026, 1, 1), datetime(2026, 3, 10, 8)),
])
def test_yearly(after, expected):
    assert S.compute_next_run(YEARLY_MAR10, after) == expected


def test_yearly_feb_29_clamps_in_non_leap_years():
    s = {'frequency': 'yearly', 'month': 2, 'day': 29, 'hour': 8}
    assert S.compute_next_run(s, datetime(2026, 3, 1)) == datetime(2027, 2, 28, 8)
    assert S.compute_next_run(s, datetime(2027, 3, 1)) == datetime(2028, 2, 29, 8)


def test_monthly_31st_clamps_to_month_end_and_recovers():
    s = {'frequency': 'monthly', 'day': 31, 'hour': 8}
    assert S.compute_next_run(s, datetime(2026, 1, 31, 9)) == datetime(2026, 2, 28, 8)
    assert S.compute_next_run(s, datetime(2026, 2, 28, 8)) == datetime(2026, 3, 31, 8)
    assert S.compute_next_run(s, datetime(2026, 4, 1)) == datetime(2026, 4, 30, 8)
    assert S.compute_next_run(s, datetime(2028, 2, 1)) == datetime(2028, 2, 29, 8)
    assert S.compute_next_run(s, datetime(2026, 12, 31, 9)) == datetime(2027, 1, 31, 8)


def test_quarterly_every_three_months_from_configured_month():
    s = {'frequency': 'quarterly', 'month': 2, 'day': 15, 'hour': 8}   # Feb/May/Aug/Nov
    assert S.compute_next_run(s, datetime(2026, 10, 19)) == datetime(2026, 11, 15, 8)
    assert S.compute_next_run(s, datetime(2026, 11, 15, 8)) == datetime(2027, 2, 15, 8)
    assert S.compute_next_run(s, datetime(2026, 2, 10)) == datetime(2026, 2, 15, 8)
    s31 = {'frequency': 'quarterly', 'month': 11, 'day': 31, 'hour': 0}  # Nov/Feb/May/Aug
    assert S.compute_next_run(s31, datetime(2026, 12, 1)) == datetime(2027, 2, 28, 0)


def test_weekly():
    s = {'frequency': 'weekly', 'weekday': 0, 'hour': 6}  # Monday 6am; 2026-10-19 is a Monday
    assert S.compute_next_run(s, datetime(2026, 10, 19, 5)) == datetime(2026, 10, 19, 6)
    assert S.compute_next_run(s, datetime(2026, 10, 19, 6)) == datetime(2026, 10, 26, 6)
    assert S.compute_next_run(s, datetime(2026, 10, 21)) == datetime(2026, 10, 26, 6)


def test_once_returns_none_after_it_fires():
    s = {'frequency': 'once', 'date': datetime(2026, 12, 1), 'hour': 9}
    assert S.compute_next_run(s, datetime(2026, 10, 19)) == datetime(2026, 12, 1, 9)
    assert S.compute_next_run(s, datetime(2026, 12, 1, 9)) is None


def test_hour_zero_is_midnight_not_default():
    s = {'frequency': 'monthly', 'day': 1, 'hour': 0}
    assert S.compute_next_run(s, datetime(2026, 10, 19)) == datetime(2026, 11, 1, 0)


def test_describe_schedule():
    assert S.describe_schedule(YEARLY_MAR10) == 'Every year on Mar 10 at 8am'
    assert S.describe_schedule({'frequency': 'weekly', 'weekday': 0, 'hour': 18}) == \
        'Every week on Monday at 6pm'
    assert 'Feb, May, Aug, Nov' in S.describe_schedule(
        {'frequency': 'quarterly', 'month': 2, 'day': 15, 'hour': 8})
    assert S.describe_schedule({'frequency': 'once', 'date': datetime(2026, 12, 1), 'hour': 0}) == \
        'Once on Dec 01, 2026 at 12am'


def test_normalize_schedule_validation():
    now = datetime(2026, 10, 19)
    assert S.normalize_schedule({'frequency': 'yearly', 'day': 15}, now)[1]  # missing month
    assert S.normalize_schedule({'frequency': 'weekly'}, now)[1]
    assert S.normalize_schedule({'frequency': 'hourly'}, now)[1]
    assert S.normalize_schedule({'frequency': 'monthly', 'day': 32}, now)[1]
    assert S.normalize_schedule({'frequency': 'once', 'date': '2026-01-01'}, now)[1]  # past
    sched, err = S.normalize_schedule({'frequency': 'once', 'month': 3, 'day': 1, 'hour': 9}, now)
    assert err is None and sched['date'] == datetime(2027, 3, 1)
    assert type(sched['date']) is datetime  # never datetime.date in Mongo


# ---------------------------------------------------------------- claim / catch-up / loop
def test_atomic_claim_prevents_double_run(db, prices):
    tid = _reminder_task(db)
    now = datetime(2026, 3, 10, 8, 5)
    snapshot = db.scheduled_tasks.find_one({'_id': ObjectId(tid)})  # both replicas read this
    first = S.claim_task(snapshot, now)
    second = S.claim_task(snapshot, now)
    assert first is not None and second is None
    assert first['next_run'] == datetime(2027, 3, 10, 8)


def test_run_due_tasks_twice_runs_once(db, prices):
    _reminder_task(db)
    now = datetime(2026, 3, 10, 8, 5)
    assert S.run_due_tasks(now) == 1
    assert S.run_due_tasks(now) == 0
    assert db.reports.count_documents({'type': 'scheduled'}) == 1


def test_catch_up_runs_once_then_advances_to_future(db, prices):
    tid = _reminder_task(db)
    db.scheduled_tasks.update_one({'_id': ObjectId(tid)},
                                  {'$set': {'next_run': datetime(2024, 3, 10, 8)}})  # 2 missed
    now = datetime(2026, 10, 19, 9)
    assert S.run_due_tasks(now) == 1
    assert S.run_due_tasks(now) == 0
    t = db.scheduled_tasks.find_one({'_id': ObjectId(tid)})
    assert t['next_run'] == datetime(2027, 3, 10, 8)
    assert t['last_status'] == 'ok' and t['last_run'] == now and t['running_since'] is None
    rep = db.reports.find_one({'_id': t['last_report_id']})
    assert rep['type'] == 'scheduled' and rep['task_id'] == t['_id']
    assert rep['data']['checklist'] == ['Home', 'Car']


def test_once_task_disables_after_firing(db, prices):
    tid = _reminder_task(db, frequency='once', date='2026-11-01', hour=9)
    assert S.run_due_tasks(datetime(2026, 11, 1, 9, 1)) == 1
    t = db.scheduled_tasks.find_one({'_id': ObjectId(tid)})
    assert t['enabled'] is False and t['next_run'] is None
    assert S.run_due_tasks(datetime(2027, 11, 1, 9, 1)) == 0


def test_failing_task_records_error_and_others_still_run(db, prices, monkeypatch):
    def boom(cfg, task):
        raise RuntimeError('kaboom')
    monkeypatch.setitem(S.TASK_TYPES, 'reminder', dict(S.TASK_TYPES['reminder'], run=boom))
    bad = _reminder_task(db)
    roth = _acct(db, 'Test Roth', 'roth_ira')
    _hold(db, roth, 'VTI', 70, 5000)
    _hold(db, roth, 'VXUS', 30, 2500)
    prices.update(VTI=100.0, VXUS=100.0)
    good = _rebalance_task(db, [{'account_id': str(roth), 'targets': {'VTI': 70, 'VXUS': 30}}])
    assert S.run_due_tasks(datetime(2026, 3, 10, 9)) == 2
    b = db.scheduled_tasks.find_one({'_id': ObjectId(bad)})
    g = db.scheduled_tasks.find_one({'_id': ObjectId(good)})
    assert b['last_status'] == 'error' and 'kaboom' in b['last_error']
    assert b['next_run'] == datetime(2027, 3, 10, 8)  # still advanced — no retry storm
    assert g['last_status'] == 'ok'


def test_loop_survives_exceptions(monkeypatch):
    calls = []
    stop = threading.Event()

    def fake_run():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError('db hiccup')
        stop.set()
        return 0
    monkeypatch.setattr(S, 'run_due_tasks', fake_run)
    S.task_loop(poll_seconds=0.01, stop_event=stop)
    assert len(calls) == 2


def test_disabled_tasks_are_skipped(db, prices):
    tid = _reminder_task(db)
    assert S.set_enabled(tid, False) is None
    assert S.run_due_tasks(datetime(2026, 3, 10, 9)) == 0


# ---------------------------------------------------------------- rebalance math
def _roth(db, vti_qty, vxus_qty, prices):
    roth = _acct(db, 'Test Roth', 'roth_ira')
    _hold(db, roth, 'VTI', vti_qty, 6000.0)
    _hold(db, roth, 'VXUS', vxus_qty, 2500.0)
    prices.update(VTI=100.0, VXUS=100.0)
    return roth


def _run(db, accounts, **cfg):
    tid = _rebalance_task(db, accounts, **cfg)
    rep = S.execute_task(S.get_task(tid), now=datetime(2026, 3, 10, 8))
    assert rep is not None
    return rep


def test_rebalance_breach_produces_trades_back_to_target(db, prices):
    roth = _roth(db, 78, 22, prices)                       # $7,800 / $2,200 → 78/22
    rep = _run(db, [{'account_id': str(roth), 'targets': {'VTI': 70, 'VXUS': 30}, 'taxable': False}])
    d = rep['data']
    a = d['accounts'][0]
    assert d['any_breach'] and a['breaches'] == ['VTI', 'VXUS']
    rows = {r['symbol']: r for r in a['rows']}
    assert rows['VTI']['current_pct'] == 78.0 and rows['VTI']['drift_pct'] == 8.0
    assert rows['VXUS']['drift_pct'] == -8.0
    trades = {t['symbol']: t for t in a['trades']}
    assert trades['VTI'] == {'symbol': 'VTI', 'action': 'sell', 'amount': 800.0,
                             'shares': 8.0, 'price': 100.0}
    assert trades['VXUS']['action'] == 'buy' and trades['VXUS']['amount'] == 800.0
    assert a['trades'][0]['action'] == 'sell'                  # sells listed first
    assert S.TAX_ADVANTAGED_NOTE in a['notes']
    assert 'Rebalance needed' in rep['summary_md'] and 'SELL VTI $800.00' in rep['summary_md']


def test_rebalance_within_threshold_needs_no_trades(db, prices):
    roth = _roth(db, 73, 27, prices)
    rep = _run(db, [{'account_id': str(roth), 'targets': {'VTI': 70, 'VXUS': 30}}])
    a = rep['data']['accounts'][0]
    assert not rep['data']['any_breach'] and a['trades'] == [] and a['breaches'] == []
    assert 'No trades needed' in rep['summary_md']


def test_custom_threshold(db, prices):
    roth = _roth(db, 73, 27, prices)
    rep = _run(db, [{'account_id': str(roth), 'targets': {'VTI': 70, 'VXUS': 30}}],
               drift_threshold_pct=2)
    assert rep['data']['any_breach']


def test_symbol_without_target_counts_as_zero(db, prices):
    roth = _acct(db, 'Test Roth', 'roth_ira')
    _hold(db, roth, 'VTI', 70, 6000)
    _hold(db, roth, 'VXUS', 20, 1900)
    _hold(db, roth, 'SCHD', 10, 900)
    prices.update(VTI=100.0, VXUS=100.0, SCHD=100.0)
    rep = _run(db, [{'account_id': str(roth), 'targets': {'VTI': 70, 'VXUS': 30}}])
    a = rep['data']['accounts'][0]
    rows = {r['symbol']: r for r in a['rows']}
    assert rows['SCHD']['target_pct'] == 0 and rows['SCHD']['untargeted'] and rows['SCHD']['breach']
    trades = {t['symbol']: t for t in a['trades']}
    assert trades['SCHD']['action'] == 'sell' and trades['SCHD']['amount'] == 1000.0
    assert trades['VXUS']['action'] == 'buy' and trades['VXUS']['amount'] == 1000.0
    assert 'VTI' not in trades                                   # already on target


def test_target_fund_not_yet_held_is_bought(db, prices):
    ira = _acct(db, 'Test IRA', 'ira')
    _hold(db, ira, 'VTI', 100, 9000)
    prices.update(VTI=100.0, BND=60.0)
    rep = _run(db, [{'account_id': str(ira), 'targets': {'VTI': 70, 'BND': 30}}])
    trades = {t['symbol']: t for t in rep['data']['accounts'][0]['trades']}
    assert trades['BND'] == {'symbol': 'BND', 'action': 'buy', 'amount': 3000.0,
                             'shares': 50.0, 'price': 60.0}


def test_price_unavailable_falls_back_to_cost_basis(db, prices):
    roth = _acct(db, 'Test Roth', 'roth_ira')
    _hold(db, roth, 'VTI', 70, 6000)
    _hold(db, roth, 'VXUS', 30, 3000)        # no price → valued at $3,000 cost
    _hold(db, roth, 'XYZ', 5)                # no price, no cost → excluded
    prices.update(VTI=100.0)
    rep = _run(db, [{'account_id': str(roth), 'targets': {'VTI': 70, 'VXUS': 30}}])
    a = rep['data']['accounts'][0]
    assert a['total_value'] == 10000.0
    rows = {r['symbol']: r for r in a['rows']}
    assert rows['VXUS']['price_source'] == 'cost_basis' and rows['VXUS']['current_pct'] == 30.0
    assert 'XYZ' not in rows
    assert any('VXUS' in w and 'stale' in w for w in a['warnings'])
    assert any('XYZ' in w and 'excluded' in w for w in a['warnings'])


def test_price_fetch_exception_is_treated_as_unavailable(db, monkeypatch):
    def broken(sym):
        raise ConnectionError('offline')
    monkeypatch.setattr(S, 'fetch_live_price', broken)
    roth = _acct(db, 'Test Roth', 'roth_ira')
    _hold(db, roth, 'VTI', 70, 7000)
    _hold(db, roth, 'VXUS', 30, 3000)
    rep = _run(db, [{'account_id': str(roth), 'targets': {'VTI': 70, 'VXUS': 30}}])
    assert rep['data']['accounts'][0]['total_value'] == 10000.0


def test_tlh_and_cross_account_wash_sale_warning(db, prices):
    roth = _roth(db, 78, 22, prices)                         # Roth will BUY VXUS
    ira = _acct(db, 'Test IRA', 'ira')
    _hold(db, ira, 'BND', 10, 800)
    brokerage = _acct(db, 'Test Brokerage', 'brokerage')
    _hold(db, brokerage, 'VXUS', 10, 1350.0)                 # $1,000 now → -$350
    _hold(db, brokerage, 'BND', 20, 1900.0)                  # $1,400 now → -$500, held in the IRA
    _hold(db, brokerage, 'SCHD', 5, 300.0)                   # gain → not a candidate
    prices.update(BND=70.0, SCHD=100.0)
    rep = _run(db, [
        {'account_id': str(roth), 'targets': {'VTI': 70, 'VXUS': 30}, 'taxable': False},
        {'account_id': str(brokerage), 'targets': {}, 'taxable': True, 'scan_only': True},
    ])
    b = rep['data']['accounts'][1]
    assert b['scan_only'] and b['rows'] == [] and b['trades'] == []
    cands = {c['symbol']: c for c in b['tlh_candidates']}
    assert set(cands) == {'VXUS', 'BND'}
    assert cands['VXUS']['unrealized_loss'] == -350.0
    assert cands['VXUS']['also_held_in'] == ['Test Roth']
    assert cands['VXUS']['planned_buys_in'] == ['Test Roth']
    assert cands['BND']['also_held_in'] == ['Test IRA']           # not even in the task config
    assert any('Wash-sale' in w and 'VXUS' in w for w in b['warnings'])
    assert any('BUYING VXUS' in w for w in b['warnings'])
    assert rep['data']['any_tlh'] and 'Wash-sale rule' in rep['summary_md']
    assert rep['data']['accounts'][0]['tlh_candidates'] == []  # Roth isn't taxable


def test_tlh_can_be_switched_off(db, prices):
    brokerage = _acct(db, 'Test Brokerage', 'brokerage')
    _hold(db, brokerage, 'VXUS', 10, 1350.0)
    prices.update(VXUS=100.0)
    rep = _run(db, [{'account_id': str(brokerage), 'taxable': True, 'scan_only': True}],
               include_tax_loss_harvest=False)
    assert rep['data']['accounts'][0]['tlh_candidates'] == []


def test_taxable_sells_carry_estimated_gain(db, prices):
    brokerage = _acct(db, 'Test Brokerage', 'brokerage')
    _hold(db, brokerage, 'VTI', 80, 6000.0)     # $8,000, basis $6,000
    _hold(db, brokerage, 'BND', 20, 2000.0)
    prices.update(VTI=100.0, BND=100.0)
    rep = _run(db, [{'account_id': str(brokerage), 'targets': {'VTI': 50, 'BND': 50},
                     'taxable': True}])
    sell = next(t for t in rep['data']['accounts'][0]['trades'] if t['action'] == 'sell')
    assert sell['amount'] == 3000.0 and sell['est_gain'] == 750.0


def test_narrative_added_only_when_ai_answers(db, prices, monkeypatch):
    roth = _roth(db, 78, 22, prices)
    acct = [{'account_id': str(roth), 'targets': {'VTI': 70, 'VXUS': 30}}]
    monkeypatch.setattr(S, '_narrative_text', lambda p: None)          # provider gave nothing
    rep = _run(db, acct)
    assert db.reports.find_one({'_id': rep['_id']})['insights'] is None
    monkeypatch.setattr(S, 'ai_available', lambda: True)
    monkeypatch.setattr(S, '_narrative_text', lambda p: 'Sell some VTI, buy VXUS. No tax impact.')
    rep = _run(db, acct)
    assert db.reports.find_one({'_id': rep['_id']})['insights'].startswith('Sell some VTI')


# ---------------------------------------------------------------- AI off
def test_ai_off_rebalance_and_reminder_run_without_calling_a_model(db, prices):
    """The two task types that need no model work with AI off, and nothing is requested."""
    from unittest import mock
    roth = _roth(db, 78, 22, prices)
    with mock.patch('requests.post') as post, mock.patch('requests.get') as get, \
            mock.patch('ai.generate') as generate:
        rep = _run(db, [{'account_id': str(roth), 'targets': {'VTI': 70, 'VXUS': 30}}],
                   ai_narrative=True)
        reminder = S.execute_task(S.get_task(_reminder_task(db)))
    assert rep['data']['any_breach'] and reminder['data']['checklist'] == ['Home', 'Car']
    assert db.reports.find_one({'_id': rep['_id']})['insights'] is None
    post.assert_not_called()
    get.assert_not_called()
    generate.assert_not_called()


def test_ai_off_report_task_is_skipped_with_a_clear_message(db, monkeypatch):
    import agent.reports as R

    def must_not_run():
        raise AssertionError('the report generator ran with AI off')
    monkeypatch.setattr(R, 'generate_weekly_report', must_not_run)
    tid, err = S.create_task({'name': 'Friday briefing', 'task_type': 'report',
                              'schedule': {'frequency': 'weekly', 'weekday': 4, 'hour': 17},
                              'config': {'report_type': 'weekly'}},
                             now=datetime(2026, 10, 19))
    assert err is None                                   # saving is allowed; AI may be turned on later
    assert S.run_due_tasks(datetime(2026, 10, 23, 17, 5)) == 1
    t = db.scheduled_tasks.find_one({'_id': ObjectId(tid)})
    assert t['last_status'] == 'skipped' and 'AI' in t['last_error'] and 'turned off' in t['last_error']
    assert t['next_run'] == datetime(2026, 10, 30, 17)   # still advanced
    assert db.reports.count_documents({}) == 0           # no empty result stored
    assert S.mcp_run_task(tid).startswith('Skipped:')
    assert 'skipped' in S.mcp_latest_result(tid)


def test_ai_off_is_explained_on_the_page_and_by_mcp_add(client, db):
    out = S.mcp_add_task('Friday briefing', 'report', 'weekly', weekday=4, hour=17,
                         config_json='{"report_type": "weekly"}')
    assert out.startswith('Created') and 'turned off' in out
    page = client.get('/scheduled-tasks').get_data(as_text=True)
    assert 'needs AI' in page and 'Advisor reports need AI' in page
    out = S.mcp_add_task('Stretch', 'reminder', 'weekly', weekday=0, config_json='{"message": "hi"}')
    assert 'turned off' not in out                       # reminders need no AI


def test_report_task_runs_generator(db, monkeypatch, ai_on):
    import agent.reports as R

    def fake_weekly():
        db.reports.insert_one({'type': 'weekly', 'period': '2026-W43', 'date': datetime.now(),
                               'data': {}, 'insights': 'hi'})
        return {'type': 'weekly', 'period': '2026-W43', 'insights': 'hi'}
    monkeypatch.setattr(R, 'generate_weekly_report', fake_weekly)
    tid, err = S.create_task({'name': 'Extra weekly', 'task_type': 'report',
                              'schedule': {'frequency': 'weekly', 'weekday': 4, 'hour': 17},
                              'config': {'report_type': 'weekly'}})
    assert err is None
    rep = S.execute_task(S.get_task(tid))
    assert rep['data']['report_type'] == 'weekly' and rep['data']['report_id']


# ---------------------------------------------------------------- validation
def test_targets_must_sum_to_100(db, prices):
    roth = _acct(db, 'Test Roth', 'roth_ira')
    _, err = S.create_task({'name': 'x', 'task_type': 'rebalance_check',
                            'schedule': YEARLY_MAR10,
                            'config': {'accounts': [{'account_id': str(roth),
                                                     'targets': {'VTI': 70, 'VXUS': 25}}]}})
    assert err and '95' in err
    _, err = S.create_task({'name': 'x', 'task_type': 'rebalance_check',
                            'schedule': YEARLY_MAR10,
                            'config': {'accounts': [{'account_id': str(roth),
                                                     'targets': {'VTI': 70.3, 'VXUS': 30}}]}})
    assert err is None   # within ±0.5


def test_rejects_non_investment_account(db):
    chk = _acct(db, 'Checking', 'checking')
    _, err = S.create_task({'name': 'x', 'task_type': 'rebalance_check', 'schedule': YEARLY_MAR10,
                            'config': {'accounts': [{'account_id': str(chk),
                                                     'targets': {'VTI': 100}}]}})
    assert err and 'not an investment account' in err


# ---------------------------------------------------------------- MCP tools
@pytest.fixture
def mcp_mod(app_module, db):
    from unittest import mock
    mongomock = pytest.importorskip('mongomock')
    import agent.db as agent_db
    with mock.patch('pymongo.MongoClient', mongomock.MongoClient):
        import mcp_server
    agent_db.init(app_module.db)  # importing mcp_server re-points agent.db; put it back
    return mcp_server


def test_mcp_round_trip(mcp_mod, db, prices):
    roth = _roth(db, 78, 22, prices)
    brokerage = _acct(db, 'Test Brokerage', 'brokerage')
    cfg = {'accounts': [
        {'account_id': str(roth), 'targets': {'VTI': 70, 'VXUS': 30}, 'taxable': False},
        {'account_id': str(brokerage), 'targets': {}, 'taxable': True, 'scan_only': True}]}

    out = mcp_mod.add_scheduled_task('Yearly rebalance check', 'rebalance_check',
                                     'yearly', month=3, day=10, hour=8,
                                     config_json=json.dumps(cfg))
    assert out.startswith('Created scheduled task'), out
    assert 'Every year on Mar 10 at 8am' in out
    tid = str(db.scheduled_tasks.find_one()['_id'])

    listing = mcp_mod.list_scheduled_tasks()
    assert tid in listing and 'rebalance_check' in listing and 'enabled' in listing

    out = mcp_mod.update_scheduled_task(tid, day=16, config_json='{"drift_threshold_pct": 4}')
    assert out.startswith('Updated'), out
    t = db.scheduled_tasks.find_one()
    assert t['schedule']['day'] == 16 and t['schedule']['month'] == 3
    assert t['config']['drift_threshold_pct'] == 4 and len(t['config']['accounts']) == 2

    assert 'disabled' in mcp_mod.update_scheduled_task(tid, enabled='false')
    assert db.scheduled_tasks.find_one()['enabled'] is False

    out = mcp_mod.run_scheduled_task(tid)
    assert 'Rebalance needed' in out
    assert 'Rebalance needed' in mcp_mod.latest_scheduled_task_result(tid)

    assert mcp_mod.delete_scheduled_task(tid).startswith('Deleted')
    assert db.scheduled_tasks.count_documents({}) == 0
    assert mcp_mod.list_scheduled_tasks() == 'No scheduled tasks.'


def test_mcp_server_registers_the_scheduled_task_and_price_alert_tools(mcp_mod):
    import asyncio
    names = {t.name for t in asyncio.run(mcp_mod.mcp.list_tools())}
    assert {'list_scheduled_tasks', 'add_scheduled_task', 'update_scheduled_task',
            'delete_scheduled_task', 'run_scheduled_task', 'latest_scheduled_task_result',
            'list_price_alerts', 'add_price_alert', 'delete_price_alert',
            'reset_price_alert'} <= names
    assert len(names) == 88          # the count docs/mcp-server.md states


def test_demo_data_tasks_and_alert_are_valid_and_run(db, prices):
    from demo_data import load_demo_data
    load_demo_data(db)
    tasks = list(db.scheduled_tasks.find())
    assert {t['task_type'] for t in tasks} == {'rebalance_check', 'reminder'}
    now = datetime.now()
    for t in tasks:
        clean, err = S.validate_task(t, now=now)
        assert err is None, err
        assert clean['schedule'] == t['schedule'] and clean['config'] == t['config']
        assert t['next_run'] == S.compute_next_run(t['schedule'], now.replace(month=12, day=31))
        assert isinstance(t['next_run'], datetime)
    prices.update(VXUS=60.0, BND=75.0, VTI=250.0, SCHD=80.0)
    for t in tasks:
        assert S.execute_task(t) is not None
    import agent.price_alerts as P
    alert = db.price_alerts.find_one()
    assert not P.validate(alert)[1] and P.position('VTI')['basis'] == 208.0


def test_mcp_validation_returns_friendly_errors(mcp_mod, db):
    assert mcp_mod.add_scheduled_task('x', 'nope', 'yearly', month=3, day=10).startswith('Error')
    assert mcp_mod.add_scheduled_task('x', 'reminder', 'yearly', month=13, day=1,
                                      config_json='{"message": "hi"}').startswith('Error')
    assert 'JSON' in mcp_mod.add_scheduled_task('x', 'reminder', 'yearly', month=1, day=1,
                                                config_json='{bad')
    assert mcp_mod.update_scheduled_task('nope', name='y').startswith('Error')
    assert mcp_mod.delete_scheduled_task('nope').startswith('Error')
    assert mcp_mod.run_scheduled_task(str(ObjectId())).startswith('Error')


# ---------------------------------------------------------------- web
def test_page_renders_empty(client, db):
    resp = client.get('/scheduled-tasks')
    assert resp.status_code == 200
    assert b'No scheduled tasks yet' in resp.data


def test_page_renders_populated_and_detail(client, db, prices):
    roth = _roth(db, 78, 22, prices)
    tid = _rebalance_task(db, [{'account_id': str(roth), 'targets': {'VTI': 70, 'VXUS': 30}}])
    _reminder_task(db)
    db.scheduled_tasks.insert_one({'name': 'Legacy doc', 'task_type': 'reminder'})  # sparse
    S.execute_task(S.get_task(tid))
    resp = client.get('/scheduled-tasks')
    assert resp.status_code == 200
    assert b'Yearly rebalance check' in resp.data
    assert b'Every year on Mar 10 at 8am' in resp.data
    assert client.get(f'/scheduled-tasks?edit={tid}').status_code == 200
    detail = client.get(f'/scheduled-tasks/{tid}')
    assert detail.status_code == 200
    assert b'Rebalance needed' in detail.data and b'SELL' in detail.data


def test_create_via_form_and_validation_error(client, db):
    roth = _acct(db, 'Test Roth', 'roth_ira')
    base = {'name': 'Annual check', 'task_type': 'rebalance_check', 'enabled': '1',
            'frequency': 'yearly', 'month': '10', 'day': '15', 'hour': '8',
            'drift_threshold_pct': '5', 'include_tax_loss_harvest': '1',
            'acct_index': ['0'], 'acct-0-account_id': str(roth),
            'acct-0-symbol': ['VTI', 'VXUS'], 'acct-0-pct': ['70', '20']}
    bad = client.post('/scheduled-tasks/save', data=base)
    assert bad.status_code == 400 and b'must be 100%' in bad.data
    assert db.scheduled_tasks.count_documents({}) == 0

    good = client.post('/scheduled-tasks/save', data=dict(base, **{'acct-0-pct': ['70', '30']}))
    assert good.status_code == 302
    t = db.scheduled_tasks.find_one()
    assert t['config']['accounts'][0]['targets'] == {'VTI': 70.0, 'VXUS': 30.0}
    assert t['enabled'] is True and isinstance(t['next_run'], datetime)

    upd = client.post('/scheduled-tasks/save', data=dict(base, task_id=str(t['_id']),
                                                         name='Renamed',
                                                         **{'acct-0-pct': ['60', '40']}))
    assert upd.status_code == 302
    assert db.scheduled_tasks.find_one()['name'] == 'Renamed'
    assert db.scheduled_tasks.count_documents({}) == 1


def test_toggle_run_and_delete_routes(client, db, prices):
    tid = _reminder_task(db)
    client.post(f'/scheduled-tasks/{tid}/toggle', data={'enabled': '0'})
    assert db.scheduled_tasks.find_one()['enabled'] is False
    client.post(f'/scheduled-tasks/{tid}/toggle', data={'enabled': '1'})
    assert db.scheduled_tasks.find_one()['enabled'] is True

    resp = client.post(f'/scheduled-tasks/{tid}/run')
    assert resp.status_code == 302
    for _ in range(100):  # runs in a daemon thread
        if db.reports.count_documents({'type': 'scheduled'}):
            break
        time.sleep(0.02)
    assert db.reports.find_one({'type': 'scheduled'})['trigger'] == 'manual'

    client.post(f'/scheduled-tasks/{tid}/delete')
    assert db.scheduled_tasks.count_documents({}) == 0


def test_reports_page_tolerates_scheduled_results(client, db, prices):
    db.reports.insert_one({'type': 'weekly', 'period': '2026-W42', 'date': datetime(2026, 10, 12),
                           'data': {}, 'insights': 'Weekly text'})
    tid = _reminder_task(db)
    rep = S.execute_task(S.get_task(tid))
    resp = client.get('/reports')
    assert resp.status_code == 200
    assert b'Review insurance cover' in resp.data and b'Scheduled Task Results' in resp.data
    # History link on a scheduled result goes to its own view
    r = client.get(f"/reports/{rep['_id']}")
    assert r.status_code == 302 and '/scheduled-tasks/results/' in r.headers['Location']
    assert client.get(f"/scheduled-tasks/results/{rep['_id']}").status_code == 200
    # The dashboard's "Latest Advisor Brief" still shows the advisor report
    db.app_settings.insert_one({'_id': 'app', 'setup_skipped': True})   # past the setup wizard
    home = client.get('/')
    assert home.status_code == 200
    assert b'Weekly text' in home.data and b'Scheduled' not in home.data.split(b'Latest Advisor Brief')[1][:400]
