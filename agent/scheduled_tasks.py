"""User-configurable scheduled tasks.

Recurring financial chores (an annual rebalance check, a quarterly reminder,
an extra advisor report) live in the `scheduled_tasks` collection and run
inside the app on their own schedule. Every run is stored in `db.reports`
as `{type: 'scheduled', task_id, task_name, task_type, ...}`.

Layout of this module:
  - schedule math (`compute_next_run`, `describe_schedule`, `normalize_schedule`)
  - task-type registry (`TASK_TYPES`) — rebalance_check, report, reminder
  - execution (`execute_task`, `claim_task`, `run_due_tasks`, polling loop)
  - validation shared by the web form and the MCP tools
  - MCP-facing helpers that return friendly strings and never raise

Times are naive local datetimes in the server's time zone (the TZ environment
variable, UTC unless set), the same convention as the rest of the app. Never
store datetime.date in Mongo.

Every web process runs the polling loop, so when more than one is running a
due task is *claimed* with one find_one_and_update that advances next_run
before it runs. Only the process whose update matched executes it.

AI is optional. The rebalance check and the reminder need no model. An
advisor-report task does: while AI is off it is skipped with a clear message
(TaskSkipped) and nothing is called. The rebalance check's optional narrative
goes through ai.py like every other AI feature and is left out while AI is off.
"""

import calendar
import json
import logging
import os
import re
import threading
from datetime import date, datetime, timedelta

from bson import ObjectId
from bson.errors import InvalidId
from pymongo import ReturnDocument

import ai
from agent.db import db
from agent.tools.portfolio import fetch_live_price
from models import INVESTMENT_ACCOUNT_TYPES

logger = logging.getLogger(__name__)

FREQUENCIES = ('weekly', 'monthly', 'quarterly', 'yearly', 'once')
DEFAULT_HOUR = 8
POLL_SECONDS = int(os.environ.get('SCHEDULED_TASKS_POLL_SECONDS', '300'))
TARGET_SUM_TOLERANCE = 0.5
MIN_TRADE_DOLLARS = 1.0
REPORT_TYPES = ('weekly', 'monthly', 'portfolio')
REPORT_TYPE_LABELS = {'weekly': 'Weekly Briefing', 'monthly': 'Monthly Review',
                      'portfolio': 'Portfolio Review'}
SYMBOL_RE = re.compile(r'^[A-Z0-9.\-]{1,10}$')
WEEKDAYS = ('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday')
TAX_ADVANTAGED_NOTE = ("Trades inside an IRA, Roth IRA or 401(k) have no tax impact — "
                       "no capital gains are realized when you sell.")
WASH_SALE_NOTE = ("Wash-sale rule: buying the same (or a substantially identical) fund in ANY "
                  "account — including IRAs/Roth, and automatic dividend reinvestment — within "
                  "30 days before or after selling it at a loss disallows the loss.")

# The narrative is added after the result is stored, in its own thread, so a
# slow or unreachable AI provider can never block or fail a task. Tests flip
# this off.
NARRATIVE_ASYNC = True

AI_OFF_MESSAGE = ("Advisor reports need AI, which is turned off. Turn it on under "
                  "Admin > AI settings (with Advisor reports ticked) and this task "
                  "will run at its next scheduled time.")


class TaskSkipped(Exception):
    """A task could not run for a reason that is not a failure (AI is off).
    ``str(e)`` is shown to the user."""


def ai_available():
    """True when the provider set on the AI settings page can write reports."""
    try:
        return ai.enabled('report')
    except Exception:
        logger.exception("Could not read the AI settings")
        return False


# ============================================================
# Small helpers
# ============================================================

def _oid(value):
    if isinstance(value, ObjectId):
        return value
    if not value:
        return None
    try:
        return ObjectId(str(value))
    except (InvalidId, TypeError):
        return None


def _as_datetime(value):
    """Accept datetime/date/'YYYY-MM-DD' and return a datetime (never a date)."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    if isinstance(value, str) and value.strip():
        try:
            return datetime.fromisoformat(value.strip())
        except ValueError:
            return None
    return None


def _as_bool(value, default=False):
    if value is None or value == '':
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in ('1', 'true', 'yes', 'on', 'y')


def _as_int(value, default=None):
    if value is None or value == '':
        return default
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _money(x):
    """$1,234.56 / -$1,234.56"""
    return f"{'-' if x < 0 else ''}${abs(x):,.2f}"


def _fmt_hour(hour):
    hour = int(hour or 0)
    suffix = 'am' if hour < 12 else 'pm'
    h12 = hour % 12 or 12
    return f"{h12}{suffix}"


def _ordinal(n):
    n = int(n)
    suffix = 'th' if 11 <= n % 100 <= 13 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')
    return f"{n}{suffix}"


def _at(year, month, day, hour):
    """datetime for (year, month, day) with day clamped to the month's length."""
    last = calendar.monthrange(year, month)[1]
    return datetime(year, month, min(int(day), last), int(hour))


# ============================================================
# Schedule math
# ============================================================

