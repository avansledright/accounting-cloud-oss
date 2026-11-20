"""Price alerts: triggers, one-time firing, atomic claims, market hours, optional Slack, web + MCP.

"ACME" and every price, quantity and cost here are invented.
"""
from datetime import datetime, timezone
from unittest import mock

import pytest

import agent.price_alerts as P


def _local(year, month, day, hour, minute=0):
    """A New York wall-clock time as the naive server-local datetime the app uses."""
    return (datetime(year, month, day, hour, minute, tzinfo=P.MARKET_TZ)
            .astimezone().replace(tzinfo=None))


OPEN = _local(2026, 11, 10, 11)       # a Tuesday, mid-session
CLOSED = _local(2026, 11, 10, 19)


@pytest.fixture
def quotes(monkeypatch):
    table = {}
    monkeypatch.setattr(P, 'fetch_quote', lambda sym: table.get(sym))
    return table


@pytest.fixture
def slack(monkeypatch):
    """Slack is configured and accepts everything; returns the texts posted."""
    sent = []
    monkeypatch.setattr(P, 'notify', lambda text: (sent.append(text), (True, None))[1])
    return sent


@pytest.fixture
def no_slack(monkeypatch):
    monkeypatch.delenv('SLACK_BOT_TOKEN', raising=False)
    monkeypatch.delenv('SLACK_CHANNEL', raising=False)


def _hold(db, symbol, qty, cost):
    db.holdings.insert_one({'symbol': symbol, 'quantity': float(qty), 'cost_basis': cost,
                            'account_id': None, 'date_added': datetime(2026, 11, 1)})


def _quote(price, prev=None, day='2026-11-10'):
    return {'price': price, 'prev_close': prev, 'day': day}


def test_ladder_fires_each_step_once(db, quotes, slack):
    _hold(db, 'ACME', 200, 4000.0)                       # $20 a share
    P.create_alert({'symbol': 'acme', 'ladder_pct': '10, 25, 50'})
    quotes['ACME'] = _quote(21.00, prev=20.80)           # +5%: nothing
    assert P.run_checks(now=OPEN) == 0
    quotes['ACME'] = _quote(26.00, prev=25.00)           # +30%: one alert, for +25
    assert P.run_checks(now=OPEN) == 1
    assert '+25% step' in slack[-1] and '200 shares' in slack[-1]
    assert P.run_checks(now=OPEN) == 0                   # not again
    quotes['ACME'] = _quote(31.00, prev=30.00)           # +55%: the +50 step
    assert P.run_checks(now=OPEN) == 1
    assert sorted(db.price_alerts.find_one()['ladder_fired']) == [10, 25, 50]


def test_target_and_daily_jump(db, quotes, slack):
    P.create_alert({'symbol': 'ACME', 'target_price': '40', 'daily_jump_pct': '15'})
    quotes['ACME'] = _quote(24.00, prev=20.00)           # +20% today
    assert P.run_checks(now=OPEN) == 1 and 'up 20.0% today' in slack[-1]
    assert P.run_checks(now=OPEN) == 0                   # once a trading day
    quotes['ACME'] = _quote(42.00, prev=35.00, day='2026-11-11')   # new day, +20% and past target
    assert P.run_checks(now=OPEN) == 2
    assert any('$40.00 target' in m for m in slack)


def test_holiday_quote_does_not_refire_daily(db, quotes, slack):
    """The daily trigger is keyed on the quote's trading day, not today's date."""
    P.create_alert({'symbol': 'ACME', 'daily_jump_pct': '15'})
    quotes['ACME'] = _quote(24.00, prev=20.00, day='2026-11-10')
    assert P.run_checks(now=OPEN) == 1
    assert P.run_checks(now=_local(2026, 11, 11, 11)) == 0


