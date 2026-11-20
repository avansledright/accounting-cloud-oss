"""Price alerts: get told when a stock you watch goes up.

An alert (collection `price_alerts`) watches one symbol with any of three triggers:

  - gain ladder: each step (e.g. +10/+25/+50/+100%) over the cost basis alerts
    once, the first time the price is at or above it;
  - target price: alerts once when the price reaches it;
  - daily jump: alerts once per trading day when the price is up that much on
    the previous close.

The basis is per share. It comes from the alert's own `basis_price` if set,
otherwise from the holdings (total cost_basis / total quantity across accounts,
counting only lots whose cost is known).

Checks run every PRICE_ALERT_POLL_SECONDS during the regular US trading
session, in every web process. Each firing is *claimed* with one
find_one_and_update that records it, so when several processes are running
only the one whose update matched records and sends it.

Every firing is stored in `price_alert_events` and shown on the Portfolio
page. Sending it anywhere is optional: with SLACK_BOT_TOKEN and SLACK_CHANNEL
set it is also posted to that Slack channel; without them nothing is sent and
nothing fails.

Stored times are naive local datetimes (the TZ environment variable), like the
rest of the app. Market hours are worked out in New York time whatever TZ is.
"""

import logging
import os
import re
import threading
from datetime import datetime, time as dtime, timedelta, timezone

import requests
from bson import ObjectId
from bson.errors import InvalidId
from pymongo import ReturnDocument

from agent.db import db

logger = logging.getLogger(__name__)

POLL_SECONDS = int(os.environ.get('PRICE_ALERT_POLL_SECONDS', '300'))
DEFAULT_LADDER = [10.0, 25.0, 50.0, 100.0]
SYMBOL_RE = re.compile(r'^[A-Z0-9.\-]{1,10}$')

# Regular US session in New York time, with a few minutes after the close so
# the closing price is seen once. Market holidays are harmless: the quote
# doesn't move, and the daily trigger is keyed on the quote's own trading day.
MARKET_OPEN = dtime(9, 30)
MARKET_CLOSE = dtime(16, 10)


def _market_tz():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo('America/New_York')
    except Exception:
        # No time zone database (the Docker image installs tzdata). US Eastern
        # standard time is close enough to decide whether to poll.
        logger.warning("No time zone data for America/New_York; assuming UTC-5")
        return timezone(timedelta(hours=-5))


MARKET_TZ = _market_tz()


def slack_settings():
    """(token, channel) from the environment, read at call time."""
    return (os.environ.get('SLACK_BOT_TOKEN', '').strip(),
            os.environ.get('SLACK_CHANNEL', '').strip())


def slack_configured():
    token, channel = slack_settings()
    return bool(token and channel)


# ---------------------------------------------------------------- data in

def fetch_quote(symbol):
    """{'price', 'prev_close', 'day'} from Yahoo, or None."""
    try:
        resp = requests.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=1d&interval=1d",
            headers={'User-Agent': 'Mozilla/5.0'}, timeout=8)
        resp.raise_for_status()
        result = (resp.json().get('chart') or {}).get('result') or []
        if not result:
            return None
        meta = result[0].get('meta', {})
        price = meta.get('regularMarketPrice')
        if not price:
            return None
        ts = meta.get('regularMarketTime')
        return {'price': float(price),
                'prev_close': float(meta['chartPreviousClose']) if meta.get('chartPreviousClose') else None,
                'day': datetime.fromtimestamp(ts, MARKET_TZ).strftime('%Y-%m-%d') if ts else None}
    except Exception:
        logger.warning("Price alert quote failed for %s", symbol, exc_info=True)
        return None


def position(symbol):
    """{'quantity', 'cost', 'basis'} for a symbol across all accounts."""
    qty = cost = qty_with_cost = 0.0
    for h in db.holdings.find({'symbol': symbol}):
        q = float(h.get('quantity') or 0)
        qty += q
        if h.get('cost_basis'):
            cost += float(h['cost_basis'])
            qty_with_cost += q
    basis = cost / qty_with_cost if qty_with_cost else None
    return {'quantity': qty, 'cost': cost if qty_with_cost else None, 'basis': basis}


def market_open(now):
    """True during the regular US session. A naive ``now`` is server local time."""
    local = now.astimezone(MARKET_TZ)   # astimezone() reads a naive value as local time
    return local.weekday() < 5 and MARKET_OPEN <= local.time() <= MARKET_CLOSE