def compute_next_run(schedule, after):
    """Next fire time strictly after `after`, or None when there is none.

    schedule: {frequency, month, day, weekday, hour, date}
      weekly    — weekday (0=Mon..6=Sun) at hour
      monthly   — day (1-31, clamped to the month's length) at hour
      quarterly — every 3 months starting from `month`, on `day`
      yearly    — `month`/`day` (Feb 29 → Feb 28 in non-leap years)
      once      — `date` at hour; None once that moment has passed
    """
    if not schedule or not isinstance(after, datetime):
        return None
    freq = schedule.get('frequency')
    hour = _as_int(schedule.get('hour'), DEFAULT_HOUR)
    if hour is None or not 0 <= hour <= 23:
        hour = DEFAULT_HOUR

    if freq == 'once':
        when = _as_datetime(schedule.get('date'))
        if not when:
            return None
        fire = datetime(when.year, when.month, when.day, hour)
        return fire if fire > after else None

    if freq == 'weekly':
        weekday = _as_int(schedule.get('weekday'), 0) % 7
        base = datetime(after.year, after.month, after.day, hour)
        candidate = base + timedelta(days=(weekday - after.weekday()) % 7)
        if candidate <= after:
            candidate += timedelta(days=7)
        return candidate

    day = _as_int(schedule.get('day'), 1) or 1
    if freq == 'monthly':
        step, anchor = 1, after.month
    elif freq == 'quarterly':
        step, anchor = 3, _as_int(schedule.get('month'), 1) or 1
    elif freq == 'yearly':
        step, anchor = 12, _as_int(schedule.get('month'), 1) or 1
    else:
        return None

    year, month = after.year, after.month
    for _ in range(26):  # > 2 years covers every step size
        if (month - anchor) % step == 0:
            candidate = _at(year, month, day, hour)
            if candidate > after:
                return candidate
        month += 1
        if month > 12:
            month, year = 1, year + 1
    return None


def describe_schedule(schedule):
    """Plain-English schedule, e.g. 'Every year on Oct 15 at 8am'."""
    if not schedule:
        return 'No schedule'
    freq = schedule.get('frequency')
    at = f"at {_fmt_hour(_as_int(schedule.get('hour'), DEFAULT_HOUR))}"
    day = _as_int(schedule.get('day'), 1) or 1
    month = _as_int(schedule.get('month'), 1) or 1
    if freq == 'weekly':
        return f"Every week on {WEEKDAYS[_as_int(schedule.get('weekday'), 0) % 7]} {at}"
    if freq == 'monthly':
        tail = ' (or the last day of shorter months)' if day > 28 else ''
        return f"Every month on the {_ordinal(day)}{tail} {at}"
    if freq == 'quarterly':
        months = [calendar.month_abbr[(month - 1 + 3 * k) % 12 + 1] for k in range(4)]
        return f"Every quarter on the {_ordinal(day)} of {', '.join(months)} {at}"
    if freq == 'yearly':
        return f"Every year on {calendar.month_abbr[month]} {day} {at}"
    if freq == 'once':
        when = _as_datetime(schedule.get('date'))
        return f"Once on {when.strftime('%b %d, %Y')} {at}" if when else 'Once (no date set)'
    return 'Unknown schedule'


def normalize_schedule(raw, now=None):
    """Validate/clean a schedule dict. Returns (schedule, error_or_None)."""
    now = now or datetime.now()
    raw = raw or {}
    freq = (raw.get('frequency') or '').strip().lower()
    if freq not in FREQUENCIES:
        return None, f"frequency must be one of {', '.join(FREQUENCIES)}."
    hour = _as_int(raw.get('hour'), DEFAULT_HOUR)
    if hour is None or not 0 <= hour <= 23:
        return None, "hour must be 0-23 (local time)."
    sched = {'frequency': freq, 'hour': hour}

    if freq == 'weekly':
        weekday = _as_int(raw.get('weekday'), -1)
        if weekday is None or not 0 <= weekday <= 6:
            return None, "weekly schedules need weekday 0-6 (0 = Monday)."
        sched['weekday'] = weekday
        return sched, None

    if freq == 'once':
        when = _as_datetime(raw.get('date'))
        if when is None:
            month, day = _as_int(raw.get('month'), 0), _as_int(raw.get('day'), 0)
            if not (1 <= (month or 0) <= 12 and 1 <= (day or 0) <= 31):
                return None, "'once' needs a date (YYYY-MM-DD) or month + day."
            # Next occurrence of that month/day at the given hour
            year = now.year
            when = _at(year, month, day, hour)
            if when <= now:
                when = _at(year + 1, month, day, hour)
        sched['date'] = datetime(when.year, when.month, when.day)
        if compute_next_run(sched, now) is None:
            return None, "that date/hour is already in the past."
        return sched, None

    day = _as_int(raw.get('day'), 0)
    if day is None or not 1 <= day <= 31:
        return None, f"{freq} schedules need day 1-31."
    sched['day'] = day
    if freq in ('quarterly', 'yearly'):
        month = _as_int(raw.get('month'), 0)
        if month is None or not 1 <= month <= 12:
            return None, f"{freq} schedules need month 1-12."
        sched['month'] = month
    return sched, None


# ============================================================
# Task type: rebalance_check
# ============================================================

def _account_holdings(account_oid):
    """Holdings for one account (accepts legacy string account_ids too)."""
    return list(db.holdings.find({'account_id': {'$in': [account_oid, str(account_oid)]}}))


def _price_lookup():
    cache = {}

    def get(symbol):
        if symbol not in cache:
            try:
                cache[symbol] = fetch_live_price(symbol)
            except Exception:
                logger.warning("Price fetch raised for %s", symbol, exc_info=True)
                cache[symbol] = None
        return cache[symbol]
    return get


def _positions(holdings, price_of):
    """Aggregate lots by symbol and value them.

    price_source: 'live' | 'cost_basis' (stale fallback) | 'unavailable'.
    Unavailable positions are excluded from totals and drift math.
    """
    by_symbol = {}
    for h in holdings:
        sym = (h.get('symbol') or '').upper()
        if not sym:
            continue
        p = by_symbol.setdefault(sym, {'symbol': sym, 'quantity': 0.0, 'cost_basis': 0.0,
                                       'cost_known': True})
        p['quantity'] += float(h.get('quantity') or 0)
        cost = h.get('cost_basis')
        if cost in (None, ''):
            p['cost_known'] = False
        else:
            p['cost_basis'] += float(cost)

    out = []
    for sym in sorted(by_symbol):
        p = by_symbol[sym]
        cost = p['cost_basis'] if p['cost_known'] else None
        price = price_of(sym)
        if price is not None:
            value, source = float(price) * p['quantity'], 'live'
        elif cost:
            value, source = cost, 'cost_basis'
        else:
            value, source = None, 'unavailable'
        out.append({
            'symbol': sym,
            'quantity': round(p['quantity'], 6),
            'price': float(price) if price is not None else None,
            'value': round(value, 2) if value is not None else None,
            'cost_basis': round(cost, 2) if cost is not None else None,
            'price_source': source,
        })
    return out