def test_claim_lets_only_one_process_fire(db, quotes, slack):
    _hold(db, 'ACME', 10, 200.0)
    P.create_alert({'symbol': 'ACME', 'ladder_pct': '10'})
    alert = db.price_alerts.find_one()                   # two processes read it before either fires
    assert P._claim(alert['_id'], 'ladder', 10.0, alert) is True
    assert P._claim(alert['_id'], 'ladder', 10.0, alert) is False


def test_closed_market_skips_unless_forced(db, quotes, slack):
    P.create_alert({'symbol': 'ACME', 'target_price': '25'})
    quotes['ACME'] = _quote(26.00)
    assert P.run_checks(now=CLOSED) == 0
    assert P.run_checks(now=CLOSED, force=True) == 1


def test_market_hours_are_new_york_time_whatever_the_server_zone():
    ny = P.MARKET_TZ
    assert P.market_open(datetime(2026, 11, 10, 9, 30, tzinfo=ny))
    assert P.market_open(datetime(2026, 11, 10, 16, 5, tzinfo=ny))       # just after the close
    assert not P.market_open(datetime(2026, 11, 10, 9, 29, tzinfo=ny))
    assert not P.market_open(datetime(2026, 11, 10, 16, 11, tzinfo=ny))
    assert not P.market_open(datetime(2026, 11, 14, 11, 0, tzinfo=ny))   # a Saturday
    # 15:00 UTC is 10:00 in New York in November: a UTC server still polls.
    assert P.market_open(datetime(2026, 11, 10, 15, 0, tzinfo=timezone.utc))
    assert not P.market_open(datetime(2026, 11, 10, 10, 0, tzinfo=timezone.utc))


def test_no_basis_means_no_ladder_but_explicit_basis_works(db, quotes, slack):
    P.create_alert({'symbol': 'ACME', 'ladder_pct': '10'})
    quotes['ACME'] = _quote(100.00)
    assert P.run_checks(now=OPEN) == 0
    P.create_alert({'symbol': 'ACME', 'ladder_pct': '10', 'basis_price': '20'})
    assert P.run_checks(now=OPEN) == 1


def test_reset_rearms(db, quotes, slack):
    P.create_alert({'symbol': 'ACME', 'target_price': '25'})
    quotes['ACME'] = _quote(26.00)
    assert P.run_checks(now=OPEN) == 1
    P.reset_alert(str(db.price_alerts.find_one()['_id']))
    assert P.run_checks(now=OPEN) == 1


def test_undelivered_alert_is_recorded(db, quotes, monkeypatch):
    monkeypatch.setattr(P, 'notify', lambda text: (False, 'channel_not_found'))
    P.create_alert({'symbol': 'ACME', 'target_price': '25'})
    quotes['ACME'] = _quote(26.00)
    assert P.run_checks(now=OPEN) == 1
    ev = db.price_alert_events.find_one()
    assert ev['delivered'] is False and ev['error'] == 'channel_not_found'


# ---------------------------------------------------------------- Slack is optional
def test_without_slack_alerts_are_recorded_and_nothing_is_sent(client, db, quotes, no_slack):
    assert P.slack_configured() is False
    P.create_alert({'symbol': 'ACME', 'target_price': '25'})
    quotes['ACME'] = _quote(26.00)
    with mock.patch('requests.post') as post:
        assert P.run_checks(now=OPEN) == 1
    post.assert_not_called()
    ev = db.price_alert_events.find_one()
    assert ev['symbol'] == 'ACME' and ev['delivered'] is False and ev['error'] is None
    page = client.get('/portfolio').get_data(as_text=True)
    assert 'Recent alerts' in page and 'not sent to Slack' not in page
    assert 'recorded (Slack not configured)' in P.mcp_list()


def test_token_without_channel_sends_nothing(monkeypatch):
    monkeypatch.setenv('SLACK_BOT_TOKEN', 'xoxb-test-not-a-real-token')
    monkeypatch.delenv('SLACK_CHANNEL', raising=False)
    with mock.patch('requests.post') as post:
        assert P.notify('hello') == (False, None)
    post.assert_not_called()