# ---------------------------------------------------------------- deciding

def _money(x):
    return f"${x:,.4f}".rstrip('0').rstrip('.') if x < 1 else f"${x:,.2f}"


def _position_line(price, pos):
    if not pos or not pos.get('quantity'):
        return ''
    value = price * pos['quantity']
    line = f" Your {pos['quantity']:g} shares are worth {_money(value)}"
    if pos.get('cost'):
        gain = value - pos['cost']
        line += f" ({'+' if gain >= 0 else '-'}{_money(abs(gain))} on {_money(pos['cost'])})"
    return line + '.'


def evaluate(alert, quote, pos):
    """What should fire now. Pure: returns [(kind, key, message)].

    kind/key identify the firing for the claim: ('ladder', step), ('target', None),
    ('daily', trading_day).
    """
    sym, price = alert['symbol'], quote['price']
    basis = alert.get('basis_price') or (pos or {}).get('basis')
    out = []

    if basis and alert.get('ladder_pct'):
        gain = (price / basis - 1) * 100
        fired = set(alert.get('ladder_fired') or [])
        crossed = [s for s in alert['ladder_pct'] if gain >= s and s not in fired]
        if crossed:
            top = max(crossed)
            out.append(('ladder', top,
                        f":chart_with_upwards_trend: *{sym}* is up {gain:.1f}% on your cost "
                        f"(past your +{top:g}% step): {_money(price)} vs {_money(basis)} a share."
                        + _position_line(price, pos)))

    target = alert.get('target_price')
    if target and not alert.get('target_fired') and price >= target:
        out.append(('target', None,
                    f":dart: *{sym}* reached your {_money(target)} target: now {_money(price)}."
                    + _position_line(price, pos)))

    jump = alert.get('daily_jump_pct')
    prev = quote.get('prev_close')
    day = quote.get('day')
    if jump and prev and day and alert.get('daily_fired_on') != day:
        change = (price / prev - 1) * 100
        if change >= jump:
            out.append(('daily', day,
                        f":rocket: *{sym}* is up {change:.1f}% today: {_money(price)} "
                        f"(previous close {_money(prev)})." + _position_line(price, pos)))
    return out


def _claim(alert_id, kind, key, alert):
    """Record a firing; True only for the process whose update matched."""
    if kind == 'ladder':
        crossed = [s for s in alert['ladder_pct'] if s <= key]
        q = {'_id': alert_id, 'ladder_fired': {'$ne': key}}
        upd = {'$addToSet': {'ladder_fired': {'$each': crossed}}}
    elif kind == 'target':
        q = {'_id': alert_id, 'target_fired': {'$ne': True}}
        upd = {'$set': {'target_fired': True}}
    else:
        q = {'_id': alert_id, 'daily_fired_on': {'$ne': key}}
        upd = {'$set': {'daily_fired_on': key}}
    return db.price_alerts.find_one_and_update(q, upd, return_document=ReturnDocument.AFTER) is not None


# ---------------------------------------------------------------- sending

def notify(text):
    """Post one alert to Slack, if Slack is configured.

    Returns (delivered, error). With no token or channel set it sends nothing
    and returns (False, None): the alert is still recorded, and that is not an
    error.
    """
    token, channel = slack_settings()
    if not (token and channel):
        return False, None
    try:
        resp = requests.post('https://slack.com/api/chat.postMessage',
                             headers={'Authorization': f'Bearer {token}'},
                             json={'channel': channel, 'text': text}, timeout=10)
        body = resp.json()
        # Slack answers 200 even when it refuses the message; the body says why.
        if body.get('ok'):
            return True, None
        return False, str(body.get('error', 'unknown'))
    except Exception as e:
        return False, str(e)[:200]


# ---------------------------------------------------------------- running