def _rebalance_account(acct_cfg, threshold, price_of):
    oid = _oid(acct_cfg.get('account_id'))
    acct = db.accounts.find_one({'_id': oid}) if oid else None
    taxable = bool(acct_cfg.get('taxable'))
    scan_only = bool(acct_cfg.get('scan_only'))
    targets = {str(k).upper(): float(v) for k, v in (acct_cfg.get('targets') or {}).items()}
    res = {
        'account_id': str(oid) if oid else str(acct_cfg.get('account_id')),
        'account_name': (acct or {}).get('name', 'Unknown account'),
        'account_type': (acct or {}).get('account_type'),
        'taxable': taxable,
        'scan_only': scan_only,
        'total_value': 0.0,
        'rows': [],
        'breaches': [],
        'trades': [],
        'tlh_candidates': [],
        'warnings': [],
        'notes': [],
        'positions': [],
    }
    if not acct:
        res['warnings'].append("Account not found — was it deleted?")
        return res

    positions = _positions(_account_holdings(oid), price_of)
    res['positions'] = positions
    priced = [p for p in positions if p['value'] is not None]
    total = sum(p['value'] for p in priced)
    res['total_value'] = round(total, 2)

    for p in positions:
        if p['price_source'] == 'cost_basis':
            res['warnings'].append(f"{p['symbol']}: live price unavailable — valued at cost basis "
                                   f"(${p['value']:,.2f}); figures for it are stale.")
        elif p['price_source'] == 'unavailable':
            res['warnings'].append(f"{p['symbol']}: no live price and no cost basis — "
                                   "excluded from the account total and drift math.")

    if not positions:
        res['warnings'].append("No holdings recorded for this account.")

    if not scan_only and total > 0:
        by_sym = {p['symbol']: p for p in priced}
        for sym in sorted(set(targets) | set(by_sym)):
            p = by_sym.get(sym)
            value = p['value'] if p else 0.0
            current_pct = value / total * 100
            target_pct = targets.get(sym, 0.0)
            drift = current_pct - target_pct
            target_value = total * target_pct / 100
            row = {
                'symbol': sym,
                'value': round(value, 2),
                'current_pct': round(current_pct, 2),
                'target_pct': round(target_pct, 2),
                'drift_pct': round(drift, 2),
                'target_value': round(target_value, 2),
                'diff': round(target_value - value, 2),
                'price': p['price'] if p else None,
                'price_source': p['price_source'] if p else None,
                'untargeted': sym not in targets,
                'breach': abs(drift) > threshold,
            }
            res['rows'].append(row)
            if row['breach']:
                res['breaches'].append(sym)

        if res['breaches']:
            price_known = {p['symbol']: p['price'] for p in positions if p['price'] is not None}
            for row in res['rows']:
                if abs(row['diff']) < MIN_TRADE_DOLLARS:
                    continue
                action = 'buy' if row['diff'] > 0 else 'sell'
                amount = abs(row['diff'])
                price = price_known.get(row['symbol'])
                if price is None and action == 'buy':
                    price = price_of(row['symbol'])  # target fund not held yet
                trade = {
                    'symbol': row['symbol'],
                    'action': action,
                    'amount': round(amount, 2),
                    'shares': round(amount / price, 4) if price else None,
                    'price': price,
                }
                if action == 'sell' and taxable:
                    p = by_sym.get(row['symbol'])
                    if p and p.get('cost_basis') is not None and p['value']:
                        basis_sold = p['cost_basis'] * (amount / p['value'])
                        trade['est_gain'] = round(amount - basis_sold, 2)
                res['trades'].append(trade)
            res['trades'].sort(key=lambda t: (t['action'] != 'sell', -t['amount']))
            res['notes'].append(
                "Taxable account: sells may realize capital gains (est_gain is a pro-rata "
                "estimate from total cost basis). Consider directing new contributions to the "
                "underweight funds instead of selling."
                if taxable else TAX_ADVANTAGED_NOTE)
    elif not scan_only and positions:
        res['warnings'].append("Couldn't value any holdings — drift not computed.")

    return res


def _tax_loss_candidates(acct_res):
    out = []
    for p in acct_res['positions']:
        # Only a live price can show a real loss; cost-basis fallback is 0 by definition
        if p['price_source'] != 'live' or not p.get('cost_basis'):
            continue
        if p['value'] < p['cost_basis']:
            loss = p['value'] - p['cost_basis']
            out.append({
                'symbol': p['symbol'],
                'value': p['value'],
                'cost_basis': p['cost_basis'],
                'unrealized_loss': round(loss, 2),
                'loss_pct': round(loss / p['cost_basis'] * 100, 2),
                'also_held_in': [],
            })
    return out