def test_slack_post_uses_the_configured_channel_and_reports_refusals(monkeypatch):
    monkeypatch.setenv('SLACK_BOT_TOKEN', 'xoxb-test-not-a-real-token')
    monkeypatch.setenv('SLACK_CHANNEL', '#example-alerts')
    with mock.patch('requests.post') as post:
        post.return_value.json.return_value = {'ok': True}
        assert P.notify('hello') == (True, None)
        assert post.call_args.kwargs['json'] == {'channel': '#example-alerts', 'text': 'hello'}
        post.return_value.json.return_value = {'ok': False, 'error': 'not_in_channel'}
        assert P.notify('hello') == (False, 'not_in_channel')
        post.side_effect = ConnectionError('offline')
        ok, err = P.notify('hello')
        assert ok is False and 'offline' in err


def test_validation():
    assert P.validate({'symbol': 'ACME'})[1]             # no trigger
    assert P.validate({'symbol': '', 'target_price': 2})[1]
    assert P.validate({'symbol': 'ACME', 'ladder_pct': '10, -5'})[1]
    doc, errs = P.validate({'symbol': 'acme', 'ladder_pct': '50,10%,25'})
    assert not errs and doc['ladder_pct'] == [10.0, 25.0, 50.0] and doc['symbol'] == 'ACME'


def test_mcp_add_warns_without_basis(db, no_slack):
    out = P.mcp_add('ACME', ladder_pct='10,25')
    assert out.startswith('Created') and "can't fire" in out and 'Slack is not configured' in out
    assert 'ACME' in P.mcp_list()
    alert_id = str(db.price_alerts.find_one()['_id'])
    assert P.mcp_reset(alert_id).startswith('Re-armed')
    assert P.mcp_delete(alert_id) == 'Deleted.'
    assert P.mcp_delete('nope').startswith('Error')


def test_portfolio_page_and_routes(client, db, quotes, slack):
    assert 'Price Alerts' in client.get('/portfolio').get_data(as_text=True)   # empty state
    _hold(db, 'ACME', 200, 4000.0)
    r = client.post('/portfolio/alerts/add', data={'symbol': 'ACME', 'ladder_pct': '10, 25',
                                                   'daily_jump_pct': '15'})
    assert r.status_code == 302
    quotes['ACME'] = _quote(26.00, prev=20.00)
    client.post('/portfolio/alerts/check')
    page = client.get('/portfolio').get_data(as_text=True)
    assert 'Price Alerts' in page and 'ACME' in page and '+30.0%' in page
    bad = client.post('/portfolio/alerts/add', data={'symbol': 'ACME'})
    assert 'alert_error' in bad.headers['Location']
    alert_id = str(db.price_alerts.find_one()['_id'])
    assert client.post(f'/portfolio/alerts/{alert_id}/reset').status_code == 302
    client.post(f'/portfolio/alerts/{alert_id}/delete')
    assert db.price_alerts.count_documents({}) == 0


def test_old_records_missing_fields_still_render(client, db):
    db.price_alerts.insert_one({'symbol': 'ACME', 'target_price': 25.0})
    db.price_alert_events.insert_one({'symbol': 'ACME', 'kind': 'target'})
    assert client.get('/portfolio').status_code == 200
    assert 'ACME' in P.mcp_list()


def test_slack_variables_are_documented():
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for path in ('.env.example', 'README.md', os.path.join('docs', 'deployment.md'),
                 os.path.join('docs', 'price-alerts.md')):
        with open(os.path.join(root, path)) as fh:
            text = fh.read()
        assert 'SLACK_BOT_TOKEN' in text and 'SLACK_CHANNEL' in text, path
    with open(os.path.join(root, '.env.example')) as fh:
        env = fh.read()
    assert '\nSLACK_BOT_TOKEN=\n' in env and '\nSLACK_CHANNEL=\n' in env   # blank: off by default