def run_checks(now=None, force=False):
    """Check every enabled alert once. Returns the number of alerts that fired."""
    now = now or datetime.now()
    if not force and not market_open(now):
        return 0
    alerts = list(db.price_alerts.find({'enabled': True}))
    quotes, sent = {}, 0
    for alert in alerts:
        sym = alert.get('symbol')
        if not sym:
            continue
        if sym not in quotes:
            quotes[sym] = fetch_quote(sym)
        quote = quotes[sym]
        if not quote:
            continue
        db.price_alerts.update_one({'_id': alert['_id']},
                                   {'$set': {'last_price': quote['price'], 'last_checked': now}})
        pos = position(sym)
        for kind, key, message in evaluate(alert, quote, pos):
            if not _claim(alert['_id'], kind, key, alert):
                continue  # another process recorded it
            ok, err = notify(message)
            if err:
                logger.warning("Price alert for %s not delivered to Slack: %s", sym, err)
            db.price_alert_events.insert_one({
                'alert_id': alert['_id'], 'symbol': sym, 'kind': kind, 'message': message,
                'price': quote['price'], 'at': now, 'delivered': ok, 'error': err})
            sent += 1
    return sent


def alert_loop(poll_seconds=None, stop_event=None):
    poll = poll_seconds or POLL_SECONDS
    stop = stop_event or threading.Event()
    while not stop.is_set():
        try:
            n = run_checks()
            if n:
                logger.info("%d price alert(s) fired", n)
        except Exception:
            logger.exception("Price alert check failed")
        stop.wait(poll)


def start_alert_loop(poll_seconds=None):
    stop = threading.Event()
    threading.Thread(target=alert_loop, args=(poll_seconds, stop), daemon=True).start()
    logger.info("Price-alert loop started (every %ss in US market hours; Slack %s)",
                poll_seconds or POLL_SECONDS, 'on' if slack_configured() else 'not configured')
    return stop


# ---------------------------------------------------------------- managing

def _num(value, name, errors, minimum=0.0):
    if value in (None, '', 0, '0'):
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        errors.append(f"{name} must be a number")
        return None
    if v <= minimum:
        errors.append(f"{name} must be more than {minimum:g}")
        return None
    return v


def parse_ladder(value):
    """'10, 25, 50' / [10, 25] -> sorted floats; None for empty."""
    if value in (None, ''):
        return None
    items = value if isinstance(value, (list, tuple)) else str(value).replace('%', '').split(',')
    steps = sorted({float(str(s).strip()) for s in items if str(s).strip()})
    if any(s <= 0 for s in steps):
        raise ValueError("ladder steps must be positive percentages")
    return steps or None


def validate(fields):
    """Normalize create/update fields. Returns (doc, errors)."""
    errors = []
    symbol = str(fields.get('symbol') or '').upper().strip()
    if not SYMBOL_RE.match(symbol):
        errors.append("symbol is required (e.g. VTI)")
    try:
        ladder = parse_ladder(fields.get('ladder_pct'))
    except ValueError as e:
        ladder = None
        errors.append(str(e))
    doc = {
        'symbol': symbol,
        'ladder_pct': ladder,
        'target_price': _num(fields.get('target_price'), 'target price', errors),
        'daily_jump_pct': _num(fields.get('daily_jump_pct'), 'daily jump %', errors),
        'basis_price': _num(fields.get('basis_price'), 'basis price', errors),
        'notes': (fields.get('notes') or '').strip() or None,
    }
    if not (doc['ladder_pct'] or doc['target_price'] or doc['daily_jump_pct']):
        errors.append("set at least one trigger: a gain ladder, a target price or a daily jump %")
    return doc, errors


def create_alert(fields, now=None):
    doc, errors = validate(fields)
    if errors:
        return None, '; '.join(errors)
    doc.update({'enabled': True, 'ladder_fired': [], 'target_fired': False,
                'daily_fired_on': None, 'last_price': None, 'last_checked': None,
                'created_at': now or datetime.now()})
    return db.price_alerts.insert_one(doc).inserted_id, None


def _oid(alert_id):
    try:
        return ObjectId(str(alert_id))
    except (InvalidId, TypeError):
        return None


def delete_alert(alert_id):
    oid = _oid(alert_id)
    return bool(oid) and db.price_alerts.delete_one({'_id': oid}).deleted_count == 1


def reset_alert(alert_id):
    """Re-arm every trigger (e.g. after selling and buying back)."""
    oid = _oid(alert_id)
    if not oid:
        return False
    return db.price_alerts.update_one({'_id': oid}, {'$set': {
        'ladder_fired': [], 'target_fired': False, 'daily_fired_on': None,
        'enabled': True}}).matched_count == 1