def _wash_sale_scan(acct_res, candidates, all_results):
    """Attach other-account holdings (any account, configured or not) to each candidate."""
    if not candidates:
        return
    syms = [c['symbol'] for c in candidates]
    this_id = acct_res['account_id']
    names = {str(a['_id']): a.get('name', 'Unknown') for a in db.accounts.find({}, {'name': 1})}
    held = {}
    for h in db.holdings.find({'symbol': {'$in': syms}}):
        aid = str(h.get('account_id', ''))
        if aid == this_id:
            continue
        held.setdefault(h['symbol'].upper(), set()).add(names.get(aid, 'Unknown account'))
    planned_buys = {}
    for other in all_results:
        if other['account_id'] == this_id:
            continue
        for t in other['trades']:
            if t['action'] == 'buy':
                planned_buys.setdefault(t['symbol'], []).append(other['account_name'])
    for c in candidates:
        c['also_held_in'] = sorted(held.get(c['symbol'], ()))
        c['planned_buys_in'] = sorted(set(planned_buys.get(c['symbol'], ())))
        if c['also_held_in'] or c['planned_buys_in']:
            where = ', '.join(sorted(set(c['also_held_in']) | set(c['planned_buys_in'])))
            acct_res['warnings'].append(
                f"Wash-sale risk: {c['symbol']} is also held/bought in {where}. Buying "
                f"{c['symbol']} in any account within 30 days before or after harvesting this "
                "loss disallows it.")
            if c['planned_buys_in']:
                acct_res['warnings'].append(
                    f"This check also suggests BUYING {c['symbol']} in "
                    f"{', '.join(c['planned_buys_in'])} — don't do both within 30 days.")


def run_rebalance_check(config, task=None):
    threshold = float(config.get('drift_threshold_pct') or 5)
    include_tlh = _as_bool(config.get('include_tax_loss_harvest'), True)
    price_of = _price_lookup()
    accounts = [_rebalance_account(a, threshold, price_of) for a in config.get('accounts') or []]
    if include_tlh:
        for acct in accounts:
            if acct['taxable'] and acct['positions']:
                acct['tlh_candidates'] = _tax_loss_candidates(acct)
                _wash_sale_scan(acct, acct['tlh_candidates'], accounts)
    data = {
        'drift_threshold_pct': threshold,
        'include_tax_loss_harvest': include_tlh,
        'any_breach': any(a['breaches'] for a in accounts),
        'any_tlh': any(a['tlh_candidates'] for a in accounts),
        'accounts': accounts,
        'wash_sale_note': WASH_SALE_NOTE,
    }
    return {'data': data, 'summary_md': rebalance_summary_md(data), 'narrative': True}


def rebalance_summary_md(data):
    thr = data['drift_threshold_pct']
    lines = []
    if data['any_breach']:
        n = sum(1 for a in data['accounts'] if a['breaches'])
        lines.append(f"**Rebalance needed** in {n} account(s) — drift beyond ±{thr:g}%.")
    else:
        lines.append(f"**No trades needed** — every account is within ±{thr:g}% of target.")
    for a in data['accounts']:
        lines.append('')
        kind = 'taxable' if a['taxable'] else 'tax-advantaged'
        lines.append(f"### {a['account_name']} ({kind}) — ${a['total_value']:,.2f}")
        if a['scan_only']:
            lines.append("_Scan only: no targets, checked for tax-loss harvesting._")
        elif a['rows']:
            lines.append('')
            lines.append('| Symbol | Value | Current | Target | Drift |')
            lines.append('|---|---:|---:|---:|---:|')
            for r in a['rows']:
                flag = ' ⚠' if r['breach'] else ''
                lines.append(f"| {r['symbol']} | ${r['value']:,.2f} | {r['current_pct']:.1f}% | "
                             f"{r['target_pct']:.1f}% | {r['drift_pct']:+.1f}%{flag} |")
            if a['trades']:
                lines.append('')
                lines.append('Suggested trades:')
                for t in a['trades']:
                    sh = f" (~{t['shares']:,.2f} sh)" if t.get('shares') else ''
                    gain = f", est. gain {_money(t['est_gain'])}" if 'est_gain' in t else ''
                    lines.append(f"- {t['action'].upper()} {t['symbol']} ${t['amount']:,.2f}{sh}{gain}")
            elif not a['breaches']:
                lines.append('')
                lines.append('Within threshold — no trades needed.')
        if a['tlh_candidates']:
            lines.append('')
            lines.append('Tax-loss harvest candidates:')
            for c in a['tlh_candidates']:
                lines.append(f"- {c['symbol']}: {_money(c['unrealized_loss'])} ({c['loss_pct']:+.1f}%)")
        elif a['taxable'] and data['include_tax_loss_harvest'] and a['total_value']:
            lines.append('')
            lines.append('No positions at an unrealized loss.')
        for note in a['notes']:
            lines.append('')
            lines.append(f"_{note}_")
        for w in a['warnings']:
            lines.append(f"- ⚠ {w}")
    if data['any_tlh']:
        lines.append('')
        lines.append(f"_{WASH_SALE_NOTE}_")
    return "\n".join(lines)


def _rebalance_narrative_prompt(task, summary_md):
    return (
        "You are a concise fiduciary financial planner. Below is the result of an "
        f"automated portfolio rebalance check ('{task.get('name', 'Rebalance check')}'). "
        "Write 2-3 plain sentences summarizing what, if anything, should be done and any "
        "tax caveats. Do not invent numbers that are not in the data.\n\n" + summary_md
    )


# ============================================================
# Task type: report
# ============================================================

def run_report_task(config, task=None):
    from agent import reports as advisor_reports  # late: tests patch the generators
    rtype = config.get('report_type')
    if rtype not in REPORT_TYPES:
        raise ValueError(f"unknown report_type {rtype!r}")
    if not ai_available():
        raise TaskSkipped(AI_OFF_MESSAGE)
    gen = {
        'weekly': advisor_reports.generate_weekly_report,
        'monthly': advisor_reports.generate_monthly_report,
        'portfolio': advisor_reports.generate_portfolio_report,
    }.get(rtype)
    if not gen:
        raise ValueError(f"unknown report_type {rtype!r}")
    rep = gen() or {}
    stored = db.reports.find_one({'type': rtype, 'period': rep.get('period')}, {'_id': 1})
    label = REPORT_TYPE_LABELS[rtype]
    has_ai = bool(rep.get('insights'))
    data = {
        'report_type': rtype,
        'report_period': rep.get('period'),
        'report_id': str(stored['_id']) if stored else None,
        'has_insights': has_ai,
    }
    summary = (f"Generated the **{label}** for {rep.get('period')}. "
               + ("AI insights included." if has_ai else "The AI provider did not answer, so it has no insights."))
    return {'data': data, 'summary_md': summary}


# ============================================================
# Task type: reminder
# ============================================================

def run_reminder(config, task=None):
    message = config.get('message') or ''
    checklist = list(config.get('checklist') or [])
    lines = [message] if message else []
    if checklist:
        lines.append('')
        lines.extend(f"- [ ] {item}" for item in checklist)
    return {'data': {'message': message, 'checklist': checklist}, 'summary_md': "\n".join(lines)}


# ============================================================
# Config validation (per type)
# ============================================================

def _validate_rebalance_config(cfg):
    if not isinstance(cfg, dict):
        return None, "config must be an object."
    accounts = cfg.get('accounts')
    if not isinstance(accounts, list) or not accounts:
        return None, "rebalance_check needs at least one account in config.accounts."
    try:
        threshold = float(cfg.get('drift_threshold_pct', 5) or 5)
    except (TypeError, ValueError):
        return None, "drift_threshold_pct must be a number."
    if not 0 < threshold <= 100:
        return None, "drift_threshold_pct must be between 0 and 100."
    seen = set()
    clean_accounts = []
    for i, a in enumerate(accounts, 1):
        if not isinstance(a, dict):
            return None, f"account #{i} must be an object."
        oid = _oid(a.get('account_id'))
        acct = db.accounts.find_one({'_id': oid}) if oid else None
        if not acct:
            return None, f"account #{i}: unknown account_id {a.get('account_id')!r}."
        if acct.get('account_type') not in INVESTMENT_ACCOUNT_TYPES:
            return None, f"account #{i} ({acct.get('name')}) is not an investment account."
        if str(oid) in seen:
            return None, f"account {acct.get('name')} is listed twice."
        seen.add(str(oid))
        scan_only = _as_bool(a.get('scan_only'))
        raw_targets = a.get('targets') or {}
        if not isinstance(raw_targets, dict):
            return None, f"{acct.get('name')}: targets must be an object of SYMBOL: percent."
        targets = {}
        for sym, pct in raw_targets.items():
            sym = str(sym).strip().upper()
            if not SYMBOL_RE.match(sym):
                return None, f"{acct.get('name')}: invalid symbol {sym!r}."
            try:
                pct = float(pct)
            except (TypeError, ValueError):
                return None, f"{acct.get('name')}: target for {sym} must be a number."
            if pct < 0 or pct > 100:
                return None, f"{acct.get('name')}: target for {sym} must be 0-100."
            targets[sym] = pct
        if not scan_only:
            total = sum(targets.values())
            if abs(total - 100) > TARGET_SUM_TOLERANCE:
                return None, (f"{acct.get('name')}: targets sum to {total:g}%, must be 100% "
                              f"(±{TARGET_SUM_TOLERANCE}). Use scan_only for a TLH-only account.")
        clean_accounts.append({
            'account_id': str(oid),
            'targets': targets,
            'taxable': _as_bool(a.get('taxable')),
            'scan_only': scan_only,
        })
    return {
        'accounts': clean_accounts,
        'drift_threshold_pct': threshold,
        'include_tax_loss_harvest': _as_bool(cfg.get('include_tax_loss_harvest'), True),
        'ai_narrative': _as_bool(cfg.get('ai_narrative'), True),
    }, None


def _validate_report_config(cfg):
    rtype = (cfg or {}).get('report_type')
    if rtype not in REPORT_TYPES:
        return None, f"report_type must be one of {', '.join(REPORT_TYPES)}."
    return {'report_type': rtype}, None


def _validate_reminder_config(cfg):
    cfg = cfg or {}
    message = str(cfg.get('message') or '').strip()
    checklist = cfg.get('checklist') or []
    if isinstance(checklist, str):
        checklist = checklist.splitlines()
    checklist = [str(c).strip() for c in checklist if str(c).strip()]
    if not message and not checklist:
        return None, "reminder needs a message or at least one checklist item."
    return {'message': message, 'checklist': checklist}, None


# ============================================================
# Registry
# ============================================================

TASK_TYPES = {
    'rebalance_check': {
        'label': 'Rebalance check',
        'run': run_rebalance_check,
        'validate': _validate_rebalance_config,
    },
    'report': {
        'label': 'Advisor report',
        'run': run_report_task,
        'validate': _validate_report_config,
        'needs_ai': True,
    },
    'reminder': {
        'label': 'Reminder',
        'run': run_reminder,
        'validate': _validate_reminder_config,
    },
}


def validate_task(fields, existing=None, now=None):
    """Build a clean task doc from user input. Returns (doc, error_or_None).

    fields: {name, task_type, schedule{...}, config{...}, notes, enabled}
    existing: the stored task when updating (missing fields fall back to it).
    """
    now = now or datetime.now()
    existing = existing or {}
    name = str(fields.get('name', existing.get('name')) or '').strip()
    if not name:
        return None, "name is required."
    if len(name) > 120:
        return None, "name is too long (max 120 characters)."
    task_type = fields.get('task_type') or existing.get('task_type')
    if task_type not in TASK_TYPES:
        return None, f"task_type must be one of {', '.join(TASK_TYPES)}."
    schedule, err = normalize_schedule(fields.get('schedule') or existing.get('schedule'), now)
    if err:
        return None, err
    config, err = TASK_TYPES[task_type]['validate'](
        fields['config'] if 'config' in fields else existing.get('config') or {})
    if err:
        return None, err
    enabled = _as_bool(fields.get('enabled'), existing.get('enabled', True))
    notes = fields.get('notes', existing.get('notes'))
    return {
        'name': name,
        'task_type': task_type,
        'enabled': enabled,
        'schedule': schedule,
        'config': config,
        'notes': (str(notes).strip() or None) if notes is not None else None,
    }, None


# ============================================================
# CRUD
# ============================================================