def describe(alert):
    parts = []
    if alert.get('ladder_pct'):
        fired = set(alert.get('ladder_fired') or [])
        parts.append('gain ladder ' + ', '.join(
            f"+{s:g}%" + (' (fired)' if s in fired else '') for s in alert['ladder_pct']))
    if alert.get('target_price'):
        parts.append(f"target {_money(alert['target_price'])}" + (' (fired)' if alert.get('target_fired') else ''))
    if alert.get('daily_jump_pct'):
        parts.append(f"daily jump +{alert['daily_jump_pct']:g}%")
    return '; '.join(parts)


def list_for_page():
    """Alerts with their basis and gain, plus the latest events, for the Portfolio page."""
    alerts = list(db.price_alerts.find().sort('symbol', 1))
    for a in alerts:
        # Older or hand-made records may lack fields the template reads.
        for key in ('symbol', 'last_price', 'last_checked', 'basis_price', 'notes'):
            a.setdefault(key, None)
        a.setdefault('enabled', True)
        a['symbol'] = a['symbol'] or '?'
        pos = position(a['symbol'])
        a['basis'] = a.get('basis_price') or pos['basis']
        a['basis_source'] = 'set on alert' if a.get('basis_price') else ('holdings' if pos['basis'] else None)
        a['gain_pct'] = ((a['last_price'] / a['basis'] - 1) * 100
                         if a.get('last_price') and a.get('basis') else None)
        a['summary'] = describe(a)
        a['_id'] = str(a['_id'])
    events = list(db.price_alert_events.find().sort('at', -1).limit(15))
    for e in events:
        for key in ('symbol', 'kind', 'price', 'at', 'error'):
            e.setdefault(key, None)
        e.setdefault('delivered', False)
    return alerts, events


def _delivery_text(event):
    if event.get('delivered'):
        return 'sent to Slack'
    if event.get('error'):
        return f"NOT delivered to Slack: {event['error']}"
    return 'recorded (Slack not configured)'


# ---------------------------------------------------------------- MCP helpers (never raise)

def mcp_list():
    alerts, events = list_for_page()
    if not alerts:
        return "No price alerts. Add one with add_price_alert."
    lines = []
    for a in alerts:
        price = _money(a['last_price']) if a.get('last_price') else 'not checked yet'
        basis = f"basis {_money(a['basis'])} ({a['basis_source']})" if a.get('basis') else 'no cost basis'
        gain = f", {a['gain_pct']:+.1f}%" if a.get('gain_pct') is not None else ''
        lines.append(f"{a['_id']} | {a['symbol']} {'on' if a.get('enabled') else 'off'} — "
                     f"{a['summary']}. Last {price}{gain}; {basis}")
    if events:
        lines.append("\nRecent alerts:")
        lines += [f"{e['at']:%Y-%m-%d %H:%M} {e['symbol']} ({e['kind']}): {_delivery_text(e)}"
                  for e in events[:5] if isinstance(e.get('at'), datetime)]
    return '\n'.join(lines)


def mcp_add(symbol, ladder_pct='', target_price=0.0, daily_jump_pct=0.0, basis_price=0.0, notes=''):
    try:
        alert_id, err = create_alert({'symbol': symbol, 'ladder_pct': ladder_pct,
                                      'target_price': target_price, 'daily_jump_pct': daily_jump_pct,
                                      'basis_price': basis_price, 'notes': notes})
    except Exception as e:
        return f"Error: {e}"
    if err:
        return f"Error: {err}"
    alert = db.price_alerts.find_one({'_id': alert_id})
    basis = alert.get('basis_price') or position(alert['symbol'])['basis']
    warn = ('' if basis or not alert.get('ladder_pct') else
            " Note: no cost basis for this symbol in holdings, so the gain ladder can't fire "
            "until a holding with cost basis exists or basis_price is set.")
    where = ('' if slack_configured() else
             " Slack is not configured, so firings are recorded on the Portfolio page only.")
    return f"Created price alert {alert_id} for {alert['symbol']}: {describe(alert)}.{warn}{where}"


def mcp_delete(alert_id):
    return "Deleted." if delete_alert(alert_id) else f"Error: no price alert {alert_id}"


def mcp_reset(alert_id):
    return "Re-armed every trigger." if reset_alert(alert_id) else f"Error: no price alert {alert_id}"