def create_task(fields, now=None):
    """Validate and insert. Returns (task_id_str, error_or_None)."""
    now = now or datetime.now()
    doc, err = validate_task(fields, now=now)
    if err:
        return None, err
    doc.update({
        'next_run': compute_next_run(doc['schedule'], now),
        'last_run': None, 'last_status': None, 'last_error': None,
        'last_report_id': None, 'running_since': None,
        'created_at': now, 'updated_at': now,
    })
    return str(db.scheduled_tasks.insert_one(doc).inserted_id), None


def update_task(task_id, fields, now=None):
    """Validate and update (missing fields keep their stored values). Returns error_or_None."""
    now = now or datetime.now()
    oid = _oid(task_id)
    existing = db.scheduled_tasks.find_one({'_id': oid}) if oid else None
    if not existing:
        return "scheduled task not found."
    doc, err = validate_task(fields, existing=existing, now=now)
    if err:
        return err
    doc['updated_at'] = now
    # Re-anchor next_run when the schedule changes or the task is (re-)enabled,
    # so editing never triggers a catch-up run for a stale next_run.
    if (doc['schedule'] != existing.get('schedule')
            or (doc['enabled'] and not existing.get('enabled'))
            or existing.get('next_run') is None):
        doc['next_run'] = compute_next_run(doc['schedule'], now)
    db.scheduled_tasks.update_one({'_id': oid}, {'$set': doc})
    return None


def set_enabled(task_id, enabled, now=None):
    now = now or datetime.now()
    oid = _oid(task_id)
    task = db.scheduled_tasks.find_one({'_id': oid}) if oid else None
    if not task:
        return "scheduled task not found."
    update = {'enabled': bool(enabled), 'updated_at': now}
    if enabled:
        nxt = compute_next_run(task.get('schedule') or {}, now)
        if nxt is None:
            return "this schedule has no future run (a 'once' date in the past?) — edit it first."
        update['next_run'] = nxt
    db.scheduled_tasks.update_one({'_id': oid}, {'$set': update})
    return None


def delete_task(task_id):
    """Delete the task. Its past results stay in db.reports. Returns error_or_None."""
    oid = _oid(task_id)
    if not oid:
        return "invalid task_id."
    res = db.scheduled_tasks.delete_one({'_id': oid})
    return None if res.deleted_count else "scheduled task not found."


def get_task(task_id):
    oid = _oid(task_id)
    return db.scheduled_tasks.find_one({'_id': oid}) if oid else None


def task_defaults(task):
    """setdefault every field templates touch (older docs may lack some)."""
    for key in ('name', 'task_type', 'next_run', 'last_run', 'last_status', 'last_error',
                'last_report_id', 'created_at', 'updated_at', 'notes', 'running_since'):
        task.setdefault(key, None)
    task.setdefault('enabled', False)
    task.setdefault('schedule', {})
    task.setdefault('config', {})
    task['schedule_text'] = describe_schedule(task['schedule'])
    spec = TASK_TYPES.get(task['task_type'], {})
    task['type_label'] = spec.get('label', task['task_type'])
    task['needs_ai'] = bool(spec.get('needs_ai'))
    return task


def needs_ai(task):
    return bool(TASK_TYPES.get((task or {}).get('task_type'), {}).get('needs_ai'))


def result_defaults(report):
    for key in ('task_id', 'task_name', 'task_type', 'date', 'period', 'summary_md',
                'insights', 'trigger', 'status', 'error'):
        report.setdefault(key, None)
    report.setdefault('data', {})
    return report


# ============================================================
# Execution
# ============================================================

def claim_task(task, now):
    """Atomically take a due task by advancing its next_run.

    The filter pins the next_run we read, so when two replicas see the same
    due task only one update matches; the other gets None and skips it.
    next_run is recomputed from *now*, so a task that missed several periods
    (app was down) runs once and then lands on the next future occurrence.
    """
    nxt = compute_next_run(task.get('schedule') or {}, now)
    update = {'next_run': nxt, 'running_since': now}
    if nxt is None:
        update['enabled'] = False  # 'once' tasks (or a broken schedule) stop here
    return db.scheduled_tasks.find_one_and_update(
        {'_id': task['_id'], 'enabled': True, 'next_run': task.get('next_run')},
        {'$set': update},
        return_document=ReturnDocument.AFTER,
    )


def execute_task(task, trigger='schedule', now=None):
    """Run one task, store the result in db.reports, record status on the task.

    Never raises: a failure is recorded as last_status='error', and a task
    that needs AI while AI is off as last_status='skipped' (with the reason in
    last_error, and no result stored). Returns the stored report dict, or None.
    """
    now = now or datetime.now()
    tid = task['_id']
    try:
        spec = TASK_TYPES.get(task.get('task_type'))
        if not spec:
            raise ValueError(f"unknown task_type {task.get('task_type')!r}")
        out = spec['run'](task.get('config') or {}, task)
        report = {
            'type': 'scheduled',
            'task_id': tid,
            'task_name': task.get('name'),
            'task_type': task.get('task_type'),
            'trigger': trigger,
            'date': now,
            'period': now.strftime('%Y-%m-%d'),
            'data': out.get('data') or {},
            'summary_md': out.get('summary_md') or '',
            'insights': None,
            'status': 'ok',
        }
        rid = db.reports.insert_one(report).inserted_id
        report['_id'] = rid
        db.scheduled_tasks.update_one({'_id': tid}, {'$set': {
            'last_run': now, 'last_status': 'ok', 'last_error': None,
            'last_report_id': rid, 'running_since': None,
        }})
        if (out.get('narrative') and (task.get('config') or {}).get('ai_narrative', True)
                and ai_available()):
            _start_narrative(rid, task, report['summary_md'])
        return report
    except TaskSkipped as e:
        logger.info("Scheduled task %s (%s) skipped: %s", task.get('name'), tid, e)
        db.scheduled_tasks.update_one({'_id': tid}, {'$set': {
            'last_run': now, 'last_status': 'skipped', 'last_error': str(e)[:500],
            'running_since': None,
        }})
        return None
    except Exception as e:
        logger.exception("Scheduled task %s (%s) failed", task.get('name'), tid)
        db.scheduled_tasks.update_one({'_id': tid}, {'$set': {
            'last_run': now, 'last_status': 'error', 'last_error': str(e)[:500],
            'running_since': None,
        }})
        return None


def _narrative_text(prompt):
    """Two or three sentences from the configured AI provider, or None.

    Goes through ai.py with the 'report' purpose, so it follows the AI
    settings page: off means no request is made.
    """
    if not ai_available():
        return None
    try:
        return ai.generate('report', prompt)
    except ai.AIError as e:
        logger.warning("Rebalance narrative not generated: %s", e)
        return None


def _narrative_worker(report_id, task, summary_md):
    try:
        text = _narrative_text(_rebalance_narrative_prompt(task, summary_md))
    except Exception:
        logger.exception("Narrative generation raised")
        text = None
    if text:
        db.reports.update_one({'_id': report_id}, {'$set': {'insights': text.strip()}})


def _start_narrative(report_id, task, summary_md):
    if NARRATIVE_ASYNC:
        threading.Thread(target=_narrative_worker, args=(report_id, task, summary_md),
                         daemon=True).start()
    else:
        _narrative_worker(report_id, task, summary_md)


def run_due_tasks(now=None):
    """Claim and run every enabled task whose next_run has passed. Returns run count."""
    now = now or datetime.now()
    ran = 0
    due = list(db.scheduled_tasks.find(
        {'enabled': True, 'next_run': {'$ne': None, '$lte': now}}).sort('next_run', 1))
    for task in due:
        try:
            claimed = claim_task(task, now)
        except Exception:
            logger.exception("Claiming scheduled task %s failed", task.get('_id'))
            continue
        if not claimed:
            continue  # another replica got it
        execute_task(claimed, trigger='schedule', now=now)
        ran += 1
    return ran


def task_loop(poll_seconds=None, stop_event=None):
    """Poll forever (or until stop_event is set). The first pass is the startup catch-up."""
    poll = poll_seconds or POLL_SECONDS
    stop = stop_event or threading.Event()
    while not stop.is_set():
        try:
            n = run_due_tasks()
            if n:
                logger.info("Ran %d scheduled task(s)", n)
        except Exception:
            logger.exception("Scheduled task poll failed")
        stop.wait(poll)


def start_task_loop(poll_seconds=None):
    stop = threading.Event()
    t = threading.Thread(target=task_loop, args=(poll_seconds, stop), daemon=True)
    t.start()
    logger.info("Scheduled-task loop started (every %ss)", poll_seconds or POLL_SECONDS)
    return stop


# ---- manual "Run now" (non-blocking) ----

_RUNNING_LOCK = threading.Lock()
_RUNNING = {}  # task_id str -> started datetime


def is_running(task_id):
    with _RUNNING_LOCK:
        return str(task_id) in _RUNNING


def run_task_async(task_id):
    """Run a task now in a daemon thread. Does not touch next_run. Returns error_or_None."""
    task = get_task(task_id)
    if not task:
        return "scheduled task not found."
    key = str(task['_id'])
    with _RUNNING_LOCK:
        if key in _RUNNING:
            return "already running."
        _RUNNING[key] = datetime.now()

    def _worker():
        try:
            db.scheduled_tasks.update_one({'_id': task['_id']},
                                          {'$set': {'running_since': datetime.now()}})
            execute_task(task, trigger='manual')
        finally:
            with _RUNNING_LOCK:
                _RUNNING.pop(key, None)

    threading.Thread(target=_worker, daemon=True).start()
    return None


# ============================================================
# Web form parsing
# ============================================================

def fields_from_form(form):
    """Turn the /scheduled-tasks form (a MultiDict) into validate_task() fields.

    Rebalance accounts arrive as acct-<n>-account_id / -taxable / -scan_only /
    -symbol (list) / -pct (list), where <n> is listed in `acct_index`.
    """
    task_type = form.get('task_type', '')
    schedule = {
        'frequency': form.get('frequency', ''),
        'month': form.get('month', ''),
        'day': form.get('day', ''),
        'weekday': form.get('weekday', ''),
        'hour': form.get('hour', ''),
        'date': form.get('date', ''),
    }
    config = {}
    if task_type == 'rebalance_check':
        accounts = []
        for idx in form.getlist('acct_index'):
            prefix = f'acct-{idx}-'
            symbols = form.getlist(prefix + 'symbol')
            pcts = form.getlist(prefix + 'pct')
            targets = {}
            for sym, pct in zip(symbols, pcts):
                sym = sym.strip().upper()
                if not sym and not pct.strip():
                    continue
                targets[sym] = pct.strip()
            accounts.append({
                'account_id': form.get(prefix + 'account_id', ''),
                'taxable': bool(form.get(prefix + 'taxable')),
                'scan_only': bool(form.get(prefix + 'scan_only')),
                'targets': targets,
            })
        config = {
            'accounts': accounts,
            'drift_threshold_pct': form.get('drift_threshold_pct', '5'),
            'include_tax_loss_harvest': bool(form.get('include_tax_loss_harvest')),
            'ai_narrative': bool(form.get('ai_narrative')),
        }
    elif task_type == 'report':
        config = {'report_type': form.get('report_type', '')}
    elif task_type == 'reminder':
        config = {'message': form.get('message', ''), 'checklist': form.get('checklist', '')}
    return {
        'name': form.get('name', ''),
        'task_type': task_type,
        'schedule': schedule,
        'config': config,
        'notes': form.get('notes', ''),
        'enabled': bool(form.get('enabled')),
    }


# ============================================================
# MCP-facing helpers (return strings, never raise)
# ============================================================

def _fmt_dt(dt):
    return dt.strftime('%Y-%m-%d %H:%M') if isinstance(dt, datetime) else '—'


def list_tasks_text():
    tasks = list(db.scheduled_tasks.find().sort('name', 1))
    if not tasks:
        return "No scheduled tasks."
    lines = []
    for t in tasks:
        task_defaults(t)
        state = 'enabled' if t['enabled'] else 'DISABLED'
        last = f"{_fmt_dt(t['last_run'])} ({t['last_status']})" if t['last_run'] else 'never'
        lines.append(f"{t['_id']} | {t['name']} [{t['task_type']}, {state}] — {t['schedule_text']}; "
                     f"next {_fmt_dt(t['next_run'])}; last {last}")
    return "\n".join(lines)


def _parse_config_json(config_json):
    if config_json in (None, ''):
        return None, None
    try:
        cfg = json.loads(config_json)
    except (TypeError, ValueError) as e:
        return None, f"config_json is not valid JSON ({e})."
    if not isinstance(cfg, dict):
        return None, "config_json must be a JSON object."
    return cfg, None


def mcp_add_task(name, task_type, frequency, month=0, day=0, weekday=-1, hour=8,
                 config_json="{}", notes="", date=""):
    try:
        cfg, err = _parse_config_json(config_json or '{}')
        if err:
            return f"Error: {err}"
        fields = {
            'name': name, 'task_type': task_type, 'notes': notes, 'enabled': True,
            'schedule': {'frequency': frequency, 'month': month or '', 'day': day or '',
                         'weekday': weekday if weekday is not None and weekday >= 0 else '',
                         'hour': hour, 'date': date},
            'config': cfg or {},
        }
        tid, err = create_task(fields)
        if err:
            return f"Error: {err}"
        t = task_defaults(get_task(tid))
        note = '' if not t['needs_ai'] or ai_available() else f" Note: {AI_OFF_MESSAGE}"
        return (f"Created scheduled task {tid}: {t['name']} — {t['schedule_text']}; "
                f"next run {_fmt_dt(t['next_run'])}.{note}")
    except Exception as e:  # never raise into the MCP transport
        logger.exception("add_scheduled_task failed")
        return f"Error: {e}"


def mcp_update_task(task_id, name="", task_type="", frequency="", month=0, day=0,
                    weekday=-1, hour=-1, config_json="", notes="", enabled="", date=""):
    try:
        task = get_task(task_id)
        if not task:
            return "Error: scheduled task not found (see list_scheduled_tasks)."
        fields = {}
        if name:
            fields['name'] = name
        if task_type:
            fields['task_type'] = task_type
        sched = dict(task.get('schedule') or {})
        changed = False
        for key, val, unset in (('frequency', frequency, ''), ('month', month, 0),
                                ('day', day, 0), ('weekday', weekday, -1),
                                ('hour', hour, -1), ('date', date, '')):
            if val != unset and val is not None:
                sched[key] = val
                changed = True
        if changed:
            fields['schedule'] = sched
        if config_json:
            cfg, err = _parse_config_json(config_json)
            if err:
                return f"Error: {err}"
            merged = dict(task.get('config') or {})
            merged.update(cfg)  # top-level merge; nested values (accounts) are replaced whole
            fields['config'] = merged
        elif task_type and task_type != task.get('task_type'):
            return "Error: changing task_type needs a matching config_json."
        if notes:
            fields['notes'] = notes
        if enabled != '':
            fields['enabled'] = _as_bool(enabled)
        if not fields:
            return "Nothing to update."
        err = update_task(task_id, fields)
        if err:
            return f"Error: {err}"
        t = task_defaults(get_task(task_id))
        state = 'enabled' if t['enabled'] else 'disabled'
        return f"Updated {t['name']} ({state}) — {t['schedule_text']}; next run {_fmt_dt(t['next_run'])}."
    except Exception as e:
        logger.exception("update_scheduled_task failed")
        return f"Error: {e}"


def mcp_delete_task(task_id):
    err = delete_task(task_id)
    return f"Error: {err}" if err else "Deleted (past results are kept on /reports)."


def mcp_run_task(task_id):
    try:
        task = get_task(task_id)
        if not task:
            return "Error: scheduled task not found."
        if needs_ai(task) and not ai_available():
            return f"Skipped: {AI_OFF_MESSAGE}"
        if task.get('task_type') == 'report':
            err = run_task_async(task_id)
            if err:
                return f"Error: {err}"
            return ("Started in the background — advisor reports take several minutes. "
                    "Call latest_scheduled_task_result later.")
        db.scheduled_tasks.update_one({'_id': task['_id']},
                                      {'$set': {'running_since': datetime.now()}})
        report = execute_task(task, trigger='manual')
        if not report:
            t = get_task(task_id) or {}
            return f"Error: task failed — {t.get('last_error')}"
        return f"Result {report['_id']}:\n\n{report['summary_md']}"
    except Exception as e:
        logger.exception("run_scheduled_task failed")
        return f"Error: {e}"


def latest_result(task_id):
    oid = _oid(task_id)
    if not oid:
        return None
    return db.reports.find_one({'type': 'scheduled', 'task_id': oid}, sort=[('date', -1)])


def mcp_latest_result(task_id):
    task = get_task(task_id)
    if not task:
        return "Error: scheduled task not found."
    rep = latest_result(task_id)
    if not rep:
        status = f" Last status: {task.get('last_status')} — {task.get('last_error')}" \
            if task.get('last_status') in ('error', 'skipped') else ''
        return f"No results yet for {task.get('name')}.{status}"
    out = f"{task.get('name')} — run {_fmt_dt(rep.get('date'))} ({rep.get('trigger') or 'schedule'})\n\n"
    out += rep.get('summary_md') or ''
    if rep.get('insights'):
        out += f"\n\nAI narrative: {rep['insights']}"
    return out
