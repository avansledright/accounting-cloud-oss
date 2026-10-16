import copy
import csv
import io
import json
import secrets
import time
import re
import logging
from flask import Flask, request, jsonify, render_template, redirect, url_for, Response, send_from_directory, session
from flask_wtf.csrf import CSRFProtect, CSRFError
from pymongo import MongoClient
import os
import requests as http_requests
from datetime import datetime, date, timedelta
from dateutil.relativedelta import relativedelta
import calendar
import home_move
import benefits
from recurrence import calc_monthly_amount, occurrence_dates
from models import (Transaction, RecurringTransaction, Account, Asset, SavingsGoal, NetWorthSnapshot, ProjectedOverride, Transfer, Debt, RecurringTransfer,
                    Holding, Dividend, HouseholdEmployee, PayrollEntry, PaymentPlan,
                    CATEGORIES, EXPENSE_CATEGORIES, INCOME_CATEGORIES, ACCOUNT_TYPES, INVESTMENT_ACCOUNT_TYPES, CASH_ACCOUNT_TYPES, ASSET_TYPES, LIABILITY_TYPES, GOAL_CATEGORIES, GOAL_TYPES,
                    LIVE_GOAL_TYPES, SAVINGS_GOAL_TYPES, DEBT_TYPES,
                    CARD_ISSUERS, CC_CATEGORIES, CHASE_CATEGORY_MAP, MERCHANT_CATEGORY_RULES, MERCHANT_OVERRIDES, PAYMENT_KEYWORDS, CREDIT_KEYWORDS,
                    SUBSCRIPTION_GROUPS, PAYROLL_DEFAULTS, PAY_FREQUENCIES, EXTRA_DEBT_PAYMENT_DESC)
from bson import ObjectId
from bson.errors import InvalidId
from bson.json_util import dumps, loads


def safe_objectid(value):
    """Convert a string to ObjectId, returning None if invalid."""
    try:
        return ObjectId(value)
    except (InvalidId, TypeError):
        return None


def _form_float(field, default=None):
    """Parse an optional numeric form field; blank or malformed -> default."""
    raw = (request.form.get(field) or '').strip()
    if not raw:
        return default
    try:
        return float(raw.replace(',', '').replace('$', ''))
    except ValueError:
        return default


def _form_int(field, default=None):
    value = _form_float(field)
    return int(value) if value is not None else default


def _parse_form_date(field, default=None):
    """Parse a date input into a datetime (BSON has no date type)."""
    raw = (request.form.get(field) or '').strip()
    if not raw:
        return default
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return default


logger = logging.getLogger(__name__)

app = Flask(__name__)

# Secret key for CSRF protection - use environment variable or generate one
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', secrets.token_hex(32))
app.config['WTF_CSRF_TIME_LIMIT'] = 3600  # CSRF tokens expire after 1 hour
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB upload limit

# Support for base path (e.g., /accounting when deployed via reverse proxy)
app.config['APPLICATION_ROOT'] = os.environ.get('APPLICATION_ROOT', '/')

# Initialize CSRF protection
csrf = CSRFProtect(app)


@app.context_processor
def inject_account_type_sets():
    """Expose the account-type groupings to every template.

    Templates branch on these both server-side (Jinja `selectattr`) and
    client-side (show/hide the investment fields), so they should never be
    re-spelled as literals per page.
    """
    return {
        'investment_account_types': INVESTMENT_ACCOUNT_TYPES,
        'cash_account_types': CASH_ACCOUNT_TYPES,
    }


@app.template_filter('usd')
def usd_filter(value):
    """Format a number as USD with commas and 2 decimal places."""
    try:
        return f"{float(value):,.2f}"
    except (ValueError, TypeError):
        return "0.00"


@app.errorhandler(CSRFError)
def handle_csrf_error(e):
    return jsonify({'error': 'CSRF token missing or invalid'}), 400


@app.after_request
def set_security_headers(response):
    """Add security headers to all responses."""
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'SAMEORIGIN'
    response.headers['X-XSS-Protection'] = '1; mode=block'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    response.headers['Content-Security-Policy'] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "font-src 'self'; "
        "connect-src 'self'"
    )
    return response

class ReverseProxyMiddleware:
    def __init__(self, wsgi_app, application_root='/'):
        self.wsgi_app = wsgi_app
        self.application_root = application_root.rstrip('/') if application_root != '/' else ''

    def __call__(self, environ, start_response):
        # Check for proxy header first, fall back to configured APPLICATION_ROOT
        script_name = environ.get('HTTP_X_SCRIPT_NAME', '')
        if not script_name:
            script_name = self.application_root

        if script_name and script_name != '/':
            environ['SCRIPT_NAME'] = script_name
            path_info = environ.get('PATH_INFO', '')
            if path_info.startswith(script_name):
                environ['PATH_INFO'] = path_info[len(script_name):]
        return self.wsgi_app(environ, start_response)

app.wsgi_app = ReverseProxyMiddleware(app.wsgi_app, app.config['APPLICATION_ROOT'])

# MongoDB connection
mongo_uri = os.environ.get("MONGO_URI", "mongodb://localhost:27017/")
client = MongoClient(mongo_uri)
db = client[os.environ.get("MONGO_DB_NAME", "accounting_cloud")]

# ============ FINANCIAL REPORTS ============

import agent.db as agent_db
import agent.scheduler as agent_scheduler
import agent.reports as agent_reports
agent_db.init(db)
agent_scheduler.schedule_reports()


import threading as _threading
_REPORT_JOBS_LOCK = _threading.Lock()
_REPORT_JOBS = {}  # {'weekly': {'started': dt, 'error': str|None}, ...}


def _run_report_async(report_type):
    """Run report generation in a daemon thread, tracking in-flight state."""
    with _REPORT_JOBS_LOCK:
        if report_type in _REPORT_JOBS:
            return False  # already running
        _REPORT_JOBS[report_type] = {'started': datetime.now(), 'error': None}

    def _worker():
        try:
            with app.app_context():
                if report_type == 'weekly':
                    agent_reports.generate_weekly_report()
                elif report_type == 'portfolio':
                    agent_reports.generate_portfolio_report()
                else:
                    agent_reports.generate_monthly_report()
        except Exception as e:
            logger.exception("Async %s report generation failed", report_type)
            with _REPORT_JOBS_LOCK:
                if report_type in _REPORT_JOBS:
                    _REPORT_JOBS[report_type]['error'] = str(e)
                    # Keep the error visible briefly, then clear after 60s
                    def _clear():
                        _threading.Event().wait(60)
                        with _REPORT_JOBS_LOCK:
                            _REPORT_JOBS.pop(report_type, None)
                    _threading.Thread(target=_clear, daemon=True).start()
            return
        with _REPORT_JOBS_LOCK:
            _REPORT_JOBS.pop(report_type, None)

    _threading.Thread(target=_worker, daemon=True).start()
    return True


@app.route('/reports')
def reports_page():
    import markdown as _md
    weekly = db.reports.find_one({'type': 'weekly'}, sort=[('date', -1)])
    monthly = db.reports.find_one({'type': 'monthly'}, sort=[('date', -1)])
    portfolio = db.reports.find_one({'type': 'portfolio'}, sort=[('date', -1)])

    def _render(report):
        if report and report.get('insights'):
            report['insights_html'] = _md.markdown(
                report['insights'], extensions=['extra', 'sane_lists', 'nl2br']
            )
        return report

    weekly = _render(weekly)
    monthly = _render(monthly)
    portfolio = _render(portfolio)

    history = list(db.reports.find(
        {}, {'type': 1, 'period': 1, 'date': 1}
    ).sort('date', -1).limit(40))

    with _REPORT_JOBS_LOCK:
        jobs = {k: dict(v) for k, v in _REPORT_JOBS.items()}

    return render_template('reports.html', weekly=weekly, monthly=monthly,
                           portfolio=portfolio, history=history, jobs=jobs)


@app.route('/api/reports/status')
def reports_status():
    with _REPORT_JOBS_LOCK:
        jobs = {
            k: {
                'started': v['started'].isoformat(),
                'elapsed_seconds': int((datetime.now() - v['started']).total_seconds()),
                'error': v.get('error'),
            }
            for k, v in _REPORT_JOBS.items()
        }
    return jsonify({'jobs': jobs})


@app.route('/reports/<report_id>')
def report_detail(report_id):
    import markdown as _md
    rid = safe_objectid(report_id)
    if not rid:
        return redirect(url_for('reports_page'))
    report = db.reports.find_one({'_id': rid})
    if not report:
        return redirect(url_for('reports_page'))
    if report.get('insights'):
        report['insights_html'] = _md.markdown(
            report['insights'], extensions=['extra', 'sane_lists', 'nl2br']
        )
    history = list(db.reports.find(
        {}, {'type': 1, 'period': 1, 'date': 1}
    ).sort('date', -1).limit(40))
    return render_template('reports.html',
                           weekly=report if report['type']=='weekly' else None,
                           monthly=report if report['type']=='monthly' else None,
                           portfolio=report if report['type']=='portfolio' else None,
                           history=history, viewing_id=str(rid))


@app.route('/api/reports/generate', methods=['POST'])
def generate_reports():
    report_type = request.form.get('report_type', 'both')
    if report_type in ('weekly', 'both', 'all'):
        _run_report_async('weekly')
    if report_type in ('monthly', 'both', 'all'):
        _run_report_async('monthly')
    if report_type in ('portfolio', 'all'):
        _run_report_async('portfolio')
    return redirect(url_for('reports_page'))


# ============ STOCK DATA SERVICE ============

ALPHA_VANTAGE_KEY = os.environ.get('ALPHA_VANTAGE_KEY', '')
SYMBOL_PATTERN = re.compile(r'^[A-Z0-9]{1,10}$')
STOCK_CACHE = {'data': {}, 'timestamp': 0}
STOCK_CACHE_TTL = 300  # 5 minutes

STOCK_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
}

# Background refresh scheduler
import threading

def schedule_stock_refresh():
    """Schedule daily stock price refresh at 5pm."""
    def refresh_loop():
        while True:
            now = datetime.now()
            # Calculate seconds until next 5pm
            target = now.replace(hour=17, minute=0, second=0, microsecond=0)
            if now >= target:
                target += timedelta(days=1)
            wait_seconds = (target - now).total_seconds()
            threading.Event().wait(wait_seconds)

            try:
                with app.app_context():
                    symbols = list(set(h['symbol'] for h in db.holdings.find({}, {'symbol': 1})))
                    if symbols:
                        STOCK_CACHE['timestamp'] = 0  # Force refresh
                        get_live_data(symbols)
                        logger.info(f'Background stock refresh completed for {len(symbols)} symbols')
            except Exception as e:
                logger.error(f'Background stock refresh failed: {e}')

    t = threading.Thread(target=refresh_loop, daemon=True)
    t.start()


def validate_symbol(symbol):
    """Validate stock symbol format."""
    if not symbol or not SYMBOL_PATTERN.match(symbol):
        return None
    return symbol


def fetch_yahoo(symbol):
    """Fetch stock data from Yahoo Finance."""
    url = f'https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=1y&interval=1d&events=div'
    try:
        resp = http_requests.get(url, headers=STOCK_HEADERS, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        if data['chart']['result'] is None:
            return None

        result = data['chart']['result'][0]
        price = result['meta']['regularMarketPrice']

        # Compute trailing 1-year return from price history
        closes = result.get('indicators', {}).get('quote', [{}])[0].get('close', [])
        closes = [c for c in closes if c is not None]
        trailing_return = None
        if len(closes) >= 2:
            first_close = closes[0]
            last_close = closes[-1]
            if first_close > 0:
                trailing_return = ((last_close - first_close) / first_close) * 100

        divs = result.get('events', {}).get('dividends', {})
        annual_div = 0
        div_months = []

        div_history = []
        if divs:
            for ts, d in divs.items():
                annual_div += d['amount']
                div_date = datetime.fromtimestamp(int(ts))
                month = div_date.month
                if month not in div_months:
                    div_months.append(month)
                div_history.append({'date': div_date, 'amount_per_share': d['amount']})

        return {'price': price, 'dividend': annual_div, 'div_months': sorted(div_months),
                'div_history': sorted(div_history, key=lambda x: x['date']),
                'trailing_return': trailing_return,
                'valid': True, 'source': 'yahoo'}
    except Exception as e:
        logger.warning(f'{symbol} Yahoo fetch failed: {e}')
        return None


def fetch_alpha_vantage(symbol):
    """Fetch stock data from Alpha Vantage (fallback)."""
    if not ALPHA_VANTAGE_KEY:
        return None

    try:
        quote_url = f'https://www.alphavantage.co/query?function=GLOBAL_QUOTE&symbol={symbol}&apikey={ALPHA_VANTAGE_KEY}'
        resp = http_requests.get(quote_url, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        if 'Global Quote' not in data or not data['Global Quote']:
            return None

        price = float(data['Global Quote'].get('05. price', 0))

        div_url = f'https://www.alphavantage.co/query?function=TIME_SERIES_MONTHLY_ADJUSTED&symbol={symbol}&apikey={ALPHA_VANTAGE_KEY}'
        div_resp = http_requests.get(div_url, timeout=10)
        div_resp.raise_for_status()
        div_data = div_resp.json()

        annual_div = 0
        div_months = []

        if 'Monthly Adjusted Time Series' in div_data:
            now = datetime.now()
            one_year_ago = now.replace(year=now.year - 1)

            for date_str, values in div_data['Monthly Adjusted Time Series'].items():
                d = datetime.strptime(date_str, '%Y-%m-%d')
                if d >= one_year_ago:
                    div_amount = float(values.get('7. dividend amount', 0))
                    if div_amount > 0:
                        annual_div += div_amount
                        if d.month not in div_months:
                            div_months.append(d.month)

        return {'price': price, 'dividend': annual_div, 'div_months': sorted(div_months), 'valid': True, 'source': 'alphavantage'}
    except Exception as e:
        logger.warning(f'{symbol} Alpha Vantage fetch failed: {e}')
        return None


def fetch_symbol_data(symbol):
    """Fetch symbol data with Yahoo primary, Alpha Vantage fallback."""
    result = fetch_yahoo(symbol)
    if result:
        return result
    result = fetch_alpha_vantage(symbol)
    if result:
        return result
    return {'price': 0, 'dividend': 0, 'div_months': [], 'trailing_return': None, 'valid': False, 'source': None}


def get_live_data(symbols):
    """Get live stock data with caching."""
    if not symbols:
        return {}

    now = time.time()
    if now - STOCK_CACHE['timestamp'] < STOCK_CACHE_TTL and all(s in STOCK_CACHE['data'] for s in symbols):
        return {s: STOCK_CACHE['data'][s] for s in symbols}

    data = {}
    for symbol in symbols:
        result = fetch_symbol_data(symbol)
        data[symbol] = result

    STOCK_CACHE['data'].update(data)
    STOCK_CACHE['timestamp'] = now
    return data


def derive_account_return_rate(account_id_str):
    """Compute value-weighted trailing 1-year return for a self-managed account from its holdings."""
    holdings = list(db.holdings.find({'account_id': safe_objectid(account_id_str)}))
    if not holdings:
        return None

    symbols = list(set(h['symbol'] for h in holdings))
    live_data = get_live_data(symbols)

    total_value = 0
    weighted_return = 0
    has_return_data = False

    for h in holdings:
        sym_data = live_data.get(h['symbol'], {})
        price = sym_data.get('price', 0)
        value = price * h['quantity']
        total_value += value

    if total_value <= 0:
        return None

    for h in holdings:
        sym_data = live_data.get(h['symbol'], {})
        price = sym_data.get('price', 0)
        value = price * h['quantity']
        tr = sym_data.get('trailing_return')
        if tr is not None and value > 0:
            weighted_return += tr * (value / total_value)
            has_return_data = True

    if has_return_data:
        derived_rate = round(weighted_return, 2)
        db.accounts.update_one(
            {'_id': safe_objectid(account_id_str)},
            {'$set': {'estimated_return_rate': derived_rate}}
        )
        return derived_rate
    return None


def calculate_investment_projection(account, months=120):
    """
    Project investment account value over time.

    Uses the account's current balance, estimated return rate, and
    recurring deposits assigned to this account to project growth.

    Args:
        account: MongoDB account document (dict)
        months: Number of months to project forward (default 120 = 10 years)

    Returns:
        List of dicts: [{month_label, projected_value, total_deposits, total_growth}]
    """
    acc_id = str(account['_id'])
    balance = account.get('balance', 0)
    annual_rate = account.get('estimated_return_rate', 0) or 0
    monthly_rate = annual_rate / 12 / 100

    # Sum monthly deposits from recurring transactions assigned to this account
    monthly_deposit = 0
    for rt in db.recurring_transactions.find({'transaction_type': 'income', **ACTIVE_RECURRING}):
        rt_account_id = str(rt['account_id']) if rt.get('account_id') else None
        if rt_account_id == acc_id:
            monthly_deposit += calc_monthly_amount(
                rt['amount'], rt['frequency'], rt.get('day_of_month')
            )

    # Also count recurring transfers INTO this account
    for rt in db.recurring_transfers.find():
        to_id = str(rt.get('to_account_id', ''))
        if to_id == acc_id:
            monthly_deposit += calc_monthly_amount(
                rt['amount'], rt['frequency'], rt.get('day_of_month')
            )

    today = date.today()
    projections = []
    total_deposits = 0
    starting_balance = balance

    for i in range(months + 1):
        month_date = today + relativedelta(months=i)
        month_label = month_date.strftime("%b %Y")

        if i > 0:
            growth = balance * monthly_rate
            balance += growth + monthly_deposit
            total_deposits += monthly_deposit

        projections.append({
            "month_label": month_label,
            "month_index": i,
            "projected_value": round(balance, 2),
            "total_deposits": round(total_deposits, 2),
            "total_growth": round(balance - starting_balance - total_deposits, 2),
        })

    return projections


# Escrow simulation: which months the escrow account pays property tax (the
# annual bill is split evenly across them) and homeowner's insurance. Counties
# differ, so both are configurable. Defaults: semi-annual tax in June and
# December, insurance renewing in January.
def _parse_months(raw, default):
    try:
        months = tuple(int(m) for m in str(raw).split(',') if m.strip())
        return tuple(m for m in months if 1 <= m <= 12) or default
    except ValueError:
        return default


ESCROW_PROPERTY_TAX_MONTHS = _parse_months(os.environ.get('ESCROW_PROPERTY_TAX_MONTHS', '6,12'), (6, 12))
ESCROW_INSURANCE_MONTH = (_parse_months(os.environ.get('ESCROW_INSURANCE_MONTH', '1'), (1,)))[0]

_MONTH_ABBR = ['', 'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']


@app.context_processor
def inject_escrow_schedule():
    return {
        'escrow_tax_months_label': ' & '.join(_MONTH_ABBR[m] for m in ESCROW_PROPERTY_TAX_MONTHS),
        'escrow_insurance_month_label': _MONTH_ABBR[ESCROW_INSURANCE_MONTH],
    }


# Ensure unique index on monthly budget target overrides
db.monthly_budget_targets.create_index([('month', 1), ('category', 1)], unique=True)

def get_total_account_balance():
    """Calculate the sum of all account balances.
    Falls back to STARTING_BALANCE env var if no accounts exist."""
    accounts = list(db.accounts.find())
    if not accounts:
        # No accounts set up yet, use environment variable
        return float(os.environ.get("STARTING_BALANCE", 0.0))

    total = 0
    for account in accounts:
        total += account.get('balance', 0)
    return total


def get_primary_account_balance():
    """Get the balance of the primary account (used for ledger calculations).
    Falls back to STARTING_BALANCE env var if no primary account exists."""
    primary = db.accounts.find_one({'is_primary': True})
    if primary:
        return primary.get('balance', 0)
    # Fall back to STARTING_BALANCE if no primary account
    return float(os.environ.get("STARTING_BALANCE", 0.0))


def get_accounts_summary():
    """Get all accounts with their balances for display."""
    accounts = list(db.accounts.find())
    summary = []
    for a in accounts:
        summary.append({
            '_id': str(a['_id']),
            'name': a.get('name', ''),
            'account_type': a.get('account_type', ''),
            'balance': a.get('balance', 0),
            'is_primary': a.get('is_primary', False)
        })
    return summary


@app.route('/health')
def health():
    """Health check endpoint for Kubernetes liveness and readiness probes."""
    try:
        # Test MongoDB connection
        db.command('ping')
        return jsonify({
            'status': 'healthy',
            'timestamp': datetime.utcnow().isoformat(),
            'database': 'connected'
        }), 200
    except Exception as e:
        logger.error("Health check failed: %s", e)
        return jsonify({
            'status': 'unhealthy',
            'error': 'Database connection failed',
            'database': 'disconnected'
        }), 503


def calculate_yearly_outlook(start_balance=0.0, account_filter=None, max_months_back=None, sim=None):
    """
    Calculate monthly outlook.
    account_filter: None = all accounts, 'primary' = primary only, ObjectId string = specific account
    max_months_back: None = show from January, 1 = show previous month + forward, etc.
    sim: optional non-destructive "what-if" overlay applied in-memory only (never written to the
         DB). A dict with any of:
           'overrides'  -> {recurring_id (str): new_base_amount (positive magnitude)}
           'disabled'   -> set/list of recurring_ids to exclude entirely
           'additions'  -> list of recurring-transaction-like dicts to inject as hypothetical items
           'start_date' -> date: anchor the 12-month window on this date instead of today
           'budget_only'-> bool: ignore actual transactions, skips and per-date overrides so the
                           projection is driven purely by the recurring budget items
    """
    today = date.today()
    sim = sim or {}
    anchor = sim.get('start_date') or today
    if isinstance(anchor, datetime):
        anchor = anchor.date()
    budget_only = bool(sim.get('budget_only'))
    monthly_balances = []
    running_balance = start_balance

    # Build account lookup for names
    accounts_lookup = {}
    for acc in db.accounts.find():
        accounts_lookup[str(acc['_id'])] = acc.get('name', 'Unknown')

    # Loan-name lookups so linked payments can show a badge in the ledger
    loan_names = {str(d['_id']): d.get('name') for d in db.debts.find()}
    loan_names.update({str(a['_id']): a.get('name') for a in db.assets.find()})

    # Get primary account id
    primary_account = db.accounts.find_one({'is_primary': True})
    primary_account_id = str(primary_account['_id']) if primary_account else None

    # Get all transactions, recurring transactions, recurring transfers, skipped transactions, and overrides
    transactions = [] if budget_only else list(db.transactions.find())
    recurring_transactions = list(db.recurring_transactions.find(ACTIVE_RECURRING))
    recurring_transfers = list(db.recurring_transfers.find())
    skipped = set() if budget_only else {(s['description'], s['date']) for s in db.skipped_transactions.find()}

    # Loan payoff simulation: projected loan payments from the current month on
    # are run against each loan's actual balance (see _apply_projected_loan_payment).
    loan_state = _loan_projection_state()
    loan_sim_floor = date(today.year, today.month, 1)

    # Simulation overlay (what-if scenario) — applied in-memory only, never persisted.
    sim_overrides = sim.get('overrides', {}) or {}
    sim_disabled = set(sim.get('disabled', []) or [])
    sim_additions = sim.get('additions', []) or []
    if sim_additions:
        recurring_transactions = recurring_transactions + sim_additions

    # Build override lookup: (recurring_id, date) -> amount
    overrides = {}
    if not budget_only:
        for override in db.projected_overrides.find():
            # Stored as a 'YYYY-MM-DD' string; older MCP writes used a datetime.
            o_date = override['date']
            if isinstance(o_date, (datetime, date)):
                o_date = o_date.strftime('%Y-%m-%d')
            key = (str(override['recurring_id']), o_date)
            overrides[key] = override['amount']

    # Calculate month range (anchored on `anchor`, normally today)
    current_month = anchor.month  # 1-12
    if max_months_back is not None:
        months_back = -min(max_months_back, current_month - 1)
    else:
        months_back = -(current_month - 1)  # Back to January
    # Rolling 12 months forward from current month (crosses calendar year boundary)
    months_forward = 12

    # Calculate the start date of the first visible month
    first_visible = anchor + relativedelta(months=months_back)
    first_month_start = date(first_visible.year, first_visible.month, 1)

    # Add all transactions that occurred BEFORE the first visible month to the running balance
    for t in transactions:
        transaction_date = t['date'].date() if isinstance(t['date'], datetime) else datetime.fromisoformat(t['date']).date()
        if transaction_date < first_month_start:
            # Get account info for transaction
            t_account_id = str(t.get('account_id')) if t.get('account_id') else primary_account_id

            # Apply account filter
            if account_filter and account_filter != 'all':
                if account_filter != t_account_id:
                    continue

            # Add transaction to running balance
            if t['transaction_type'] == 'income':
                running_balance += t['amount']
            else:
                running_balance -= t['amount']

    # Actuals that matched no projection in their own month: description -> dates
    carried_actuals = {}

    for i in range(months_back, months_forward + 1):
        month_date = anchor + relativedelta(months=i)
        month_start = date(month_date.year, month_date.month, 1)
        month_end = date(month_date.year, month_date.month, calendar.monthrange(month_date.year, month_date.month)[1])

        month_transactions = []

        # One-time transactions for the month
        for t in transactions:
            transaction_date = t['date'].date() if isinstance(t['date'], datetime) else datetime.fromisoformat(t['date']).date()
            if month_start <= transaction_date <= month_end:
                # Get account info for transaction
                t_account_id = str(t.get('account_id')) if t.get('account_id') else primary_account_id
                t_account_name = accounts_lookup.get(t_account_id, 'Primary')

                # Apply account filter
                if account_filter and account_filter != 'all':
                    if account_filter != t_account_id:
                        continue

                amount = t['amount']
                if t['transaction_type'] == 'income':
                    month_transactions.append({
                        "_id": str(t['_id']),
                        "date": transaction_date.isoformat(),
                        "description": t['description'],
                        "amount": amount,
                        "type": "actual",
                        "account_id": t_account_id,
                        "account_name": t_account_name
                    })
                else:
                    if t.get('debt_id'):
                        loan_ref, loan_label = 'debt:' + str(t['debt_id']), loan_names.get(str(t['debt_id']))
                    elif t.get('asset_liability_id'):
                        loan_ref, loan_label = 'asset:' + str(t['asset_liability_id']), loan_names.get(str(t['asset_liability_id']))
                    else:
                        loan_ref, loan_label = '', None
                    month_transactions.append({
                        "_id": str(t['_id']),
                        "date": transaction_date.isoformat(),
                        "description": t['description'],
                        "amount": -amount,
                        "type": "actual",
                        "account_id": t_account_id,
                        "account_name": t_account_name,
                        "loan_label": loan_label,
                        "loan_ref": loan_ref
                    })

        # Recurring transactions for the month
        for rt in recurring_transactions:
            sim_id = str(rt['_id'])
            if sim_id in sim_disabled:
                continue
            rt_start_date = rt['start_date'].date() if isinstance(rt['start_date'], datetime) else datetime.fromisoformat(rt['start_date']).date()

            if rt_start_date > month_end or (rt.get('end_date') and (rt.get('end_date').date() if isinstance(rt.get('end_date'), datetime) else datetime.fromisoformat(rt.get('end_date')).date()) < month_start):
                continue

            # Dates this recurring item falls on within the month (on/after start_date)
            projected_dates = occurrence_dates(rt, month_start, month_end)

            if not projected_dates:
                continue

            amount = sim_overrides.get(sim_id, rt['amount'])
            rt_account_id = str(rt['account_id']) if rt.get('account_id') else primary_account_id
            proj_account_name = accounts_lookup.get(rt_account_id, 'Primary') if rt_account_id else 'Primary'

            if account_filter and account_filter != 'all' and account_filter != rt_account_id:
                continue

            recurring_id = str(rt['_id'])
            for projected_date in projected_dates:
                date_str = projected_date.isoformat()

                override_key = (recurring_id, date_str)
                effective_amount = overrides.get(override_key, amount)
                has_override = override_key in overrides

                if rt['transaction_type'] == 'income':
                    month_transactions.append({
                        "_id": None,
                        "recurring_id": recurring_id,
                        "date": date_str,
                        "description": rt['description'],
                        "amount": effective_amount,
                        "base_amount": amount,
                        "has_override": has_override,
                        "type": "projected",
                        "is_transfer": False,
                        "account_id": rt_account_id,
                        "account_name": proj_account_name
                    })
                else:
                    month_transactions.append({
                        "_id": None,
                        "recurring_id": recurring_id,
                        "date": date_str,
                        "description": rt['description'],
                        "amount": -effective_amount,
                        "base_amount": -amount,
                        "has_override": has_override,
                        "type": "projected",
                        "is_transfer": False,
                        "account_id": rt_account_id,
                        "account_name": proj_account_name
                    })

        # Recurring transfers for the month
        for rt in recurring_transfers:
            sim_id = str(rt['_id'])
            if sim_id in sim_disabled:
                continue
            rt_start_date = rt['start_date'].date() if isinstance(rt['start_date'], datetime) else datetime.fromisoformat(rt['start_date']).date()

            if rt_start_date > month_end or (rt.get('end_date') and (rt.get('end_date').date() if isinstance(rt.get('end_date'), datetime) else datetime.fromisoformat(rt.get('end_date')).date()) < month_start):
                continue

            # Dates this recurring transfer falls on within the month (on/after start_date)
            projected_dates = occurrence_dates(rt, month_start, month_end)

            if not projected_dates:
                continue

            amount = sim_overrides.get(sim_id, rt['amount'])
            from_id = str(rt.get('from_account_id', ''))
            to_id = str(rt.get('to_account_id', ''))
            from_name = accounts_lookup.get(from_id, 'Unknown')
            to_name = accounts_lookup.get(to_id, 'Unknown')
            description = rt.get('description', 'Recurring Transfer')
            recurring_id = str(rt['_id'])

            for projected_date in projected_dates:
                date_str = projected_date.isoformat()

                override_key = (recurring_id, date_str)
                effective_amount = overrides.get(override_key, amount)
                has_override = override_key in overrides

                show_from = (not account_filter or account_filter == from_id)
                show_to = (not account_filter or account_filter == to_id)

                if show_from:
                    month_transactions.append({
                        "_id": None,
                        "recurring_id": recurring_id,
                        "date": date_str,
                        "description": f"Transfer to: {description}",
                        "amount": -effective_amount,
                        "base_amount": -amount,
                        "has_override": has_override,
                        "type": "projected",
                        "is_transfer": True,
                        "transfer_from_id": from_id,
                        "transfer_to_id": to_id,
                        "account_id": from_id,
                        "account_name": from_name
                    })
                if show_to:
                    month_transactions.append({
                        "_id": None,
                        "recurring_id": recurring_id,
                        "date": date_str,
                        "description": f"Transfer from: {description}",
                        "amount": effective_amount,
                        "base_amount": amount,
                        "has_override": has_override,
                        "type": "projected",
                        "is_transfer": True,
                        "transfer_from_id": from_id,
                        "transfer_to_id": to_id,
                        "account_id": to_id,
                        "account_name": to_name
                    })

        # Sort transactions for the month by date
        month_transactions.sort(key=lambda x: x['date'])

        # Count skipped transactions per description for this month
        month_prefix = f"{month_date.year}-{month_date.month:02d}"
        skipped_counts = {}
        for desc, skip_date in skipped:
            if skip_date.startswith(month_prefix):
                skipped_counts[desc] = skipped_counts.get(desc, 0) + 1

        # Match actual transactions to the projected occurrence each one
        # REALIZES, then suppress those projections. An actual cancels the
        # nearest same-description projection within a few days (a payment made
        # a bit early or late), or — failing that — any still-unmatched
        # projection that is already in the past. A past actual never cancels an
        # unrelated occurrence that is still upcoming: e.g. a biweekly draft date
        # that shifted within the month leaves an already-paid date AND a new
        # upcoming date in the same month, and both must remain visible.
        #
        # An actual that realized nothing in its own month is carried into the
        # next one, where it may still cancel a projection within tolerance:
        # a bill due on the 1st but paid on the 30th belongs to next month's
        # occurrence even though it posted in this one.
        DEDUP_TOLERANCE_DAYS = 7
        projected_by_desc = {}
        for idx, t in enumerate(month_transactions):
            if t['type'] == 'projected':
                projected_by_desc.setdefault(t['description'], []).append(idx)

        def _proj_date(idx):
            return datetime.fromisoformat(month_transactions[idx]['date'][:10]).date()

        cancelled = set()
        actuals_by_desc = {}
        for t in month_transactions:
            if t['type'] == 'actual':
                actuals_by_desc.setdefault(t['description'], []).append(
                    datetime.fromisoformat(t['date'][:10]).date())
        next_carried = {}
        for desc in set(actuals_by_desc) | set(carried_actuals):
            adates = actuals_by_desc.get(desc, [])
            early = carried_actuals.get(desc, [])
            idxs = projected_by_desc.get(desc, [])
            leftover = []
            # Pass 1: nearest projection within tolerance (paid early/late).
            # Last month's unmatched actuals go first, and only take part here.
            for a in sorted(early) + sorted(adates):
                best, best_dist = None, None
                for i in idxs:
                    if i in cancelled:
                        continue
                    dist = abs((_proj_date(i) - a).days)
                    if best_dist is None or dist < best_dist:
                        best, best_dist = i, dist
                if best is not None and best_dist <= DEDUP_TOLERANCE_DAYS:
                    cancelled.add(best)
                elif a >= month_start:
                    leftover.append(a)
            # Pass 2: remaining actuals cancel any unmatched PAST projection.
            for a in leftover:
                for i in idxs:
                    if i not in cancelled and _proj_date(i) <= today:
                        cancelled.add(i)
                        break
                else:
                    next_carried.setdefault(desc, []).append(a)
        carried_actuals = next_carried

        filtered_month_transactions = []
        min_balance = running_balance
        for idx, t in enumerate(month_transactions):
            if t['type'] == 'projected':
                if idx in cancelled:
                    continue
                desc = t['description']
                if skipped_counts.get(desc, 0) > 0:
                    skipped_counts[desc] -= 1
                    continue
                if month_start >= loan_sim_floor and not t.get('is_transfer') and t['amount'] < 0:
                    loan = loan_state.get(_normalize_desc(desc))
                    if loan is not None and not _apply_projected_loan_payment(loan, t):
                        continue  # loan already paid off — nothing left to project

            # Calculate running balance for each transaction
            running_balance += t['amount']
            t['running_balance'] = running_balance
            filtered_month_transactions.append(t)
            if running_balance < min_balance:
                min_balance = running_balance

        monthly_balances.append({
            "month": month_start.strftime("%B %Y"),
            "balance": running_balance,
            "min_balance": min_balance,
            "transactions": filtered_month_transactions
        })

    return monthly_balances

# ============ CUSTOMIZABLE DASHBOARD ============
# The dashboard grid cards can be reordered, resized (1–4 column slots), and
# shown/hidden. The chosen layout is persisted in a single `dashboard_prefs`
# doc as an ordered list of {key, span, visible}. New cards added to the
# registry appear automatically (appended, visible, default span).

DASHBOARD_CARD_DEFS = [
    # (key, title, default span 1–4)
    ('upcoming_bills', 'Upcoming Bills', 2),
    ('month_to_date', 'Month to Date', 1),
    ('payment_plans', 'Payment Plans', 1),
    ('outlook', 'Next 3 Months', 2),
    ('goals', 'Savings Goals', 1),
    ('advisor_brief', 'Advisor Brief', 2),
    ('benefit_alerts', 'Tax Year & Perks', 2),
]
_DASHBOARD_CARD_KEYS = {k for k, _t, _s in DASHBOARD_CARD_DEFS}


def _dashboard_layout():
    """Merge saved dashboard prefs with the card registry into an ordered list.

    Each entry: {key, title, span, visible}. Unknown/removed keys are dropped;
    cards not yet in the saved prefs are appended (visible, default span).
    """
    known = {k: {'title': t, 'span': s} for k, t, s in DASHBOARD_CARD_DEFS}
    prefs = db.dashboard_prefs.find_one({}) or {}
    layout, seen = [], set()
    for item in prefs.get('cards', []):
        k = item.get('key')
        if k in known and k not in seen:
            base = known[k]
            span = item.get('span', base['span'])
            try:
                span = max(1, min(4, int(span)))
            except (TypeError, ValueError):
                span = base['span']
            layout.append({'key': k, 'title': base['title'], 'span': span,
                           'visible': bool(item.get('visible', True))})
            seen.add(k)
    for k, t, s in DASHBOARD_CARD_DEFS:
        if k not in seen:
            layout.append({'key': k, 'title': t, 'span': s, 'visible': True})
    return layout


@app.route('/dashboard/layout', methods=['POST'])
def save_dashboard_layout():
    """Persist the dashboard card layout (order, span, visibility), or reset it."""
    data = request.get_json(silent=True) or {}
    if data.get('reset'):
        db.dashboard_prefs.delete_many({})
        return jsonify({'ok': True, 'reset': True})

    cards, seen = [], set()
    for item in data.get('cards', []):
        k = item.get('key')
        if k in _DASHBOARD_CARD_KEYS and k not in seen:
            try:
                span = max(1, min(4, int(item.get('span', 2))))
            except (TypeError, ValueError):
                span = 2
            cards.append({'key': k, 'span': span, 'visible': bool(item.get('visible', True))})
            seen.add(k)
    if not cards:
        return jsonify({'ok': False, 'error': 'no valid cards'}), 400
    db.dashboard_prefs.update_one({}, {'$set': {'cards': cards, 'updated': datetime.now()}}, upsert=True)
    return jsonify({'ok': True})


# ============ FIRST-RUN SETUP ============
#
# A fresh install (no accounts, setup never finished or skipped) sends the
# dashboard to a short wizard. State lives in the `app_settings` singleton:
#   setup_completed / setup_skipped  -- either one stops the redirect
#   household_name                   -- shown on the dashboard
#   checklist_dismissed              -- hides the getting-started card

SETUP_STEPS = [
    ('welcome', 'Welcome'),
    ('account', 'Primary account'),
    ('income', 'Income'),
    ('bills', 'Bills'),
    ('extras', 'Extras'),
    ('done', 'Done'),
]
_SETUP_STEP_KEYS = [k for k, _ in SETUP_STEPS]

# Frequencies offered by the wizard. 'twice_monthly' is stored as a monthly
# item with two days (e.g. "1,15"), the same way the Budget page does it.
SETUP_INCOME_FREQUENCIES = [
    ('biweekly', 'Every 2 weeks'),
    ('twice_monthly', 'Twice a month'),
    ('monthly', 'Monthly'),
    ('weekly', 'Weekly'),
]
SETUP_BILL_FREQUENCIES = [
    ('monthly', 'Monthly'),
    ('yearly', 'Yearly'),
    ('bi-monthly', 'Every 2 months'),
    ('weekly', 'Weekly'),
    ('biweekly', 'Every 2 weeks'),
]
SETUP_BILL_SUGGESTIONS = [
    ('Rent / Mortgage', 'Housing'),
    ('Electric', 'Utilities'),
    ('Internet', 'Utilities'),
    ('Phone', 'Phone'),
    ('Car insurance', 'Insurance'),
]


def _app_settings():
    return db.app_settings.find_one({'_id': 'app'}) or {}


def _save_app_settings(**fields):
    fields['updated_at'] = datetime.now()
    db.app_settings.update_one({'_id': 'app'}, {'$set': fields}, upsert=True)


def needs_first_run_setup():
    """True for a fresh install: setup never finished/skipped and no accounts."""
    s = _app_settings()
    if s.get('setup_completed') or s.get('setup_skipped'):
        return False
    return db.accounts.count_documents({}) == 0


def _today_dt():
    """Today at midnight as a datetime (BSON has no date type)."""
    return datetime.combine(date.today(), datetime.min.time())


def _next_on_or_after(anchor, step_days, floor):
    """First date in anchor + k*step_days (k may be negative) that is >= floor."""
    if anchor >= floor:
        return anchor
    steps = -(-(floor - anchor).days // step_days)  # ceil
    return anchor + timedelta(days=steps * step_days)


def _setup_schedule(frequency, when, second_day=None):
    """Turn a wizard row (frequency + next date) into budget-item fields.

    The balance entered in the wizard is "as of today", so nothing may project
    before today: monthly items start today (their day(s) of month carry the
    schedule) and weekly/bi-weekly items are re-anchored on their next
    occurrence on or after today, keeping the cadence. Yearly and bi-monthly
    items keep the given date as the anchor month.
    Returns (frequency, day_of_month, start_date).
    """
    today = _today_dt()
    if frequency in ('weekly', 'biweekly'):
        step = 7 if frequency == 'weekly' else 14
        start = _next_on_or_after(when, step, today)
        # The day is informational (the start date anchors the cadence) but
        # the Budget edit form requires one.
        return frequency, str(start.day), start
    if frequency == 'twice_monthly':
        days = sorted({when.day, int(second_day)})
        return 'monthly', ','.join(str(d) for d in days), today
    if frequency in ('yearly', 'bi-monthly'):
        start = when
        while start < today:
            start += relativedelta(years=1) if frequency == 'yearly' else relativedelta(months=2)
        return frequency, str(when.day), start
    return 'monthly', str(when.day), today


def _setup_rows(prefix, fields):
    """Collect parallel `prefix-field[]` form lists into row dicts."""
    cols = {f: request.form.getlist(f'{prefix}_{f}') for f in fields}
    n = max((len(v) for v in cols.values()), default=0)
    rows = []
    for i in range(n):
        rows.append({f: (cols[f][i].strip() if i < len(cols[f]) else '') for f in fields})
    return rows


def _parse_amount(raw):
    try:
        value = float((raw or '').replace(',', '').replace('$', ''))
    except ValueError:
        return None
    return value if value > 0 else None


def _parse_iso_date(raw):
    try:
        return datetime.fromisoformat(raw) if raw else None
    except ValueError:
        return None


def _setup_primary():
    return db.accounts.find_one({'is_primary': True})


def _setup_summary():
    primary = _setup_primary()
    income = list(db.recurring_transactions.find({'transaction_type': 'income', **ACTIVE_RECURRING}))
    bills = list(db.recurring_transactions.find({'transaction_type': 'expense', **ACTIVE_RECURRING}))
    monthly_income = sum(calc_monthly_amount(r.get('amount', 0), r.get('frequency'), r.get('day_of_month')) for r in income)
    monthly_bills = sum(calc_monthly_amount(r.get('amount', 0), r.get('frequency'), r.get('day_of_month')) for r in bills)
    others = list(db.accounts.find({'is_primary': {'$ne': True}}))
    debts = list(db.debts.find())
    return {
        'primary': primary,
        'income': income,
        'bills': bills,
        'monthly_income': monthly_income,
        'monthly_bills': monthly_bills,
        'other_accounts': others,
        'debts': debts,
    }


def getting_started_checklist():
    """Getting-started items, each ticked off from real data."""
    has_income = db.recurring_transactions.count_documents({'transaction_type': 'income'}) > 0
    has_bills = db.recurring_transactions.count_documents({'transaction_type': 'expense'}) > 0
    items = [
        ('Add your primary account', db.accounts.count_documents({'is_primary': True}) > 0,
         url_for('accounts_page') + '#add-account', 'The checking account your bills come out of; the ledger projects its balance.'),
        ('Add recurring income', has_income, url_for('budget_page'),
         'Paychecks and other regular income, so projections know what comes in.'),
        ('Add recurring bills', has_bills, url_for('budget_page'),
         'Rent or mortgage, utilities, insurance: anything on a schedule.'),
        ('Set budget targets', (db.budget_targets.count_documents({}) + db.monthly_budget_targets.count_documents({})) > 0,
         url_for('budget_page') + '#budget-targets', 'Monthly spending targets per category (groceries, dining, ...).'),
        ('Record or import a transaction', db.transactions.count_documents({}) > 0,
         url_for('ledger_page'), 'Log what actually happened; it replaces the matching projection.'),
        ('Import a credit card statement', db.cc_transactions.count_documents({}) > 0,
         url_for('credit_cards_page'), 'Upload a CSV from your card issuer to see where the money goes.'),
        ('Take a net worth snapshot', db.networth_snapshots.count_documents({}) > 0,
         url_for('networth_page'), 'Snapshots build the net worth history chart over time.'),
    ]
    return [{'label': l, 'done': d, 'url': u, 'hint': h} for l, d, u, h in items]


@app.route('/setup')
def setup_start():
    return redirect(url_for('setup_step', step='welcome'))


@app.route('/setup/<step>', methods=['GET', 'POST'])
def setup_step(step):
    if step not in _SETUP_STEP_KEYS:
        return redirect(url_for('setup_step', step='welcome'))
    errors = []
    settings = _app_settings()

    if request.method == 'POST':
        if step == 'welcome':
            _save_app_settings(household_name=(request.form.get('household_name') or '').strip()[:80] or None,
                               setup_started_at=settings.get('setup_started_at') or datetime.now())
            return redirect(url_for('setup_step', step='account'))

        if step == 'account':
            name = (request.form.get('name') or '').strip()
            institution = (request.form.get('institution') or '').strip()
            account_type = request.form.get('account_type') or 'checking'
            balance = _form_float('balance', 0.0)
            if account_type not in CASH_ACCOUNT_TYPES:
                errors.append('Choose a cash account type.')
            if not name:
                errors.append('Give the account a name.')
            if not errors:
                primary = _setup_primary()
                if primary:
                    # Re-running setup (or stepping back) edits the primary
                    # account instead of creating a duplicate.
                    db.accounts.update_one({'_id': primary['_id']}, {'$set': {
                        'name': name, 'institution': institution, 'account_type': account_type,
                        'balance': balance, 'balance_as_of_date': _today_dt()}})
                else:
                    _create_account(name=name, account_type=account_type, institution=institution,
                                    balance=balance, is_primary=True, balance_as_of_date=_today_dt())
                return redirect(url_for('setup_step', step='income'))

        if step in ('income', 'bills'):
            primary = _setup_primary()
            if not primary:
                return redirect(url_for('setup_step', step='account'))
            is_income = step == 'income'
            fields = ['description', 'amount', 'frequency', 'date', 'second_day', 'category', 'auto_pay']
            allowed = dict(SETUP_INCOME_FREQUENCIES if is_income else SETUP_BILL_FREQUENCIES)
            categories = INCOME_CATEGORIES if is_income else CATEGORIES
            to_create = []
            for i, row in enumerate(_setup_rows(step, fields), start=1):
                if not row['description'] and not row['amount']:
                    continue  # blank row
                amount = _parse_amount(row['amount'])
                when = _parse_iso_date(row['date'])
                label = row['description'] or f'Row {i}'
                if not row['description']:
                    errors.append(f'Row {i}: add a description.')
                if amount is None:
                    errors.append(f'{label}: enter an amount greater than zero.')
                if when is None:
                    errors.append(f'{label}: pick the next {"payday" if is_income else "due date"}.')
                if row['frequency'] not in allowed:
                    errors.append(f'{label}: choose how often it repeats.')
                second = None
                if row['second_day']:
                    try:
                        second = int(row['second_day'])
                        if not 1 <= second <= 31:
                            raise ValueError
                    except ValueError:
                        errors.append(f'{label}: the second payday must be a day of the month (1-31).')
                if row['frequency'] == 'twice_monthly' and second is None:
                    errors.append(f'{label}: twice-a-month pay needs the second payday (day of month).')
                if errors:
                    continue
                frequency, day_of_month, start = _setup_schedule(row['frequency'], when, second)
                to_create.append(dict(
                    description=row['description'], amount=amount, frequency=frequency,
                    start_date=start, transaction_type='income' if is_income else 'expense',
                    day_of_month=day_of_month,
                    category=row['category'] if row['category'] in categories else (categories[0] if is_income else 'Other'),
                    auto_pay=(row['auto_pay'] == 'on'), account_id=str(primary['_id']),
                ))
            if not errors:
                for fields_ in to_create:
                    _create_recurring_item(**fields_)
                nxt = 'bills' if is_income else 'extras'
                if request.form.get('action') == 'add_more':
                    nxt = step
                return redirect(url_for('setup_step', step=nxt))

        if step == 'extras':
            kind = request.form.get('kind')
            if kind == 'account':
                name = (request.form.get('name') or '').strip()
                account_type = request.form.get('account_type')
                if not name or account_type not in dict(ACCOUNT_TYPES):
                    errors.append('An extra account needs a name and a type.')
                else:
                    is_inv = account_type in INVESTMENT_ACCOUNT_TYPES
                    _create_account(name=name, account_type=account_type,
                                    institution=(request.form.get('institution') or '').strip(),
                                    balance=_form_float('balance', 0.0), is_primary=False,
                                    balance_as_of_date=_today_dt(),
                                    estimated_return_rate=_form_float('estimated_return_rate') if is_inv else None)
                    return redirect(url_for('setup_step', step='extras'))
            elif kind == 'debt':
                name = (request.form.get('name') or '').strip()
                debt_type = request.form.get('debt_type')
                if not name or debt_type not in dict(DEBT_TYPES):
                    errors.append('A debt needs a name and a type.')
                else:
                    _create_debt(name=name, debt_type=debt_type, balance=_form_float('balance', 0.0),
                                 interest_rate=_form_float('interest_rate', 0.0),
                                 minimum_payment=_form_float('minimum_payment', 0.0),
                                 creditor=(request.form.get('creditor') or '').strip() or None)
                    return redirect(url_for('setup_step', step='extras'))
            else:
                return redirect(url_for('setup_step', step='done'))

    if step == 'done' and request.method == 'GET':
        _save_app_settings(setup_completed=True, setup_completed_at=datetime.now(), setup_skipped=False)

    primary = _setup_primary()
    if step in ('income', 'bills') and not primary:
        return redirect(url_for('setup_step', step='account'))

    outlook = []
    if step == 'done' and primary:
        outlook = calculate_yearly_outlook(start_balance=primary.get('balance', 0),
                                           account_filter=str(primary['_id']), max_months_back=0)[:3]

    return render_template('setup.html',
                           step=step,
                           steps=SETUP_STEPS,
                           step_index=_SETUP_STEP_KEYS.index(step),
                           errors=errors,
                           form=request.form,
                           settings=_app_settings(),
                           summary=_setup_summary(),
                           outlook=outlook,
                           server_tz=time.strftime('%Z'),
                           today=date.today(),
                           primary_account_types=[(k, l) for k, l in ACCOUNT_TYPES if k in CASH_ACCOUNT_TYPES],
                           extra_account_types=[(k, l) for k, l in ACCOUNT_TYPES if k != 'checking'],
                           debt_types=DEBT_TYPES,
                           income_frequencies=SETUP_INCOME_FREQUENCIES,
                           bill_frequencies=SETUP_BILL_FREQUENCIES,
                           bill_suggestions=SETUP_BILL_SUGGESTIONS,
                           categories=CATEGORIES,
                           income_categories=INCOME_CATEGORIES,
                           first_run=needs_first_run_setup()), (400 if errors else 200)


@app.route('/setup/skip', methods=['POST'])
def setup_skip():
    _save_app_settings(setup_skipped=True)
    return redirect(url_for('index'))


@app.route('/setup/demo', methods=['POST'])
def setup_demo():
    """Load the fake demo household instead of entering real data."""
    import demo_data
    try:
        demo_data.load_demo_data(db)
    except demo_data.DemoDataRefused:
        return render_template('setup.html', step='welcome', steps=SETUP_STEPS, step_index=0,
                               errors=['Demo data can only be loaded into an empty database (no accounts yet).'],
                               form={}, settings=_app_settings(), summary=_setup_summary(), outlook=[],
                               server_tz=time.strftime('%Z'), today=date.today(),
                               first_run=needs_first_run_setup()), 400
    _save_app_settings(setup_completed=True, setup_completed_at=datetime.now(), demo_data=True,
                       household_name=_app_settings().get('household_name') or 'Demo household')
    return redirect(url_for('index'))


@app.route('/setup/restart', methods=['POST'])
def setup_restart():
    """Admin: walk the wizard again. Existing data is kept; step 2 edits the primary account."""
    db.app_settings.update_one({'_id': 'app'}, {'$unset': {'setup_completed': '', 'setup_skipped': ''}}, upsert=True)
    return redirect(url_for('setup_step', step='welcome'))


@app.route('/setup/checklist/dismiss', methods=['POST'])
def setup_checklist_dismiss():
    _save_app_settings(checklist_dismissed=request.form.get('show') != '1')
    return redirect(url_for('index'))


@app.route('/')
def index():
    """Primary dashboard — high-signal financial snapshot."""
    if needs_first_run_setup():
        return redirect(url_for('setup_step', step='welcome'))
    _apply_auto_links()
    today = date.today()
    today_dt = datetime.combine(today, datetime.min.time())

    accounts = list(db.accounts.find())
    primary_account = next((a for a in accounts if a.get('is_primary')), None)
    primary_balance = primary_account.get('balance', 0) if primary_account else 0
    primary_name = primary_account.get('name') if primary_account else None

    cash_types = set(CASH_ACCOUNT_TYPES)
    investment_types = set(INVESTMENT_ACCOUNT_TYPES)
    cash_total = sum(a.get('balance', 0) or 0 for a in accounts if a.get('account_type') in cash_types)
    investment_total = sum(a.get('balance', 0) or 0 for a in accounts if a.get('account_type') in investment_types)

    # Net worth + 30-day delta
    assets = list(db.assets.find())
    debts_list = list(db.debts.find())
    payment_plans = list(db.payment_plans.find({'status': {'$ne': 'closed'}}))
    total_assets_val = sum(a.get('estimated_value', 0) or 0 for a in assets)
    asset_liabilities = _asset_liability_total()
    standalone_debts = _standalone_debt_total()
    total_account_balance = sum(a.get('balance', 0) or 0 for a in accounts)
    current_assets_total = total_account_balance + total_assets_val
    current_liabilities_total = asset_liabilities + standalone_debts
    current_net_worth = current_assets_total - current_liabilities_total

    # Net worth delta — find snapshot ~30 days ago (closest)
    cutoff = today_dt - timedelta(days=30)
    prior_snap = db.networth_snapshots.find_one(
        {'date': {'$lte': cutoff}}, sort=[('date', -1)]
    ) or db.networth_snapshots.find_one({}, sort=[('date', 1)])
    nw_delta = None
    if prior_snap and prior_snap.get('net_worth') is not None:
        nw_delta = current_net_worth - prior_snap['net_worth']

    # Cash flow warning — sniff next 90 days for negative low points
    outlook = calculate_yearly_outlook(
        start_balance=primary_balance,
        account_filter=str(primary_account['_id']) if primary_account else None,
        max_months_back=0,
    )
    upcoming_outlook = outlook[:4]  # current month + next 3
    cashflow_warnings = [m for m in upcoming_outlook if m.get('min_balance', 0) < 0]
    next_month_end = upcoming_outlook[0]['balance'] if upcoming_outlook else primary_balance

    # Upcoming bills (next 14 days) from recurring expenses + recurring transfers
    horizon = today + timedelta(days=14)
    upcoming_bills = []
    for r in db.recurring_transactions.find({'transaction_type': 'expense', **ACTIVE_RECURRING}):
        is_biweekly = r.get('frequency') == 'biweekly'
        days_field = (r.get('day_of_month') or '').strip()
        if not is_biweekly:
            if not days_field:
                continue
            try:
                days = [int(d) for d in days_field.split(',') if d.strip()]
            except ValueError:
                continue
        start = r.get('start_date')
        anchor = None
        if start:
            anchor = start.date() if isinstance(start, datetime) else start
        for offset in range(0, 15):
            d = today + timedelta(days=offset)
            if is_biweekly:
                hits = anchor is not None and d >= anchor and (d - anchor).days % 14 == 0
            else:
                hits = d.day in days
            if hits:
                # Respect start_date / end_date if set
                if anchor and d < anchor:
                    continue
                end = r.get('end_date')
                if end:
                    ed = end.date() if isinstance(end, datetime) else end
                    if d > ed:
                        continue
                upcoming_bills.append({
                    'date': d,
                    'description': r.get('description', ''),
                    'amount': r.get('amount', 0),
                    'category': r.get('category', ''),
                })
                break  # Only first occurrence in window
    upcoming_bills.sort(key=lambda b: b['date'])
    upcoming_bills_total = sum(b['amount'] for b in upcoming_bills)

    # MTD spending — sum of expense transactions on primary this month
    month_start = date(today.year, today.month, 1)
    mtd_query = {'transaction_type': 'expense',
                 'date': {'$gte': datetime.combine(month_start, datetime.min.time())}}
    if primary_account:
        mtd_query['account_id'] = primary_account['_id']
    mtd_spent = sum(t.get('amount', 0) or 0 for t in db.transactions.find(mtd_query))

    mtd_income_query = {'transaction_type': 'income',
                        'date': {'$gte': datetime.combine(month_start, datetime.min.time())}}
    if primary_account:
        mtd_income_query['account_id'] = primary_account['_id']
    mtd_income = sum(t.get('amount', 0) or 0 for t in db.transactions.find(mtd_income_query))

    # Savings goals — top 3 by closeness to target (live values per goal type)
    goals = [_decorate_goal(g) for g in db.savings_goals.find()]
    goals.sort(key=lambda g: g['progress_pct'], reverse=True)
    top_goals = goals[:3]

    # Latest advisor brief
    latest_brief = db.reports.find_one({}, sort=[('date', -1)])
    if latest_brief:
        latest_brief['_id'] = str(latest_brief['_id'])
        if isinstance(latest_brief.get('date'), datetime):
            latest_brief['date_str'] = latest_brief['date'].strftime('%b %d, %Y')
        # Use first ~280 chars of insights as snippet
        insights = latest_brief.get('insights', '') or ''
        # Strip markdown headers/lists for preview
        snippet = ' '.join(insights.split())[:280]
        if len(insights) > 280:
            snippet += '…'
        latest_brief['snippet'] = snippet

    # Active payment plans — surface most-progressed
    plans_decorated = [_decorate_payment_plan(p) for p in payment_plans]
    plans_decorated.sort(key=lambda p: p.get('progress_pct', 0), reverse=True)
    top_plans = plans_decorated[:3]
    plans_remaining_total = sum(p['remaining'] for p in plans_decorated)

    # Tax-year contribution room and expiring card credits (top items only;
    # the full picture lives on the Benefits page).
    try:
        benefit_ctx = _benefit_context(today.year)
        benefit_alerts = [a for a in benefit_ctx['alerts'] if a['severity'] in ('high', 'med')][:4]
    except Exception:
        logger.exception('benefit alerts failed')
        benefit_alerts = []

    # Customizable dashboard: layout + which cards actually have data to show
    dashboard_layout = _dashboard_layout()
    card_available = {
        'upcoming_bills': True,
        'month_to_date': True,
        'payment_plans': bool(top_plans),
        'outlook': bool(upcoming_outlook),
        'goals': bool(top_goals),
        'advisor_brief': bool(latest_brief),
        'benefit_alerts': bool(benefit_alerts),
    }

    app_settings = _app_settings()
    checklist = getting_started_checklist()
    show_checklist = not app_settings.get('checklist_dismissed') and not all(i['done'] for i in checklist)

    return render_template('dashboard.html',
                           today=today,
                           household_name=app_settings.get('household_name'),
                           checklist=checklist,
                           show_checklist=show_checklist,
                           checklist_done=sum(1 for i in checklist if i['done']),
                           dashboard_layout=dashboard_layout,
                           card_available=card_available,
                           primary_name=primary_name,
                           primary_balance=primary_balance,
                           cash_total=cash_total,
                           investment_total=investment_total,
                           current_net_worth=current_net_worth,
                           nw_delta=nw_delta,
                           total_debts=current_liabilities_total,
                           standalone_debts=standalone_debts,
                           asset_liabilities=asset_liabilities,
                           active_plan_count=len(plans_decorated),
                           plans_remaining_total=plans_remaining_total,
                           top_plans=top_plans,
                           upcoming_outlook=upcoming_outlook,
                           cashflow_warnings=cashflow_warnings,
                           next_month_end=next_month_end,
                           upcoming_bills=upcoming_bills,
                           upcoming_bills_total=upcoming_bills_total,
                           mtd_spent=mtd_spent,
                           mtd_income=mtd_income,
                           top_goals=top_goals,
                           latest_brief=latest_brief,
                           benefit_alerts=benefit_alerts)


def resolve_account_filter(primary_account_id):
    """Resolve the active account filter and persist the user's selection across pages.

    An explicit ?account= param updates and stores the selection in the session; when no
    param is present we fall back to the last stored selection, then the primary account,
    then 'all'. This keeps the chosen account sticky as the user moves between the ledger,
    history, and other account-scoped pages until they change it."""
    account_filter = request.args.get('account')
    if account_filter:
        session['account_filter'] = account_filter
    else:
        account_filter = session.get('account_filter')
    if not account_filter:
        account_filter = primary_account_id or 'all'
    return account_filter


@app.route('/ledger')
def ledger_page():
    # Auto-attach loan payments by their budget-item description so the ledger
    # badges and loan balances are correct without manual linking.
    _apply_auto_links()

    # Get all accounts for filter dropdown
    all_accounts = list(db.accounts.find())
    accounts_for_filter = []
    primary_account_id = None
    accounts_lookup = {}
    for acc in all_accounts:
        acc_id = str(acc['_id'])
        if acc.get('is_primary'):
            primary_account_id = acc_id
        accounts_lookup[acc_id] = acc.get('name', 'Unknown')
        accounts_for_filter.append({
            'id': acc_id,
            'name': acc.get('name', 'Unknown'),
            'is_primary': acc.get('is_primary', False)
        })

    # Get account filter — persists the last selection, defaults to primary account
    account_filter = resolve_account_filter(primary_account_id)

    # Check for transaction search/filter params
    search = request.args.get('search', '').strip()
    date_from = request.args.get('date_from', '').strip()
    date_to = request.args.get('date_to', '').strip()
    amount_min = request.args.get('amount_min', '').strip()
    amount_max = request.args.get('amount_max', '').strip()
    filter_type = request.args.get('type', '').strip()

    filter_params = {
        'search': search,
        'date_from': date_from,
        'date_to': date_to,
        'amount_min': amount_min,
        'amount_max': amount_max,
        'type': filter_type
    }
    filtered_mode = any([search, date_from, date_to, amount_min, amount_max, filter_type])

    filtered_results = []
    yearly_outlook = []

    if filtered_mode:
        # Build MongoDB query for filtering
        query = {}
        if search:
            query['description'] = {'$regex': re.escape(search), '$options': 'i'}
        try:
            if date_from:
                query.setdefault('date', {})['$gte'] = datetime.fromisoformat(date_from)
            if date_to:
                query.setdefault('date', {})['$lte'] = datetime.fromisoformat(date_to + 'T23:59:59')
            if amount_min:
                query['amount'] = query.get('amount', {})
                query['amount']['$gte'] = float(amount_min)
            if amount_max:
                query.setdefault('amount', {})['$lte'] = float(amount_max)
        except (ValueError, TypeError):
            return "Invalid filter values", 400
        if filter_type:
            query['transaction_type'] = filter_type

        transactions = list(db.transactions.find(query).sort('date', -1))
        for t in transactions:
            t_account_id = str(t.get('account_id')) if t.get('account_id') else primary_account_id
            filtered_results.append({
                '_id': str(t['_id']),
                'date': t['date'].strftime('%Y-%m-%d') if isinstance(t['date'], datetime) else t['date'],
                'description': t['description'],
                'amount': t['amount'],
                'transaction_type': t['transaction_type'],
                'account_id': t_account_id,
                'account_name': accounts_lookup.get(t_account_id, 'Primary')
            })
    else:
        # Determine starting balance based on filter
        if account_filter == 'all' or not account_filter:
            start_balance = get_total_account_balance()
        else:
            oid = safe_objectid(account_filter)
            filtered_account = db.accounts.find_one({'_id': oid}) if oid else None
            start_balance = filtered_account.get('balance', 0) if filtered_account else 0

        yearly_outlook = calculate_yearly_outlook(start_balance, account_filter if account_filter != 'all' else None, max_months_back=1)

    # Calculate transaction adjustments per account.
    # Two sets: total (all actual transactions) and confirmed (only up to today).
    today_dt = datetime.combine(date.today(), datetime.max.time())
    transaction_adjustments = {}  # account_id -> total adjustment (all time)
    confirmed_adjustments = {}   # account_id -> adjustment from transactions dated <= today
    total_transaction_adjustment = 0
    for t in db.transactions.find():
        t_account_id = str(t.get('account_id')) if t.get('account_id') else primary_account_id
        if t_account_id not in transaction_adjustments:
            transaction_adjustments[t_account_id] = 0
            confirmed_adjustments[t_account_id] = 0

        t_date = t['date'] if isinstance(t['date'], datetime) else datetime.fromisoformat(t['date'])
        adj = t['amount'] if t['transaction_type'] == 'income' else -t['amount']

        transaction_adjustments[t_account_id] += adj
        total_transaction_adjustment += adj
        if t_date <= today_dt:
            confirmed_adjustments[t_account_id] += adj

    # Calculate total balance across ALL accounts + transactions
    total_account_balance = get_total_account_balance()
    current_balance = total_account_balance + total_transaction_adjustment

    # Get accounts breakdown for display, with calculated balance per account
    accounts = get_accounts_summary()
    cash_accounts = []
    investment_accounts = []
    cash_total = 0
    investment_total = 0

    for account in accounts:
        acc_id = str(account['_id'])
        # Each account shows its balance + its own transactions
        account_adjustment = transaction_adjustments.get(acc_id, 0)
        account['calculated_balance'] = account['balance'] + account_adjustment

        # Confirmed balance: base + only actual transactions dated on or before today
        confirmed_adj = confirmed_adjustments.get(acc_id, 0)
        account['confirmed_balance'] = account['balance'] + confirmed_adj

        # Categorize accounts
        if account['account_type'] in ['checking', 'savings']:
            cash_accounts.append(account)
            cash_total += account['calculated_balance']
        elif account['account_type'] in INVESTMENT_ACCOUNT_TYPES:
            investment_accounts.append(account)
            investment_total += account['calculated_balance']
        else:
            cash_accounts.append(account)  # Put 'other' in cash
            cash_total += account['calculated_balance']

    # Build income vs expense monthly summary for chart
    monthly_income_expense = []
    for month_data in yearly_outlook:
        income = sum(t['amount'] for t in month_data['transactions'] if t['amount'] > 0)
        expenses = sum(-t['amount'] for t in month_data['transactions'] if t['amount'] < 0)
        monthly_income_expense.append({
            'month': month_data['month'],
            'income': round(income, 2),
            'expenses': round(expenses, 2)
        })

    # Get current month name for expanding in the view
    current_month_name = date.today().strftime("%B %Y")

    # Loans a ledger transaction can be linked to (debts + asset liabilities)
    loan_options = [{'value': 'debt:' + str(d['_id']), 'name': d['name']} for d in db.debts.find()]
    loan_options += [{'value': 'asset:' + str(a['_id']), 'name': a['name']}
                     for a in db.assets.find({'liability_balance': {'$gt': 0}})]

    return render_template('index.html',
                           yearly_outlook=yearly_outlook,
                           loan_options=loan_options,
                           current_balance=current_balance,
                           cash_accounts=cash_accounts,
                           investment_accounts=investment_accounts,
                           cash_total=cash_total,
                           investment_total=investment_total,
                           accounts_for_filter=accounts_for_filter,
                           account_filter=account_filter,
                           primary_account_id=primary_account_id,
                           current_month=current_month_name,
                           today=date.today().isoformat(),
                           filtered_mode=filtered_mode,
                           filtered_results=filtered_results,
                           filter_params=filter_params,
                           monthly_income_expense=monthly_income_expense)

@app.route('/simulation')
def simulation_page():
    """Non-destructive what-if simulator. Change the amount of any recurring budget item or
    transfer, turn items off, or add hypothetical new ones, and see how the projected ledger
    responds. Everything runs in-memory — nothing is ever written to the database."""
    # Accounts for the filter dropdown + primary lookup
    accounts_for_filter = []
    primary_account_id = None
    accounts_lookup = {}
    for acc in db.accounts.find():
        acc_id = str(acc['_id'])
        if acc.get('is_primary'):
            primary_account_id = acc_id
        accounts_lookup[acc_id] = acc.get('name', 'Unknown')
        accounts_for_filter.append({
            'id': acc_id,
            'name': acc.get('name', 'Unknown'),
            'is_primary': acc.get('is_primary', False)
        })

    account_filter = resolve_account_filter(primary_account_id)

    # Editable list of existing recurring budget items + transfers
    sim_items = []
    for rt in db.recurring_transactions.find(ACTIVE_RECURRING):
        rid = str(rt['_id'])
        sim_items.append({
            'id': rid,
            'kind': 'income' if rt.get('transaction_type') == 'income' else 'expense',
            'description': rt.get('description', 'Unnamed'),
            'base_amount': rt.get('amount', 0) or 0,
            'frequency': rt.get('frequency', 'monthly'),
            'account_name': accounts_lookup.get(str(rt['account_id']), 'Primary') if rt.get('account_id') else 'Primary',
        })
    for rt in db.recurring_transfers.find():
        rid = str(rt['_id'])
        from_name = accounts_lookup.get(str(rt.get('from_account_id', '')), 'Unknown')
        to_name = accounts_lookup.get(str(rt.get('to_account_id', '')), 'Unknown')
        sim_items.append({
            'id': rid,
            'kind': 'transfer',
            'description': rt.get('description', 'Recurring Transfer'),
            'base_amount': rt.get('amount', 0) or 0,
            'frequency': rt.get('frequency', 'monthly'),
            'account_name': '%s → %s' % (from_name, to_name),
        })

    kind_order = {'income': 0, 'expense': 1, 'transfer': 2}
    sim_items.sort(key=lambda x: (kind_order.get(x['kind'], 3), x['description'].lower()))

    # Parse what-if changes from query params onto each item
    overrides = {}
    disabled = set()
    active = False
    for item in sim_items:
        rid = item['id']
        item['disabled'] = bool(request.args.get('off_' + rid))
        item['changed'] = False
        item['sim_amount'] = item['base_amount']
        if item['disabled']:
            disabled.add(rid)
            active = True
        raw = request.args.get('amt_' + rid)
        if raw not in (None, ''):
            try:
                val = float(raw)
            except ValueError:
                val = item['base_amount']
            item['sim_amount'] = val
            if not item['disabled'] and abs(val - item['base_amount']) > 0.005:
                overrides[rid] = val
                item['changed'] = True
                active = True

    # Hypothetical new items (parallel arrays from the "add item" rows)
    new_descs = request.args.getlist('new_desc')
    new_amounts = request.args.getlist('new_amount')
    new_types = request.args.getlist('new_type')
    new_freqs = request.args.getlist('new_freq')
    new_days = request.args.getlist('new_day')
    additions = []
    new_items_display = []
    today = date.today()
    for i in range(len(new_descs)):
        desc = (new_descs[i] or '').strip()
        amt_raw = new_amounts[i] if i < len(new_amounts) else ''
        if not desc or amt_raw in (None, ''):
            continue
        try:
            amt = float(amt_raw)
        except ValueError:
            continue
        ttype = 'income' if (i < len(new_types) and new_types[i] == 'income') else 'expense'
        freq = new_freqs[i] if i < len(new_freqs) and new_freqs[i] else 'monthly'
        day = new_days[i] if i < len(new_days) and new_days[i] else '1'
        additions.append({
            '_id': 'sim-new-%d' % i,
            'description': desc,
            'amount': amt,
            'transaction_type': ttype,
            'frequency': freq,
            'day_of_month': day,
            'start_date': datetime(today.year, today.month, 1),
            # Attach to the account currently in view so the item is always visible
            'account_id': account_filter if account_filter != 'all' else None,
        })
        new_items_display.append({'description': desc, 'amount': amt, 'type': ttype,
                                  'frequency': freq, 'day': day})
        active = True

    # Projection settings: where the 12-month window starts and what feeds it.
    #   mode=ledger (default): current account balance + actual transactions + recurring items
    #   mode=budget: recurring budget items only (no actuals/skips/overrides), starting from
    #                the given balance (default $0) — a pure "what does my budget do" run
    sim_mode = 'budget' if request.args.get('sim_mode') == 'budget' else 'ledger'
    sim_start = None
    sim_start_raw = request.args.get('sim_start') or ''
    if sim_start_raw:
        try:
            sim_start = date.fromisoformat(sim_start_raw[:10])
        except ValueError:
            sim_start_raw = ''
    sim_start_balance_raw = request.args.get('sim_start_balance') or ''
    sim_start_balance = None
    if sim_start_balance_raw != '':
        try:
            sim_start_balance = float(sim_start_balance_raw)
        except ValueError:
            sim_start_balance_raw = ''

    # Starting balance for the chosen account (mirrors the ledger logic)
    if account_filter == 'all':
        account_balance = get_total_account_balance()
        eff_filter = None
    else:
        oid = safe_objectid(account_filter)
        acc = db.accounts.find_one({'_id': oid}) if oid else None
        account_balance = acc.get('balance', 0) if acc else 0
        eff_filter = account_filter

    if sim_start_balance is not None:
        start_balance = sim_start_balance
    elif sim_mode == 'budget':
        start_balance = 0.0
    else:
        start_balance = account_balance

    # Hypothetical items start in the anchor month, not necessarily today's month
    if sim_start:
        for a in additions:
            a['start_date'] = datetime(sim_start.year, sim_start.month, 1)

    settings = {'start_date': sim_start, 'budget_only': sim_mode == 'budget'}
    sim = {'overrides': overrides, 'disabled': disabled, 'additions': additions, **settings}

    # Forward-looking projections (anchor month onward) for baseline and the what-if scenario
    baseline = calculate_yearly_outlook(start_balance, eff_filter, max_months_back=0, sim=settings)
    scenario = calculate_yearly_outlook(start_balance, eff_filter, max_months_back=0, sim=sim)

    comparison = []
    for i, m in enumerate(scenario):
        base_m = baseline[i] if i < len(baseline) else None
        base_bal = base_m['balance'] if base_m else 0
        comparison.append({
            'month': m['month'],
            'sim_balance': m['balance'],
            'base_balance': base_bal,
            'delta': m['balance'] - base_bal,
        })

    sim_end = scenario[-1]['balance'] if scenario else start_balance
    base_end = baseline[-1]['balance'] if baseline else start_balance
    sim_low = min((m['min_balance'] for m in scenario), default=start_balance)
    base_low = min((m['min_balance'] for m in baseline), default=start_balance)

    return render_template('simulation.html',
                           accounts_for_filter=accounts_for_filter,
                           account_filter=account_filter,
                           sim_items=sim_items,
                           new_items=new_items_display,
                           yearly_outlook=scenario,
                           comparison=comparison,
                           active=active,
                           current_month=(sim_start or date.today()).strftime('%B %Y'),
                           sim_mode=sim_mode,
                           sim_start=sim_start_raw,
                           sim_start_balance=sim_start_balance_raw,
                           account_balance=account_balance,
                           summary={
                               'start_balance': start_balance,
                               'sim_end': sim_end, 'base_end': base_end,
                               'end_delta': sim_end - base_end,
                               'sim_low': sim_low, 'base_low': base_low,
                               'low_delta': sim_low - base_low,
                           })

@app.route('/history')
def history_page():
    """Immutable ledger view of closed months (older than previous month)."""
    today = date.today()
    current_month = today.month

    # History shows January through 2 months ago (months before the previous month)
    # For March: history = January only. For April: Jan + Feb. For Jan/Feb: nothing
    # in the current year (past years are still browsable via ?year=).
    months_to_show = current_month - 2  # e.g., March (3) -> 1 month (January)

    # Get all accounts for filter dropdown
    all_accounts = list(db.accounts.find())
    accounts_for_filter = []
    primary_account_id = None
    accounts_lookup = {}
    for acc in all_accounts:
        acc_id = str(acc['_id'])
        if acc.get('is_primary'):
            primary_account_id = acc_id
        accounts_lookup[acc_id] = acc.get('name', 'Unknown')
        accounts_for_filter.append({
            'id': acc_id,
            'name': acc.get('name', 'Unknown'),
            'is_primary': acc.get('is_primary', False)
        })

    account_filter = resolve_account_filter(primary_account_id)

    # Get requested year (default current year)
    try:
        year = int(request.args.get('year', today.year))
    except ValueError:
        year = today.year

    # For past years, show all 12 months; for current year, show up to 2 months ago
    if year < today.year:
        months_to_show = 12
    elif year > today.year or months_to_show <= 0:
        return render_template('history.html',
                               monthly_data=[],
                               accounts_for_filter=accounts_for_filter,
                               account_filter=account_filter,
                               primary_account_id=primary_account_id,
                               year=year)

    # Fetch only actual transactions for the relevant months
    year_start = datetime(year, 1, 1)
    if year == today.year:
        # Up through the end of the last history month
        history_end_month = months_to_show  # e.g., March -> 1 (January)
        history_end = datetime(year, history_end_month, calendar.monthrange(year, history_end_month)[1], 23, 59, 59)
    else:
        history_end = datetime(year, 12, 31, 23, 59, 59)

    query = {'date': {'$gte': year_start, '$lte': history_end}}
    transactions = list(db.transactions.find(query).sort('date', 1))

    # Group transactions by month
    monthly_data = []
    for m in range(1, months_to_show + 1):
        month_start = date(year, m, 1)
        month_end = date(year, m, calendar.monthrange(year, m)[1])
        month_transactions = []

        for t in transactions:
            t_date = t['date'].date() if isinstance(t['date'], datetime) else datetime.fromisoformat(t['date']).date()
            if month_start <= t_date <= month_end:
                t_account_id = str(t.get('account_id')) if t.get('account_id') else primary_account_id

                # Apply account filter
                if account_filter and account_filter != 'all':
                    if account_filter != t_account_id:
                        continue

                amount = t['amount'] if t['transaction_type'] == 'income' else -t['amount']
                month_transactions.append({
                    'date': t_date.isoformat(),
                    'description': t['description'],
                    'amount': amount,
                    'transaction_type': t['transaction_type'],
                    'account_id': t_account_id,
                    'account_name': accounts_lookup.get(t_account_id, 'Primary')
                })

        # Sort by date
        month_transactions.sort(key=lambda x: x['date'])

        # Calculate totals
        income = sum(t['amount'] for t in month_transactions if t['amount'] > 0)
        expenses = sum(-t['amount'] for t in month_transactions if t['amount'] < 0)

        monthly_data.append({
            'month': month_start.strftime('%B %Y'),
            'transactions': month_transactions,
            'income': income,
            'expenses': expenses,
            'net': income - expenses
        })

    return render_template('history.html',
                           monthly_data=monthly_data,
                           accounts_for_filter=accounts_for_filter,
                           account_filter=account_filter,
                           primary_account_id=primary_account_id,
                           year=year)

@app.route('/transactions', methods=['GET', 'POST'])
def handle_transactions():
    if request.method == 'POST':
        data = request.get_json()
        # Get account_id - if not provided, use primary account
        account_id = data.get('account_id')
        if not account_id:
            primary = db.accounts.find_one({'is_primary': True})
            account_id = str(primary['_id']) if primary else None

        new_transaction = Transaction(
            description=data['description'],
            amount=float(data['amount']),
            date=datetime.fromisoformat(data['date']),
            transaction_type=data['transaction_type'],
            account_id=account_id
        )
        db.transactions.insert_one(new_transaction.__dict__)
        _auto_link_transaction(new_transaction._id, new_transaction.description)
        return jsonify(new_transaction.to_dict()), 201
    else:
        transactions_cursor = db.transactions.find()
        transactions_list = []
        for t in transactions_cursor:
            t['_id'] = str(t['_id'])
            t['date'] = t['date'].isoformat()
            transactions_list.append(t)
        return jsonify(transactions_list)

@app.route('/transactions/link-loan', methods=['POST'])
def link_transaction_loan():
    """Link/unlink a ledger transaction to a loan (debt or asset liability) from the ledger row."""
    data = request.get_json() or {}
    tid = safe_objectid(data.get('transaction_id'))
    if not tid:
        return jsonify({'error': 'Invalid transaction'}), 400
    ref = (data.get('loan_ref') or '').strip()
    if not ref:
        db.transactions.update_one({'_id': tid}, {'$unset': {'debt_id': '', 'asset_liability_id': ''}})
        _sync_payoff_archive()
        return jsonify({'success': True, 'loan_label': None})
    kind, _, oid_raw = ref.partition(':')
    oid = safe_objectid(oid_raw)
    if not oid or kind not in ('debt', 'asset'):
        return jsonify({'error': 'Invalid loan'}), 400
    if kind == 'debt':
        db.transactions.update_one({'_id': tid}, {'$set': {'debt_id': oid}, '$unset': {'asset_liability_id': ''}})
        loan = db.debts.find_one({'_id': oid})
    else:
        db.transactions.update_one({'_id': tid}, {'$set': {'asset_liability_id': oid}, '$unset': {'debt_id': ''}})
        loan = db.assets.find_one({'_id': oid})
    _sync_payoff_archive()
    return jsonify({'success': True, 'loan_label': loan.get('name') if loan else None})


@app.route('/transactions/<transaction_id>', methods=['DELETE'])
def delete_transaction(transaction_id):
    # Get transaction before deleting to add to skipped
    oid = safe_objectid(transaction_id)
    if not oid:
        return jsonify({'error': 'Invalid transaction ID'}), 400
    transaction = db.transactions.find_one({'_id': oid})
    if transaction:
        # Add to skipped transactions so projected doesn't reappear
        transaction_date = transaction['date'].strftime('%Y-%m-%d') if isinstance(transaction['date'], datetime) else transaction['date']
        db.skipped_transactions.update_one(
            {'description': transaction['description'], 'date': transaction_date},
            {'$set': {'description': transaction['description'], 'date': transaction_date}},
            upsert=True
        )
        db.transactions.delete_one({'_id': oid})
        return jsonify({'message': 'Transaction deleted successfully'}), 200
    else:
        return jsonify({'message': 'Transaction not found'}), 404

@app.route('/transactions/skip', methods=['POST'])
def skip_transaction():
    data = request.get_json() or request.form
    description = data.get('description')
    date_str = data.get('date')
    if description and date_str:
        db.skipped_transactions.update_one(
            {'description': description, 'date': date_str},
            {'$set': {'description': description, 'date': date_str}},
            upsert=True
        )
        # If this is a transfer, also skip the other side
        if description.startswith('Transfer to: '):
            mirror_desc = 'Transfer from: ' + description[len('Transfer to: '):]
            db.skipped_transactions.update_one(
                {'description': mirror_desc, 'date': date_str},
                {'$set': {'description': mirror_desc, 'date': date_str}},
                upsert=True
            )
        elif description.startswith('Transfer from: '):
            mirror_desc = 'Transfer to: ' + description[len('Transfer from: '):]
            db.skipped_transactions.update_one(
                {'description': mirror_desc, 'date': date_str},
                {'$set': {'description': mirror_desc, 'date': date_str}},
                upsert=True
            )
        if request.headers.get('Accept') == 'application/json':
            return jsonify({'message': 'Transaction skipped'}), 200
        return redirect(url_for('ledger_page'))
    return jsonify({'error': 'Missing description or date'}), 400


@app.route('/projected/update-amount', methods=['POST'])
def update_projected_amount():
    """Update the amount for a specific occurrence of a recurring transaction."""
    data = request.get_json() or request.form
    recurring_id = data.get('recurring_id')
    date_str = data.get('date')
    amount = data.get('amount')

    if recurring_id is None or date_str is None or amount is None:
        return jsonify({'error': 'Missing required fields'}), 400

    try:
        amount = float(amount)
    except ValueError:
        return jsonify({'error': 'Invalid amount'}), 400

    # Upsert the override
    db.projected_overrides.update_one(
        {'recurring_id': safe_objectid(recurring_id), 'date': date_str},
        {'$set': {
            'recurring_id': safe_objectid(recurring_id),
            'date': date_str,
            'amount': amount
        }},
        upsert=True
    )

    if request.headers.get('Accept') == 'application/json':
        return jsonify({'message': 'Amount updated', 'amount': amount}), 200
    return redirect(url_for('ledger_page'))


@app.route('/transactions/edit/<transaction_id>', methods=['GET', 'POST'])
def edit_transaction(transaction_id):
    if request.method == 'POST':
        db.transactions.update_one(
            {'_id': safe_objectid(transaction_id)},
            {'$set': {
                'description': request.form['description'],
                'amount': float(request.form['amount']),
                'date': datetime.fromisoformat(request.form['date']),
                'transaction_type': request.form['transaction_type']
            }}
        )
        return redirect(url_for('ledger_page'))
    else:
        transaction = db.transactions.find_one({'_id': safe_objectid(transaction_id)})
        if transaction:
            transaction['date'] = transaction['date'].strftime('%Y-%m-%d')
            return render_template('edit_transaction.html', transaction=transaction)
        else:
            return jsonify({'message': 'Transaction not found'}), 404

@app.route('/transactions/verify', methods=['GET', 'POST'])
def verify_transaction():
    if request.method == 'POST':
        data = request.form

        # Check if this is a transfer verification
        if data.get('is_transfer') == '1':
            from_account_id = data['transfer_from_id']
            to_account_id = data['transfer_to_id']
            amount = float(data['amount'])
            transfer_date = datetime.fromisoformat(data['date'])
            description = data['description']
            # Strip "Transfer to:/from:" prefix if present (projected transfers already have it)
            if description.startswith('Transfer to: '):
                description = description[len('Transfer to: '):]
            elif description.startswith('Transfer from: '):
                description = description[len('Transfer from: '):]

            # Create the transfer record
            new_transfer = Transfer(
                from_account_id=from_account_id,
                to_account_id=to_account_id,
                amount=amount,
                date=transfer_date,
                description=description
            )
            db.transfers.insert_one(new_transfer.__dict__)

            # Create linked transactions for the ledger
            expense_transaction = Transaction(
                description=f"Transfer to: {description}",
                amount=amount,
                date=transfer_date,
                transaction_type='expense',
                account_id=from_account_id
            )
            income_transaction = Transaction(
                description=f"Transfer from: {description}",
                amount=amount,
                date=transfer_date,
                transaction_type='income',
                account_id=to_account_id
            )
            db.transactions.insert_one(expense_transaction.__dict__)
            db.transactions.insert_one(income_transaction.__dict__)
            return redirect(url_for('ledger_page'))

        # Regular transaction verification
        account_id = data.get('account_id')
        if not account_id:
            primary = db.accounts.find_one({'is_primary': True})
            account_id = str(primary['_id']) if primary else None

        new_transaction = Transaction(
            description=data['description'],
            amount=float(data['amount']),
            date=datetime.fromisoformat(data['date']),
            transaction_type=data['transaction_type'],
            account_id=account_id
        )
        db.transactions.insert_one(new_transaction.__dict__)
        _auto_link_transaction(new_transaction._id, new_transaction.description)
        return redirect(url_for('ledger_page'))
    else:
        description = request.args.get('description', '')
        amount = float(request.args.get('amount', 0.0))
        date_str = request.args.get('date', date.today().isoformat())
        transaction_type = request.args.get('transaction_type', 'expense')
        is_transfer = request.args.get('is_transfer', '')
        transfer_from_id = request.args.get('transfer_from_id', '')
        transfer_to_id = request.args.get('transfer_to_id', '')
        account_id = request.args.get('account_id', '')

        projected_trans = {
            'description': description,
            'amount': amount,
            'date': date_str,
            'transaction_type': transaction_type,
            'is_transfer': is_transfer,
            'transfer_from_id': transfer_from_id,
            'transfer_to_id': transfer_to_id,
            'account_id': account_id
        }
        # Get accounts for dropdown
        accounts = list(db.accounts.find())
        return render_template('verify_transaction.html', transaction=projected_trans, accounts=accounts)

@app.route('/recurring_transactions', methods=['GET', 'POST'])
def handle_recurring_transactions():
    if request.method == 'POST':
        data = request.get_json()
        new_transaction = RecurringTransaction(
            description=data['description'],
            amount=float(data['amount']),
            frequency=data['frequency'],
            start_date=datetime.fromisoformat(data['start_date']),
            transaction_type=data['transaction_type'],
            end_date=datetime.fromisoformat(data['end_date']) if data.get('end_date') else None,
            day_of_month=data.get('day_of_month')
        )
        db.recurring_transactions.insert_one(new_transaction.__dict__)
        return jsonify(new_transaction.to_dict()), 201
    else:
        transactions_cursor = db.recurring_transactions.find()
        transactions_list = []
        for t in transactions_cursor:
            t['_id'] = str(t['_id'])
            t['start_date'] = t['start_date'].isoformat()
            if t.get('end_date'):
                t['end_date'] = t['end_date'].isoformat()
            transactions_list.append(t)
        return jsonify(transactions_list)

@app.route('/budget')
def budget_page():
    # Archive/restore lines of paid-off loans before totalling the plan.
    _apply_auto_links()
    recurring = list(db.recurring_transactions.find(ACTIVE_RECURRING))
    archived_items = list(db.recurring_transactions.find({'archived': True}).sort('archived_at', -1))
    loan_names = {'debt:' + str(d['_id']): d.get('name') for d in db.debts.find()}
    loan_names.update({'asset:' + str(a['_id']): a.get('name') for a in db.assets.find()})
    for r in archived_items:
        r['_id'] = str(r['_id'])
        r['loan_name'] = loan_names.get(r.get('archived_loan_ref'))
        r.setdefault('archived_at', None)
        r.setdefault('archived_reason', None)
        r['monthly_amount'] = calc_monthly_amount(r['amount'], r['frequency'], r.get('day_of_month'))

    # Month picker: get selected month or default to current
    selected_month = request.args.get('month', datetime.now().strftime('%Y-%m'))

    # Load default template targets and overlay month-specific overrides
    default_targets = {t['category']: t['target'] for t in db.budget_targets.find()}
    overrides = {t['category']: t['target'] for t in db.monthly_budget_targets.find({'month': selected_month})}
    budget_targets = {**default_targets, **overrides}
    month_has_overrides = bool(overrides)

    # Get available months from cc_transactions, always include current month
    current_month = datetime.now().strftime('%Y-%m')
    months_set = set(db.cc_transactions.distinct('statement_date'))
    months_set.add(current_month)
    available_months = sorted(months_set, reverse=True)

    # Aggregate CC actual spending for selected month by category
    cc_pipeline = [
        {'$match': {'statement_date': selected_month, 'amount': {'$gt': 0}}},
        {'$group': {'_id': '$category', 'total': {'$sum': '$amount'}}}
    ]
    cc_actuals = {}
    for doc in db.cc_transactions.aggregate(cc_pipeline):
        if doc['_id']:
            cc_actuals[doc['_id']] = doc['total']

    # Calculate summaries
    monthly_income = 0
    monthly_expenses = 0
    annual_income = 0
    annual_expenses = 0
    category_totals = {}

    # Pre-load accounts for account_id lookups
    all_accounts = {str(a['_id']): a for a in db.accounts.find()}
    investment_types = set(INVESTMENT_ACCOUNT_TYPES)

    for r in recurring:
        r['_id'] = str(r['_id'])
        r['start_date'] = r['start_date'].strftime('%Y-%m-%d') if isinstance(r['start_date'], datetime) else r['start_date']
        if r.get('end_date'):
            r['end_date'] = r['end_date'].strftime('%Y-%m-%d') if isinstance(r['end_date'], datetime) else r['end_date']

        # Calculate monthly amount based on frequency and days
        monthly_amt = calc_monthly_amount(r['amount'], r['frequency'], r.get('day_of_month'))
        annual_amt = monthly_amt * 12

        r['monthly_amount'] = monthly_amt
        r['annual_amount'] = annual_amt

        if r['transaction_type'] == 'income':
            monthly_income += monthly_amt
            annual_income += annual_amt

            # Income items attributed to savings/investment accounts count
            # toward those budget categories (e.g. salary → savings account)
            acct = all_accounts.get(str(r.get('account_id', '')), {})
            acct_type = acct.get('account_type', '')
            if acct_type == 'savings':
                savings_cat = 'Savings'
                if savings_cat not in category_totals:
                    category_totals[savings_cat] = {'planned': 0, 'actual': 0, 'annual': 0, 'target': budget_targets.get(savings_cat, 0)}
                category_totals[savings_cat]['planned'] += monthly_amt
                category_totals[savings_cat]['annual'] += annual_amt
            elif acct_type in investment_types:
                invest_cat = 'Investments'
                if invest_cat not in category_totals:
                    category_totals[invest_cat] = {'planned': 0, 'actual': 0, 'annual': 0, 'target': budget_targets.get(invest_cat, 0)}
                category_totals[invest_cat]['planned'] += monthly_amt
                category_totals[invest_cat]['annual'] += annual_amt
        else:
            monthly_expenses += monthly_amt
            annual_expenses += annual_amt

        # Track category totals
        cat = r.get('category', 'Other')
        if cat not in category_totals:
            category_totals[cat] = {'planned': 0, 'actual': 0, 'annual': 0, 'target': budget_targets.get(cat, 0)}
        category_totals[cat]['planned'] += monthly_amt
        category_totals[cat]['annual'] += annual_amt

    # Add recurring transfers as budget items and to category totals
    for rt in db.recurring_transfers.find():
        monthly_amt = calc_monthly_amount(rt['amount'], rt['frequency'], rt.get('day_of_month'))
        annual_amt = monthly_amt * 12
        to_id = str(rt.get('to_account_id', ''))
        from_id = str(rt.get('from_account_id', ''))
        to_account = all_accounts.get(to_id, {})
        from_account = all_accounts.get(from_id, {})

        # Determine category based on destination account type
        to_type = to_account.get('account_type', '')
        if to_type == 'savings':
            cat = 'Savings'
        elif to_type in investment_types:
            cat = 'Investments'
        else:
            cat = 'Other'

        if cat not in category_totals:
            category_totals[cat] = {'planned': 0, 'actual': 0, 'annual': 0, 'target': budget_targets.get(cat, 0)}
        category_totals[cat]['planned'] += monthly_amt
        category_totals[cat]['annual'] += annual_amt

        monthly_expenses += monthly_amt
        annual_expenses += annual_amt

        # Create synthetic budget item so it appears in the budget items list
        rt_start = rt['start_date'].strftime('%Y-%m-%d') if isinstance(rt['start_date'], datetime) else rt['start_date']
        rt_end = None
        if rt.get('end_date'):
            rt_end = rt['end_date'].strftime('%Y-%m-%d') if isinstance(rt['end_date'], datetime) else rt['end_date']
        recurring.append({
            '_id': str(rt['_id']),
            'description': rt.get('description', 'Recurring Transfer'),
            'amount': rt['amount'],
            'frequency': rt['frequency'],
            'day_of_month': rt.get('day_of_month'),
            'transaction_type': 'expense',
            'category': cat,
            'notes': f"{from_account.get('name', '?')} \u2192 {to_account.get('name', '?')}",
            'auto_pay': True,
            'monthly_amount': monthly_amt,
            'annual_amount': annual_amt,
            'account_id': from_id,
            'account_name': from_account.get('name'),
            'is_transfer': True,
            'start_date': rt_start,
            'end_date': rt_end,
            'payment_method': None,
            'account_number': None,
        })

    # Merge CC actuals into category_totals
    for cat, total in cc_actuals.items():
        if cat not in category_totals:
            category_totals[cat] = {'planned': 0, 'actual': 0, 'annual': 0, 'target': budget_targets.get(cat, 0)}
        category_totals[cat]['actual'] = total

    # Use budget targets for expense total (covers both recurring and discretionary)
    budget_monthly_expenses = sum(budget_targets.get(cat, 0) for cat in budget_targets)
    # Subtract income-type targets (Savings, Investments count as expenses in the budget)
    # so we include all targets as the budgeted expense total
    budget_annual_expenses = budget_monthly_expenses * 12

    summary = {
        'monthly_income': monthly_income,
        'monthly_expenses': budget_monthly_expenses,
        'monthly_net': monthly_income - budget_monthly_expenses,
        'annual_income': annual_income,
        'annual_expenses': budget_annual_expenses,
        'annual_net': annual_income - budget_annual_expenses
    }

    # Join account names onto recurring items for display
    accounts = list(db.accounts.find())
    accounts_by_id = {str(a['_id']): a.get('name', 'Unknown') for a in accounts}
    for r in recurring:
        r.setdefault('account_id', None)
        r.setdefault('is_transfer', False)
        if r.get('is_transfer'):
            # Synthetic transfer items already have account_name set via notes
            pass
        elif r['account_id']:
            r['account_id'] = str(r['account_id'])
            r['account_name'] = accounts_by_id.get(r['account_id'], 'Unknown')
        else:
            r['account_name'] = None

    return render_template('budget.html',
                           recurring_transactions=recurring,
                           archived_items=archived_items,
                           categories=CATEGORIES,
                           expense_categories=CATEGORIES,
                           income_categories=INCOME_CATEGORIES,
                           summary=summary,
                           category_totals=category_totals,
                           budget_targets=budget_targets,
                           default_targets=default_targets,
                           month_has_overrides=month_has_overrides,
                           selected_month=selected_month,
                           available_months=available_months,
                           accounts=accounts)

@app.route('/budget/add', methods=['POST'])
def add_budget_item():
    data = request.form
    _create_recurring_item(
        description=data['description'],
        amount=float(data['amount']),
        frequency=data['frequency'],
        start_date=datetime.fromisoformat(data['start_date']),
        transaction_type=data['transaction_type'],
        end_date=datetime.fromisoformat(data['end_date']) if data.get('end_date') else None,
        day_of_month=data.get('day_of_month'),
        category=data.get('category'),
        notes=data.get('notes'),
        account_number=data.get('account_number'),
        auto_pay=data.get('auto_pay') == 'on',
        payment_method=data.get('payment_method'),
        account_id=data.get('account_id') or None
    )
    return redirect(url_for('budget_page'))

@app.route('/budget/edit/<item_id>', methods=['GET', 'POST'])
def edit_budget_item(item_id):
    if request.method == 'POST':
        account_id_val = request.form.get('account_id')
        db.recurring_transactions.update_one(
            {'_id': safe_objectid(item_id)},
            {'$set': {
                'description': request.form['description'],
                'amount': float(request.form['amount']),
                'frequency': request.form['frequency'],
                'start_date': datetime.fromisoformat(request.form['start_date']),
                'end_date': datetime.fromisoformat(request.form['end_date']) if request.form.get('end_date') else None,
                'transaction_type': request.form['transaction_type'],
                'day_of_month': request.form.get('day_of_month'),
                'category': request.form.get('category'),
                'notes': request.form.get('notes'),
                'account_number': request.form.get('account_number'),
                'auto_pay': request.form.get('auto_pay') == 'on',
                'payment_method': request.form.get('payment_method'),
                'account_id': safe_objectid(account_id_val) if account_id_val else None
            }}
        )
        return redirect(url_for('budget_page'))
    else:
        item = db.recurring_transactions.find_one({'_id': safe_objectid(item_id)})
        if item:
            item['_id'] = str(item['_id'])
            item['start_date'] = item['start_date'].strftime('%Y-%m-%d') if isinstance(item['start_date'], datetime) else item['start_date']
            if item.get('end_date'):
                item['end_date'] = item['end_date'].strftime('%Y-%m-%d') if isinstance(item['end_date'], datetime) else item['end_date']
            item.setdefault('account_id', None)
            if item['account_id']:
                item['account_id'] = str(item['account_id'])
            accounts = list(db.accounts.find())
            return render_template('edit_budget.html', item=item,
                                   categories=CATEGORIES,
                                   income_categories=INCOME_CATEGORIES,
                                   accounts=accounts)
        return redirect(url_for('budget_page'))

@app.route('/budget/delete/<item_id>', methods=['POST'])
def delete_budget_item(item_id):
    db.recurring_transactions.delete_one({'_id': safe_objectid(item_id)})
    if request.headers.get('Accept') == 'application/json':
        return jsonify({'message': 'Deleted'}), 200
    return redirect(url_for('budget_page'))

@app.route('/budget/targets', methods=['POST'])
def set_budget_targets():
    month = request.form.get('month')
    for key, value in request.form.items():
        if key.startswith('target_'):
            category = key.replace('target_', '')
            if value:
                db.monthly_budget_targets.update_one(
                    {'month': month, 'category': category},
                    {'$set': {'month': month, 'category': category, 'target': float(value)}},
                    upsert=True
                )
            else:
                # Empty value: remove override so it falls back to template default
                db.monthly_budget_targets.delete_one({'month': month, 'category': category})
    return redirect(url_for('budget_page', month=month))

@app.route('/budget/targets/template', methods=['POST'])
def set_default_targets():
    """Save targets to the default template (applies to all months without overrides)."""
    for key, value in request.form.items():
        if key.startswith('target_'):
            category = key.replace('target_', '')
            if value:
                db.budget_targets.update_one(
                    {'category': category},
                    {'$set': {'category': category, 'target': float(value)}},
                    upsert=True
                )
            else:
                db.budget_targets.delete_one({'category': category})
    return redirect(url_for('budget_page'))

@app.route('/budget/targets/reset', methods=['POST'])
def reset_month_targets():
    month = request.form.get('month')
    db.monthly_budget_targets.delete_many({'month': month})
    return redirect(url_for('budget_page', month=month))

@app.route('/budget/targets/seed', methods=['POST'])
def seed_targets_from_recurring():
    """Populate default template targets from recurring expense transactions."""
    recurring = list(db.recurring_transactions.find({'transaction_type': 'expense', **ACTIVE_RECURRING}))
    category_sums = {}
    for r in recurring:
        cat = r.get('category', 'Other')
        monthly_amt = calc_monthly_amount(r['amount'], r['frequency'], r.get('day_of_month'))
        category_sums[cat] = category_sums.get(cat, 0) + monthly_amt
    for category, total in category_sums.items():
        db.budget_targets.update_one(
            {'category': category},
            {'$set': {'category': category, 'target': round(total, 2)}},
            upsert=True
        )
    return redirect(url_for('budget_page'))

# ============ ACCOUNTS ============

@app.route('/accounts')
def accounts_page():
    accounts = list(db.accounts.find())

    # Calculate totals by category
    cash_total = 0  # checking + savings
    investment_total = 0  # ira, roth_ira, brokerage, 401k

    for a in accounts:
        a['_id'] = str(a['_id'])
        balance = a.get('balance', 0)
        if a['account_type'] in ['checking', 'savings']:
            cash_total += balance
        elif a['account_type'] in INVESTMENT_ACCOUNT_TYPES:
            investment_total += balance

    summary = {
        'cash_total': cash_total,
        'investment_total': investment_total,
        'total': cash_total + investment_total
    }

    return render_template('accounts.html',
                           accounts=accounts,
                           account_types=ACCOUNT_TYPES,
                           summary=summary)


def _create_account(**fields):
    """Insert an Account; a new primary account demotes any existing one.

    Shared by the Accounts page and the setup wizard. Returns the new id.
    """
    if fields.get('is_primary'):
        db.accounts.update_many({}, {'$set': {'is_primary': False}})
    account = Account(**fields)
    db.accounts.insert_one(account.__dict__)
    return account._id


def _create_recurring_item(**fields):
    """Insert a RecurringTransaction (budget item). Shared by Budget and setup."""
    item = RecurringTransaction(**fields)
    db.recurring_transactions.insert_one(item.__dict__)
    return item._id


def _create_debt(**fields):
    """Insert a Debt. Shared by the Debts page and the setup wizard."""
    debt = Debt(**fields)
    db.debts.insert_one(debt.__dict__)
    return debt._id


@app.route('/accounts/add', methods=['POST'])
def add_account():
    data = request.form
    is_primary = data.get('is_primary') == 'on'

    # Parse investment-specific fields
    estimated_return_rate = None
    if data.get('estimated_return_rate'):
        estimated_return_rate = float(data['estimated_return_rate'])
    balance_as_of_date = None
    if data.get('balance_as_of_date'):
        balance_as_of_date = datetime.fromisoformat(data['balance_as_of_date'])
    management_type = data.get('management_type') or None

    _create_account(
        name=data['name'],
        account_type=data['account_type'],
        institution=data['institution'],
        balance=float(data.get('balance', 0)),
        account_number=data.get('account_number'),
        notes=data.get('notes'),
        is_primary=is_primary,
        estimated_return_rate=estimated_return_rate,
        balance_as_of_date=balance_as_of_date,
        management_type=management_type,
    )
    return redirect(url_for('accounts_page'))


@app.route('/accounts/edit/<account_id>', methods=['GET', 'POST'])
def edit_account(account_id):
    if request.method == 'POST':
        is_primary = request.form.get('is_primary') == 'on'

        # If this is set as primary, unset any existing primary
        if is_primary:
            db.accounts.update_many(
                {'_id': {'$ne': safe_objectid(account_id)}},
                {'$set': {'is_primary': False}}
            )

        # Parse investment-specific fields
        estimated_return_rate = None
        if request.form.get('estimated_return_rate'):
            estimated_return_rate = float(request.form['estimated_return_rate'])
        balance_as_of_date = None
        if request.form.get('balance_as_of_date'):
            balance_as_of_date = datetime.fromisoformat(request.form['balance_as_of_date'])
        management_type = request.form.get('management_type') or None

        db.accounts.update_one(
            {'_id': safe_objectid(account_id)},
            {'$set': {
                'name': request.form['name'],
                'account_type': request.form['account_type'],
                'institution': request.form['institution'],
                'balance': float(request.form.get('balance', 0)),
                'account_number': request.form.get('account_number'),
                'notes': request.form.get('notes'),
                'is_primary': is_primary,
                'estimated_return_rate': estimated_return_rate,
                'balance_as_of_date': balance_as_of_date,
                'management_type': management_type,
            }}
        )
        return redirect(url_for('accounts_page'))
    else:
        account = db.accounts.find_one({'_id': safe_objectid(account_id)})
        if account:
            account['_id'] = str(account['_id'])
            return render_template('edit_account.html',
                                   account=account,
                                   account_types=ACCOUNT_TYPES)
        return redirect(url_for('accounts_page'))


@app.route('/accounts/delete/<account_id>', methods=['POST'])
def delete_account(account_id):
    db.accounts.delete_one({'_id': safe_objectid(account_id)})
    if request.headers.get('Accept') == 'application/json':
        return jsonify({'message': 'Deleted'}), 200
    return redirect(url_for('accounts_page'))


@app.route('/accounts/update-balance/<account_id>', methods=['POST'])
def update_account_balance(account_id):
    # Handle both JSON and form data
    if request.is_json:
        data = request.get_json()
    else:
        data = request.form
    new_balance = float(data.get('balance', 0))
    db.accounts.update_one(
        {'_id': safe_objectid(account_id)},
        {'$set': {'balance': new_balance, 'balance_as_of_date': datetime.utcnow()}}
    )
    if request.headers.get('Accept') == 'application/json':
        return jsonify({'message': 'Balance updated', 'balance': new_balance}), 200
    return redirect(url_for('accounts_page'))


@app.route('/accounts/<account_id>')
def account_detail(account_id):
    account = db.accounts.find_one({'_id': safe_objectid(account_id)})
    if not account:
        return redirect(url_for('accounts_page'))

    account['_id'] = str(account['_id'])
    # Persist this account as the sticky selection so ledger/history follow it
    session['account_filter'] = account['_id']
    account.setdefault('estimated_return_rate', None)
    account.setdefault('balance_as_of_date', None)
    account.setdefault('management_type', None)

    is_investment = account['account_type'] in INVESTMENT_ACCOUNT_TYPES

    # Auto-derive return rate for self-managed accounts with holdings
    if is_investment and account.get('management_type') == 'self_managed':
        derived = derive_account_return_rate(account['_id'])
        if derived is not None:
            account['estimated_return_rate'] = derived

    projection = None
    if is_investment and account.get('estimated_return_rate'):
        # Default 10 years; ?horizon=<months> allows longer projections
        try:
            horizon = int(request.args.get('horizon', 120))
        except (TypeError, ValueError):
            horizon = 120
        if not 1 <= horizon <= 600:
            horizon = 120
        projection = calculate_investment_projection(account, months=horizon)

    # Get recurring items for this account
    recurring_items = []
    for rt in db.recurring_transactions.find(ACTIVE_RECURRING):
        rt_account_id = str(rt['account_id']) if rt.get('account_id') else None
        if rt_account_id == account['_id']:
            recurring_items.append({
                'description': rt['description'],
                'amount': rt['amount'],
                'frequency': rt['frequency'],
                'type': rt['transaction_type'],
                'monthly': calc_monthly_amount(rt['amount'], rt['frequency'], rt.get('day_of_month'))
            })

    # Get recurring transfers involving this account
    for rt in db.recurring_transfers.find():
        from_id = str(rt.get('from_account_id', ''))
        to_id = str(rt.get('to_account_id', ''))
        if to_id == account['_id']:
            recurring_items.append({
                'description': rt.get('description', 'Transfer'),
                'amount': rt['amount'],
                'frequency': rt['frequency'],
                'type': 'income',
                'monthly': calc_monthly_amount(rt['amount'], rt['frequency'], rt.get('day_of_month'))
            })
        elif from_id == account['_id']:
            recurring_items.append({
                'description': rt.get('description', 'Transfer'),
                'amount': rt['amount'],
                'frequency': rt['frequency'],
                'type': 'expense',
                'monthly': calc_monthly_amount(rt['amount'], rt['frequency'], rt.get('day_of_month'))
            })

    # For cash accounts, compute yearly outlook
    yearly_outlook = []
    if not is_investment:
        start_balance = account.get('balance', 0)
        yearly_outlook = calculate_yearly_outlook(start_balance, account['_id'], max_months_back=1)

    return render_template('account_detail.html',
                           account=account,
                           account_types=ACCOUNT_TYPES,
                           is_investment=is_investment,
                           projection=projection,
                           recurring_items=recurring_items,
                           yearly_outlook=yearly_outlook)


# ============ PORTFOLIO / HOLDINGS ============

@app.route('/portfolio')
def portfolio_page():
    account_filter = request.args.get('account', 'all')

    # Get all self-managed investment accounts
    investment_accounts = list(db.accounts.find({
        'account_type': {'$in': INVESTMENT_ACCOUNT_TYPES},
        'management_type': 'self_managed'
    }))
    account_lookup = {}
    for a in investment_accounts:
        a['_id'] = str(a['_id'])
        account_lookup[a['_id']] = a.get('name', 'Unknown')

    # Also include managed accounts for the filter (but they won't have holdings)
    all_investment = list(db.accounts.find({
        'account_type': {'$in': INVESTMENT_ACCOUNT_TYPES}
    }))
    for a in all_investment:
        a['_id'] = str(a['_id'])
        if a['_id'] not in account_lookup:
            account_lookup[a['_id']] = a.get('name', 'Unknown')

    # Get holdings (filtered by account if selected)
    if account_filter and account_filter != 'all':
        holdings = list(db.holdings.find({'account_id': safe_objectid(account_filter)}))
    else:
        holdings = list(db.holdings.find())
    symbols = list(set(h['symbol'] for h in holdings))
    live_data = get_live_data(symbols) if symbols else {}

    # Enrich holdings with live data
    portfolio_value = 0
    annual_dividends = 0
    for h in holdings:
        h['_id'] = str(h['_id'])
        h['account_id'] = str(h.get('account_id', ''))
        h['account_name'] = account_lookup.get(h['account_id'], 'Unknown')
        symbol = h['symbol']
        data = live_data.get(symbol, {'price': 0, 'dividend': 0, 'div_months': [], 'valid': False})
        h['price'] = data['price']
        h['value'] = data['price'] * h['quantity']
        h['annual_dividend'] = data['dividend'] * h['quantity']
        h['monthly_dividend'] = h['annual_dividend'] / 12 if h['annual_dividend'] else 0
        h['div_months'] = data.get('div_months', [])
        h['valid'] = data.get('valid', True)
        portfolio_value += h['value']
        annual_dividends += h['annual_dividend']

    # Auto-derive return rates for self-managed accounts
    seen_accounts = set()
    for h in holdings:
        if h['account_id'] and h['account_id'] not in seen_accounts:
            seen_accounts.add(h['account_id'])
            acc = next((a for a in investment_accounts if a['_id'] == h['account_id']), None)
            if acc and acc.get('management_type') == 'self_managed':
                derive_account_return_rate(h['account_id'])

    # Auto-populate dividends from API data
    holdings_by_symbol = {}
    for h in holdings:
        holdings_by_symbol.setdefault(h['symbol'], []).append(h)

    for symbol, data in live_data.items():
        for div_record in data.get('div_history', []):
            div_date = div_record['date']
            # Check if we already have this dividend recorded
            existing = db.dividends.find_one({
                'symbol': symbol,
                'date': {'$gte': datetime(div_date.year, div_date.month, div_date.day),
                         '$lt': datetime(div_date.year, div_date.month, div_date.day) + timedelta(days=1)},
                'source': 'api'
            })
            if not existing:
                # Calculate total dividend for all holdings of this symbol
                total_qty = sum(h['quantity'] for h in holdings_by_symbol.get(symbol, []))
                if total_qty > 0:
                    total_amount = div_record['amount_per_share'] * total_qty
                    db.dividends.insert_one({
                        '_id': ObjectId(),
                        'symbol': symbol,
                        'amount': total_amount,
                        'date': datetime(div_date.year, div_date.month, div_date.day),
                        'source': 'api'
                    })

    # Get dividend history
    dividend_history = list(db.dividends.find().sort('date', -1).limit(50))
    for d in dividend_history:
        d['_id'] = str(d['_id'])
        if isinstance(d['date'], datetime):
            d['date_str'] = d['date'].strftime('%Y-%m-%d')
        else:
            d['date_str'] = str(d['date'])

    # Compute yearly/monthly dividend totals from actual records
    now = datetime.now()
    year_start = datetime(now.year, 1, 1)
    ytd_dividends = sum(d['amount'] for d in dividend_history
                        if isinstance(d.get('date'), datetime) and d['date'] >= year_start)

    # Build monthly dividend calendar
    month_names = ['', 'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                   'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
    dividend_calendar = []
    for m in range(1, 13):
        month_data = {'month': m, 'name': month_names[m], 'total': 0, 'holdings': []}
        for h in holdings:
            if m in h.get('div_months', []):
                div_months_count = len(h['div_months']) if h['div_months'] else 1
                payment = h['annual_dividend'] / div_months_count
                month_data['total'] += payment
                month_data['holdings'].append({'symbol': h['symbol'], 'amount': payment})
        dividend_calendar.append(month_data)

    return render_template('portfolio.html',
                           holdings=sorted(holdings, key=lambda x: x['value'], reverse=True),
                           investment_accounts=investment_accounts,
                           all_investment=all_investment,
                           account_lookup=account_lookup,
                           account_filter=account_filter,
                           portfolio_value=portfolio_value,
                           annual_dividends=annual_dividends,
                           monthly_dividends=annual_dividends / 12 if annual_dividends else 0,
                           dividend_calendar=dividend_calendar,
                           dividend_history=dividend_history,
                           ytd_dividends=ytd_dividends)


@app.route('/portfolio/add', methods=['POST'])
def add_holding():
    data = request.form
    symbol = validate_symbol(data.get('symbol', '').upper().strip())
    if not symbol:
        return redirect(url_for('portfolio_page'))

    quantity = float(data.get('quantity', 0))
    cost_basis = float(data['cost_basis']) if data.get('cost_basis') else None
    account_id = data.get('account_id') or None

    # Check if holding already exists for this symbol + account
    existing = db.holdings.find_one({'symbol': symbol, 'account_id': safe_objectid(account_id) if account_id else None})
    if existing:
        # Update quantity
        db.holdings.update_one(
            {'_id': existing['_id']},
            {'$inc': {'quantity': quantity}}
        )
    else:
        new_holding = Holding(
            account_id=account_id,
            symbol=symbol,
            quantity=quantity,
            cost_basis=cost_basis,
            date_added=datetime.utcnow(),
            notes=data.get('notes'),
        )
        db.holdings.insert_one(new_holding.__dict__)

    return redirect(url_for('portfolio_page'))


@app.route('/portfolio/update/<holding_id>', methods=['POST'])
def update_holding(holding_id):
    data = request.form if request.form else request.get_json()
    update_fields = {}
    if 'quantity' in data:
        update_fields['quantity'] = float(data['quantity'])
    if 'cost_basis' in data and data['cost_basis']:
        update_fields['cost_basis'] = float(data['cost_basis'])
    if update_fields:
        db.holdings.update_one({'_id': safe_objectid(holding_id)}, {'$set': update_fields})
    if request.headers.get('Accept') == 'application/json':
        return jsonify({'status': 'ok'})
    return redirect(url_for('portfolio_page'))


@app.route('/portfolio/delete/<holding_id>', methods=['POST'])
def delete_holding(holding_id):
    db.holdings.delete_one({'_id': safe_objectid(holding_id)})
    if request.headers.get('Accept') == 'application/json':
        return jsonify({'status': 'ok'})
    return redirect(url_for('portfolio_page'))


@app.route('/portfolio/delete-all', methods=['POST'])
def delete_all_holdings():
    account_id = request.form.get('account_id') or None
    if account_id:
        db.holdings.delete_many({'account_id': safe_objectid(account_id)})
    else:
        db.holdings.delete_many({})
    STOCK_CACHE['timestamp'] = 0
    return redirect(url_for('portfolio_page'))


@app.route('/portfolio/import-csv', methods=['POST'])
def import_holdings_csv():
    if 'file' not in request.files:
        return redirect(url_for('portfolio_page'))

    file = request.files['file']
    if not file.filename.endswith('.csv'):
        return redirect(url_for('portfolio_page'))

    account_id = request.form.get('account_id') or None

    try:
        content = file.read().decode('utf-8-sig')
        lines = content.strip().splitlines()

        # Find the header row — look for a line containing "Symbol" as a column
        header_idx = 0
        for i, line in enumerate(lines):
            if 'symbol' in line.lower() and ',' in line:
                header_idx = i
                break

        csv_content = '\n'.join(lines[header_idx:])
        reader = csv.DictReader(io.StringIO(csv_content))

        def parse_dollar(val):
            """Strip $, commas, quotes from dollar values."""
            if not val:
                return None
            val = val.strip().replace('$', '').replace(',', '').replace('"', '')
            if val in ('--', '-', ''):
                return None
            try:
                return float(val)
            except ValueError:
                return None

        imported = 0
        for row in reader:
            symbol = None
            quantity = None
            cost_basis = None

            for key in row:
                if key is None:
                    continue
                key_lower = key.lower().strip()
                val = (row[key] or '').strip()

                # Match symbol column
                if key_lower in ['symbol', 'ticker', 'stock']:
                    symbol = validate_symbol(val.upper())

                # Match quantity column — support parenthesized names like "Qty (Quantity)"
                elif any(q in key_lower for q in ['quantity', 'qty', 'shares']):
                    parsed = parse_dollar(val)
                    if parsed and parsed > 0:
                        quantity = parsed

                # Match cost basis column
                elif 'cost' in key_lower and 'basis' in key_lower:
                    cost_basis = parse_dollar(val)

            # Skip non-stock rows (cash, totals, etc.)
            if not symbol or not quantity:
                continue

            existing = db.holdings.find_one({
                'symbol': symbol,
                'account_id': safe_objectid(account_id) if account_id else None
            })
            if existing:
                db.holdings.update_one(
                    {'_id': existing['_id']},
                    {'$set': {'quantity': quantity, 'cost_basis': cost_basis or existing.get('cost_basis')}}
                )
            else:
                new_holding = Holding(
                    account_id=account_id,
                    symbol=symbol,
                    quantity=quantity,
                    cost_basis=cost_basis,
                    date_added=datetime.utcnow(),
                )
                db.holdings.insert_one(new_holding.__dict__)
            imported += 1

        STOCK_CACHE['timestamp'] = 0  # Clear cache
    except Exception as e:
        logger.error(f'Holdings CSV import error: {e}')

    return redirect(url_for('portfolio_page'))


@app.route('/portfolio/dividend/add', methods=['POST'])
def add_dividend():
    data = request.form
    symbol = validate_symbol(data.get('symbol', '').upper().strip())
    if not symbol:
        return redirect(url_for('portfolio_page'))
    amount = float(data.get('amount', 0))
    div_date = datetime.fromisoformat(data.get('date', datetime.now().strftime('%Y-%m-%d')))
    db.dividends.insert_one({
        '_id': ObjectId(),
        'symbol': symbol,
        'amount': amount,
        'date': div_date,
        'source': 'manual'
    })
    return redirect(url_for('portfolio_page'))


@app.route('/portfolio/dividend/delete/<dividend_id>', methods=['POST'])
def delete_dividend(dividend_id):
    db.dividends.delete_one({'_id': safe_objectid(dividend_id)})
    if request.headers.get('Accept') == 'application/json':
        return jsonify({'status': 'ok'})
    return redirect(url_for('portfolio_page'))


@app.route('/api/stock/<symbol>')
def api_stock_data(symbol):
    """API endpoint to get live stock data for a symbol."""
    symbol = validate_symbol(symbol.upper())
    if not symbol:
        return jsonify({'error': 'Invalid symbol'}), 400
    data = get_live_data([symbol])
    return jsonify(data.get(symbol, {}))


# ============ TRANSFERS ============

@app.route('/transfers')
def transfers_page():
    accounts = list(db.accounts.find())
    for a in accounts:
        a['_id'] = str(a['_id'])

    # Build account lookup for display names
    accounts_lookup = {str(a['_id']): a.get('name', 'Unknown') for a in accounts}

    # Get transfer history
    transfers = list(db.transfers.find().sort('date', -1).limit(50))
    for t in transfers:
        t['_id'] = str(t['_id'])
        t['from_account_id'] = str(t.get('from_account_id', ''))
        t['to_account_id'] = str(t.get('to_account_id', ''))
        t['from_account_name'] = accounts_lookup.get(t['from_account_id'], 'Unknown')
        t['to_account_name'] = accounts_lookup.get(t['to_account_id'], 'Unknown')
        if isinstance(t['date'], datetime):
            t['date'] = t['date'].strftime('%Y-%m-%d')

    # Get recurring transfers
    recurring_transfers = list(db.recurring_transfers.find())
    for rt in recurring_transfers:
        rt['_id'] = str(rt['_id'])
        rt['from_account_id'] = str(rt.get('from_account_id', ''))
        rt['to_account_id'] = str(rt.get('to_account_id', ''))
        rt['from_account_name'] = accounts_lookup.get(rt['from_account_id'], 'Unknown')
        rt['to_account_name'] = accounts_lookup.get(rt['to_account_id'], 'Unknown')
        if isinstance(rt.get('start_date'), datetime):
            rt['start_date'] = rt['start_date'].strftime('%Y-%m-%d')
        if rt.get('end_date') and isinstance(rt['end_date'], datetime):
            rt['end_date'] = rt['end_date'].strftime('%Y-%m-%d')
        rt['monthly_amount'] = calc_monthly_amount(rt['amount'], rt['frequency'], rt.get('day_of_month'))

    return render_template('transfers.html',
                           accounts=accounts,
                           transfers=transfers,
                           recurring_transfers=recurring_transfers,
                           today=date.today().isoformat())


@app.route('/transfers/add', methods=['POST'])
def add_transfer():
    data = request.form
    from_account_id = data['from_account_id']
    to_account_id = data['to_account_id']
    amount = float(data['amount'])
    transfer_date = datetime.fromisoformat(data['date'])
    description = data.get('description', '').strip() or 'Account Transfer'
    notes = data.get('notes', '').strip() or None

    if from_account_id == to_account_id:
        # Redirect back with error - can't transfer to the same account
        return redirect(url_for('ledger_page'))

    if amount <= 0:
        return redirect(url_for('ledger_page'))

    # Create the transfer record
    new_transfer = Transfer(
        from_account_id=from_account_id,
        to_account_id=to_account_id,
        amount=amount,
        date=transfer_date,
        description=description,
        notes=notes
    )
    db.transfers.insert_one(new_transfer.__dict__)

    # Create linked transactions for the ledger
    # (these handle the balance accounting via the index page calculation)
    expense_transaction = Transaction(
        description=f"Transfer to: {description}",
        amount=amount,
        date=transfer_date,
        transaction_type='expense',
        account_id=from_account_id
    )
    income_transaction = Transaction(
        description=f"Transfer from: {description}",
        amount=amount,
        date=transfer_date,
        transaction_type='income',
        account_id=to_account_id
    )
    db.transactions.insert_one(expense_transaction.__dict__)
    db.transactions.insert_one(income_transaction.__dict__)

    return redirect(url_for('ledger_page'))


@app.route('/transfers/delete/<transfer_id>', methods=['POST'])
def delete_transfer(transfer_id):
    transfer = db.transfers.find_one({'_id': safe_objectid(transfer_id)})
    if transfer:
        # Delete the linked ledger transactions
        # The Transaction model stores account_id as an ObjectId; match the
        # string form too in case an older row was written that way.
        description = transfer.get('description', 'Account Transfer')
        transfer_date = transfer['date']
        from_id, to_id = transfer['from_account_id'], transfer['to_account_id']
        db.transactions.delete_one({
            'description': f"Transfer to: {description}",
            'date': transfer_date,
            'account_id': {'$in': [from_id, str(from_id)]}
        })
        db.transactions.delete_one({
            'description': f"Transfer from: {description}",
            'date': transfer_date,
            'account_id': {'$in': [to_id, str(to_id)]}
        })
        db.transfers.delete_one({'_id': safe_objectid(transfer_id)})

    if request.headers.get('Accept') == 'application/json':
        return jsonify({'message': 'Transfer deleted and reversed'}), 200
    return redirect(url_for('transfers_page'))


# ============ RECURRING TRANSFERS ============

@app.route('/transfers/recurring/add', methods=['POST'])
def add_recurring_transfer():
    data = request.form
    from_account_id = data['from_account_id']
    to_account_id = data['to_account_id']
    amount = float(data['amount'])

    if from_account_id == to_account_id:
        return redirect(url_for('transfers_page'))
    if amount <= 0:
        return redirect(url_for('transfers_page'))

    new_rt = RecurringTransfer(
        from_account_id=from_account_id,
        to_account_id=to_account_id,
        amount=amount,
        frequency=data['frequency'],
        start_date=datetime.fromisoformat(data['start_date']),
        day_of_month=data.get('day_of_month'),
        end_date=datetime.fromisoformat(data['end_date']) if data.get('end_date') else None,
        description=data.get('description', '').strip() or 'Recurring Transfer',
        notes=data.get('notes', '').strip() or None
    )
    db.recurring_transfers.insert_one(new_rt.__dict__)
    return redirect(url_for('transfers_page'))


@app.route('/transfers/recurring/edit/<rt_id>', methods=['GET', 'POST'])
def edit_recurring_transfer(rt_id):
    if request.method == 'POST':
        data = request.form
        from_account_id = data['from_account_id']
        to_account_id = data['to_account_id']
        amount = float(data['amount'])

        if from_account_id == to_account_id:
            return redirect(url_for('transfers_page'))
        if amount <= 0:
            return redirect(url_for('transfers_page'))

        db.recurring_transfers.update_one(
            {'_id': safe_objectid(rt_id)},
            {'$set': {
                'from_account_id': safe_objectid(from_account_id),
                'to_account_id': safe_objectid(to_account_id),
                'amount': amount,
                'frequency': data['frequency'],
                'start_date': datetime.fromisoformat(data['start_date']),
                'day_of_month': data.get('day_of_month'),
                'end_date': datetime.fromisoformat(data['end_date']) if data.get('end_date') else None,
                'description': data.get('description', '').strip() or 'Recurring Transfer',
                'notes': data.get('notes', '').strip() or None
            }}
        )
        return redirect(url_for('transfers_page'))
    else:
        rt = db.recurring_transfers.find_one({'_id': safe_objectid(rt_id)})
        if rt:
            rt['_id'] = str(rt['_id'])
            rt['from_account_id'] = str(rt.get('from_account_id', ''))
            rt['to_account_id'] = str(rt.get('to_account_id', ''))
            if isinstance(rt.get('start_date'), datetime):
                rt['start_date'] = rt['start_date'].strftime('%Y-%m-%d')
            if rt.get('end_date') and isinstance(rt['end_date'], datetime):
                rt['end_date'] = rt['end_date'].strftime('%Y-%m-%d')
            accounts = list(db.accounts.find())
            for a in accounts:
                a['_id'] = str(a['_id'])
            return render_template('edit_recurring_transfer.html',
                                   recurring_transfer=rt,
                                   accounts=accounts)
        return redirect(url_for('transfers_page'))


@app.route('/transfers/recurring/delete/<rt_id>', methods=['POST'])
def delete_recurring_transfer(rt_id):
    db.recurring_transfers.delete_one({'_id': safe_objectid(rt_id)})
    if request.headers.get('Accept') == 'application/json':
        return jsonify({'message': 'Recurring transfer deleted'}), 200
    return redirect(url_for('transfers_page'))


# ============ ASSETS ============

@app.route('/assets')
def assets_page():
    assets = list(db.assets.find())

    total_value = 0
    total_liabilities = 0

    for a in assets:
        a['_id'] = str(a['_id'])
        if a.get('purchase_date') and isinstance(a['purchase_date'], datetime):
            a['purchase_date'] = a['purchase_date'].strftime('%Y-%m-%d')
        a.setdefault('payment_start_date', None)

        value = a.get('estimated_value', 0) or 0
        liability = a.get('liability_balance', 0) or 0
        a['equity'] = value - liability

        total_value += value
        total_liabilities += liability

    summary = {
        'total_value': total_value,
        'total_liabilities': total_liabilities,
        'net_worth': total_value - total_liabilities
    }

    return render_template('assets.html',
                           assets=assets,
                           asset_types=ASSET_TYPES,
                           liability_types=LIABILITY_TYPES,
                           summary=summary)


@app.route('/assets/add', methods=['POST'])
def add_asset():
    data = request.form
    new_asset = Asset(
        name=data['name'],
        asset_type=data['asset_type'],
        estimated_value=float(data.get('estimated_value', 0)),
        purchase_date=datetime.fromisoformat(data['purchase_date']) if data.get('purchase_date') else None,
        purchase_price=float(data['purchase_price']) if data.get('purchase_price') else None,
        liability_balance=float(data['liability_balance']) if data.get('liability_balance') else None,
        liability_type=data.get('liability_type') if data.get('liability_balance') else None,
        address=data.get('address'),
        notes=data.get('notes'),
        interest_rate=float(data['interest_rate']) if data.get('interest_rate') else None,
        loan_term_months=int(data['loan_term_months']) if data.get('loan_term_months') else None,
        monthly_payment=float(data['monthly_payment']) if data.get('monthly_payment') else None,
        payment_start_date=datetime.fromisoformat(data['payment_start_date']) if data.get('payment_start_date') else None,
        payment_frequency=data.get('payment_frequency') or 'monthly',
        property_tax_annual=float(data['property_tax_annual']) if data.get('property_tax_annual') else None,
        homeowners_insurance_annual=float(data['homeowners_insurance_annual']) if data.get('homeowners_insurance_annual') else None,
        pmi_monthly=float(data['pmi_monthly']) if data.get('pmi_monthly') else None,
        hoa_monthly=float(data['hoa_monthly']) if data.get('hoa_monthly') else None,
        escrow_balance=float(data['escrow_balance']) if data.get('escrow_balance') else None,
        payment_match=_parse_payment_match(data.get('payment_match'))
    )
    db.assets.insert_one(new_asset.__dict__)
    return redirect(url_for('assets_page'))


@app.route('/assets/edit/<asset_id>', methods=['GET', 'POST'])
def edit_asset(asset_id):
    if request.method == 'POST':
        data = request.form
        db.assets.update_one(
            {'_id': safe_objectid(asset_id)},
            {'$set': {
                'name': data['name'],
                'asset_type': data['asset_type'],
                'estimated_value': float(data.get('estimated_value', 0)),
                'purchase_date': datetime.fromisoformat(data['purchase_date']) if data.get('purchase_date') else None,
                'purchase_price': float(data['purchase_price']) if data.get('purchase_price') else None,
                'liability_balance': float(data['liability_balance']) if data.get('liability_balance') else None,
                'liability_type': data.get('liability_type') if data.get('liability_balance') else None,
                'address': data.get('address'),
                'notes': data.get('notes'),
                'interest_rate': float(data['interest_rate']) if data.get('interest_rate') else None,
                'loan_term_months': int(data['loan_term_months']) if data.get('loan_term_months') else None,
                'monthly_payment': float(data['monthly_payment']) if data.get('monthly_payment') else None,
                'payment_start_date': datetime.fromisoformat(data['payment_start_date']) if data.get('payment_start_date') else None,
                'payment_frequency': data.get('payment_frequency') or 'monthly',
                'property_tax_annual': float(data['property_tax_annual']) if data.get('property_tax_annual') else None,
                'homeowners_insurance_annual': float(data['homeowners_insurance_annual']) if data.get('homeowners_insurance_annual') else None,
                'pmi_monthly': float(data['pmi_monthly']) if data.get('pmi_monthly') else None,
                'hoa_monthly': float(data['hoa_monthly']) if data.get('hoa_monthly') else None,
                'escrow_balance': float(data['escrow_balance']) if data.get('escrow_balance') else None,
                'payment_match': _parse_payment_match(data.get('payment_match'))
            }}
        )
        return redirect(url_for('assets_page'))
    else:
        asset = db.assets.find_one({'_id': safe_objectid(asset_id)})
        if asset:
            asset['_id'] = str(asset['_id'])
            if asset.get('purchase_date') and isinstance(asset['purchase_date'], datetime):
                asset['purchase_date'] = asset['purchase_date'].strftime('%Y-%m-%d')
            if asset.get('payment_start_date'):
                psd = asset['payment_start_date']
                if isinstance(psd, datetime):
                    asset['payment_start_date'] = psd.strftime('%Y-%m-%d')
                elif isinstance(psd, date):
                    asset['payment_start_date'] = psd.strftime('%Y-%m-%d')
            # Default escrow fields for older records missing them
            asset.setdefault('property_tax_annual', None)
            asset.setdefault('homeowners_insurance_annual', None)
            asset.setdefault('pmi_monthly', None)
            asset.setdefault('hoa_monthly', None)
            asset.setdefault('escrow_balance', None)
            asset.setdefault('payment_match', [])
            return render_template('edit_asset.html',
                                   asset=asset,
                                   asset_types=ASSET_TYPES,
                                   liability_types=LIABILITY_TYPES)
        return redirect(url_for('assets_page'))


@app.route('/assets/delete/<asset_id>', methods=['POST'])
def delete_asset(asset_id):
    aid = safe_objectid(asset_id)
    db.assets.delete_one({'_id': aid})
    if aid:
        db.transactions.update_many({'asset_liability_id': aid}, {'$unset': {'asset_liability_id': ''}})
    if request.headers.get('Accept') == 'application/json':
        return jsonify({'message': 'Deleted'}), 200
    return redirect(url_for('assets_page'))


@app.route('/export')
def export_data():
    """Full JSON backup of every collection.

    Enumerates collections dynamically so data from new features is always
    included automatically — no need to update this list as the app grows.
    Serialized as Extended JSON (bson.json_util) so ObjectId/date types are
    preserved exactly and round-trip through /import.
    """
    all_data = {
        "_meta": {
            "version": 3,
            "exported_at": datetime.utcnow().isoformat()
        }
    }
    for name in sorted(db.list_collection_names()):
        all_data[name] = list(db[name].find())
    return jsonify(json.loads(dumps(all_data)))

@app.route('/export/csv/transactions')
def export_transactions_csv():
    transactions = list(db.transactions.find())

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['date', 'description', 'amount', 'transaction_type', 'account_id'])

    for t in transactions:
        date_val = t['date'].strftime('%Y-%m-%d') if isinstance(t['date'], datetime) else t['date']
        writer.writerow([date_val, t['description'], t['amount'], t['transaction_type'], str(t.get('account_id', ''))])

    output.seek(0)
    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename=transactions.csv'}
    )

@app.route('/export/csv/recurring')
def export_recurring_csv():
    recurring = list(db.recurring_transactions.find())

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['description', 'amount', 'frequency', 'start_date', 'end_date', 'transaction_type'])

    for r in recurring:
        start_date = r['start_date'].strftime('%Y-%m-%d') if isinstance(r['start_date'], datetime) else r['start_date']
        end_date = ''
        if r.get('end_date'):
            end_date = r['end_date'].strftime('%Y-%m-%d') if isinstance(r['end_date'], datetime) else r['end_date']
        writer.writerow([r['description'], r['amount'], r['frequency'], start_date, end_date, r['transaction_type']])

    output.seek(0)
    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename=recurring_transactions.csv'}
    )

@app.route('/export/csv/holdings')
def export_holdings_csv():
    holdings = list(db.holdings.find())
    accounts = {str(a['_id']): a.get('name', '') for a in db.accounts.find()}

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['symbol', 'quantity', 'cost_basis', 'account', 'date_added'])

    for h in holdings:
        date_val = h.get('date_added', '')
        if isinstance(date_val, datetime):
            date_val = date_val.strftime('%Y-%m-%d')
        account_name = accounts.get(str(h.get('account_id', '')), '')
        writer.writerow([h['symbol'], h['quantity'], h.get('cost_basis', ''), account_name, date_val])

    output.seek(0)
    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename=holdings.csv'}
    )


@app.route('/export/csv/dividends')
def export_dividends_csv():
    dividends = list(db.dividends.find().sort('date', -1))

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['symbol', 'amount', 'date', 'source'])

    for d in dividends:
        date_val = d.get('date', '')
        if isinstance(date_val, datetime):
            date_val = date_val.strftime('%Y-%m-%d')
        writer.writerow([d['symbol'], d['amount'], date_val, d.get('source', '')])

    output.seek(0)
    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename=dividends.csv'}
    )


@app.route('/import', methods=['POST'])
def import_data():
    """Restore a JSON backup produced by /export.

    Every collection present in the file is dropped and replaced. Parsing is
    done with Extended JSON (bson.json_util.loads) so ObjectId and date fields
    are reconstructed exactly as stored — and iterating the file dynamically
    means collections from new features are restored without code changes.
    """
    if 'file' not in request.files:
        return "No file part", 400
    file = request.files['file']
    if file.filename == '':
        return "No selected file", 400

    # Parse Extended JSON -> native BSON types ($oid -> ObjectId, $date -> datetime).
    # Whatever type a field had at export time round-trips back unchanged, so
    # collections that intentionally store dates as strings stay strings.
    try:
        raw = file.read()
        if isinstance(raw, bytes):
            raw = raw.decode('utf-8')
        data = loads(raw)
    except Exception as e:
        return f"Invalid backup file: {e}", 400

    if not isinstance(data, dict):
        return "Invalid backup file: expected a JSON object of collections", 400

    data.pop('_meta', None)

    for name, docs in data.items():
        if not isinstance(docs, list):
            continue
        db[name].drop()
        if docs:
            db[name].insert_many(docs)

    return redirect(url_for('ledger_page'))


# ============ ADMIN PAGE ============

@app.route('/admin')
def admin_page():
    stats = {
        'transactions': db.transactions.count_documents({}),
        'recurring_transactions': db.recurring_transactions.count_documents({}),
        'accounts': db.accounts.count_documents({}),
        'assets': db.assets.count_documents({}),
        'savings_goals': db.savings_goals.count_documents({}),
        'networth_snapshots': db.networth_snapshots.count_documents({}),
        'transfers': db.transfers.count_documents({}),
        'recurring_transfers': db.recurring_transfers.count_documents({}),
        'debts': db.debts.count_documents({}),
        'cc_transactions': db.cc_transactions.count_documents({}),
        'holdings': db.holdings.count_documents({}),
        'dividends': db.dividends.count_documents({})
    }

    # Data health checks
    health_issues = []
    valid_account_ids = set(str(a['_id']) for a in db.accounts.find({}, {'_id': 1}))
    for h in db.holdings.find():
        aid = str(h.get('account_id', ''))
        if aid and aid not in valid_account_ids:
            health_issues.append(f"Holding '{h.get('symbol', '?')}' references missing account {aid}")

    valid_holding_ids = set(str(h['_id']) for h in db.holdings.find({}, {'_id': 1}))
    for d in db.dividends.find():
        hid = str(d.get('holding_id', ''))
        if hid and hid not in valid_holding_ids:
            health_issues.append(f"Dividend for '{d.get('symbol', '?')}' references missing holding {hid}")

    orphan_recurring = []
    for rt in db.recurring_transactions.find():
        aid = str(rt.get('account_id', ''))
        if aid and aid not in valid_account_ids:
            orphan_recurring.append(f"Budget item '{rt.get('description', '?')}' references missing account {aid}")
    health_issues.extend(orphan_recurring)

    return render_template('admin.html', stats=stats, health_issues=health_issues)


@app.route('/import/csv/transactions', methods=['POST'])
def import_transactions_csv():
    if 'file' not in request.files:
        return jsonify({'error': 'No file part'}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No selected file'}), 400

    clear_form = request.form.get('clear')
    clear_arg = request.args.get('clear', 'false')
    clear = (clear_form or clear_arg).lower() == 'true'
    if clear:
        db.transactions.drop()

    stream = io.StringIO(file.stream.read().decode('utf-8-sig'))
    reader = csv.DictReader(stream)
    count = 0
    match_index = _payment_match_index()

    for row in reader:
        transaction = {
            'date': datetime.fromisoformat(row['date'].strip()),
            'description': row['description'].strip(),
            'amount': float(row['amount'].strip()),
            'transaction_type': row['transaction_type'].strip()
        }
        result = db.transactions.insert_one(transaction)
        _auto_link_transaction(result.inserted_id, transaction['description'], match_index)
        count += 1

    if request.headers.get('Accept') == 'application/json':
        return jsonify({'message': f'Imported {count} transactions'}), 200
    return redirect(url_for('ledger_page'))

@app.route('/import/csv/recurring', methods=['POST'])
def import_recurring_csv():
    if 'file' not in request.files:
        return jsonify({'error': 'No file part'}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No selected file'}), 400

    clear_form = request.form.get('clear')
    clear_arg = request.args.get('clear', 'false')
    clear = (clear_form or clear_arg).lower() == 'true'
    if clear:
        db.recurring_transactions.drop()

    stream = io.StringIO(file.stream.read().decode('utf-8-sig'))
    reader = csv.DictReader(stream)
    count = 0

    for row in reader:
        end_date_str = row.get('end_date', '').strip()
        recurring = {
            'description': row['description'].strip(),
            'amount': float(row['amount'].strip()),
            'frequency': row['frequency'].strip(),
            'start_date': datetime.fromisoformat(row['start_date'].strip()),
            'end_date': datetime.fromisoformat(end_date_str) if end_date_str else None,
            'transaction_type': row['transaction_type'].strip()
        }
        db.recurring_transactions.insert_one(recurring)
        count += 1

    if request.headers.get('Accept') == 'application/json':
        return jsonify({'message': f'Imported {count} recurring transactions'}), 200
    return redirect(url_for('ledger_page'))


# ============ DEBTS ============

def _decorate_payment_plan(plan):
    """Add computed fields: paid, remaining, payments_made, num_payments, end_date_estimate."""
    pid = plan['_id']
    monthly = plan.get('monthly_payment') or 0
    original = plan.get('original_amount') or 0

    linked = list(db.cc_transactions.find({'payment_plan_id': pid}))
    paid = sum((t.get('amount') or 0) for t in linked)
    payments_made = len(linked)

    import math
    num_payments = math.ceil(original / monthly) if monthly > 0 else 0
    remaining = max(0.0, original - paid)
    payments_remaining = max(0, num_payments - payments_made)

    end_date_estimate = None
    start = plan.get('start_date')
    if start and num_payments > 0:
        if isinstance(start, datetime):
            start_d = start.date()
        elif isinstance(start, date):
            start_d = start
        else:
            start_d = None
        if start_d:
            end_date_estimate = start_d + relativedelta(months=max(0, num_payments - 1))

    plan['_id'] = str(pid)
    plan['paid'] = paid
    plan['remaining'] = remaining
    plan['payments_made'] = payments_made
    plan['num_payments'] = num_payments
    plan['payments_remaining'] = payments_remaining
    plan['end_date_estimate'] = end_date_estimate
    plan['progress_pct'] = (paid / original * 100) if original > 0 else 0
    return plan


# --- Linking ledger transactions to loans (debts + asset liabilities) -------
#
# A payment is an ordinary ledger transaction tagged with `debt_id` (standalone
# debt) or `asset_liability_id` (loan attached to an asset). The entered balance
# is the *starting* balance; the current balance is found by walking the linked
# payments in date order and reducing by the *principal* portion of each one
# (payment minus accrued interest minus escrow). This keeps interest-bearing
# loans like the mortgage accurate instead of paying them off far too fast.

# Optional seed rules so existing payments link without hand-entry. Keyed by
# the loan's name; only applied to records that have no payment_match yet.
# Example: {'Car Loan': ['Car Payment'], 'Mortgage': ['Mortgage Payment']}.
# Empty by default -- set payment_match per loan from the debt/asset page.
DEFAULT_PAYMENT_MATCHES = {}


def _as_date(value):
    """Coerce a stored date (datetime/date/ISO string) to a date for arithmetic."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value).date()
        except ValueError:
            return None
    return None


def _amortize(starting_balance, annual_rate, monthly_escrow, payments, frequency='monthly'):
    """Walk linked payments in date order, splitting each into interest, escrow,
    and principal. Interest accrues on elapsed days since the previous payment;
    escrow is prorated the same way. Returns totals plus a per-payment breakdown."""
    rate = (annual_rate or 0) / 100.0
    escrow_m = monthly_escrow or 0
    default_days = 14 if frequency == 'biweekly' else 30.44
    remaining = starting_balance or 0
    prev_date = None
    total_principal = total_interest = total_escrow = 0.0
    rows = []
    for p in sorted(payments, key=lambda x: _as_date(x['date']) or date.min):
        pdate = _as_date(p['date'])
        if prev_date is not None and pdate is not None:
            days = max(0.0, (pdate - prev_date).days)
        else:
            days = default_days
        interest = remaining * rate * (days / 365.0) if rate else 0.0
        escrow = escrow_m * (days / 30.44) if escrow_m else 0.0
        principal = (p.get('amount') or 0) - interest - escrow
        if principal < 0:
            principal = 0.0
        if principal > remaining:
            principal = remaining
        remaining -= principal
        total_principal += principal
        total_interest += interest
        total_escrow += escrow
        rows.append({
            '_id': p.get('_id'),
            'date': p['date'],
            'description': p.get('description'),
            'account_name': p.get('account_name'),
            'amount': p.get('amount') or 0,
            'interest': round(interest, 2),
            'escrow': round(escrow, 2),
            'principal': round(principal, 2),
            'balance_after': round(remaining, 2),
        })
        prev_date = pdate
    return {
        'current_balance': max(0.0, round(remaining, 2)),
        'principal_paid': round(total_principal, 2),
        'interest_paid': round(total_interest, 2),
        'escrow_paid': round(total_escrow, 2),
        'rows': rows,
    }


def _payments_by_link(field):
    """Map of linked-loan id (str) -> list of payment dicts, for the given link field."""
    out = {}
    acct_names = {str(a['_id']): a['name'] for a in db.accounts.find()}
    for t in db.transactions.find({field: {'$exists': True, '$ne': None}}):
        key = str(t[field])
        out.setdefault(key, []).append({
            '_id': str(t['_id']),
            'date': t.get('date'),
            'amount': t.get('amount') or 0,
            'description': t.get('description'),
            'account_name': acct_names.get(str(t.get('account_id')), '—'),
        })
    return out


PAID_OFF_EPSILON = 0.005  # balances at or below this (half a cent) count as paid off


def _payoff_status(starting_balance, amort):
    """(paid_off, paid_off_date) for an amortized loan. The date is the payment
    that first brought the balance to zero; a loan entered at $0 with no
    payments is never "paid off" (there's nothing to archive)."""
    if (starting_balance or 0) <= 0 or amort['current_balance'] > PAID_OFF_EPSILON:
        return False, None
    for r in amort['rows']:
        if r['balance_after'] <= PAID_OFF_EPSILON:
            return True, _as_date(r['date'])
    return True, None


def _decorate_debt(debt, payments=None):
    """Add starting_balance/current_balance/paid + amortized breakdown for a standalone debt."""
    if payments is None:
        payments = _payments_by_link('debt_id').get(str(debt['_id']), [])
    starting = debt.get('balance', 0) or 0
    amort = _amortize(starting, debt.get('interest_rate', 0), 0, payments, 'monthly')
    debt['starting_balance'] = starting
    debt['current_balance'] = amort['current_balance']
    debt['principal_paid'] = amort['principal_paid']
    debt['interest_paid'] = amort['interest_paid']
    debt['paid'] = round(sum(r['amount'] for r in amort['rows']), 2)  # total cash paid
    debt['payment_rows'] = amort['rows']
    debt['progress_pct'] = ((starting - amort['current_balance']) / starting * 100) if starting > 0 else 0
    debt['payment_match'] = debt.get('payment_match') or []
    debt['paid_off'], debt['paid_off_date'] = _payoff_status(starting, amort)
    return debt


def _decorate_asset_liability(asset, payments=None):
    """Add liability_current_balance + amortized breakdown for an asset's loan."""
    if payments is None:
        payments = _payments_by_link('asset_liability_id').get(str(asset['_id']), [])
    starting = asset.get('liability_balance', 0) or 0
    escrow = _calculate_monthly_escrow(asset)
    freq = asset.get('payment_frequency', 'monthly') or 'monthly'
    amort = _amortize(starting, asset.get('interest_rate', 0), escrow, payments, freq)
    asset['liability_starting_balance'] = starting
    asset['liability_current_balance'] = amort['current_balance']
    asset['principal_paid'] = amort['principal_paid']
    asset['interest_paid'] = amort['interest_paid']
    asset['escrow_paid'] = amort['escrow_paid']
    asset['paid'] = round(sum(r['amount'] for r in amort['rows']), 2)
    asset['payment_rows'] = amort['rows']
    asset['progress_pct'] = ((starting - amort['current_balance']) / starting * 100) if starting > 0 else 0
    asset['payment_match'] = asset.get('payment_match') or []
    asset['liability_paid_off'], asset['liability_paid_off_date'] = _payoff_status(starting, amort)
    return asset


def _normalize_desc(text):
    return (text or '').strip().lower()


def _parse_payment_match(raw):
    """Parse the comma/newline-separated payment-match field into a clean list."""
    if not raw:
        return []
    parts = [p.strip() for p in raw.replace('\n', ',').split(',')]
    return [p for p in parts if p]


def _payment_match_index():
    """Map normalized payment description -> ('debt'|'asset', ObjectId) for auto-linking."""
    idx = {}
    for d in db.debts.find({'payment_match': {'$exists': True, '$ne': []}}):
        for pat in (d.get('payment_match') or []):
            idx[_normalize_desc(pat)] = ('debt', d['_id'])
    for a in db.assets.find({'payment_match': {'$exists': True, '$ne': []}}):
        for pat in (a.get('payment_match') or []):
            idx[_normalize_desc(pat)] = ('asset', a['_id'])
    return idx


def _auto_link_transaction(txn_id, description, index=None):
    """If the description matches a loan's payment_match rule, tag the transaction."""
    if not description:
        return
    idx = index if index is not None else _payment_match_index()
    hit = idx.get(_normalize_desc(description))
    if not hit:
        return
    kind, oid = hit
    field = 'debt_id' if kind == 'debt' else 'asset_liability_id'
    db.transactions.update_one({'_id': txn_id}, {'$set': {field: oid}})


def _seed_default_matches():
    """Fill the known loans' payment_match (the budget-item description that pays
    them) where it hasn't been set yet. Idempotent — never overwrites a real value."""
    empty = {'$or': [{'payment_match': {'$exists': False}}, {'payment_match': []}, {'payment_match': None}]}
    for name, patterns in DEFAULT_PAYMENT_MATCHES.items():
        db.debts.update_one({'name': name, **empty}, {'$set': {'payment_match': patterns}})
        db.assets.update_one({'name': name, **empty}, {'$set': {'payment_match': patterns}})


def _apply_auto_links(seed_defaults=True):
    """Seed match rules, then link every unlinked expense transaction whose
    description matches a loan. Idempotent and cheap — safe to run on page loads
    so loan payments attach themselves with no manual step. Returns count linked."""
    if seed_defaults:
        _seed_default_matches()
    index = _payment_match_index()
    if not index:
        return 0
    linked = 0
    for t in db.transactions.find({'transaction_type': 'expense',
                                   'debt_id': {'$exists': False},
                                   'asset_liability_id': {'$exists': False}}):
        hit = index.get(_normalize_desc(t.get('description')))
        if hit:
            kind, oid = hit
            field = 'debt_id' if kind == 'debt' else 'asset_liability_id'
            db.transactions.update_one({'_id': t['_id']}, {'$set': {field: oid}})
            linked += 1
    # With payments attached, archive/restore budget lines of paid-off loans.
    _sync_payoff_archive()
    return linked


def _link_candidates():
    """Unlinked ledger expense transactions (last 18 months) available to link as a payment."""
    cutoff = datetime.now() - relativedelta(months=18)
    acct_names = {str(a['_id']): a['name'] for a in db.accounts.find()}
    rows = list(db.transactions.find({
        'transaction_type': 'expense',
        'debt_id': {'$exists': False},
        'asset_liability_id': {'$exists': False},
        'date': {'$gte': cutoff},
    }).sort('date', -1))
    for t in rows:
        t['_id'] = str(t['_id'])
        t['account_name'] = acct_names.get(str(t.get('account_id')), '—')
    return rows


def _standalone_debt_total():
    """Sum of current (amortized) balances across all standalone debts."""
    pmap = _payments_by_link('debt_id')
    return sum(_decorate_debt(d, pmap.get(str(d['_id']), []))['current_balance']
               for d in db.debts.find())


def _asset_liability_total():
    """Sum of current (amortized) balances across all asset-attached loans."""
    pmap = _payments_by_link('asset_liability_id')
    return sum(_decorate_asset_liability(a, pmap.get(str(a['_id']), []))['liability_current_balance']
               for a in db.assets.find({'liability_balance': {'$gt': 0}}))


# --- Loan payoff: projection capping + budget-line archiving ----------------
#
# A loan's budget line (matched by payment_match) is otherwise projected forever.
# Two mechanisms keep the plan honest:
#   1. The outlook walks each loan's projected payments against its *actual*
#      balance. The payment that clears the loan is capped to the payoff amount
#      and badged; later occurrences are dropped. Because the starting point is
#      the actual balance, a projected payoff that doesn't happen (a smaller
#      real payment realizes that occurrence instead) leaves a balance and the
#      regular payment simply resumes the next month.
#   2. Once the ledger shows the loan actually paid off, its budget lines are
#      archived (archived_reason='paid_off') so they drop out of the budget,
#      estimates, calendar and bills. If the loan reopens (payment unlinked or
#      edited, balance corrected upward) the lines are restored automatically.

ACTIVE_RECURRING = {'archived': {'$ne': True}}  # recurring_transactions query filter


def _loan_statuses():
    """Every standalone debt and asset loan, decorated, as a list of dicts:
    {ref, name, balance, rate, escrow, frequency, last_payment, paid_off,
    paid_off_date, patterns}. `ref` is 'debt:<id>' / 'asset:<id>'."""
    out = []
    debt_payments = _payments_by_link('debt_id')
    for d in db.debts.find():
        _decorate_debt(d, debt_payments.get(str(d['_id']), []))
        out.append({
            'ref': 'debt:' + str(d['_id']), 'name': d.get('name'),
            'balance': d['current_balance'], 'rate': d.get('interest_rate', 0) or 0,
            'escrow': 0.0, 'frequency': 'monthly',
            'last_payment': _as_date(d['payment_rows'][-1]['date']) if d['payment_rows'] else None,
            'paid_off': d['paid_off'], 'paid_off_date': d['paid_off_date'],
            'patterns': d['payment_match'],
        })
    asset_payments = _payments_by_link('asset_liability_id')
    for a in db.assets.find({'liability_balance': {'$gt': 0}}):
        _decorate_asset_liability(a, asset_payments.get(str(a['_id']), []))
        out.append({
            'ref': 'asset:' + str(a['_id']), 'name': a.get('name'),
            'balance': a['liability_current_balance'], 'rate': a.get('interest_rate', 0) or 0,
            'escrow': _calculate_monthly_escrow(a),
            'frequency': a.get('payment_frequency', 'monthly') or 'monthly',
            'last_payment': _as_date(a['payment_rows'][-1]['date']) if a['payment_rows'] else None,
            'paid_off': a['liability_paid_off'], 'paid_off_date': a['liability_paid_off_date'],
            'patterns': a['payment_match'],
        })
    return out


def _loan_projection_state():
    """Map normalized payment description -> mutable loan state for the outlook's
    payoff simulation. Descriptions shared by several loans are left out (can't
    tell which balance a payment reduces)."""
    by_desc, dupes = {}, set()
    for loan in _loan_statuses():
        state = {k: loan[k] for k in ('ref', 'name', 'balance', 'rate', 'escrow', 'frequency', 'last_payment')}
        for pat in loan['patterns']:
            key = _normalize_desc(pat)
            if key in by_desc and by_desc[key]['ref'] != loan['ref']:
                dupes.add(key)
            by_desc[key] = state
    for key in dupes:
        del by_desc[key]
    return by_desc


def _apply_projected_loan_payment(loan, txn):
    """Run one projected ledger row (negative amount) against a loan's simulated
    balance. Caps the row to the payoff amount when it clears the loan and marks
    it `loan_payoff`. Returns False if the loan was already paid off, meaning the
    row should be dropped from the outlook."""
    if loan['balance'] <= PAID_OFF_EPSILON:
        return False
    pdate = _as_date(txn['date'])
    last = loan['last_payment']
    if last is not None and pdate is not None:
        days = max(0, (pdate - last).days)
    else:
        days = 14 if loan['frequency'] == 'biweekly' else 30.44
    rate = loan['rate'] / 100.0
    interest = loan['balance'] * rate * (days / 365.0) if rate else 0.0
    escrow = loan['escrow'] * (days / 30.44) if loan['escrow'] else 0.0
    payoff_amount = round(loan['balance'] + interest + escrow, 2)
    payment = -txn['amount']
    txn['loan_label'] = loan['name']
    if payment >= payoff_amount - PAID_OFF_EPSILON:
        txn['amount'] = -payoff_amount
        txn['loan_payoff'] = True
        loan['balance'] = 0.0
    else:
        loan['balance'] -= max(0.0, payment - interest - escrow)
    loan['last_payment'] = pdate
    return True


def _sync_payoff_archive():
    """Archive the budget lines of loans the ledger shows as paid off, and restore
    lines whose loan has reopened. Only touches lines this function archived
    (archived_reason='paid_off'). Idempotent; returns (archived, restored)."""
    archived = restored = 0
    for loan in _loan_statuses():
        if not loan['patterns']:
            continue
        pattern_re = '|'.join(re.escape(p.strip()) for p in loan['patterns'] if p.strip())
        if not pattern_re:
            continue
        match = {'transaction_type': 'expense',
                 'description': {'$regex': '^\\s*(' + pattern_re + ')\\s*$', '$options': 'i'}}
        if loan['paid_off']:
            when = loan['paid_off_date'] or date.today()
            res = db.recurring_transactions.update_many(
                {**match, 'archived': {'$ne': True}},
                {'$set': {'archived': True, 'archived_reason': 'paid_off',
                          'archived_loan_ref': loan['ref'],
                          'archived_at': datetime.combine(when, datetime.min.time())}})
            archived += res.modified_count
        else:
            res = db.recurring_transactions.update_many(
                {**match, 'archived': True, 'archived_reason': 'paid_off',
                 'archived_loan_ref': loan['ref']},
                {'$unset': {'archived': '', 'archived_reason': '',
                            'archived_loan_ref': '', 'archived_at': ''}})
            restored += res.modified_count
    return archived, restored


@app.route('/debts')
def debts_page():
    # Loan payments attach themselves by matching their budget-item description,
    # so balances stay current without any manual linking.
    _apply_auto_links()

    debt_payments = _payments_by_link('debt_id')
    debts, paid_off_debts = [], []
    for d in db.debts.find():
        d['_id'] = str(d['_id'])
        _decorate_debt(d, debt_payments.get(d['_id'], []))
        (paid_off_debts if d['paid_off'] else debts).append(d)

    # Payment plans (no-interest installment plans charged to a CC)
    payment_plans = [_decorate_payment_plan(p) for p in db.payment_plans.find().sort('status', 1)]

    # Get asset liabilities (mortgage, car, etc.) with amortized current balances
    asset_payments = _payments_by_link('asset_liability_id')
    asset_liabilities, paid_off_assets = [], []
    for a in db.assets.find({'liability_balance': {'$gt': 0}}):
        a['_id'] = str(a['_id'])
        _decorate_asset_liability(a, asset_payments.get(a['_id'], []))
        (paid_off_assets if a['liability_paid_off'] else asset_liabilities).append(a)

    # Compute summary
    total_debt = sum(d.get('current_balance', 0) for d in debts) + sum(a.get('liability_current_balance', 0) for a in asset_liabilities)
    total_minimums = sum(d.get('minimum_payment', 0) for d in debts) + sum(a.get('monthly_payment', 0) or 0 for a in asset_liabilities)

    all_rates = []
    for d in debts:
        if d.get('interest_rate'):
            all_rates.append(d['interest_rate'])
    for a in asset_liabilities:
        if a.get('interest_rate'):
            all_rates.append(a['interest_rate'])
    avg_rate = sum(all_rates) / len(all_rates) if all_rates else 0

    summary = {
        'total_debt': total_debt,
        'total_minimums': total_minimums,
        'avg_rate': avg_rate
    }

    # Available CC card names for the payment-plan form picker
    cc_card_names = sorted(db.cc_transactions.distinct('card_name'))

    # Budget-aware payoff plan (target debt, schedule, debt-free date)
    plan = _get_debt_plan()
    cash_accounts = [{'_id': str(a['_id']), 'name': a.get('name')}
                     for a in db.accounts.find({'account_type': {'$in': ['checking', 'savings']}}).sort('name', 1)]

    return render_template('debts.html',
                           debts=debts,
                           asset_liabilities=asset_liabilities,
                           paid_off_debts=paid_off_debts,
                           paid_off_assets=paid_off_assets,
                           debt_types=DEBT_TYPES,
                           summary=summary,
                           payment_plans=payment_plans,
                           cc_card_names=cc_card_names,
                           categories=CATEGORIES,
                           plan=plan,
                           cash_accounts=cash_accounts,
                           today=date.today())


@app.route('/debts/add', methods=['POST'])
def add_debt():
    data = request.form
    _create_debt(
        name=data['name'],
        debt_type=data['debt_type'],
        balance=float(data.get('balance', 0)),
        interest_rate=float(data.get('interest_rate', 0)),
        minimum_payment=float(data.get('minimum_payment', 0)),
        creditor=data.get('creditor'),
        account_number=data.get('account_number'),
        notes=data.get('notes'),
        deferment_end_date=datetime.fromisoformat(data['deferment_end_date']) if data.get('deferment_end_date') else None,
        subsidized=bool(data.get('subsidized')),
        payment_match=_parse_payment_match(data.get('payment_match'))
    )
    return redirect(url_for('debts_page'))


@app.route('/debts/edit/<debt_id>', methods=['GET', 'POST'])
def edit_debt(debt_id):
    if request.method == 'POST':
        db.debts.update_one(
            {'_id': safe_objectid(debt_id)},
            {'$set': {
                'name': request.form['name'],
                'debt_type': request.form['debt_type'],
                'balance': float(request.form.get('balance', 0)),
                'interest_rate': float(request.form.get('interest_rate', 0)),
                'minimum_payment': float(request.form.get('minimum_payment', 0)),
                'creditor': request.form.get('creditor'),
                'account_number': request.form.get('account_number'),
                'notes': request.form.get('notes'),
                'deferment_end_date': datetime.fromisoformat(request.form['deferment_end_date']) if request.form.get('deferment_end_date') else None,
                'subsidized': bool(request.form.get('subsidized')),
                'payment_match': _parse_payment_match(request.form.get('payment_match'))
            }}
        )
        return redirect(url_for('debts_page'))
    else:
        debt = db.debts.find_one({'_id': safe_objectid(debt_id)})
        if debt:
            debt['_id'] = str(debt['_id'])
            if debt.get('deferment_end_date'):
                ded = debt['deferment_end_date']
                if isinstance(ded, datetime):
                    debt['deferment_end_date'] = ded.strftime('%Y-%m-%d')
                elif isinstance(ded, date):
                    debt['deferment_end_date'] = ded.strftime('%Y-%m-%d')
            debt.setdefault('payment_match', [])
            return render_template('edit_debt.html', debt=debt, debt_types=DEBT_TYPES)
        return redirect(url_for('debts_page'))


@app.route('/debts/delete/<debt_id>', methods=['POST'])
def delete_debt(debt_id):
    did = safe_objectid(debt_id)
    db.debts.delete_one({'_id': did})
    # Unlink any ledger transactions tagged as payments toward this debt
    if did:
        db.transactions.update_many({'debt_id': did}, {'$unset': {'debt_id': ''}})
    if request.headers.get('Accept') == 'application/json':
        return jsonify({'message': 'Deleted'}), 200
    return redirect(url_for('debts_page'))


@app.route('/debts/<debt_id>')
def debt_detail(debt_id):
    did = safe_objectid(debt_id)
    if not did:
        return redirect(url_for('debts_page'))
    debt = db.debts.find_one({'_id': did})
    if not debt:
        return redirect(url_for('debts_page'))
    _decorate_debt(debt)
    debt['_id'] = str(debt['_id'])
    # Linked payments, newest first, with the amortized principal/interest split
    linked = list(reversed(debt['payment_rows']))
    candidates = _link_candidates()

    return render_template('debt_detail.html',
                           debt=debt,
                           linked=linked,
                           candidates=candidates,
                           show_interest=(debt.get('interest_rate') or 0) > 0)


@app.route('/debts/<debt_id>/link-transaction', methods=['POST'])
def link_debt_transaction(debt_id):
    did = safe_objectid(debt_id)
    tid = safe_objectid(request.form.get('transaction_id'))
    if did and tid:
        db.transactions.update_one({'_id': tid}, {'$set': {'debt_id': did}})
        _sync_payoff_archive()
    return redirect(url_for('debt_detail', debt_id=debt_id))


@app.route('/debts/<debt_id>/unlink-transaction', methods=['POST'])
def unlink_debt_transaction(debt_id):
    tid = safe_objectid(request.form.get('transaction_id'))
    if tid:
        db.transactions.update_one({'_id': tid}, {'$unset': {'debt_id': ''}})
        _sync_payoff_archive()
    return redirect(url_for('debt_detail', debt_id=debt_id))


@app.route('/debts/auto-link', methods=['POST'])
def auto_link_payments():
    """Manual trigger for the same auto-link pass that runs on page load."""
    linked = _apply_auto_links(seed_defaults=True)
    return redirect(url_for('debts_page', linked=linked))


@app.route('/debts/asset/<asset_id>')
def asset_liability_detail(asset_id):
    aid = safe_objectid(asset_id)
    if not aid:
        return redirect(url_for('debts_page'))
    asset = db.assets.find_one({'_id': aid})
    if not asset:
        return redirect(url_for('debts_page'))
    _decorate_asset_liability(asset)
    asset['_id'] = str(asset['_id'])
    linked = list(reversed(asset['payment_rows']))
    candidates = _link_candidates()
    return render_template('asset_liability_detail.html',
                           asset=asset,
                           linked=linked,
                           candidates=candidates,
                           show_interest=(asset.get('interest_rate') or 0) > 0,
                           show_escrow=_calculate_monthly_escrow(asset) > 0)


@app.route('/debts/asset/<asset_id>/link-transaction', methods=['POST'])
def link_asset_transaction(asset_id):
    aid = safe_objectid(asset_id)
    tid = safe_objectid(request.form.get('transaction_id'))
    if aid and tid:
        db.transactions.update_one({'_id': tid}, {'$set': {'asset_liability_id': aid}})
        _sync_payoff_archive()
    return redirect(url_for('asset_liability_detail', asset_id=asset_id))


@app.route('/debts/asset/<asset_id>/unlink-transaction', methods=['POST'])
def unlink_asset_transaction(asset_id):
    tid = safe_objectid(request.form.get('transaction_id'))
    if tid:
        db.transactions.update_one({'_id': tid}, {'$unset': {'asset_liability_id': ''}})
        _sync_payoff_archive()
    return redirect(url_for('asset_liability_detail', asset_id=asset_id))


@app.route('/debts/payment-plans/add', methods=['POST'])
def add_payment_plan():
    data = request.form
    try:
        original = float(data.get('original_amount') or 0)
        monthly = float(data.get('monthly_payment') or 0)
    except ValueError:
        return redirect(url_for('debts_page'))
    if not data.get('vendor') or original <= 0 or monthly <= 0:
        return redirect(url_for('debts_page'))

    start_raw = data.get('start_date')
    start_dt = datetime.fromisoformat(start_raw) if start_raw else None

    plan = PaymentPlan(
        vendor=data['vendor'].strip(),
        original_amount=original,
        monthly_payment=monthly,
        start_date=start_dt,
        card_name=(data.get('card_name') or None),
        category=(data.get('category') or None),
        notes=data.get('notes'),
        status='active',
    )
    db.payment_plans.insert_one(plan.__dict__)
    return redirect(url_for('debts_page'))


@app.route('/debts/payment-plans/edit/<plan_id>', methods=['POST'])
def edit_payment_plan(plan_id):
    pid = safe_objectid(plan_id)
    if not pid:
        return redirect(url_for('debts_page'))
    start_raw = request.form.get('start_date')
    update = {
        'vendor': request.form.get('vendor', '').strip(),
        'original_amount': float(request.form.get('original_amount') or 0),
        'monthly_payment': float(request.form.get('monthly_payment') or 0),
        'start_date': datetime.fromisoformat(start_raw) if start_raw else None,
        'card_name': request.form.get('card_name') or None,
        'category': request.form.get('category') or None,
        'notes': request.form.get('notes'),
        'status': request.form.get('status') or 'active',
    }
    db.payment_plans.update_one({'_id': pid}, {'$set': update})
    return redirect(url_for('debts_page'))


@app.route('/debts/payment-plans/delete/<plan_id>', methods=['POST'])
def delete_payment_plan(plan_id):
    pid = safe_objectid(plan_id)
    if pid:
        db.payment_plans.delete_one({'_id': pid})
        # Unlink any cc_transactions referencing this plan
        db.cc_transactions.update_many(
            {'payment_plan_id': pid},
            {'$unset': {'payment_plan_id': ''}}
        )
    return redirect(url_for('debts_page'))


@app.route('/debts/payment-plans/<plan_id>')
def payment_plan_detail(plan_id):
    pid = safe_objectid(plan_id)
    if not pid:
        return redirect(url_for('debts_page'))
    plan = db.payment_plans.find_one({'_id': pid})
    if not plan:
        return redirect(url_for('debts_page'))
    plan = _decorate_payment_plan(plan)

    linked = list(db.cc_transactions.find({'payment_plan_id': pid}).sort('date', 1))
    for t in linked:
        t['_id'] = str(t['_id'])

    cc_card_names = sorted(db.cc_transactions.distinct('card_name'))
    return render_template('payment_plan_detail.html',
                           plan=plan,
                           linked=linked,
                           cc_card_names=cc_card_names,
                           categories=CATEGORIES)


@app.route('/credit-cards/link-plan', methods=['POST'])
def cc_link_plan():
    data = request.get_json() or {}
    tid = safe_objectid(data.get('transaction_id'))
    if not tid:
        return jsonify({'error': 'Invalid transaction'}), 400
    plan_id_raw = data.get('plan_id')
    if plan_id_raw:
        pid = safe_objectid(plan_id_raw)
        if not pid:
            return jsonify({'error': 'Invalid plan'}), 400
        db.cc_transactions.update_one({'_id': tid}, {'$set': {'payment_plan_id': pid}})
    else:
        db.cc_transactions.update_one({'_id': tid}, {'$unset': {'payment_plan_id': ''}})
    return jsonify({'success': True})


@app.route('/debts/<debt_id>/payoff')
def debt_payoff_schedule(debt_id):
    debt = db.debts.find_one({'_id': safe_objectid(debt_id)})
    if not debt:
        return redirect(url_for('debts_page'))

    # Project the payoff from the current (remaining) balance, not the original.
    _decorate_debt(debt)
    balance = debt.get('current_balance', 0) or 0
    rate = debt.get('interest_rate', 0) or 0
    payment = debt.get('minimum_payment', 0) or 0

    if balance <= 0 or payment <= 0:
        return redirect(url_for('debts_page'))

    # Check deferment
    start = date.today()
    deferred = False
    ded = debt.get('deferment_end_date')
    if ded:
        if isinstance(ded, datetime):
            ded = ded.date()
        elif isinstance(ded, date):
            pass
        if ded > start:
            deferred = True
            # Accrue interest during deferment if not subsidized
            if not debt.get('subsidized') and rate > 0:
                months_deferred = (ded.year - start.year) * 12 + (ded.month - start.month)
                monthly_rate = rate / 100 / 12
                for _ in range(max(0, months_deferred)):
                    balance += balance * monthly_rate
                balance = round(balance, 2)
            start = ded

    monthly_rate = rate / 100 / 12
    schedule = []
    remaining = balance
    total_interest = 0
    total_paid = 0
    period_num = 0
    max_periods = 600

    while remaining > 0.01 and period_num < max_periods:
        period_num += 1
        payment_date = start + relativedelta(months=period_num)

        interest = remaining * monthly_rate if rate > 0 else 0

        if payment <= interest and rate > 0:
            break

        actual_payment = min(payment, remaining + interest)
        principal = actual_payment - interest
        remaining -= principal
        if remaining < 0.01:
            remaining = 0
        total_interest += interest
        total_paid += actual_payment

        schedule.append({
            'number': period_num,
            'date': payment_date.strftime('%b %Y'),
            'payment': round(actual_payment, 2),
            'principal': round(principal, 2),
            'interest': round(interest, 2),
            'balance': round(remaining, 2)
        })

    if period_num < max_periods and schedule:
        payoff_date = (start + relativedelta(months=period_num)).strftime('%B %Y')
    else:
        payoff_date = 'N/A'

    summary = {
        'total_payments': round(total_paid, 2),
        'total_interest': round(total_interest, 2),
        'payoff_date': payoff_date,
        'periods': period_num,
        'period_payment': round(payment, 2),
        'starting_balance': round(balance, 2),
        'deferred': deferred,
        'deferment_end': ded.strftime('%b %Y') if deferred and ded else None
    }

    debt['_id'] = str(debt['_id'])
    return render_template('debt_payoff.html', debt=debt, schedule=schedule, summary=summary)


# ============ DEBT PAYOFF PLAN ============
# The plan turns the avalanche/snowball simulation into an actionable, budget-
# aware strategy: it picks the target debt, reserves the extra payment as a
# recurring budget line, and logs real payments to the ledger on demand.

# EXTRA_DEBT_PAYMENT_DESC lives in models.py: the MCP make_debt_payment tool uses it too.


def _unified_debts():
    """All active debts + asset liabilities with current amortized balances, unified shape.

    Debts in deferment are skipped. Each entry: {id, kind, name, balance,
    interest_rate, minimum_payment}.
    """
    today = date.today()
    unified = []
    debt_payments = _payments_by_link('debt_id')
    for d in db.debts.find():
        _decorate_debt(d, debt_payments.get(str(d['_id']), []))
        if d['current_balance'] <= 0:
            continue
        ded = d.get('deferment_end_date')
        if ded:
            if isinstance(ded, datetime):
                ded = ded.date()
            if isinstance(ded, date) and ded > today:
                continue
        unified.append({
            'id': str(d['_id']), 'kind': 'debt', 'name': d['name'],
            'balance': d['current_balance'],
            'interest_rate': d.get('interest_rate', 0) or 0,
            'minimum_payment': d.get('minimum_payment', 0) or 0,
        })
    asset_payments = _payments_by_link('asset_liability_id')
    for a in db.assets.find({'liability_balance': {'$gt': 0}}):
        _decorate_asset_liability(a, asset_payments.get(str(a['_id']), []))
        if a['liability_current_balance'] <= 0:
            continue
        unified.append({
            'id': str(a['_id']), 'kind': 'asset', 'name': a['name'] + ' (Loan)',
            'balance': a['liability_current_balance'],
            'interest_rate': a.get('interest_rate', 0) or 0,
            'minimum_payment': a.get('monthly_payment', 0) or 0,
        })
    return unified


def _order_debts(unified, strategy):
    """Order debts by payoff strategy: snowball = lowest balance first, avalanche = highest rate first."""
    if strategy == 'snowball':
        return sorted(unified, key=lambda d: d['balance'])
    return sorted(unified, key=lambda d: -d['interest_rate'])


def _simulate_payoff(debts_list, extra):
    """Month-by-month payoff simulation. Pays minimums, then rolls freed minimums +
    extra into the first unpaid debt (list order = strategy priority).

    Returns {total_months, total_interest, payoff_month: {id: month}}.
    """
    debts = copy.deepcopy(debts_list)
    total_interest = 0
    month = 0
    max_months = 600  # 50 year cap
    for d in debts:
        if d['minimum_payment'] <= 0:
            d['minimum_payment'] = max(25, d['balance'] * 0.02)
    payoff_month = {d['id']: 0 for d in debts}
    while any(d['balance'] > 0.01 for d in debts) and month < max_months:
        month += 1
        remaining_extra = extra
        for d in debts:
            if d['balance'] <= 0:
                continue
            interest = d['balance'] * (d['interest_rate'] / 100 / 12)
            total_interest += interest
            d['balance'] += interest
        freed = 0
        for d in debts:
            if d['balance'] <= 0:
                freed += d['minimum_payment']
                continue
            payment = min(d['minimum_payment'], d['balance'])
            d['balance'] -= payment
            if d['balance'] <= 0.01:
                d['balance'] = 0
                payoff_month[d['id']] = month
        remaining_extra += freed
        for d in debts:
            if d['balance'] <= 0 or remaining_extra <= 0:
                continue
            payment = min(remaining_extra, d['balance'])
            d['balance'] -= payment
            remaining_extra -= payment
            if d['balance'] <= 0.01:
                d['balance'] = 0
                payoff_month[d['id']] = month
    return {'total_months': month, 'total_interest': round(total_interest, 2), 'payoff_month': payoff_month}


def _monthly_income_estimate():
    """Estimated monthly income from recurring income budget items."""
    total = 0.0
    for rt in db.recurring_transactions.find({'transaction_type': 'income', **ACTIVE_RECURRING}):
        total += calc_monthly_amount(rt.get('amount', 0) or 0,
                                     rt.get('frequency'), rt.get('day_of_month'))
    return total


def _projected_monthly_surplus():
    """Disposable income available for extra debt payoff: income − expenses.

    Excludes the auto-managed 'Extra Debt Payment' line so that reserving the
    extra in the budget doesn't shrink the surplus that funds it (a feedback
    loop that would otherwise spiral the auto-extra toward zero).
    """
    return _monthly_income_estimate() - _monthly_expense_estimate(
        exclude_descriptions=[EXTRA_DEBT_PAYMENT_DESC])


def _get_debt_plan():
    """Load plan settings and compute the live, budget-aware payoff plan.

    Returns a dict consumed by the debts page, the Debt-Free goal widget, and
    the dashboard. Safe to call when no plan is configured (returns defaults).
    """
    doc = db.debt_plan.find_one({}) or {}
    strategy = doc.get('strategy', 'avalanche')
    auto_extra = doc.get('auto_extra', True)
    surplus = _projected_monthly_surplus()
    extra = max(0.0, surplus) if auto_extra else float(doc.get('extra_payment') or 0)

    unified = _unified_debts()
    ordered = _order_debts(unified, strategy)
    target = next((d for d in ordered if d['balance'] > 0.01), None)
    total_min = sum(d['minimum_payment'] for d in unified)
    total_balance = sum(d['balance'] for d in unified)

    empty = {'total_months': 0, 'total_interest': 0, 'payoff_month': {}}
    with_plan = _simulate_payoff(ordered, extra) if ordered else empty
    minimums_only = _simulate_payoff(ordered, 0) if ordered else empty

    debtfree_date = None
    if with_plan['total_months']:
        debtfree_date = date.today() + relativedelta(months=with_plan['total_months'])
    months_saved = max(0, minimums_only['total_months'] - with_plan['total_months'])
    interest_saved = max(0, round(minimums_only['total_interest'] - with_plan['total_interest'], 2))

    schedule = []
    for i, d in enumerate(ordered):
        m = with_plan['payoff_month'].get(d['id'], 0)
        schedule.append({**d, 'order': i + 1, 'payoff_month': m,
                         'payoff_eta': (date.today() + relativedelta(months=m)).strftime('%b %Y') if m else None,
                         'is_target': target is not None and d['id'] == target['id']})

    return {
        'configured': bool(doc),
        'strategy': strategy,
        'auto_extra': auto_extra,
        'surplus': round(surplus, 2),
        'extra': round(extra, 2),
        'fits': extra <= surplus + 0.01,
        'over_by': round(max(0.0, extra - surplus), 2),
        'target': target,
        'target_name': target['name'] if target else None,
        'total_balance': round(total_balance, 2),
        'total_min': round(total_min, 2),
        'debtfree_date': debtfree_date,
        'debtfree_label': debtfree_date.strftime('%b %Y') if debtfree_date else None,
        'months_to_free': with_plan['total_months'],
        'total_interest': with_plan['total_interest'],
        'months_saved': months_saved,
        'interest_saved': interest_saved,
        'schedule': schedule,
        'cash_account_id': doc.get('cash_account_id'),
        'recurring_item_id': str(doc['recurring_item_id']) if doc.get('recurring_item_id') else None,
        'has_recurring': bool(doc.get('recurring_item_id')),
    }


@app.route('/debts/plan', methods=['POST'])
def save_debt_plan():
    """Persist the payoff-plan settings and (optionally) maintain the recurring
    'Extra Debt Payment' budget line so the extra is reserved in budget/outlook."""
    data = request.form
    strategy = data.get('strategy', 'avalanche')
    auto_extra = bool(data.get('auto_extra'))
    try:
        extra_payment = max(0.0, float(data.get('extra_payment') or 0))
    except (TypeError, ValueError):
        extra_payment = 0.0
    cash_account_id = data.get('cash_account_id') or None
    create_line = bool(data.get('create_recurring'))

    doc = db.debt_plan.find_one({}) or {}
    surplus = _projected_monthly_surplus()
    effective_extra = max(0.0, surplus) if auto_extra else extra_payment

    update = {
        'strategy': strategy,
        'auto_extra': auto_extra,
        'extra_payment': extra_payment,
        'cash_account_id': cash_account_id,
        'updated': datetime.now(),
    }

    rec_id = doc.get('recurring_item_id')
    if create_line and effective_extra > 0:
        if rec_id and db.recurring_transactions.find_one({'_id': rec_id}):
            db.recurring_transactions.update_one(
                {'_id': rec_id},
                {'$set': {'amount': round(effective_extra, 2),
                          'account_id': safe_objectid(cash_account_id) if cash_account_id else None}})
            update['recurring_item_id'] = rec_id
        else:
            rt = RecurringTransaction(
                description=EXTRA_DEBT_PAYMENT_DESC,
                amount=round(effective_extra, 2),
                frequency='monthly',
                start_date=datetime.combine(date.today().replace(day=1), datetime.min.time()),
                transaction_type='expense',
                day_of_month='1',
                category='Debt Payments',
                notes='Auto-managed by the debt payoff plan',
                account_id=cash_account_id,
            )
            db.recurring_transactions.insert_one(rt.__dict__)
            update['recurring_item_id'] = rt._id
    else:
        if rec_id:
            db.recurring_transactions.delete_one({'_id': rec_id})
        update['recurring_item_id'] = None

    if doc:
        db.debt_plan.update_one({'_id': doc['_id']}, {'$set': update})
    else:
        db.debt_plan.insert_one(update)
    return redirect(url_for('debts_page'))


@app.route('/debts/plan/log-payment', methods=['POST'])
def log_debt_payment():
    """Log the extra payment to the ledger as a real transaction linked to the
    current target debt. Shares the recurring line's description so the outlook
    dedups this month's projected occurrence instead of double-counting it."""
    plan = _get_debt_plan()
    target = plan['target']
    if not target:
        return redirect(url_for('debts_page'))

    form_amt = request.form.get('amount')
    try:
        amount = float(form_amt) if form_amt else plan['extra']
    except (TypeError, ValueError):
        amount = plan['extra']
    if not amount or amount <= 0:
        return redirect(url_for('debts_page'))

    acct_id = request.form.get('cash_account_id') or plan['cash_account_id']
    if not acct_id:
        primary = db.accounts.find_one({'is_primary': True})
        acct_id = str(primary['_id']) if primary else None

    txn = Transaction(
        description=EXTRA_DEBT_PAYMENT_DESC,
        amount=round(amount, 2),
        date=datetime.combine(date.today(), datetime.min.time()),
        transaction_type='expense',
        account_id=acct_id,
    )
    doc = txn.__dict__
    if target['kind'] == 'debt':
        doc['debt_id'] = safe_objectid(target['id'])
    else:
        doc['asset_liability_id'] = safe_objectid(target['id'])
    db.transactions.insert_one(doc)
    return redirect(url_for('debts_page'))


@app.route('/debts/calculate', methods=['POST'])
def calculate_payoff():
    """Calculate debt payoff using avalanche and snowball strategies (calculator widget)."""
    data = request.get_json()
    extra_payment = float(data.get('extra_payment', 0))

    unified = _unified_debts()
    if not unified:
        return jsonify({'avalanche': {'total_months': 0, 'total_interest': 0, 'debts': []},
                        'snowball': {'total_months': 0, 'total_interest': 0, 'debts': []}})

    id_to_name = {d['id']: d['name'] for d in unified}

    def result_for(strategy):
        ordered = _order_debts(unified, strategy)
        sim = _simulate_payoff(ordered, extra_payment)
        return {
            'total_months': sim['total_months'],
            'total_interest': sim['total_interest'],
            'debts': [{'name': id_to_name.get(did, ''), 'months': m}
                      for did, m in sim['payoff_month'].items()],
        }

    return jsonify({'avalanche': result_for('avalanche'), 'snowball': result_for('snowball')})


# ============ AMORTIZATION ============


def _calculate_monthly_escrow(asset):
    """Sum monthly escrow: property_tax/12 + insurance/12 + pmi + hoa."""
    total = 0
    if asset.get('property_tax_annual'):
        total += asset['property_tax_annual'] / 12
    if asset.get('homeowners_insurance_annual'):
        total += asset['homeowners_insurance_annual'] / 12
    if asset.get('pmi_monthly'):
        total += asset['pmi_monthly']
    if asset.get('hoa_monthly'):
        total += asset['hoa_monthly']
    return total


def _calculate_loan_payoff(balance, rate_apr, period_payment, frequency):
    """Standalone payoff calculator returning total_interest and periods."""
    if frequency == 'biweekly':
        period_rate = rate_apr / 100 / 26
        max_periods = 1200
    else:
        period_rate = rate_apr / 100 / 12
        max_periods = 600

    remaining = balance
    total_interest = 0
    periods = 0

    while remaining > 0.01 and periods < max_periods:
        periods += 1
        interest = remaining * period_rate
        if period_payment <= interest:
            return {'total_interest': float('inf'), 'periods': max_periods}
        actual = min(period_payment, remaining + interest)
        principal = actual - interest
        remaining -= principal
        if remaining < 0.01:
            remaining = 0
        total_interest += interest

    return {'total_interest': round(total_interest, 2), 'periods': periods}


def _calculate_payment_comparison(balance, rate, monthly_payment, current_frequency):
    """Compare monthly vs biweekly scenarios."""
    monthly_result = _calculate_loan_payoff(balance, rate, monthly_payment, 'monthly')
    biweekly_result = _calculate_loan_payoff(balance, rate, monthly_payment / 2, 'biweekly')

    # Convert biweekly periods to months for comparison
    biweekly_months = round(biweekly_result['periods'] * 12 / 26, 1)
    monthly_months = monthly_result['periods']

    interest_saved = round(monthly_result['total_interest'] - biweekly_result['total_interest'], 2)
    time_saved = round(monthly_months - biweekly_months, 1)

    if current_frequency == 'biweekly':
        return {
            'interest_saved': interest_saved,
            'time_saved_months': time_saved,
            'is_alternative': False,
            'label': "You're saving with bi-weekly payments"
        }
    else:
        return {
            'interest_saved': interest_saved,
            'time_saved_months': time_saved,
            'is_alternative': True,
            'label': 'Switch to bi-weekly to save'
        }


@app.route('/assets/<asset_id>/amortization')
def amortization_page(asset_id):
    asset = db.assets.find_one({'_id': safe_objectid(asset_id)})
    if not asset:
        return redirect(url_for('assets_page'))

    balance = asset.get('liability_balance', 0) or 0
    rate = asset.get('interest_rate', 0) or 0
    payment = asset.get('monthly_payment', 0) or 0

    if balance <= 0 or rate <= 0 or payment <= 0:
        return redirect(url_for('assets_page'))

    frequency = asset.get('payment_frequency', 'monthly') or 'monthly'

    # Determine start date
    start = None
    psd = asset.get('payment_start_date')
    if psd:
        if isinstance(psd, datetime):
            start = psd.date()
        elif isinstance(psd, date):
            start = psd
    if not start:
        start = date.today()

    # The projected schedule opens from `liability_balance`, which holds the
    # loan's *current* balance (re-baselined from the latest statement). So the
    # projection must begin at today's date, not the loan's original
    # payment_start_date — otherwise it amortizes the current balance backward
    # in time and shows past-dated rows far below the real balance. For a loan
    # that hasn't started yet, payment_start_date is in the future and wins.
    proj_start = max(start, date.today())

    # Calculate escrow
    monthly_escrow = _calculate_monthly_escrow(asset)
    escrow_components = {
        'property_tax': round((asset.get('property_tax_annual') or 0) / 12, 2),
        'homeowners_insurance': round((asset.get('homeowners_insurance_annual') or 0) / 12, 2),
        'pmi': round(asset.get('pmi_monthly') or 0, 2),
        'hoa': round(asset.get('hoa_monthly') or 0, 2)
    }

    if frequency == 'biweekly':
        period_payment = payment / 2
        period_rate = rate / 100 / 26
        period_escrow = monthly_escrow / 2
        max_periods = 1200
    else:
        period_payment = payment
        period_rate = rate / 100 / 12
        period_escrow = monthly_escrow
        max_periods = 600

    schedule = []
    remaining = balance
    total_interest = 0
    total_paid = 0
    period_num = 0

    while remaining > 0.01 and period_num < max_periods:
        period_num += 1

        if frequency == 'biweekly':
            payment_date = proj_start + timedelta(weeks=2 * period_num)
        else:
            payment_date = proj_start + relativedelta(months=period_num)

        interest = remaining * period_rate

        if period_payment <= interest:
            break

        actual_payment = min(period_payment, remaining + interest)
        principal = actual_payment - interest
        remaining -= principal
        if remaining < 0.01:
            remaining = 0
        total_interest += interest
        total_paid += actual_payment

        date_fmt = '%b %d, %Y' if frequency == 'biweekly' else '%b %Y'
        row = {
            'number': period_num,
            'date': payment_date.strftime(date_fmt),
            'payment': round(actual_payment, 2),
            'principal': round(principal, 2),
            'interest': round(interest, 2),
            'balance': round(remaining, 2)
        }
        if monthly_escrow > 0:
            row['escrow'] = round(period_escrow, 2)
            row['total_payment'] = round(actual_payment + period_escrow, 2)
        schedule.append(row)

    if period_num < max_periods:
        if frequency == 'biweekly':
            payoff_dt = proj_start + timedelta(weeks=2 * period_num)
        else:
            payoff_dt = proj_start + relativedelta(months=period_num)
        payoff_date = payoff_dt.strftime('%B %Y')
    else:
        payoff_date = 'N/A'

    # Payment comparison
    comparison = _calculate_payment_comparison(balance, rate, payment, frequency)

    # Mortgage position (if we have a start date in the past or loan term)
    mortgage_position = None
    loan_term = asset.get('loan_term_months')
    if loan_term and loan_term > 0:
        # Estimate original balance from loan term
        monthly_rate_calc = rate / 100 / 12
        if monthly_rate_calc > 0:
            original_balance = payment * (1 - (1 + monthly_rate_calc) ** (-loan_term)) / monthly_rate_calc
        else:
            original_balance = payment * loan_term

        if frequency == 'biweekly':
            total_payments = loan_term  # approximate same number of months worth
        else:
            total_payments = loan_term

        # Calculate payments made based on start date
        if start < date.today():
            months_elapsed = (date.today().year - start.year) * 12 + (date.today().month - start.month)
            if frequency == 'biweekly':
                payments_made = int(months_elapsed * 26 / 12)
            else:
                payments_made = months_elapsed
            payments_made = max(0, min(payments_made, period_num))
        else:
            payments_made = 0

        equity_pct = round((1 - balance / original_balance) * 100, 1) if original_balance > 0 else 0
        pct_complete = round(payments_made / total_payments * 100, 1) if total_payments > 0 else 0

        mortgage_position = {
            'original_balance': round(original_balance, 2),
            'payments_made': payments_made,
            'total_payments': total_payments,
            'percent_complete': min(pct_complete, 100),
            'equity_percent': min(equity_pct, 100),
            'start_date': start.strftime('%b %Y')
        }

    # Escrow account simulation (first 36 periods)
    escrow_schedule = []
    if monthly_escrow > 0:
        escrow_balance = asset.get('escrow_balance') or 0
        prop_tax_semi = (asset.get('property_tax_annual') or 0) / max(len(ESCROW_PROPERTY_TAX_MONTHS), 1)
        insurance_annual = asset.get('homeowners_insurance_annual') or 0
        last_month_seen = None

        # Show starting balance row
        if escrow_balance > 0:
            escrow_schedule.append({
                'number': '-',
                'date': 'Starting Balance',
                'deposit': 0,
                'disbursement': 0,
                'disbursement_desc': '',
                'balance': round(escrow_balance, 2),
                'shortage': escrow_balance < 0
            })

        for i in range(1, min(37, period_num + 1)):
            if frequency == 'biweekly':
                pmt_date = proj_start + timedelta(weeks=2 * i)
            else:
                pmt_date = proj_start + relativedelta(months=i)

            deposit = round(period_escrow, 2)
            escrow_balance += deposit
            disbursement = 0
            disbursement_desc = ''

            current_month = (pmt_date.year, pmt_date.month)
            if current_month != last_month_seen:
                # Property tax installments (ESCROW_PROPERTY_TAX_MONTHS)
                if pmt_date.month in ESCROW_PROPERTY_TAX_MONTHS and prop_tax_semi > 0:
                    disbursement += prop_tax_semi
                    disbursement_desc = 'Property Tax'
                # Homeowner's insurance premium (ESCROW_INSURANCE_MONTH)
                if pmt_date.month == ESCROW_INSURANCE_MONTH and insurance_annual > 0:
                    disbursement += insurance_annual
                    if disbursement_desc:
                        disbursement_desc += ' + Insurance'
                    else:
                        disbursement_desc = 'Insurance'
            last_month_seen = current_month

            disbursement = round(disbursement, 2)
            escrow_balance -= disbursement

            escrow_schedule.append({
                'number': i,
                'date': schedule[i - 1]['date'] if i <= len(schedule) else '',
                'deposit': deposit,
                'disbursement': disbursement,
                'disbursement_desc': disbursement_desc,
                'balance': round(escrow_balance, 2),
                'shortage': escrow_balance < 0
            })

    summary = {
        'total_payments': round(total_paid, 2),
        'total_interest': round(total_interest, 2),
        'payoff_date': payoff_date,
        'periods': period_num,
        'frequency': frequency,
        'period_payment': round(period_payment, 2),
        'period_escrow': round(period_escrow, 2),
        'monthly_escrow': round(monthly_escrow, 2),
        'total_period_payment': round(period_payment + period_escrow, 2),
        'escrow_components': escrow_components,
        'escrow_balance': asset.get('escrow_balance') or 0
    }

    asset['_id'] = str(asset['_id'])
    return render_template('amortization.html', asset=asset, schedule=schedule,
                           summary=summary, escrow_schedule=escrow_schedule,
                           comparison=comparison, mortgage_position=mortgage_position)


# ============ HOME MOVE PLANNER (sell X, buy Y) ============

def _home_move_current_homes():
    """Real-estate assets with their amortized mortgage balance + monthly cost,
    used to prefill the 'current home' side of the planner."""
    homes = []
    for a in db.assets.find({'asset_type': 'real_estate'}):
        a['_id'] = str(a['_id'])
        if a.get('liability_balance'):
            _decorate_asset_liability(a)
            payoff = a.get('liability_current_balance') or 0
        else:
            payoff = 0
        homes.append({
            'id': a['_id'],
            'name': a.get('name'),
            'value': a.get('estimated_value') or 0,
            'payoff': round(payoff, 2),
            'monthly': round((a.get('monthly_payment') or 0) + _calculate_monthly_escrow(a), 2),
            'tax_annual': a.get('property_tax_annual') or 0,
        })
    return homes


def _home_move_monthly_income():
    total = 0
    for r in db.recurring_transactions.find({'transaction_type': 'income', **ACTIVE_RECURRING}):
        try:
            total += calc_monthly_amount(r['amount'], r['frequency'], r.get('day_of_month'))
        except Exception:
            continue
    return total


@app.route('/tools/home-move')
def home_move_page():
    homes = _home_move_current_homes()
    rates = home_move.fetch_mortgage_rates(force=request.args.get('refresh_rates') == '1')

    source = request.args
    scenario = None
    if request.args.get('scenario'):
        scenario = db.home_move_scenarios.find_one({'_id': safe_objectid(request.args['scenario'])})
        if scenario:
            source = scenario.get('params', {})

    params = home_move.parse_params(source)

    # Prefill the current home from the first real-estate asset if nothing given
    if not params['sale_price'] and homes:
        h = homes[0]
        params['sale_price'] = h['value']
        params['mortgage_payoff'] = h['payoff']
        params['current_monthly_housing'] = h['monthly']
        params['current_tax_annual'] = h['tax_annual']

    has_input = bool(params['purchase_price'] or params['sale_price'] or params['target_payment'])
    result = None
    solved_price = None
    buydown_compare = None
    if has_input:
        income = _home_move_monthly_income()
        if params['solve_for'] == 'payment' and params['target_payment'] > 0:
            solved_price, result = home_move.solve_price_for_payment(params, rates, income)
            if result:
                params['purchase_price'] = solved_price
            else:
                result = home_move.calculate_home_move(params, rates, income)
            if params['target_basis'] == 'year1':
                buydown_compare = home_move.compare_buydowns(params, rates, income)
        else:
            result = home_move.calculate_home_move(params, rates, income)

    scenarios = []
    for s in db.home_move_scenarios.find().sort('created_at', -1):
        scenarios.append({'id': str(s['_id']), 'name': s.get('name'),
                          'summary': s.get('summary', '')})

    return render_template('home_move.html', params=params, result=result, rates=rates,
                           homes=homes, scenarios=scenarios, loan_types=home_move.LOAN_TYPES,
                           active_scenario=str(scenario['_id']) if scenario else None,
                           solved_price=solved_price, buydown_compare=buydown_compare,
                           defaults=home_move.DEFAULTS)


@app.route('/tools/home-move/save', methods=['POST'])
def home_move_save():
    params = home_move.parse_params(request.form)
    rates = home_move.fetch_mortgage_rates()
    if params['solve_for'] == 'payment' and params['target_payment'] > 0:
        solved, _r = home_move.solve_price_for_payment(params, rates)
        if solved:
            params['purchase_price'] = solved
    name = (request.form.get('scenario_name') or '').strip() or \
        f"Sell ${params['sale_price']:,.0f} → Buy ${params['purchase_price']:,.0f}"
    result = home_move.calculate_home_move(params, rates)
    summary = f"${result['monthly']['total']:,.0f}/mo · cash to close ${result['purchase']['cash_to_close']:,.0f}"
    doc = {'name': name, 'params': params, 'summary': summary, 'created_at': datetime.now()}
    ins = db.home_move_scenarios.insert_one(doc)
    return redirect(url_for('home_move_page', scenario=str(ins.inserted_id)))


@app.route('/tools/home-move/delete/<scenario_id>', methods=['POST'])
def home_move_delete(scenario_id):
    db.home_move_scenarios.delete_one({'_id': safe_objectid(scenario_id)})
    return redirect(url_for('home_move_page'))


# ============ DEBT/ASSET CSV EXPORT/IMPORT ============

@app.route('/export/csv/debts')
def export_debts_csv():
    debts = list(db.debts.find())
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['name', 'debt_type', 'balance', 'interest_rate', 'minimum_payment', 'creditor', 'account_number', 'notes', 'deferment_end_date', 'subsidized'])
    for d in debts:
        ded = d.get('deferment_end_date')
        if ded:
            ded = ded.strftime('%Y-%m-%d') if isinstance(ded, (datetime, date)) else ded
        else:
            ded = ''
        writer.writerow([d['name'], d.get('debt_type', ''), d.get('balance', 0),
                         d.get('interest_rate', 0), d.get('minimum_payment', 0),
                         d.get('creditor', ''), d.get('account_number', ''), d.get('notes', ''),
                         ded, d.get('subsidized', False)])
    output.seek(0)
    return Response(output.getvalue(), mimetype='text/csv',
                    headers={'Content-Disposition': 'attachment; filename=debts.csv'})


@app.route('/export/csv/assets')
def export_assets_csv():
    assets = list(db.assets.find())
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['name', 'asset_type', 'estimated_value', 'purchase_date', 'purchase_price',
                     'liability_balance', 'liability_type', 'address', 'notes',
                     'interest_rate', 'loan_term_months', 'monthly_payment',
                     'payment_start_date', 'payment_frequency',
                     'property_tax_annual', 'homeowners_insurance_annual',
                     'pmi_monthly', 'hoa_monthly', 'escrow_balance'])
    for a in assets:
        purchase_date = ''
        if a.get('purchase_date'):
            purchase_date = a['purchase_date'].strftime('%Y-%m-%d') if isinstance(a['purchase_date'], datetime) else a['purchase_date']
        payment_start = ''
        if a.get('payment_start_date'):
            psd = a['payment_start_date']
            payment_start = psd.strftime('%Y-%m-%d') if isinstance(psd, (datetime, date)) else psd
        writer.writerow([a['name'], a.get('asset_type', ''), a.get('estimated_value', 0),
                         purchase_date, a.get('purchase_price', ''),
                         a.get('liability_balance', ''), a.get('liability_type', ''),
                         a.get('address', ''), a.get('notes', ''),
                         a.get('interest_rate', ''), a.get('loan_term_months', ''),
                         a.get('monthly_payment', ''),
                         payment_start, a.get('payment_frequency', 'monthly'),
                         a.get('property_tax_annual', ''), a.get('homeowners_insurance_annual', ''),
                         a.get('pmi_monthly', ''), a.get('hoa_monthly', ''),
                         a.get('escrow_balance', '')])
    output.seek(0)
    return Response(output.getvalue(), mimetype='text/csv',
                    headers={'Content-Disposition': 'attachment; filename=assets.csv'})


@app.route('/import/csv/debts', methods=['POST'])
def import_debts_csv():
    if 'file' not in request.files:
        return jsonify({'error': 'No file part'}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No selected file'}), 400

    clear_form = request.form.get('clear')
    clear_arg = request.args.get('clear', 'false')
    clear = (clear_form or clear_arg).lower() == 'true'
    if clear:
        db.debts.drop()

    stream = io.StringIO(file.stream.read().decode('utf-8-sig'))
    reader = csv.DictReader(stream)
    count = 0
    for row in reader:
        ded_str = row.get('deferment_end_date', '').strip()
        sub_str = row.get('subsidized', '').strip().lower()
        debt = {
            'name': row['name'].strip(),
            'debt_type': row.get('debt_type', 'other').strip(),
            'balance': float(row.get('balance', 0)),
            'interest_rate': float(row.get('interest_rate', 0)),
            'minimum_payment': float(row.get('minimum_payment', 0)),
            'creditor': row.get('creditor', '').strip() or None,
            'account_number': row.get('account_number', '').strip() or None,
            'notes': row.get('notes', '').strip() or None,
            'deferment_end_date': datetime.fromisoformat(ded_str) if ded_str else None,
            'subsidized': sub_str in ('true', '1', 'yes')
        }
        db.debts.insert_one(debt)
        count += 1

    if request.headers.get('Accept') == 'application/json':
        return jsonify({'message': f'Imported {count} debts'}), 200
    return redirect(url_for('admin_page'))


@app.route('/import/csv/assets', methods=['POST'])
def import_assets_csv():
    if 'file' not in request.files:
        return jsonify({'error': 'No file part'}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No selected file'}), 400

    clear_form = request.form.get('clear')
    clear_arg = request.args.get('clear', 'false')
    clear = (clear_form or clear_arg).lower() == 'true'
    if clear:
        db.assets.drop()

    stream = io.StringIO(file.stream.read().decode('utf-8-sig'))
    reader = csv.DictReader(stream)
    count = 0
    for row in reader:
        psd_str = row.get('payment_start_date', '').strip()
        asset = {
            'name': row['name'].strip(),
            'asset_type': row.get('asset_type', 'other').strip(),
            'estimated_value': float(row.get('estimated_value', 0)),
            'purchase_date': datetime.fromisoformat(row['purchase_date'].strip()) if row.get('purchase_date', '').strip() else None,
            'purchase_price': float(row['purchase_price']) if row.get('purchase_price', '').strip() else None,
            'liability_balance': float(row['liability_balance']) if row.get('liability_balance', '').strip() else None,
            'liability_type': row.get('liability_type', '').strip() or None,
            'address': row.get('address', '').strip() or None,
            'notes': row.get('notes', '').strip() or None,
            'interest_rate': float(row['interest_rate']) if row.get('interest_rate', '').strip() else None,
            'loan_term_months': int(row['loan_term_months']) if row.get('loan_term_months', '').strip() else None,
            'monthly_payment': float(row['monthly_payment']) if row.get('monthly_payment', '').strip() else None,
            'payment_start_date': datetime.fromisoformat(psd_str) if psd_str else None,
            'payment_frequency': row.get('payment_frequency', '').strip() or 'monthly',
            'property_tax_annual': float(row['property_tax_annual']) if row.get('property_tax_annual', '').strip() else None,
            'homeowners_insurance_annual': float(row['homeowners_insurance_annual']) if row.get('homeowners_insurance_annual', '').strip() else None,
            'pmi_monthly': float(row['pmi_monthly']) if row.get('pmi_monthly', '').strip() else None,
            'hoa_monthly': float(row['hoa_monthly']) if row.get('hoa_monthly', '').strip() else None,
            'escrow_balance': float(row['escrow_balance']) if row.get('escrow_balance', '').strip() else None
        }
        db.assets.insert_one(asset)
        count += 1

    if request.headers.get('Accept') == 'application/json':
        return jsonify({'message': f'Imported {count} assets'}), 200
    return redirect(url_for('admin_page'))


# ============ SAVINGS GOALS ============

def _accounts_balance_sum(account_ids):
    """Live combined balance of the given account ids."""
    if not account_ids:
        return 0.0
    oids = [safe_objectid(a) for a in account_ids if a]
    oids = [o for o in oids if o is not None]
    if not oids:
        return 0.0
    return sum(a.get('balance', 0) or 0 for a in db.accounts.find({'_id': {'$in': oids}}))


def _current_net_worth():
    """Live net worth: all account balances + asset values − all liabilities."""
    total_account_balance = sum(a.get('balance', 0) or 0 for a in db.accounts.find())
    total_assets_val = sum(a.get('estimated_value', 0) or 0 for a in db.assets.find())
    liabilities = _asset_liability_total() + _standalone_debt_total()
    return total_account_balance + total_assets_val - liabilities


def _liquid_savings_total():
    """Combined balance of cash accounts (checking + savings)."""
    return sum(a.get('balance', 0) or 0
               for a in db.accounts.find({'account_type': {'$in': ['checking', 'savings']}}))


def _monthly_expense_estimate(exclude_descriptions=None):
    """Estimated monthly expenses from recurring expense budget items.

    exclude_descriptions: optional iterable of descriptions to skip (e.g. the
    auto-managed 'Extra Debt Payment' line, so it doesn't reduce the surplus
    that funds it).
    """
    exclude = set(exclude_descriptions or ())
    total = 0.0
    for rt in db.recurring_transactions.find({'transaction_type': 'expense', **ACTIVE_RECURRING}):
        if exclude and rt.get('description') in exclude:
            continue
        total += calc_monthly_amount(rt.get('amount', 0) or 0,
                                     rt.get('frequency'), rt.get('day_of_month'))
    return total


def _retirement_eta(account_ids, target, horizon_months=600):
    """First month the combined projected value of linked accounts reaches target."""
    if not account_ids or not target or target <= 0:
        return None
    projections = []
    for aid in account_ids:
        acc = db.accounts.find_one({'_id': safe_objectid(aid)})
        if acc:
            projections.append(calculate_investment_projection(acc, months=horizon_months))
    if not projections:
        return None
    for i in range(horizon_months + 1):
        total = sum(p[i]['projected_value'] for p in projections if i < len(p))
        if total >= target:
            eta_date = date.today() + relativedelta(months=i)
            years = i // 12
            rem_months = i % 12
            if years and rem_months:
                human = f"{years}y {rem_months}m"
            elif years:
                human = f"{years}y"
            else:
                human = f"{rem_months}m"
            return {'months': i, 'label': eta_date.strftime('%b %Y'), 'human': human}
    return None  # target not reached within horizon


def _decorate_goal(g):
    """Compute live current/target/progress for a goal based on its type.

    Mutates and returns the goal dict with display-ready fields. For live goal
    types the stored current_amount is ignored in favor of real data.
    """
    g['_id'] = str(g['_id'])
    gt = g.get('goal_type') or 'manual'
    g['goal_type'] = gt
    g.setdefault('linked_account_ids', [])
    g.setdefault('linked_debt_id', None)
    g.setdefault('baseline_amount', None)
    g.setdefault('months_target', None)
    g['live'] = gt in LIVE_GOAL_TYPES
    g['type_label'] = dict(GOAL_TYPES).get(gt, 'Manual')
    g['remaining_debt'] = None
    g['eta_label'] = None
    g['eta_human'] = None
    g['monthly_expense'] = None
    g['months_covered'] = None
    g['plan'] = None

    target = g.get('target_amount') or 0

    if gt == 'account':
        current = _accounts_balance_sum(g['linked_account_ids'])
    elif gt == 'networth':
        current = _current_net_worth()
    elif gt == 'emergency':
        monthly = _monthly_expense_estimate()
        months = g.get('months_target') or 0
        target = monthly * months
        g['monthly_expense'] = monthly
        current = (_accounts_balance_sum(g['linked_account_ids'])
                   if g['linked_account_ids'] else _liquid_savings_total())
        g['months_covered'] = (current / monthly) if monthly > 0 else 0
    elif gt == 'debt_free':
        live_debt = _standalone_debt_total()
        baseline = g.get('baseline_amount')
        if not baseline or baseline <= 0:
            baseline = live_debt
        target = baseline
        current = max(0, baseline - live_debt)  # amount paid off
        g['remaining_debt'] = live_debt
        # Attach a compact payoff-plan summary for the widget
        plan = _get_debt_plan()
        if plan['target']:
            g['plan'] = {
                'strategy': plan['strategy'],
                'strategy_label': 'Avalanche' if plan['strategy'] == 'avalanche' else 'Snowball',
                'target_name': plan['target_name'],
                'extra': plan['extra'],
                'fits': plan['fits'],
                'debtfree_label': plan['debtfree_label'],
                'months_to_free': plan['months_to_free'],
                'interest_saved': plan['interest_saved'],
            }
    elif gt == 'debt_payoff':
        live_debt = 0
        did = g.get('linked_debt_id')
        if did:
            d = db.debts.find_one({'_id': safe_objectid(did)})
            if d:
                _decorate_debt(d)
                live_debt = d['current_balance']
        baseline = g.get('baseline_amount')
        if not baseline or baseline <= 0:
            baseline = live_debt
        target = baseline
        current = max(0, baseline - live_debt)
        g['remaining_debt'] = live_debt
    elif gt == 'retirement':
        current = _accounts_balance_sum(g['linked_account_ids'])
        eta = _retirement_eta(g['linked_account_ids'], target)
        if eta:
            g['eta_label'] = eta['label']
            g['eta_human'] = eta['human']
    else:  # manual
        current = g.get('current_amount') or 0

    g['current_amount'] = round(current, 2)
    g['target_amount'] = round(target, 2)
    g['progress'] = min(100, (current / target * 100)) if target > 0 else 0
    g['progress_pct'] = g['progress']  # alias used by dashboard card
    g['remaining'] = max(0, round(target - current, 2))

    td = g.get('target_date')
    if isinstance(td, datetime):
        g['target_date'] = td.strftime('%Y-%m-%d')
        g['days_remaining'] = (td.date() - date.today()).days
    elif isinstance(td, str) and td:
        try:
            g['days_remaining'] = (datetime.strptime(td, '%Y-%m-%d').date() - date.today()).days
        except ValueError:
            g['days_remaining'] = None
    else:
        g['target_date'] = None
        g['days_remaining'] = None
    return g


def _goal_fields_from_form(form):
    """Build a persistable field dict from the add/edit goal form."""
    gt = form.get('goal_type') or 'manual'
    linked_account_ids = [a for a in form.getlist('linked_account_ids') if a]
    linked_debt_id = form.get('linked_debt_id') or None

    baseline = form.get('baseline_amount')
    baseline_amount = float(baseline) if baseline else None
    # Capture the starting debt at creation so progress measures from today.
    if gt == 'debt_free' and baseline_amount is None:
        baseline_amount = _standalone_debt_total()
    elif gt == 'debt_payoff' and baseline_amount is None and linked_debt_id:
        d = db.debts.find_one({'_id': safe_objectid(linked_debt_id)})
        if d:
            _decorate_debt(d)
            baseline_amount = d['current_balance']

    return {
        'name': form['name'],
        'goal_type': gt,
        'target_amount': float(form['target_amount']) if form.get('target_amount') else 0,
        'current_amount': float(form.get('current_amount') or 0),
        'target_date': datetime.fromisoformat(form['target_date']) if form.get('target_date') else None,
        'category': form.get('category'),
        'notes': form.get('notes'),
        'linked_account_ids': linked_account_ids,
        'linked_debt_id': linked_debt_id,
        'baseline_amount': baseline_amount,
        'months_target': float(form['months_target']) if form.get('months_target') else None,
    }


@app.route('/goals')
def goals_page():
    _apply_auto_links()  # keep debt balances current for debt-linked goals
    goals = [_decorate_goal(g) for g in db.savings_goals.find()]

    # Money-set-aside summary (excludes debt-free / net-worth milestone goals,
    # whose targets would otherwise dwarf the totals).
    saveable = [g for g in goals if g['goal_type'] in SAVINGS_GOAL_TYPES]
    total_saved = sum(g['current_amount'] for g in saveable)
    total_target = sum(g['target_amount'] for g in saveable)

    # Account + debt options for the goal-type pickers
    accounts = []
    for a in db.accounts.find().sort('name', 1):
        accounts.append({'_id': str(a['_id']), 'name': a.get('name'),
                         'account_type': a.get('account_type'),
                         'institution': a.get('institution')})
    debts = [{'_id': str(d['_id']), 'name': d.get('name')} for d in db.debts.find().sort('name', 1)]

    return render_template('goals.html', goals=goals,
                           goal_categories=GOAL_CATEGORIES, goal_types=GOAL_TYPES,
                           accounts=accounts, debts=debts,
                           total_saved=total_saved, total_target=total_target)


@app.route('/goals/add', methods=['POST'])
def add_goal():
    fields = _goal_fields_from_form(request.form)
    new_goal = SavingsGoal(**fields)
    db.savings_goals.insert_one(new_goal.__dict__)
    return redirect(url_for('goals_page'))


@app.route('/goals/edit/<goal_id>', methods=['GET', 'POST'])
def edit_goal(goal_id):
    if request.method == 'POST':
        fields = _goal_fields_from_form(request.form)
        db.savings_goals.update_one(
            {'_id': safe_objectid(goal_id)},
            {'$set': fields}
        )
        return redirect(url_for('goals_page'))
    else:
        goal = db.savings_goals.find_one({'_id': safe_objectid(goal_id)})
        if goal:
            goal['_id'] = str(goal['_id'])
            goal.setdefault('goal_type', 'manual')
            goal.setdefault('linked_account_ids', [])
            goal.setdefault('linked_debt_id', None)
            goal.setdefault('baseline_amount', None)
            goal.setdefault('months_target', None)
            if goal.get('target_date') and isinstance(goal['target_date'], datetime):
                goal['target_date'] = goal['target_date'].strftime('%Y-%m-%d')
            accounts = []
            for a in db.accounts.find().sort('name', 1):
                accounts.append({'_id': str(a['_id']), 'name': a.get('name'),
                                 'account_type': a.get('account_type'),
                                 'institution': a.get('institution')})
            debts = [{'_id': str(d['_id']), 'name': d.get('name')} for d in db.debts.find().sort('name', 1)]
            return render_template('edit_goal.html', goal=goal,
                                   goal_categories=GOAL_CATEGORIES, goal_types=GOAL_TYPES,
                                   accounts=accounts, debts=debts)
        return redirect(url_for('goals_page'))


@app.route('/goals/delete/<goal_id>', methods=['POST'])
def delete_goal(goal_id):
    db.savings_goals.delete_one({'_id': safe_objectid(goal_id)})
    return redirect(url_for('goals_page'))


@app.route('/goals/contribute/<goal_id>', methods=['POST'])
def contribute_to_goal(goal_id):
    data = request.form if request.form else request.get_json()
    amount = float(data.get('amount', 0))
    db.savings_goals.update_one(
        {'_id': safe_objectid(goal_id)},
        {'$inc': {'current_amount': amount}}
    )
    if request.headers.get('Accept') == 'application/json':
        return jsonify({'message': 'Contribution added'}), 200
    return redirect(url_for('goals_page'))


# ============ NET WORTH HISTORY ============

@app.route('/networth')
def networth_page():
    # Get current totals
    accounts = list(db.accounts.find())
    assets = list(db.assets.find())
    debts = list(db.debts.find())

    total_account_balance = sum(a.get('balance', 0) for a in accounts)
    total_asset_value = sum(a.get('estimated_value', 0) for a in assets)
    asset_liabilities = _asset_liability_total()
    standalone_debts = _standalone_debt_total()
    total_liabilities = asset_liabilities + standalone_debts

    current_assets = total_account_balance + total_asset_value
    current_net_worth = current_assets - total_liabilities

    # Get historical snapshots
    snapshots = list(db.networth_snapshots.find().sort('date', -1).limit(24))
    for s in snapshots:
        s['_id'] = str(s['_id'])
        if isinstance(s['date'], datetime):
            s['date'] = s['date'].strftime('%Y-%m-%d')

    # Project net worth forward using investment growth
    investment_accounts_list = [a for a in accounts
                                if a.get('account_type') in INVESTMENT_ACCOUNT_TYPES
                                and a.get('estimated_return_rate')]
    nw_projection = []
    if investment_accounts_list:
        # Project 24 months forward
        months_forward = 24
        # Sum non-investment account balances (assumed static for projection)
        static_balance = sum(a.get('balance', 0) for a in accounts
                           if a.get('account_type') not in INVESTMENT_ACCOUNT_TYPES)
        static_assets = total_asset_value
        static_liabilities = total_liabilities

        # Build combined investment projection
        for i in range(months_forward + 1):
            month_date = date.today() + relativedelta(months=i)
            total_inv = 0
            for inv_acc in investment_accounts_list:
                proj = calculate_investment_projection(inv_acc, months=i)
                if proj:
                    total_inv += proj[-1]['projected_value']
            # Non-investment accounts that do have projections contribute their current balance
            for a in accounts:
                if a.get('account_type') not in INVESTMENT_ACCOUNT_TYPES:
                    continue
                if not a.get('estimated_return_rate'):
                    total_inv += a.get('balance', 0)

            projected_assets = static_balance + static_assets + total_inv
            projected_nw = projected_assets - static_liabilities
            nw_projection.append({
                'month': month_date.strftime('%b %Y'),
                'projected_net_worth': round(projected_nw, 2)
            })

    return render_template('networth.html',
                           current_assets=current_assets,
                           current_liabilities=total_liabilities,
                           current_net_worth=current_net_worth,
                           snapshots=snapshots,
                           nw_projection=nw_projection)


@app.route('/networth/snapshot', methods=['POST'])
def take_networth_snapshot():
    # Calculate current values
    accounts = list(db.accounts.find())
    assets = list(db.assets.find())
    debts = list(db.debts.find())

    total_account_balance = sum(a.get('balance', 0) for a in accounts)
    total_asset_value = sum(a.get('estimated_value', 0) for a in assets)
    asset_liabilities = _asset_liability_total()
    standalone_debts = _standalone_debt_total()
    total_liabilities = asset_liabilities + standalone_debts

    total_assets = total_account_balance + total_asset_value
    net_worth = total_assets - total_liabilities

    # Build breakdown
    breakdown = {
        'accounts': total_account_balance,
        'assets': total_asset_value,
        'asset_liabilities': asset_liabilities,
        'standalone_debts': standalone_debts,
        'liabilities': total_liabilities
    }

    snapshot = NetWorthSnapshot(
        date=datetime.now(),
        total_assets=total_assets,
        total_liabilities=total_liabilities,
        net_worth=net_worth,
        breakdown=breakdown
    )
    db.networth_snapshots.insert_one(snapshot.__dict__)

    if request.headers.get('Accept') == 'application/json':
        return jsonify({'message': 'Snapshot recorded', 'net_worth': net_worth}), 200
    return redirect(url_for('networth_page'))


@app.route('/api/accounts')
def accounts_api():
    """Return list of accounts for dropdowns."""
    accounts = list(db.accounts.find())
    result = []
    for acc in accounts:
        result.append({
            'id': str(acc['_id']),
            'name': acc.get('name', 'Unknown'),
            'account_type': acc.get('account_type', 'other'),
            'is_primary': acc.get('is_primary', False)
        })
    return jsonify(result)


@app.route('/api/networth/history')
def networth_history_api():
    snapshots = list(db.networth_snapshots.find().sort('date', 1))
    data = []
    for s in snapshots:
        data.append({
            'date': s['date'].strftime('%Y-%m-%d') if isinstance(s['date'], datetime) else s['date'],
            'net_worth': s['net_worth'],
            'total_assets': s['total_assets'],
            'total_liabilities': s['total_liabilities']
        })
    return jsonify(data)


# ============ SPENDING ANALYTICS ============

@app.route('/analytics')
def analytics_page():
    return render_template('analytics.html', expense_categories=CATEGORIES)


@app.route('/api/analytics/spending-by-category')
def spending_by_category_api():
    # Get month parameter or default to current month
    month_str = request.args.get('month')
    if month_str:
        year, month = map(int, month_str.split('-'))
    else:
        today = date.today()
        year, month = today.year, today.month

    month_start = datetime(year, month, 1)
    month_end = datetime(year, month, calendar.monthrange(year, month)[1], 23, 59, 59)

    # Get transactions for the month
    transactions = list(db.transactions.find({
        'date': {'$gte': month_start, '$lte': month_end},
        'transaction_type': 'expense'
    }))

    # Also include recurring transactions
    recurring = list(db.recurring_transactions.find({'transaction_type': 'expense', **ACTIVE_RECURRING}))

    # Group by category
    category_totals = {}
    for t in transactions:
        cat = t.get('category', 'Other') or 'Other'
        category_totals[cat] = category_totals.get(cat, 0) + t.get('amount', 0)

    # Add recurring for this month
    for r in recurring:
        cat = r.get('category', 'Other') or 'Other'
        # Check if applies to this month
        r_start = r['start_date']
        if isinstance(r_start, datetime):
            r_start = r_start.date()
        if r_start <= month_end.date():
            if r['frequency'] == 'monthly':
                days_count = len(str(r.get('day_of_month', '1')).split(','))
                category_totals[cat] = category_totals.get(cat, 0) + (r['amount'] * days_count)
            elif r['frequency'] == 'biweekly':
                ms, me = month_start.date(), month_end.date()
                d = r_start
                if d < ms:
                    d += timedelta(days=((ms - d).days + 13) // 14 * 14)
                occurrences = 0
                while d <= me:
                    occurrences += 1
                    d += timedelta(days=14)
                category_totals[cat] = category_totals.get(cat, 0) + (r['amount'] * occurrences)
            elif r['frequency'] == 'yearly' and r_start.month == month:
                category_totals[cat] = category_totals.get(cat, 0) + r['amount']

    return jsonify({
        'labels': list(category_totals.keys()),
        'data': list(category_totals.values()),
        'month': f"{year}-{month:02d}"
    })


@app.route('/api/analytics/monthly-trend')
def monthly_trend_api():
    # Get last 6 months of data
    today = date.today()
    months_data = []

    for i in range(5, -1, -1):
        month_date = today - relativedelta(months=i)
        month_start = datetime(month_date.year, month_date.month, 1)
        month_end = datetime(month_date.year, month_date.month,
                            calendar.monthrange(month_date.year, month_date.month)[1], 23, 59, 59)

        # Get actual transactions
        income = 0
        expenses = 0

        transactions = list(db.transactions.find({
            'date': {'$gte': month_start, '$lte': month_end}
        }))

        for t in transactions:
            if t['transaction_type'] == 'income':
                income += t['amount']
            else:
                expenses += t['amount']

        months_data.append({
            'month': month_date.strftime('%b %Y'),
            'income': income,
            'expenses': expenses
        })

    return jsonify(months_data)


# ============ BILL CALENDAR ============

@app.route('/calendar')
def calendar_page():
    return render_template('calendar.html')


@app.route('/api/calendar/events')
def calendar_events_api():
    year = int(request.args.get('year', date.today().year))
    month = int(request.args.get('month', date.today().month))

    month_start = date(year, month, 1)
    month_end = date(year, month, calendar.monthrange(year, month)[1])

    events = []

    # Get recurring transactions
    recurring = list(db.recurring_transactions.find(ACTIVE_RECURRING))

    for r in recurring:
        r_start = r['start_date']
        if isinstance(r_start, datetime):
            r_start = r_start.date()

        r_end = r.get('end_date')
        if r_end and isinstance(r_end, datetime):
            r_end = r_end.date()

        # Skip if hasn't started or has ended
        if r_start > month_end:
            continue
        if r_end and r_end < month_start:
            continue

        # Same schedule as the ledger outlook (weekly, biweekly, monthly,
        # bi-monthly, yearly), minus any occurrence after the end date
        for d in occurrence_dates(r, month_start, month_end):
            if r_end and d > r_end:
                continue
            events.append({
                'id': str(r['_id']),
                'title': r['description'],
                'date': d.isoformat(),
                'amount': r['amount'],
                'type': r['transaction_type'],
                'category': r.get('category', 'Other'),
                'auto_pay': r.get('auto_pay', False)
            })

    # Sort by date
    events.sort(key=lambda x: x['date'])
    return jsonify(events)


# ========================================
# CREDIT CARD STATEMENTS
# ========================================

def _month_range(start_ym, end_ym):
    """Inclusive list of 'YYYY-MM' strings from start_ym to end_ym. Empty if start > end."""
    try:
        sy, sm = (int(x) for x in start_ym.split('-'))
        ey, em = (int(x) for x in end_ym.split('-'))
    except (ValueError, AttributeError):
        return []
    out = []
    y, m = sy, sm
    while (y, m) <= (ey, em):
        out.append(f'{y:04d}-{m:02d}')
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return out


def _last_closed_month():
    """The previous calendar month as 'YYYY-MM' — the latest statement we'd expect to have."""
    first_of_month = datetime.now().replace(day=1)
    return (first_of_month - timedelta(days=1)).strftime('%Y-%m')


def compute_missing_statements():
    """For each card that has any statement history, list the statement months missing
    between its earliest uploaded statement and the last closed month. A card you stopped
    using will keep surfacing gaps until its last month catches up to the range end.

    A month also counts as covered when the card has transactions dated in it. An upload
    is stamped with a single statement_date (its majority month), so one CSV spanning
    Aug 1 - Sep 16 labels every row 2026-09 and August would otherwise read as missing."""
    latest_expected = _last_closed_month()
    pipeline = [{'$group': {
        '_id': '$card_name',
        'months': {'$addToSet': '$statement_date'},
        'tx_months': {'$addToSet': {'$cond': [
            {'$eq': [{'$type': '$date'}, 'date']},
            {'$dateToString': {'format': '%Y-%m', 'date': '$date'}},
            None,
        ]}},
    }}]
    missing = []
    for doc in db.cc_transactions.aggregate(pipeline):
        card_name = doc.get('_id')
        labelled = {m for m in (doc.get('months') or []) if m}
        if not card_name or not labelled:
            continue
        have = labelled | {m for m in (doc.get('tx_months') or []) if m}
        earliest = min(labelled)
        gaps = [m for m in _month_range(earliest, latest_expected) if m not in have]
        if gaps:
            missing.append({'card_name': card_name, 'months': gaps, 'count': len(gaps)})
    missing.sort(key=lambda x: x['card_name'])
    return missing


def _cc_date_range_query(start_date, end_date):
    """Build a Mongo range query on the transaction `date` field. Returns {} if no bounds."""
    q = {}
    if start_date:
        try:
            q['$gte'] = datetime.fromisoformat(start_date)
        except ValueError:
            pass
    if end_date:
        try:
            end_dt = datetime.fromisoformat(end_date).replace(hour=23, minute=59, second=59)
            q['$lte'] = end_dt
        except ValueError:
            pass
    return q


@app.route('/credit-cards')
def credit_cards_page():
    card_filter = request.args.get('card', 'all')
    month_filter = request.args.get('month', '')
    category_filter = request.args.get('category', 'all')
    start_date = request.args.get('start_date', '')
    end_date = request.args.get('end_date', '')
    has_range = bool(start_date or end_date)

    # Make sure the cc_cards collection reflects every card that has ever had a
    # statement uploaded, so existing data shows up in the new card picker.
    ensure_cc_cards_seeded()
    normalize_existing_cc_credits()
    mark_existing_cc_credits_category()
    categorize_existing_cc_phone()
    recategorize_existing_cc_other()
    # Also on page load, not just after an upload: rows can arrive at 'Other'
    # from a rules change or an earlier run with the LLM offline, and those
    # would otherwise wait for the next statement. It is a guarded daemon thread
    # that no-ops when there is nothing left to do.
    _categorize_leftovers_async()
    cards = list(db.cc_cards.find().sort('name', 1))

    # Get distinct card names and months from data
    all_cards = db.cc_transactions.distinct('card_name')
    all_months = sorted(db.cc_transactions.distinct('statement_date'), reverse=True)

    # Default month to most recent if not specified (skip when a date range is set)
    if not month_filter and not has_range and all_months:
        month_filter = all_months[0]

    # Build query
    query = {}
    if card_filter and card_filter != 'all':
        query['card_name'] = card_filter
    if category_filter and category_filter != 'all':
        query['category'] = category_filter
    date_query = _cc_date_range_query(start_date, end_date)
    if date_query:
        # A date range spans statements, so it overrides the month filter.
        query['date'] = date_query
    elif month_filter:
        query['statement_date'] = month_filter

    transactions = list(db.cc_transactions.find(query).sort('date', -1))

    # Active payment plans for the per-row "link to plan" dropdown
    active_plans = list(db.payment_plans.find({'status': {'$ne': 'closed'}}).sort('vendor', 1))
    for p in active_plans:
        p['_id'] = str(p['_id'])
    # Stringify any payment_plan_id on transactions for template comparison
    for t in transactions:
        # Rows imported before the LLM pass existed have no such key
        t.setdefault('categorized_by', None)
        if t.get('payment_plan_id'):
            t['payment_plan_id'] = str(t['payment_plan_id'])

    # Calculate summary stats
    purchases = [t for t in transactions if t['amount'] > 0]
    total_spending = sum(t['amount'] for t in purchases)
    transaction_count = len(purchases)
    avg_transaction = total_spending / transaction_count if transaction_count else 0

    # Category breakdown
    category_totals = {}
    for t in purchases:
        cat = t.get('category', 'Other')
        category_totals[cat] = category_totals.get(cat, 0) + t['amount']
    top_category = max(category_totals, key=category_totals.get) if category_totals else 'N/A'

    # Sort categories by amount descending
    sorted_categories = sorted(category_totals.items(), key=lambda x: x[1], reverse=True)

    # Statement management: group by card_name + statement_date
    pipeline = [
        {'$group': {
            '_id': {'card_name': '$card_name', 'statement_date': '$statement_date'},
            'count': {'$sum': 1},
            'total': {'$sum': '$amount'}
        }},
        {'$sort': {'_id.statement_date': -1, '_id.card_name': 1}}
    ]
    statements = list(db.cc_transactions.aggregate(pipeline))

    # Annotate each statement with the on-disk filename so the template can
    # offer a download link if the raw CSV was preserved.
    for s in statements:
        fname = f"{_cc_filename_slug(s['_id']['card_name'])}-{s['_id']['statement_date']}.csv"
        s['raw_filename'] = fname if os.path.exists(os.path.join(CC_STATEMENT_DIR, fname)) else None

    # Which statement months are missing between each card's first upload and last month
    missing_statements = compute_missing_statements()

    return render_template('credit_cards.html',
        transactions=transactions,
        all_cards=all_cards,
        all_months=all_months,
        card_filter=card_filter,
        month_filter=month_filter,
        category_filter=category_filter,
        start_date=start_date,
        end_date=end_date,
        total_spending=total_spending,
        transaction_count=transaction_count,
        avg_transaction=avg_transaction,
        top_category=top_category,
        sorted_categories=sorted_categories,
        statements=statements,
        missing_statements=missing_statements,
        last_expected_month=_last_closed_month(),
        cards=cards,
        card_issuers=CARD_ISSUERS,
        card_issuer_labels=dict(CARD_ISSUERS),
        # Alphabetical so the classification dropdowns are scannable; the
        # definition order of CC_CATEGORIES still drives rule precedence.
        cc_categories=sorted(CC_CATEGORIES),
        active_plans=active_plans
    )


# Optional LLM used for credit-card categorization and AI spending analysis.
# Any server exposing the Ollama-compatible /api/chat endpoint works (a plain
# local Ollama, or a gateway in front of one). OLLAMA_API_KEY is sent as a
# Bearer token when set; leave it blank for an unauthenticated local Ollama.
# If the endpoint is unreachable, categorization logs a warning and falls back
# to the keyword rules.
OLLAMA_URL = os.environ.get('OLLAMA_URL', 'http://localhost:11434')
OLLAMA_MODEL = os.environ.get('OLLAMA_MODEL', 'granite4.2:3b')
OLLAMA_API_KEY = os.environ.get('OLLAMA_API_KEY', '')
CC_LLM_TIMEOUT = int(os.environ.get('CC_LLM_TIMEOUT', '60'))
CC_LLM_ENABLED = os.environ.get('CC_LLM_ENABLED', 'true').lower() == 'true'
# Every part of the shape below was measured rather than assumed,
# against the merchants the keyword rules cannot resolve:
#
#   batched 8, reasoning on   2/8   54s per description
#   think=False               3/16  0.6s per description
#   think=False, one at once  6/16  1.0s per description
#   ...plus the two-vote gate 5 committed, 5 right, 0 wrong, 17 abstained
#
# So: reasoning off (it was the whole cost), one merchant per request (batching
# made the model collapse to a single category for the batch), and two
# differently-worded votes with the answer taken only when they agree. A
# disagreement leaves the row at 'Other', which is the right failure: 'Other' is
# visibly unset and invites a correction, a wrong label quietly skews the charts.
# Trimming the category list was also tried and made it worse — it forces the
# model to pick a neighbour instead of abstaining. Keep the full list.
CC_LLM_VOTE_PROMPTS = [
    "What does this merchant sell? Pick one category.\nCategories: {cats}\n\nMerchant: {desc}",
    "A credit card statement shows this merchant. Which spending category does the"
    " purchase belong in?\nCategories: {cats}\n\n{desc}",
]

# Payment-processor prefixes that sit in front of the real merchant name.
_CC_PROCESSOR_RE = re.compile(
    r'^(SQ\s*\*|TST\s*\*|SP\s+|PY\s*\*|IN\s*\*|BLVD\s*\*|FYF\*|PAYPAL\s*\*|'
    r'TCKTWEB\*|CTLP\*|MED\*|WL\s+\*|VCN\*|FRG\*|AMZ\*|SUNDAY\*|MMS-|GOOGLE\s*\*)',
    re.I)
_CC_STATE_RE = (r'A[LKZR]|C[AOT]|D[EC]|FL|GA|HI|I[DLNA]|K[SY]|LA|M[EDAINSOT]|'
                r'N[EVHJMYCD]|O[HKR]|PA|RI|S[CD]|T[NX]|UT|V[TA]|W[AVIY]')


def clean_descriptor(description):
    """Strip the parts of a bank descriptor that carry no merchant signal.

    Only used for the LLM — the keyword rules match the raw string, since some
    of them key off exactly these prefixes. Small models do measurably better on
    'JOES CAFE Springfield' than on 'SQ *JOES CAFE Springfield OH'."""
    s = _CC_PROCESSOR_RE.sub('', (description or '').strip()).strip()
    s = re.sub(r'\s+', ' ', s)
    s = re.sub(r'\s+(?:[A-Z]{3}|' + _CC_STATE_RE + r')$', '', s, flags=re.I)
    s = re.sub(r'\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}', '', s)   # phone numbers
    s = re.sub(r'[#*]\s*\d+', '', s)                             # store numbers
    s = re.sub(r'\b\d{4,}\b', '', s)                             # reference numbers
    s = re.sub(r'G\.CO[/ ]?HELPPAY#?', '', s, flags=re.I)
    return re.sub(r'\s+', ' ', s).strip(' ,.-') or (description or '').strip()


def _ollama_headers():
    """Bearer auth for an LLM gateway; empty for an unauthenticated Ollama."""
    return {'Authorization': f'Bearer {OLLAMA_API_KEY}'} if OLLAMA_API_KEY else {}


def categorize_transaction(description):
    """Match transaction description against keyword rules. Returns category or 'Other'."""
    desc_upper = description.upper()
    # Disambiguation rules win over the broad keyword lists.
    for keyword, category in MERCHANT_OVERRIDES:
        if keyword in desc_upper:
            return category
    for category, keywords in MERCHANT_CATEGORY_RULES.items():
        for keyword in keywords:
            if keyword in desc_upper:
                return category
    return 'Other'


def _llm_vote(cleaned, template):
    """One categorization vote. Returns a category from CC_CATEGORIES, or None.

    think=False matters enormously here: with reasoning left on, a small local model spent
    ~54s per description emitting an analysis it then discarded. Off, it answers
    in well under a second. The response schema pins the enum, so a malformed or
    invented category is not possible."""
    import requests
    schema = {
        'type': 'object',
        'properties': {'category': {'type': 'string', 'enum': CC_CATEGORIES}},
        'required': ['category'],
    }
    prompt = template.format(cats=', '.join(CC_CATEGORIES), desc=cleaned)
    resp = requests.post(f"{OLLAMA_URL}/api/chat", json={
        'model': OLLAMA_MODEL,
        'messages': [{'role': 'user', 'content': prompt}],
        'stream': False,
        'format': schema,
        'think': False,
        'options': {'temperature': 0, 'num_ctx': 2048},
    }, headers=_ollama_headers(), timeout=CC_LLM_TIMEOUT)
    if resp.status_code != 200:
        # Loud on purpose: a dead or unauthenticated endpoint used to look
        # identical to "the LLM had nothing to add".
        logger.warning('LLM categorization unavailable: %s returned %s %s',
                       OLLAMA_URL, resp.status_code, resp.text[:200])
        return None
    category = json.loads(resp.json().get('message', {}).get('content', '')).get('category')
    return category if category in CC_CATEGORIES else None


def categorize_with_llm(description):
    """Categorize one merchant, or return None to leave it alone.

    Two differently-worded votes, and the answer is taken only when they agree
    on something other than 'Other'. On the merchants the keyword rules cannot
    resolve this committed 5 of 22 and got all 5 right, abstaining on the rest —
    which is the point. Recall here is cheap to improve by hand; a wrong
    category is not, because nothing flags it for review."""
    cleaned = clean_descriptor(description)
    if not cleaned:
        return None
    try:
        votes = [_llm_vote(cleaned, t) for t in CC_LLM_VOTE_PROMPTS]
    except Exception:
        logger.exception('LLM categorization request failed')
        return None
    first = votes[0]
    if first and first != 'Other' and all(v == first for v in votes[1:]):
        return first
    return None


CC_STATEMENT_DIR = os.environ.get(
    'CC_STATEMENT_DIR',
    os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'cc_statements')
)


def _cc_filename_slug(value):
    """Filesystem-safe slug for use in statement filenames."""
    s = re.sub(r'[^\w\-]+', '-', (value or '').strip())
    s = re.sub(r'-+', '-', s).strip('-').lower()
    return s or 'card'


def save_raw_cc_statement(card_name, statement_date, raw_bytes):
    """Persist the raw CSV to disk. Returns filename or None on failure."""
    try:
        os.makedirs(CC_STATEMENT_DIR, exist_ok=True)
        fname = f'{_cc_filename_slug(card_name)}-{statement_date}.csv'
        with open(os.path.join(CC_STATEMENT_DIR, fname), 'wb') as f:
            f.write(raw_bytes)
        return fname
    except OSError as e:
        logger.warning(f'Could not save raw CC statement: {e}')
        return None


def ensure_cc_cards_seeded():
    """Lazy-backfill cc_cards from any (card_name, card_issuer) found on existing
    cc_transactions but not yet present as a card record. Safe to call repeatedly."""
    existing = {c['name'] for c in db.cc_cards.find({}, {'name': 1})}
    pipeline = [{'$group': {'_id': {'name': '$card_name', 'issuer': '$card_issuer'}}}]
    for doc in db.cc_transactions.aggregate(pipeline):
        key = doc.get('_id') or {}
        name = (key.get('name') or '').strip()
        issuer = key.get('issuer') or 'other'
        if name and name not in existing:
            db.cc_cards.insert_one({
                'name': name,
                'issuer': issuer,
                'created_at': datetime.now()
            })
            existing.add(name)


def normalize_existing_cc_credits():
    """One-time migration: flip previously-imported charges that are really merchant
    credits/refunds to a negative amount, matching how imports now store them. Guarded
    by a flag doc so it runs once; also idempotent (flipped rows no longer match)."""
    if db.app_migrations.find_one({'_id': 'cc_credits_negative_v1'}):
        return
    credit_re = '|'.join(re.escape(k) for k in CREDIT_KEYWORDS)
    payment_re = '|'.join(re.escape(k) for k in PAYMENT_KEYWORDS)
    db.cc_transactions.update_many(
        {
            'amount': {'$gt': 0},
            '$and': [
                {'description': {'$regex': credit_re, '$options': 'i'}},
                {'description': {'$not': {'$regex': payment_re, '$options': 'i'}}},
            ],
        },
        {'$mul': {'amount': -1}}
    )
    db.app_migrations.insert_one({'_id': 'cc_credits_negative_v1', 'run_at': datetime.now()})


def mark_existing_cc_credits_category():
    """One-time migration: tag already-stored credits (negative amounts) with the
    'Credit' category. Runs after normalize_existing_cc_credits(), so by this point
    every credit is negative. Guarded + idempotent."""
    if db.app_migrations.find_one({'_id': 'cc_credits_category_v1'}):
        return
    db.cc_transactions.update_many(
        {'amount': {'$lt': 0}, 'category': {'$ne': 'Credit'}},
        {'$set': {'category': 'Credit'}}
    )
    db.app_migrations.insert_one({'_id': 'cc_credits_category_v1', 'run_at': datetime.now()})


def categorize_existing_cc_phone():
    """One-time migration: retag existing CC transactions that match the new Phone
    rules (e.g. mobile carriers) with the 'Phone' category. Guarded + idempotent. Never
    overrides an existing Credit tag."""
    if db.app_migrations.find_one({'_id': 'cc_phone_category_v1'}):
        return
    keywords = MERCHANT_CATEGORY_RULES.get('Phone', [])
    if keywords:
        phone_re = '|'.join(re.escape(k) for k in keywords)
        db.cc_transactions.update_many(
            {
                'category': {'$nin': ['Phone', 'Credit']},
                'description': {'$regex': phone_re, '$options': 'i'},
            },
            {'$set': {'category': 'Phone'}}
        )
    db.app_migrations.insert_one({'_id': 'cc_phone_category_v1', 'run_at': datetime.now()})


def recategorize_existing_cc_other():
    """One-time migration: replay the expanded merchant rules over transactions
    still sitting at 'Other'. Only 'Other' is touched, so every category set by
    hand — or by Chase's own map — survives untouched. Guarded + idempotent."""
    if db.app_migrations.find_one({'_id': 'cc_recategorize_other_v1'}):
        return
    updated = 0
    for t in db.cc_transactions.find({'category': 'Other'}, {'description': 1}):
        category = categorize_transaction(t.get('description', ''))
        if category != 'Other':
            db.cc_transactions.update_one({'_id': t['_id']}, {'$set': {'category': category}})
            updated += 1
    logger.info('cc_recategorize_other_v1: recategorized %s transactions', updated)
    db.app_migrations.insert_one({'_id': 'cc_recategorize_other_v1', 'run_at': datetime.now()})


@app.route('/credit-cards/cards/add', methods=['POST'])
def cc_card_add():
    name = request.form.get('name', '').strip()
    issuer = request.form.get('issuer', 'other')
    if not name:
        return redirect(url_for('credit_cards_page'))
    if dict(CARD_ISSUERS).get(issuer) is None:
        issuer = 'other'
    if not db.cc_cards.find_one({'name': name}):
        db.cc_cards.insert_one({
            'name': name,
            'issuer': issuer,
            'created_at': datetime.now()
        })
    return redirect(url_for('credit_cards_page'))


@app.route('/credit-cards/cards/delete', methods=['POST'])
def cc_card_delete():
    card_id = safe_objectid(request.form.get('card_id', ''))
    if not card_id:
        return redirect(url_for('credit_cards_page'))
    card = db.cc_cards.find_one({'_id': card_id})
    if not card:
        return redirect(url_for('credit_cards_page'))
    # Refuse if there are still transactions tied to this card.
    if db.cc_transactions.count_documents({'card_name': card['name']}, limit=1):
        return redirect(url_for('credit_cards_page', _msg='Cannot delete: statements still linked'))
    db.cc_cards.delete_one({'_id': card_id})
    return redirect(url_for('credit_cards_page'))


@app.route('/credit-cards/statements/raw/<path:filename>')
def cc_statement_raw(filename):
    return send_from_directory(CC_STATEMENT_DIR, filename, as_attachment=True)


@app.route('/credit-cards/upload', methods=['POST'])
def cc_upload():
    if 'file' not in request.files:
        return redirect(url_for('credit_cards_page'))
    file = request.files['file']
    if file.filename == '':
        return redirect(url_for('credit_cards_page'))

    # Resolve card from the new card_id picker, falling back to the legacy
    # issuer + name fields so older form posts and tests still work.
    card_id = safe_objectid(request.form.get('card_id', ''))
    card = db.cc_cards.find_one({'_id': card_id}) if card_id else None
    if card:
        card_name = card['name']
        card_issuer = card.get('issuer') or 'other'
    else:
        card_issuer = request.form.get('card_issuer', 'other')
        card_name = request.form.get('card_name', '').strip()
        if not card_name:
            card_name = dict(CARD_ISSUERS).get(card_issuer, 'Card')
        # Auto-create the card record so future uploads can pick it from the dropdown.
        if card_name and not db.cc_cards.find_one({'name': card_name}):
            db.cc_cards.insert_one({
                'name': card_name,
                'issuer': card_issuer,
                'created_at': datetime.now()
            })

    raw_bytes = file.stream.read()
    stream = io.StringIO(raw_bytes.decode('utf-8-sig'))
    reader = csv.DictReader(stream)
    headers = [h.strip() for h in reader.fieldnames] if reader.fieldnames else []

    # Auto-detect format from headers
    detected = None
    if 'Post Date' in headers or 'post date' in [h.lower() for h in headers]:
        detected = 'chase'
    elif 'Debit' in headers or 'Credit' in headers:
        detected = 'citi'
    elif len(headers) == 3 and 'Amount' in headers:
        detected = 'amex'

    # Use detected format or fall back to user selection
    fmt = detected or card_issuer

    rows = []
    date_counts = {}  # For auto-detecting statement month

    for row in reader:
        # Strip whitespace from keys
        row = {k.strip(): v.strip() if v else '' for k, v in row.items()}

        try:
            if fmt == 'chase':
                tx_date = datetime.strptime(row.get('Transaction Date', row.get('transaction date', '')), '%m/%d/%Y')
                description = row.get('Description', row.get('description', ''))
                raw_amount = float(row.get('Amount', row.get('amount', '0')))
                # Chase: purchases are negative raw (flip to a positive charge),
                # credits/refunds are positive raw (become negative). Sign is
                # normalized here; payment/credit handling happens below.
                amount = -raw_amount
                bank_category = row.get('Category', row.get('category', ''))
                category = CHASE_CATEGORY_MAP.get(bank_category, 'Other')
                original_category = bank_category
            elif fmt == 'citi':
                date_str = row.get('Date', '')
                tx_date = datetime.strptime(date_str, '%m/%d/%Y')
                description = row.get('Description', '')
                debit = row.get('Debit', '')
                credit = row.get('Credit', '')
                if debit:
                    amount = float(debit)
                elif credit:
                    amount = -float(credit)
                else:
                    continue
                category = 'Other'
                original_category = ''
            elif fmt == 'amex':
                date_str = row.get('Date', '')
                tx_date = datetime.strptime(date_str, '%m/%d/%Y')
                description = row.get('Description', '')
                amount = float(row.get('Amount', '0'))
                category = 'Other'
                original_category = ''
            else:
                # Generic fallback
                date_str = row.get('Date', row.get('date', ''))
                tx_date = datetime.strptime(date_str, '%m/%d/%Y')
                description = row.get('Description', row.get('description', ''))
                amount = float(row.get('Amount', row.get('amount', '0')))
                category = 'Other'
                original_category = ''
        except (ValueError, KeyError):
            continue

        desc_upper = description.upper()

        # Card payments (paying the bill) are neither spending nor a credit — drop them.
        if any(kw in desc_upper for kw in PAYMENT_KEYWORDS):
            continue

        # Merchant credits/refunds are kept but stored as a negative amount and tagged
        # with the 'Credit' category, so they're clearly marked, net against spending,
        # and don't show as a charge. (Setting the category here keeps the keyword and
        # Ollama categorization below — which only touch 'Other' — from overriding it.)
        if amount < 0 or any(kw in desc_upper for kw in CREDIT_KEYWORDS):
            amount = -abs(amount)
            category = 'Credit'

        # Skip zero-dollar rows (statement noise).
        if round(amount, 2) == 0:
            continue

        # Track months for statement_date detection
        month_key = tx_date.strftime('%Y-%m')
        date_counts[month_key] = date_counts.get(month_key, 0) + 1

        # Apply keyword categorization (supplements Chase bank categories for 'Other')
        if category == 'Other':
            category = categorize_transaction(description)

        rows.append({
            'date': tx_date,
            'description': description,
            'amount': round(amount, 2),
            'category': category,
            'card_issuer': card_issuer,
            'card_name': card_name,
            'original_category': original_category if fmt == 'chase' else '',
        })

    # Anything still 'Other' goes to the LLM, but *after* the rows are stored —
    # a small local model can take tens of seconds per description, far longer than an upload
    # should block for. See _categorize_leftovers_async below.

    # Auto-detect statement month from majority of transactions
    statement_date = max(date_counts, key=date_counts.get) if date_counts else datetime.now().strftime('%Y-%m')

    # Persist the raw CSV under <card>-<YYYY-MM>.csv for later download.
    save_raw_cc_statement(card_name, statement_date, raw_bytes)

    # Insert with deduplication
    count = 0
    for r in rows:
        r['statement_date'] = statement_date
        # Check for duplicate: same date + description + amount + card_name
        exists = db.cc_transactions.find_one({
            'date': r['date'],
            'description': r['description'],
            'amount': r['amount'],
            'card_name': r['card_name']
        })
        if not exists:
            db.cc_transactions.insert_one(r)
            count += 1

    _categorize_leftovers_async()

    return redirect(url_for('credit_cards_page', month=statement_date, _msg=f'{count} transactions imported'))


_CC_LLM_LOCK = _threading.Lock()
_CC_LLM_RUNNING = False


def _categorize_leftovers_async():
    """Ask the LLM about every transaction the keyword rules left at 'Other',
    in a daemon thread. About 1.8s per distinct merchant — fast enough that it
    finishes in seconds, but still off the request path, so a slow or unreachable
    LLM can never hold up an upload. Only one run at a time; a second upload
    while one is in flight is a no-op, since the next run picks up whatever is
    still 'Other'."""
    global _CC_LLM_RUNNING
    if not CC_LLM_ENABLED:
        return
    with _CC_LLM_LOCK:
        if _CC_LLM_RUNNING:
            return
        _CC_LLM_RUNNING = True

    def _worker():
        global _CC_LLM_RUNNING
        try:
            descs = [d for d in db.cc_transactions.distinct(
                'description', {'category': 'Other'}) if d]
            if not descs:
                return
            logger.info('LLM categorization: %s uncategorized descriptions', len(descs))
            updated = abstained = 0
            for desc in descs:
                category = categorize_with_llm(desc)
                if not category:
                    abstained += 1
                    continue
                res = db.cc_transactions.update_many(
                    {'description': desc, 'category': 'Other'},
                    {'$set': {'category': category, 'categorized_by': 'llm'}})
                updated += res.modified_count
            logger.info('LLM categorization: updated %s transactions, abstained on %s '
                        'of %s merchants', updated, abstained, len(descs))
        except Exception:
            logger.exception('LLM categorization failed')
        finally:
            with _CC_LLM_LOCK:
                _CC_LLM_RUNNING = False

    _threading.Thread(target=_worker, daemon=True).start()


@app.route('/credit-cards/delete-statement', methods=['POST'])
def cc_delete_statement():
    card_name = request.form.get('card_name', '')
    statement_date = request.form.get('statement_date', '')
    if card_name and statement_date:
        db.cc_transactions.delete_many({'card_name': card_name, 'statement_date': statement_date})
    return redirect(url_for('credit_cards_page'))


@app.route('/credit-cards/recategorize', methods=['POST'])
def cc_recategorize():
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data'}), 400
    tid = data.get('transaction_id')
    category = data.get('category')
    if tid and category and category in CC_CATEGORIES:
        # Correcting by hand clears the LLM mark: the row is now settled, and
        # it should stop being flagged for review.
        db.cc_transactions.update_one(
            {'_id': safe_objectid(tid)},
            {'$set': {'category': category}, '$unset': {'categorized_by': ''}}
        )
        return jsonify({'success': True})
    return jsonify({'error': 'Invalid data'}), 400


@app.route('/api/cc/spending-by-category')
def cc_spending_by_category():
    card_filter = request.args.get('card', 'all')
    month_filter = request.args.get('month', '')
    category_filter = request.args.get('category', 'all')
    start_date = request.args.get('start_date', '')
    end_date = request.args.get('end_date', '')

    query = {'amount': {'$gt': 0}}
    if card_filter and card_filter != 'all':
        query['card_name'] = card_filter
    if category_filter and category_filter != 'all':
        query['category'] = category_filter
    date_query = _cc_date_range_query(start_date, end_date)
    if date_query:
        query['date'] = date_query
    elif month_filter:
        query['statement_date'] = month_filter

    pipeline = [
        {'$match': query},
        {'$group': {'_id': '$category', 'total': {'$sum': '$amount'}}},
        {'$sort': {'total': -1}}
    ]
    results = list(db.cc_transactions.aggregate(pipeline))
    labels = [r['_id'] for r in results]
    data = [round(r['total'], 2) for r in results]
    return jsonify({'labels': labels, 'data': data})


import re as _re


def normalize_merchant(desc):
    """Normalize a merchant description for grouping similar charges."""
    name = desc.upper().strip()
    name = _re.sub(r'\s*(INC|LLC|LTD|CO|CORP|#\d+|\d{4,}|[\*#]\S+)\s*', ' ', name)
    name = _re.sub(r'\s+', ' ', name).strip()
    return name[:25]


def detect_duplicate_charges(query):
    """Find merchants with 3+ charges in a single month."""
    txns = list(db.cc_transactions.find(query))
    groups = {}
    for t in txns:
        key = (normalize_merchant(t['description']), t.get('statement_date', ''))
        groups.setdefault(key, []).append(t)

    results = []
    for (merchant, month), items in groups.items():
        if len(items) >= 3:
            total = sum(i['amount'] for i in items)
            results.append({
                'merchant': merchant,
                'month': month,
                'count': len(items),
                'total': round(total, 2),
                'descriptions': list(set(i['description'] for i in items)),
                'amounts': [round(i['amount'], 2) for i in items]
            })
    results.sort(key=lambda x: x['total'], reverse=True)
    return results[:20]


def detect_subscription_overlap(query):
    """Find overlapping subscriptions across all months."""
    base_query = {k: v for k, v in query.items() if k != 'statement_date'}
    base_query.setdefault('amount', {'$gt': 0})
    txns = list(db.cc_transactions.find(base_query))

    group_matches = {}
    for group_name, keywords in SUBSCRIPTION_GROUPS.items():
        services = {}
        for t in txns:
            desc_upper = t['description'].upper()
            for kw in keywords:
                if kw in desc_upper:
                    services.setdefault(kw, []).append(t['amount'])
                    break
        if len(services) >= 2:
            service_list = []
            for svc, amounts in services.items():
                service_list.append({
                    'name': svc.title(),
                    'avg_monthly': round(sum(amounts) / max(len(amounts), 1), 2)
                })
            total_monthly = sum(s['avg_monthly'] for s in service_list)
            group_matches[group_name] = {
                'group': group_name,
                'services': service_list,
                'total_monthly': round(total_monthly, 2),
                'count': len(service_list)
            }

    return list(group_matches.values())


def get_ollama_savings_tips(query, current_month):
    """Get AI-powered savings tips from Ollama based on spending data."""
    # Quick connectivity pre-check — skip expensive aggregation if Ollama is unreachable
    try:
        import requests
        requests.get(f"{OLLAMA_URL}/api/tags", headers=_ollama_headers(), timeout=3)
    except Exception:
        return []

    # Aggregate spending by category over last 6 months
    cat_pipeline = [
        {'$match': {k: v for k, v in query.items() if k != 'statement_date'}},
        {'$group': {
            '_id': {'category': '$category', 'month': '$statement_date'},
            'total': {'$sum': '$amount'}
        }},
        {'$sort': {'_id.month': -1}}
    ]
    cat_data = list(db.cc_transactions.aggregate(cat_pipeline))

    # Top 20 merchants by total spend
    merchant_pipeline = [
        {'$match': {k: v for k, v in query.items() if k != 'statement_date'}},
        {'$group': {
            '_id': '$description',
            'total': {'$sum': '$amount'},
            'count': {'$sum': 1}
        }},
        {'$sort': {'total': -1}},
        {'$limit': 20}
    ]
    merchant_data = list(db.cc_transactions.aggregate(merchant_pipeline))

    cat_summary = {}
    for r in cat_data:
        cat = r['_id']['category']
        month = r['_id']['month']
        cat_summary.setdefault(cat, {})[month] = round(r['total'], 2)

    merchant_summary = [
        {'merchant': r['_id'], 'total': round(r['total'], 2), 'count': r['count']}
        for r in merchant_data
    ]

    prompt = f"""Analyze this credit card spending data and provide 3-5 actionable savings tips.

Current month: {current_month}

Spending by category per month:
{json.dumps(cat_summary, indent=2)}

Top merchants by total spend:
{json.dumps(merchant_summary, indent=2)}

Return a JSON array of tips: [{{"title": "short title", "detail": "1-2 sentence explanation", "potential_savings": "$X/month"}}]
Only return the JSON array, nothing else."""

    try:
        import requests
        resp = requests.post(f"{OLLAMA_URL}/api/chat", json={
            'model': OLLAMA_MODEL,
            'messages': [
                {'role': 'system', 'content': 'You are a personal finance advisor. Analyze spending data and give specific, actionable savings tips. Only respond with valid JSON.'},
                {'role': 'user', 'content': prompt}
            ],
            'stream': False,
            'format': 'json',
            # Same reason as categorization: reasoning tokens dominate the cost
            # on small local models and nothing here needs them.
            'think': False
        }, headers=_ollama_headers(), timeout=90)
        data = resp.json()
        content = data.get('message', {}).get('content', '')
        results = json.loads(content)
        if isinstance(results, dict):
            results = results.get('tips', results.get('results', []))
        return results[:5]
    except Exception:
        return []


@app.route('/api/cc/savings-insights')
def cc_savings_insights():
    card_filter = request.args.get('card', 'all')
    month_filter = request.args.get('month', '')
    category_filter = request.args.get('category', 'all')
    scope = request.args.get('scope', 'all')
    start_date = request.args.get('start_date', '')
    end_date = request.args.get('end_date', '')

    query = {'amount': {'$gt': 0}}
    if card_filter and card_filter != 'all':
        query['card_name'] = card_filter
    if category_filter and category_filter != 'all':
        query['category'] = category_filter
    date_query = _cc_date_range_query(start_date, end_date)
    if date_query:
        query['date'] = date_query
    elif scope == 'current' and month_filter:
        query['statement_date'] = month_filter

    duplicates = detect_duplicate_charges(query)
    subscriptions = detect_subscription_overlap(query)
    ai_tips = get_ollama_savings_tips(query, month_filter)

    return jsonify({
        'duplicates': duplicates,
        'subscriptions': subscriptions,
        'ai_tips': ai_tips
    })


# ============ PAYROLL (Household Employer) ============

def get_payroll_settings():
    """Return the payroll settings doc, seeding defaults if missing."""
    s = db.payroll_settings.find_one({'_id': 'default'})
    if not s:
        s = dict(PAYROLL_DEFAULTS)
        s['_id'] = 'default'
        s['employer_name'] = ''
        s['employer_ein'] = ''
        s['employer_address'] = ''
        db.payroll_settings.insert_one(s)
    # Fill missing keys from defaults for forward-compat
    for k, v in PAYROLL_DEFAULTS.items():
        s.setdefault(k, v)
    return s


def ytd_wages_for_employee(employee_id, year, before_pay_date=None):
    """Sum YTD gross wages for an employee in the given year, optionally excluding entries on/after a date."""
    q = {'employee_id': ObjectId(employee_id)}
    entries = db.payroll_entries.find(q)
    total = 0.0
    for e in entries:
        pd = e.get('pay_date')
        if not pd:
            continue
        if hasattr(pd, 'year') and pd.year == year:
            if before_pay_date and pd >= before_pay_date:
                continue
            total += float(e.get('gross_wages', 0) or 0)
    return total


def calculate_payroll(employee, gross_wages, pay_date, settings=None):
    """Compute all tax withholdings and employer taxes for a pay run.

    Returns a dict with ee_ss, ee_medicare, ee_addl_medicare, ee_fit, ee_sit,
    er_ss, er_medicare, er_futa, er_suta, net_pay, and a list of notes.
    """
    settings = settings or get_payroll_settings()
    state_code = employee.get('state') or settings.get('state') or ''
    state_cfg = settings.get('state_configs', {}).get(state_code, {})
    notes = []

    # Determine FICA applicability.
    # Household employee FICA only kicks in once annual cash wages reach the threshold.
    ytd_prior = ytd_wages_for_employee(employee['_id'], pay_date.year, before_pay_date=pay_date)
    ytd_with_this = ytd_prior + float(gross_wages)
    threshold = float(settings.get('fica_annual_threshold', 2800))

    # Relationship / age exemptions (IRS Pub 926):
    # - Your child under 21: exempt from FICA and FUTA
    # - Your parent (with some exceptions): generally exempt from FICA; exempt from FUTA
    # - Employee under 18 whose principal occupation isn't household work: exempt
    #   from FICA only
    # The child test is age 21, not 18; under_18 also implies under 21, so
    # records saved before the under_21 flag existed still qualify.
    rel = (employee.get('relationship') or '').lower()
    is_child = rel in ('child', 'son', 'daughter')
    is_parent = rel in ('parent', 'mother', 'father')
    child_under_21 = is_child and (employee.get('under_21') or employee.get('under_18'))
    fica_exempt = False
    if child_under_21:
        fica_exempt = True
        notes.append('FICA exempt: child under 21')
    elif is_parent:
        fica_exempt = True
        notes.append('FICA exempt: parent (verify Pub 926 exceptions)')
    elif employee.get('under_18'):
        fica_exempt = True
        notes.append('FICA exempt: employee under 18 (verify principal occupation)')
    futa_exempt = bool(child_under_21 or is_parent)
    if futa_exempt:
        notes.append('FUTA exempt: ' + ('child under 21' if child_under_21 else 'parent'))

    fica_applies = (not fica_exempt) and (ytd_with_this >= threshold)
    if not fica_applies and not fica_exempt:
        notes.append(f'FICA not yet triggered (YTD ${ytd_with_this:,.2f} < threshold ${threshold:,.2f})')

    # Once the year's cash wages reach the threshold, ALL of them are subject
    # to FICA, not just the part above it (Pub 926). Earlier runs this year
    # were under the threshold and taxed nothing, so the run that crosses it
    # catches up the FICA on those earlier wages.
    catch_up_wages = ytd_prior if (fica_applies and ytd_prior < threshold) else 0.0
    if catch_up_wages:
        notes.append(f'FICA threshold crossed: includes catch-up on ${catch_up_wages:,.2f} '
                     f'of earlier wages this year')

    ss_rate = float(settings.get('ss_rate', 0.062))
    mc_rate = float(settings.get('medicare_rate', 0.0145))
    ss_base = float(settings.get('ss_wage_base', 176100))
    addl_mc_rate = float(settings.get('additional_medicare_rate', 0.009))
    addl_mc_threshold = float(settings.get('additional_medicare_threshold', 200000))

    # Social Security — only on wages up to the annual wage base
    ss_wages = 0.0
    if fica_applies:
        remaining_base = max(0.0, ss_base - ytd_prior)
        ss_wages = min(float(gross_wages), remaining_base)
        # Catch-up wages sit below the base by definition (they're under the threshold)
        ss_wages += min(catch_up_wages, ss_base)

    ee_ss = round(ss_wages * ss_rate, 2)
    er_ss = round(ss_wages * ss_rate, 2)

    # Medicare — all wages
    medicare_wages = float(gross_wages) + catch_up_wages
    ee_medicare = round(medicare_wages * mc_rate, 2) if fica_applies else 0.0
    er_medicare = round(medicare_wages * mc_rate, 2) if fica_applies else 0.0

    # Additional Medicare (employee only, over annual threshold)
    ee_addl_medicare = 0.0
    if fica_applies and ytd_with_this > addl_mc_threshold:
        taxable = min(float(gross_wages), ytd_with_this - addl_mc_threshold)
        ee_addl_medicare = round(taxable * addl_mc_rate, 2)

    # Federal income tax — flat per-period from employee W-4 entry (only if they requested)
    ee_fit = float(employee.get('fit_per_period', 0) or 0)

    # State income tax — flat per-period
    ee_sit = float(employee.get('sit_per_period', 0) or 0)
    if not state_cfg.get('sit_withhold_required', False) and ee_sit == 0:
        pass  # not required, not set

    # Employer FUTA — 0.6% on first $7,000 of wages paid per year
    futa_rate = float(settings.get('futa_rate', 0.006))
    futa_base = float(settings.get('futa_wage_base', 7000))
    futa_remaining = max(0.0, futa_base - ytd_prior)
    futa_wages = 0.0 if futa_exempt else min(float(gross_wages), futa_remaining)
    er_futa = round(futa_wages * futa_rate, 2)

    # Employer SUTA — varies by state
    suta_rate = float(state_cfg.get('suta_rate', 0) or 0)
    suta_base = float(state_cfg.get('suta_wage_base', 0) or 0)
    if suta_base > 0:
        suta_remaining = max(0.0, suta_base - ytd_prior)
        suta_wages = min(float(gross_wages), suta_remaining)
    else:
        suta_wages = float(gross_wages)
    er_suta = round(suta_wages * suta_rate, 2)

    net_pay = round(float(gross_wages) - ee_ss - ee_medicare - ee_addl_medicare - ee_fit - ee_sit, 2)

    return {
        'ee_ss': ee_ss,
        'ee_medicare': ee_medicare,
        'ee_addl_medicare': ee_addl_medicare,
        'ee_fit': round(ee_fit, 2),
        'ee_sit': round(ee_sit, 2),
        'er_ss': er_ss,
        'er_medicare': er_medicare,
        'er_futa': er_futa,
        'er_suta': er_suta,
        'net_pay': net_pay,
        'state': state_code,
        'notes': notes,
    }


@app.route('/payroll')
def payroll_page():
    settings = get_payroll_settings()
    employees = list(db.household_employees.find().sort('name', 1))
    accounts = {str(a['_id']): a for a in db.accounts.find()}
    # Recent entries
    entries = list(db.payroll_entries.find().sort('pay_date', -1).limit(25))
    for e in entries:
        e['_id'] = str(e['_id'])
        emp = db.household_employees.find_one({'_id': e['employee_id']}) if e.get('employee_id') else None
        e['employee_name'] = emp['name'] if emp else 'Unknown'
        e['employee_id'] = str(e['employee_id']) if e.get('employee_id') else None

    # YTD totals (current calendar year)
    year = datetime.now().year
    ytd_by_emp = {}
    all_entries_year = db.payroll_entries.find()
    for e in all_entries_year:
        pd = e.get('pay_date')
        if not (pd and hasattr(pd, 'year') and pd.year == year):
            continue
        eid = str(e.get('employee_id'))
        bucket = ytd_by_emp.setdefault(eid, {
            'gross': 0, 'ee_ss': 0, 'ee_medicare': 0, 'ee_fit': 0, 'ee_sit': 0,
            'er_ss': 0, 'er_medicare': 0, 'er_futa': 0, 'er_suta': 0, 'net': 0, 'count': 0,
        })
        bucket['gross'] += float(e.get('gross_wages', 0) or 0)
        bucket['ee_ss'] += float(e.get('ee_ss', 0) or 0)
        bucket['ee_medicare'] += float(e.get('ee_medicare', 0) or 0) + float(e.get('ee_addl_medicare', 0) or 0)
        bucket['ee_fit'] += float(e.get('ee_fit', 0) or 0)
        bucket['ee_sit'] += float(e.get('ee_sit', 0) or 0)
        bucket['er_ss'] += float(e.get('er_ss', 0) or 0)
        bucket['er_medicare'] += float(e.get('er_medicare', 0) or 0)
        bucket['er_futa'] += float(e.get('er_futa', 0) or 0)
        bucket['er_suta'] += float(e.get('er_suta', 0) or 0)
        bucket['net'] += float(e.get('net_pay', 0) or 0)
        bucket['count'] += 1

    employees_out = []
    for emp in employees:
        emp['_id_str'] = str(emp['_id'])
        emp['ytd'] = ytd_by_emp.get(str(emp['_id']), None)
        employees_out.append(emp)

    return render_template('payroll.html',
                           employees=employees_out,
                           entries=entries,
                           accounts=accounts,
                           settings=settings,
                           year=year)


@app.route('/payroll/settings', methods=['GET', 'POST'])
def payroll_settings_page():
    settings = get_payroll_settings()
    if request.method == 'POST':
        update = {
            'employer_name': request.form.get('employer_name', '').strip(),
            'employer_ein': request.form.get('employer_ein', '').strip(),
            'employer_address': request.form.get('employer_address', '').strip(),
            'state': request.form.get('state', '').strip().upper(),
            'ss_rate': float(request.form.get('ss_rate') or PAYROLL_DEFAULTS['ss_rate']),
            'medicare_rate': float(request.form.get('medicare_rate') or PAYROLL_DEFAULTS['medicare_rate']),
            'ss_wage_base': float(request.form.get('ss_wage_base') or PAYROLL_DEFAULTS['ss_wage_base']),
            'fica_annual_threshold': float(request.form.get('fica_annual_threshold') or PAYROLL_DEFAULTS['fica_annual_threshold']),
            'futa_rate': float(request.form.get('futa_rate') or PAYROLL_DEFAULTS['futa_rate']),
            'futa_wage_base': float(request.form.get('futa_wage_base') or PAYROLL_DEFAULTS['futa_wage_base']),
            'additional_medicare_rate': float(request.form.get('additional_medicare_rate') or PAYROLL_DEFAULTS['additional_medicare_rate']),
            'additional_medicare_threshold': float(request.form.get('additional_medicare_threshold') or PAYROLL_DEFAULTS['additional_medicare_threshold']),
        }
        # Per-state config for the currently selected state
        state_code = update['state']
        state_configs = settings.get('state_configs', {}) or {}
        state_cfg = dict(state_configs.get(state_code, {}))
        state_cfg['name'] = request.form.get('state_name', state_code) or state_code
        state_cfg['suta_rate'] = float(request.form.get('suta_rate') or 0)
        state_cfg['suta_wage_base'] = float(request.form.get('suta_wage_base') or 0)
        state_cfg['suta_quarterly_threshold'] = float(request.form.get('suta_quarterly_threshold') or 0)
        state_cfg['sit_withhold_required'] = bool(request.form.get('sit_withhold_required'))
        state_configs[state_code] = state_cfg
        update['state_configs'] = state_configs
        db.payroll_settings.update_one({'_id': 'default'}, {'$set': update}, upsert=True)
        return redirect(url_for('payroll_settings_page'))
    state_code = settings.get('state', '')
    state_cfg = settings.get('state_configs', {}).get(state_code, {})
    return render_template('payroll_settings.html', settings=settings, state_cfg=state_cfg)


@app.route('/payroll/employees/new', methods=['GET', 'POST'])
@app.route('/payroll/employees/<employee_id>/edit', methods=['GET', 'POST'])
def payroll_employee_form(employee_id=None):
    employee = None
    if employee_id:
        oid = safe_objectid(employee_id)
        if not oid:
            return "Invalid ID", 400
        employee = db.household_employees.find_one({'_id': oid})
        if not employee:
            return "Not found", 404
        employee.setdefault('under_21', False)  # older records predate the flag

    if request.method == 'POST':
        def parse_date(s):
            return datetime.fromisoformat(s) if s else None
        data = {
            'name': request.form.get('name', '').strip(),
            'ssn': request.form.get('ssn', '').strip() or None,
            'address': request.form.get('address', '').strip() or None,
            'hire_date': parse_date(request.form.get('hire_date')),
            'termination_date': parse_date(request.form.get('termination_date')),
            'pay_type': request.form.get('pay_type', 'salary'),
            'pay_rate': float(request.form.get('pay_rate') or 0),
            'pay_frequency': request.form.get('pay_frequency', 'monthly'),
            'funding_account_id': safe_objectid(request.form.get('funding_account_id')),
            'fit_per_period': float(request.form.get('fit_per_period') or 0),
            'sit_per_period': float(request.form.get('sit_per_period') or 0),
            'state': (request.form.get('state') or '').strip().upper() or None,
            'relationship': request.form.get('relationship', '').strip() or None,
            'lives_with_employer': bool(request.form.get('lives_with_employer')),
            'under_18': bool(request.form.get('under_18')),
            'under_21': bool(request.form.get('under_21')),
            'notes': request.form.get('notes', '').strip() or None,
            'active': bool(request.form.get('active', 'on')),
        }
        if employee:
            db.household_employees.update_one({'_id': employee['_id']}, {'$set': data})
        else:
            new_emp = HouseholdEmployee(**{k: v for k, v in data.items() if k != 'funding_account_id'},
                                        funding_account_id=str(data['funding_account_id']) if data['funding_account_id'] else None)
            db.household_employees.insert_one(new_emp.__dict__)
        return redirect(url_for('payroll_page'))

    accounts = list(db.accounts.find({'account_type': {'$in': ['checking', 'savings']}}).sort('name', 1))
    return render_template('payroll_employee_form.html',
                           employee=employee,
                           accounts=accounts,
                           pay_frequencies=PAY_FREQUENCIES)


@app.route('/payroll/employees/<employee_id>/delete', methods=['POST'])
def payroll_employee_delete(employee_id):
    oid = safe_objectid(employee_id)
    if not oid:
        return "Invalid ID", 400
    db.household_employees.delete_one({'_id': oid})
    return redirect(url_for('payroll_page'))


@app.route('/payroll/run', methods=['GET', 'POST'])
def payroll_run():
    settings = get_payroll_settings()
    employees = list(db.household_employees.find({'active': {'$ne': False}}).sort('name', 1))
    accounts = list(db.accounts.find({'account_type': {'$in': ['checking', 'savings']}}).sort('name', 1))

    if request.method == 'POST':
        emp_id = safe_objectid(request.form.get('employee_id'))
        if not emp_id:
            return "Invalid employee", 400
        employee = db.household_employees.find_one({'_id': emp_id})
        if not employee:
            return "Employee not found", 404

        pay_date = datetime.fromisoformat(request.form.get('pay_date'))
        period_start = datetime.fromisoformat(request.form.get('period_start'))
        period_end = datetime.fromisoformat(request.form.get('period_end'))
        gross_wages = float(request.form.get('gross_wages') or 0)
        hours = request.form.get('hours')
        rate = request.form.get('rate')
        hours = float(hours) if hours else None
        rate = float(rate) if rate else None
        funding_account_id = safe_objectid(request.form.get('funding_account_id')) or employee.get('funding_account_id')

        calc = calculate_payroll(employee, gross_wages, pay_date, settings)

        # Create the ledger transaction for net pay (what actually leaves the account).
        # We post net_pay as an expense; the employer taxes/withheld amounts are tracked
        # on the payroll entry itself. When taxes are paid to IRS via 1040 / Schedule H,
        # that will be its own transaction.
        txn = None
        if funding_account_id and calc['net_pay'] > 0:
            txn = Transaction(
                description=f"Payroll: {employee['name']} ({period_start.strftime('%Y-%m-%d')} – {period_end.strftime('%Y-%m-%d')})",
                amount=calc['net_pay'],
                date=pay_date,
                transaction_type='expense',
                account_id=str(funding_account_id),
            )
            db.transactions.insert_one(txn.__dict__)

        entry = PayrollEntry(
            employee_id=str(emp_id),
            pay_date=pay_date,
            period_start=period_start,
            period_end=period_end,
            gross_wages=gross_wages,
            hours=hours,
            rate=rate,
            ee_ss=calc['ee_ss'],
            ee_medicare=calc['ee_medicare'],
            ee_addl_medicare=calc['ee_addl_medicare'],
            ee_fit=calc['ee_fit'],
            ee_sit=calc['ee_sit'],
            er_ss=calc['er_ss'],
            er_medicare=calc['er_medicare'],
            er_futa=calc['er_futa'],
            er_suta=calc['er_suta'],
            net_pay=calc['net_pay'],
            funding_account_id=str(funding_account_id) if funding_account_id else None,
            transaction_id=str(txn._id) if txn else None,
            state=calc['state'],
            notes='; '.join(calc['notes']) if calc['notes'] else None,
        )
        db.payroll_entries.insert_one(entry.__dict__)
        return redirect(url_for('payroll_page'))

    # GET: preview-enabled form. JS computes preview via /payroll/preview.
    return render_template('payroll_run.html',
                           employees=employees,
                           accounts=accounts,
                           settings=settings,
                           today=datetime.now().strftime('%Y-%m-%d'))


@app.route('/payroll/preview', methods=['POST'])
def payroll_preview():
    data = request.get_json() or {}
    emp_id = safe_objectid(data.get('employee_id'))
    if not emp_id:
        return jsonify({'error': 'invalid employee'}), 400
    employee = db.household_employees.find_one({'_id': emp_id})
    if not employee:
        return jsonify({'error': 'not found'}), 404
    try:
        gross = float(data.get('gross_wages') or 0)
        pay_date = datetime.fromisoformat(data.get('pay_date'))
    except (ValueError, TypeError):
        return jsonify({'error': 'invalid input'}), 400
    calc = calculate_payroll(employee, gross, pay_date)
    return jsonify(calc)


@app.route('/payroll/entries/<entry_id>/delete', methods=['POST'])
def payroll_entry_delete(entry_id):
    oid = safe_objectid(entry_id)
    if not oid:
        return "Invalid ID", 400
    entry = db.payroll_entries.find_one({'_id': oid})
    if entry and entry.get('transaction_id'):
        db.transactions.delete_one({'_id': entry['transaction_id']})
    db.payroll_entries.delete_one({'_id': oid})
    return redirect(url_for('payroll_page'))


@app.route('/payroll/w2/<employee_id>/<int:year>')
def payroll_w2(employee_id, year):
    oid = safe_objectid(employee_id)
    if not oid:
        return "Invalid ID", 400
    employee = db.household_employees.find_one({'_id': oid})
    if not employee:
        return "Not found", 404
    settings = get_payroll_settings()

    entries = list(db.payroll_entries.find({'employee_id': oid}))
    entries = [e for e in entries if e.get('pay_date') and hasattr(e['pay_date'], 'year') and e['pay_date'].year == year]
    entries.sort(key=lambda e: e['pay_date'])

    totals = {
        'gross': sum(float(e.get('gross_wages', 0) or 0) for e in entries),
        'ss_wages': 0.0,
        'ss_tax': sum(float(e.get('ee_ss', 0) or 0) for e in entries),
        'medicare_wages': 0.0,
        'medicare_tax': sum(float(e.get('ee_medicare', 0) or 0) + float(e.get('ee_addl_medicare', 0) or 0) for e in entries),
        'fit': sum(float(e.get('ee_fit', 0) or 0) for e in entries),
        'sit': sum(float(e.get('ee_sit', 0) or 0) for e in entries),
        'er_ss': sum(float(e.get('er_ss', 0) or 0) for e in entries),
        'er_medicare': sum(float(e.get('er_medicare', 0) or 0) for e in entries),
        'er_futa': sum(float(e.get('er_futa', 0) or 0) for e in entries),
        'er_suta': sum(float(e.get('er_suta', 0) or 0) for e in entries),
        'net': sum(float(e.get('net_pay', 0) or 0) for e in entries),
    }
    # SS/Medicare wages = wages that had those taxes applied. Reverse-compute from tax amounts for accuracy.
    ss_rate = float(settings.get('ss_rate', 0.062))
    mc_rate = float(settings.get('medicare_rate', 0.0145))
    totals['ss_wages'] = round(totals['ss_tax'] / ss_rate, 2) if ss_rate else 0
    totals['medicare_wages'] = round((totals['medicare_tax'] - sum(float(e.get('ee_addl_medicare', 0) or 0) for e in entries)) / mc_rate, 2) if mc_rate else 0

    return render_template('payroll_w2.html',
                           employee=employee,
                           year=year,
                           entries=entries,
                           totals=totals,
                           settings=settings)


@app.route('/payroll/schedule-h/<int:year>')
def payroll_schedule_h(year):
    settings = get_payroll_settings()
    # Aggregate all employees (Schedule H is for the household, not per-employee)
    totals, per_emp = _schedule_h_totals(year, settings)

    return render_template('payroll_schedule_h.html',
                           year=year,
                           totals=totals,
                           per_emp=per_emp,
                           settings=settings)


# ============ PAYROLL PDF GENERATION ============

import zipfile
import re
from io import BytesIO
from payroll_forms import (
    fill_w2_pdf, fill_schedule_h_pdf, build_efw2,
    inspect_pdf_fields, forms_dir,
    w2_template_exists, schedule_h_template_exists,
    set_template_resolver,
)
from payroll_validator import validate_efw2, summarize


def _template_from_db(key):
    doc = db.form_templates.find_one({'_id': key})
    if doc and doc.get('content'):
        return doc['content']
    return None


set_template_resolver(_template_from_db)


def _w2_totals_for(employee_oid, year, settings):
    """Compute W-2 totals for a single employee/year — same logic as payroll_w2 route."""
    entries = list(db.payroll_entries.find({'employee_id': employee_oid}))
    entries = [e for e in entries if e.get('pay_date') and hasattr(e['pay_date'], 'year') and e['pay_date'].year == year]
    totals = {
        'gross': sum(float(e.get('gross_wages', 0) or 0) for e in entries),
        'ss_tax': sum(float(e.get('ee_ss', 0) or 0) for e in entries),
        'medicare_tax': sum(float(e.get('ee_medicare', 0) or 0) + float(e.get('ee_addl_medicare', 0) or 0) for e in entries),
        'fit': sum(float(e.get('ee_fit', 0) or 0) for e in entries),
        'sit': sum(float(e.get('ee_sit', 0) or 0) for e in entries),
        'er_ss': sum(float(e.get('er_ss', 0) or 0) for e in entries),
        'er_medicare': sum(float(e.get('er_medicare', 0) or 0) for e in entries),
        'er_futa': sum(float(e.get('er_futa', 0) or 0) for e in entries),
        'er_suta': sum(float(e.get('er_suta', 0) or 0) for e in entries),
        'net': sum(float(e.get('net_pay', 0) or 0) for e in entries),
    }
    ss_rate = float(settings.get('ss_rate', 0.062)) or 0.062
    mc_rate = float(settings.get('medicare_rate', 0.0145)) or 0.0145
    totals['ss_wages'] = round(totals['ss_tax'] / ss_rate, 2)
    addl_sum = sum(float(e.get('ee_addl_medicare', 0) or 0) for e in entries)
    totals['medicare_wages'] = round((totals['medicare_tax'] - addl_sum) / mc_rate, 2)
    return totals, entries


def _schedule_h_totals(year, settings):
    """Same aggregation as payroll_schedule_h route, returned as (totals, per_emp)."""
    employees = {e['_id']: e for e in db.household_employees.find()}
    entries = list(db.payroll_entries.find())
    entries = [e for e in entries if e.get('pay_date') and hasattr(e['pay_date'], 'year') and e['pay_date'].year == year]
    totals = {
        'cash_wages_fica': 0.0, 'ss_tax': 0.0, 'medicare_tax': 0.0,
        'addl_medicare': 0.0, 'fit_withheld': 0.0, 'futa_wages': 0.0,
        'futa_tax': 0.0, 'state_suta': 0.0, 'total_wages': 0.0,
    }
    per_emp = {}
    for e in entries:
        eid = e.get('employee_id')
        emp = employees.get(eid)
        if not emp:
            continue
        bucket = per_emp.setdefault(str(eid), {'name': emp['name'], 'gross': 0, 'fica_wages': 0})
        gross = float(e.get('gross_wages', 0) or 0)
        bucket['gross'] += gross
        totals['total_wages'] += gross
        if float(e.get('ee_ss', 0) or 0) > 0 or float(e.get('ee_medicare', 0) or 0) > 0:
            bucket['fica'] = True
        totals['ss_tax'] += float(e.get('ee_ss', 0) or 0) + float(e.get('er_ss', 0) or 0)
        totals['medicare_tax'] += float(e.get('ee_medicare', 0) or 0) + float(e.get('er_medicare', 0) or 0)
        totals['addl_medicare'] += float(e.get('ee_addl_medicare', 0) or 0)
        totals['fit_withheld'] += float(e.get('ee_fit', 0) or 0)
        totals['futa_tax'] += float(e.get('er_futa', 0) or 0)
        totals['state_suta'] += float(e.get('er_suta', 0) or 0)
    # Once an employee's wages cross the FICA threshold, every cash wage paid
    # them that year is subject (Pub 926) — including runs before the crossing,
    # whose FICA was caught up on the crossing run.
    for bucket in per_emp.values():
        if bucket.pop('fica', False):
            bucket['fica_wages'] = bucket['gross']
            totals['cash_wages_fica'] += bucket['gross']
    futa_rate = float(settings.get('futa_rate', 0.006)) or 0.006
    addl_rate = float(settings.get('additional_medicare_rate', 0.009)) or 0.009
    totals['futa_wages'] = round(totals['futa_tax'] / futa_rate, 2)
    totals['addl_medicare_wages'] = round(totals['addl_medicare'] / addl_rate, 2)
    totals['total_household_tax'] = round(
        totals['ss_tax'] + totals['medicare_tax'] + totals['addl_medicare']
        + totals['fit_withheld'] + totals['futa_tax'], 2)
    return totals, list(per_emp.values())


def _safe_name(s):
    return re.sub(r'[^A-Za-z0-9_-]+', '_', s or 'unknown').strip('_') or 'unknown'


@app.route('/payroll/w2/<employee_id>/<int:year>/pdf')
def payroll_w2_pdf(employee_id, year):
    oid = safe_objectid(employee_id)
    if not oid:
        return "Invalid ID", 400
    employee = db.household_employees.find_one({'_id': oid})
    if not employee:
        return "Not found", 404
    if not w2_template_exists(year):
        return (f"W-2 template for {year} missing. Drop fw2_{year}.pdf into the forms/ directory "
                f"(see forms/README.md) and try again.", 404)
    settings = get_payroll_settings()
    totals, _ = _w2_totals_for(oid, year, settings)
    pdf = fill_w2_pdf(employee, totals, settings, year)
    if pdf is None:
        return "Failed to fill W-2 template", 500
    filename = f"W2_{_safe_name(employee.get('name'))}_{year}.pdf"
    return Response(pdf.getvalue(), mimetype='application/pdf',
                    headers={'Content-Disposition': f'attachment; filename="{filename}"'})


@app.route('/payroll/schedule-h/<int:year>/pdf')
def payroll_schedule_h_pdf(year):
    if not schedule_h_template_exists(year):
        return (f"Schedule H template for {year} missing. Drop f1040sh_{year}.pdf into the forms/ "
                f"directory (see forms/README.md) and try again.", 404)
    settings = get_payroll_settings()
    totals, per_emp = _schedule_h_totals(year, settings)
    pdf = fill_schedule_h_pdf(totals, per_emp, settings, year,
                              filer_name=settings.get('employer_name'))
    if pdf is None:
        return "Failed to fill Schedule H template", 500
    filename = f"Schedule_H_{year}.pdf"
    return Response(pdf.getvalue(), mimetype='application/pdf',
                    headers={'Content-Disposition': f'attachment; filename="{filename}"'})


@app.route('/payroll/validate/<int:year>')
def payroll_validate(year):
    """Preview local EFW2 validation results without downloading the ZIP."""
    settings = get_payroll_settings()
    rows = []
    for emp in db.household_employees.find():
        totals, entries = _w2_totals_for(emp['_id'], year, settings)
        if entries:
            rows.append({'employee': emp, 'totals': totals})
    if not rows:
        return render_template('payroll_validation.html',
                               year=year, issues=[], error_count=0, warning_count=0,
                               report_text=f'No payroll entries found for {year}.',
                               download_url=None)
    efw2 = build_efw2(year, settings, rows)
    issues = validate_efw2(efw2, expected_year=year)
    error_count, warning_count, report_text = summarize(issues)
    return render_template('payroll_validation.html',
                           year=year, issues=issues,
                           error_count=error_count, warning_count=warning_count,
                           report_text=report_text,
                           download_url=url_for('payroll_year_end_zip',
                                                year=year,
                                                force=1 if error_count else None))


@app.route('/payroll/efw2/<int:year>')
def payroll_efw2(year):
    """Download the EFW2 file for SSA Business Services Online upload (W-2 Copy A filing)."""
    settings = get_payroll_settings()
    employees = list(db.household_employees.find())
    rows = []
    for emp in employees:
        totals, entries = _w2_totals_for(emp['_id'], year, settings)
        if not entries:
            continue
        rows.append({'employee': emp, 'totals': totals})
    if not rows:
        return f"No payroll entries found for {year}", 404
    efw2 = build_efw2(year, settings, rows)
    filename = f"W2_EFW2_{year}.txt"
    return Response(efw2, mimetype='text/plain',
                    headers={'Content-Disposition': f'attachment; filename="{filename}"'})


@app.route('/payroll/year-end/<int:year>/zip')
def payroll_year_end_zip(year):
    """One-click bundle: EFW2 file, filled W-2 PDFs (official IRS forms), filled Schedule H PDF."""
    settings = get_payroll_settings()
    zip_buf = BytesIO()
    warnings = []

    employees = list(db.household_employees.find())
    employee_rows = []
    for emp in employees:
        totals, entries = _w2_totals_for(emp['_id'], year, settings)
        if not entries:
            continue
        employee_rows.append({'employee': emp, 'totals': totals, 'entries': entries})

    if not employee_rows:
        return f"No payroll entries found for {year}", 404

    # Build EFW2 and validate locally before packaging. If there are errors
    # and the caller hasn't passed ?force=1, redirect to the validation view
    # so the user can review and fix before filing.
    efw2 = build_efw2(year, settings,
                      [{'employee': r['employee'], 'totals': r['totals']} for r in employee_rows])
    issues = validate_efw2(efw2, expected_year=year)
    error_count, warning_count, report_text = summarize(issues)
    if error_count > 0 and request.args.get('force') != '1':
        return render_template('payroll_validation.html',
                               year=year, issues=issues,
                               error_count=error_count, warning_count=warning_count,
                               report_text=report_text,
                               download_url=url_for('payroll_year_end_zip', year=year, force=1))

    with zipfile.ZipFile(zip_buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        # 1. EFW2 file for SSA Business Services Online (Copy A filing)
        zf.writestr(f'W2_EFW2_{year}.txt', efw2)
        # Validation report always included in the ZIP
        zf.writestr('VALIDATION_REPORT.txt',
                    f'Local EFW2 validation (SSA Pub 42-007 subset)\n'
                    f'Tax year: {year}\n'
                    f'Generated: {datetime.now().strftime("%Y-%m-%d %H:%M")}\n\n'
                    f'{report_text}\n\n'
                    f'Run AccuWage Online for a second opinion before uploading to BSO:\n'
                    f'  https://www.ssa.gov/bso/\n')

        # 2. Per-employee filled W-2 PDFs (copies B, C, 2, D)
        if w2_template_exists(year):
            for r in employee_rows:
                pdf = fill_w2_pdf(r['employee'], r['totals'], settings, year)
                if pdf:
                    fname = f"W2_{_safe_name(r['employee'].get('name'))}_{year}.pdf"
                    zf.writestr(fname, pdf.getvalue())
        else:
            warnings.append(f"fw2_{year}.pdf not found in forms/ — W-2 PDFs not generated. "
                            f"Download from https://www.irs.gov/forms-pubs/about-form-w-2")

        # 3. Filled Schedule H PDF
        if schedule_h_template_exists(year):
            sh_totals, per_emp = _schedule_h_totals(year, settings)
            sh_pdf = fill_schedule_h_pdf(sh_totals, per_emp, settings, year,
                                         filer_name=settings.get('employer_name'))
            if sh_pdf:
                zf.writestr(f'Schedule_H_{year}.pdf', sh_pdf.getvalue())
        else:
            warnings.append(f"f1040sh_{year}.pdf not found in forms/ — Schedule H PDF not generated. "
                            f"Download from https://www.irs.gov/forms-pubs/about-schedule-h-form-1040")

        # 4. README with filing instructions
        readme = _year_end_readme(year, len(employee_rows), warnings)
        zf.writestr('README.txt', readme)

    zip_buf.seek(0)
    filename = f"Payroll_Year_End_{year}.zip"
    return Response(zip_buf.getvalue(), mimetype='application/zip',
                    headers={'Content-Disposition': f'attachment; filename="{filename}"'})


def _year_end_readme(year, employee_count, warnings):
    lines = [
        f"Payroll Year-End Filing Package — {year}",
        "=" * 50,
        "",
        f"Employees included: {employee_count}",
        "",
        "FILING STEPS",
        "-" * 50,
        "",
        "1. W-2 Copy A (to SSA) — ELECTRONIC FILING",
        f"   File: W2_EFW2_{year}.txt",
        "   - Log in to SSA Business Services Online: https://www.ssa.gov/bso/",
        "   - Create a free account if you don't have one (use your EIN).",
        "   - Under 'Report Wages to Social Security', choose 'Upload formatted",
        "     wage file', then upload W2_EFW2_{year}.txt.",
        "   - Optionally run through AccuWage Online first to validate.",
        "   - Deadline: January 31.",
        "",
        "2. W-2 Copies B, C, 2 → Employee",
        f"   File: W2_<employee>_{year}.pdf",
        "   - Print the filled PDF and hand to each employee.",
        "   - Copies B (employee's federal filing), C (employee's records),",
        "     and 2 (employee's state/city filing) are all on the form.",
        "   - Deadline: January 31.",
        "",
        "3. Schedule H → IRS",
        f"   File: Schedule_H_{year}.pdf",
        "   - File with your personal Form 1040 tax return.",
        "   - If you use tax software (TurboTax, H&R Block, etc.), enter",
        "     the figures directly; this PDF is a working/backup copy.",
        "   - Deadline: tax day (April 15).",
        "",
        "4. State Unemployment (SUTA) — separate filing",
        "   - File with your state unemployment agency per their schedule.",
        "   - This package does not include state filings.",
        "",
    ]
    if warnings:
        lines += ["WARNINGS", "-" * 50, ""]
        for w in warnings:
            lines.append(f"  ! {w}")
        lines.append("")
    lines += [
        "VERIFICATION CHECKLIST",
        "-" * 50,
        "",
        "[ ] Employer EIN is correct on every form",
        "[ ] Each employee SSN is correct",
        "[ ] Each employee's name and address match their Social Security card",
        "[ ] Box 1 (wages) matches your payroll records",
        "[ ] Box 2 (FIT) matches your payroll records",
        "[ ] Boxes 3-6 (SS/Medicare) match expected rates",
        "",
        "Generated by Accounting Cloud.",
    ]
    return '\n'.join(lines)


@app.route('/payroll/forms/inspect')
def payroll_forms_inspect():
    """List form field names in any IRS fillable PDFs (DB-uploaded or in forms/)."""
    fd = forms_dir()
    results = []
    seen = set()
    # Prefer DB-uploaded templates (survive container restarts)
    for doc in db.form_templates.find().sort('_id', 1):
        key = doc['_id']
        seen.add(key)
        try:
            fields = inspect_pdf_fields(doc['content'])
            results.append({
                'file': key, 'source': 'db (uploaded)',
                'field_count': len(fields), 'fields': fields,
                'uploaded_at': doc.get('uploaded_at'),
            })
        except Exception as ex:
            results.append({'file': key, 'source': 'db', 'error': str(ex)})
    # Then disk-based
    for pdf in sorted(fd.glob('*.pdf')):
        if pdf.name in seen:
            continue
        try:
            fields = inspect_pdf_fields(pdf)
            results.append({
                'file': pdf.name, 'source': 'disk',
                'field_count': len(fields), 'fields': fields,
            })
        except Exception as ex:
            results.append({'file': pdf.name, 'source': 'disk', 'error': str(ex)})
    return render_template('payroll_forms_inspect.html', results=results, forms_dir=str(fd))


@app.route('/payroll/forms/upload', methods=['POST'])
def payroll_forms_upload():
    """Upload an IRS fillable PDF (W-2 or Schedule H) for a specific tax year.

    Stores in MongoDB so the template persists across container redeploys.
    """
    kind = request.form.get('kind')  # 'w2' or 'sh'
    year = request.form.get('year')
    file = request.files.get('pdf')
    if not (kind in ('w2', 'sh') and year and file):
        return "Missing kind, year, or file", 400
    try:
        year_int = int(year)
    except ValueError:
        return "Invalid year", 400
    content = file.read()
    if not content or not content.startswith(b'%PDF'):
        return "File is not a valid PDF", 400
    # Sanity: ensure it has form fields
    try:
        fields = inspect_pdf_fields(content)
    except Exception as ex:
        return f"Could not parse PDF: {ex}", 400
    if not fields:
        return ("PDF has no fillable form fields — make sure you downloaded "
                "the fillable version from irs.gov, not a flat image PDF."), 400

    key = f"{'fw2' if kind == 'w2' else 'f1040sh'}_{year_int}.pdf"
    db.form_templates.replace_one(
        {'_id': key},
        {'_id': key, 'content': content, 'uploaded_at': datetime.now(),
         'field_count': len(fields)},
        upsert=True,
    )
    return redirect(url_for('payroll_forms_inspect'))


@app.route('/payroll/forms/delete/<key>', methods=['POST'])
def payroll_forms_delete(key):
    # Only allow deletion of known-format keys
    if not re.match(r'^(fw2|f1040sh)_\d{4}\.pdf$', key):
        return "Invalid key", 400
    db.form_templates.delete_one({'_id': key})
    return redirect(url_for('payroll_forms_inspect'))


# ============ TAX-ADVANTAGED BENEFITS & CARD PERKS ============
# Calendar-year tracking for HSA / 401(k) / IRA contribution room plus credit
# card statement-credit utilization, so nothing expires unused on Dec 31.
# Pure math lives in benefits.py; persistence and routing live here.
#
# Collections:
#   benefit_settings        singleton — coverage tier, age bands, salary, limit overrides
#   benefit_accounts        per-account: which limit bucket, payroll election, match formula
#   contribution_overrides  per-account/year authoritative YTD figure (from a paystub)
#   cc_perk_usage           per-card/perk/period dollars actually redeemed


def _benefit_settings():
    return benefits.get_settings(db.benefit_settings.find_one({}))


def _benefit_year(default=None):
    """Read a ?year= param, falling back to the current calendar year."""
    try:
        year = int(request.args.get('year', ''))
    except (TypeError, ValueError):
        return default or datetime.now().year
    return year if 2000 <= year <= 2100 else (default or datetime.now().year)


def _benefit_context(year, today=None):
    """Everything the benefits page and dashboard card need for one year."""
    today = today or datetime.now()
    settings = _benefit_settings()

    accounts = list(db.accounts.find().sort('name', 1))
    configs = {str(c['account_id']): c for c in db.benefit_accounts.find()
               if c.get('account_id')}
    overrides = {(str(o['account_id']), o.get('year')): o
                 for o in db.contribution_overrides.find({'year': year})
                 if o.get('account_id')}

    window = {'$gte': datetime(year, 1, 1), '$lt': datetime(year + 1, 1, 1)}
    transfers = list(db.transfers.find({'date': window}))
    transactions = list(db.transactions.find({'date': window}))

    groups = benefits.build_contribution_groups(
        accounts, configs, overrides, settings, year, transfers, transactions, today=today)
    match = benefits.employer_match_status(groups, settings)

    cards = list(db.cc_cards.find().sort('name', 1))
    # Anniversary period keys start with the year the cardmember year opened,
    # so a period overlapping this year can be keyed to the previous one.
    usage = list(db.cc_perk_usage.find(
        {'period': {'$regex': f'^({year}|{year - 1})'}}))
    # Statement charges let the page infer which credits were actually used
    # instead of asking for a hundred checkboxes.
    cc_txns = list(db.cc_transactions.find({'date': window}))
    # A card's fee and anniversary month come from its latest annual-fee
    # charge, which may predate this year — so these aren't year-bounded.
    fee_txns = list(db.cc_transactions.find({
        'description': {'$regex': benefits.ANNUAL_FEE_PATTERN.pattern, '$options': 'i'},
        'amount': {'$gt': 0}}))
    card_perks = benefits.build_card_perks(cards, usage, year, today=today,
                                           transactions=cc_txns,
                                           fee_transactions=fee_txns)

    alerts = benefits.build_alerts(groups, card_perks, match, year, today=today)
    return {
        'settings': settings,
        'accounts': accounts,
        'configs': configs,
        'groups': groups,
        'match': match,
        'cards': cards,
        'card_perks': card_perks,
        'alerts': alerts,
    }


@app.route('/benefits')
def benefits_page():
    """Contribution room, employer match, and card credit utilization."""
    year = _benefit_year()
    ctx = _benefit_context(year)

    # Accounts eligible to be linked to a contribution limit, with the bucket
    # each one currently resolves to (explicit config, else type default).
    linkable = []
    for acct in ctx['accounts']:
        aid = str(acct['_id'])
        cfg = ctx['configs'].get(aid) or {}
        default_kind = benefits.DEFAULT_LIMIT_KIND.get(acct.get('account_type'))
        # Any investment account can be pointed at a limit (or explicitly
        # excluded) — a deferred-comp or brokerage account has no statutory
        # limit of its own but may still need one recorded against it.
        if (not default_kind and not cfg.get('limit_kind')
                and acct.get('account_type') not in INVESTMENT_ACCOUNT_TYPES):
            continue
        linkable.append({
            'id': aid,
            'name': acct.get('name'),
            'institution': acct.get('institution'),
            'account_type': acct.get('account_type'),
            'limit_kind': cfg.get('limit_kind') or default_kind or 'none',
            'per_paycheck': cfg.get('per_paycheck'),
            'match_rate_pct': cfg.get('match_rate_pct'),
            'match_limit_pct': cfg.get('match_limit_pct'),
        })

    return render_template(
        'benefits.html',
        year=year,
        years=list(range(datetime.now().year + 1, datetime.now().year - 3, -1)),
        groups=ctx['groups'],
        match=ctx['match'],
        card_perks=ctx['card_perks'],
        alerts=ctx['alerts'],
        settings=ctx['settings'],
        linkable=linkable,
        all_accounts=ctx['accounts'],
        limit_kinds=benefits.TRACKED_KINDS,
        perk_cadences=benefits.PERK_CADENCES,
        reset_bases=benefits.RESET_BASES,
        benefit_categories=benefits.BENEFIT_CATEGORIES,
        perk_templates=benefits.PERK_TEMPLATES,
        template_totals=benefits.TEMPLATE_TOTALS,
        card_issuers=CARD_ISSUERS,
        today=datetime.now(),
    )


@app.route('/benefits/settings', methods=['POST'])
def benefits_settings_save():
    """Save coverage tier, catch-up eligibility, salary, and limit overrides."""
    year = _form_int('year') or datetime.now().year
    update = {
        'hsa_coverage': 'family' if request.form.get('hsa_coverage') == 'family' else 'self',
        'age_50_plus': request.form.get('age_50_plus') == 'on',
        'age_55_plus': request.form.get('age_55_plus') == 'on',
        'annual_salary': _form_float('annual_salary'),
        'pay_periods_per_year': _form_int('pay_periods_per_year') or 26,
        'updated_at': datetime.now(),
    }

    # Limit overrides are per-year and per-bucket; a blank input clears one.
    existing = (db.benefit_settings.find_one({}) or {}).get('limit_overrides') or {}
    year_key = str(year)
    year_overrides = dict(existing.get(year_key) or {})
    for kind, _label in benefits.LIMIT_KINDS:
        raw = (request.form.get(f'limit_{kind}') or '').strip()
        if raw:
            value = _form_float(f'limit_{kind}')
            if value is not None:
                year_overrides[kind] = value
        else:
            year_overrides.pop(kind, None)
    existing[year_key] = year_overrides
    update['limit_overrides'] = existing

    db.benefit_settings.update_one({}, {'$set': update}, upsert=True)
    return redirect(url_for('benefits_page', year=year))


@app.route('/benefits/account-config', methods=['POST'])
def benefits_account_config():
    """Link an account to a limit bucket and record its payroll election."""
    account_id = safe_objectid(request.form.get('account_id', ''))
    year = _form_int('year') or datetime.now().year
    if not account_id:
        return redirect(url_for('benefits_page', year=year))

    kind = request.form.get('limit_kind') or ''
    if kind not in benefits.TRACKED_LABELS and kind != 'none':
        kind = None

    db.benefit_accounts.update_one(
        {'account_id': account_id},
        {'$set': {
            'account_id': account_id,
            'limit_kind': kind,
            'per_paycheck': _form_float('per_paycheck'),
            'match_rate_pct': _form_float('match_rate_pct'),
            'match_limit_pct': _form_float('match_limit_pct'),
            'updated_at': datetime.now(),
        }},
        upsert=True,
    )
    return redirect(url_for('benefits_page', year=year))


@app.route('/benefits/contribution', methods=['POST'])
def benefits_contribution_save():
    """Record (or clear) an authoritative year-to-date contribution figure.

    Contributions withheld from payroll never pass through the ledger, so a
    paystub number is the only accurate source. Anything observed in transfers
    or transactions *after* `as_of` is added on top of it.
    """
    account_id = safe_objectid(request.form.get('account_id', ''))
    year = _form_int('year') or datetime.now().year
    if not account_id:
        return redirect(url_for('benefits_page', year=year))

    if request.form.get('clear') == '1':
        db.contribution_overrides.delete_one({'account_id': account_id, 'year': year})
        return redirect(url_for('benefits_page', year=year))

    amount = _form_float('ytd_amount')
    if amount is None:
        return redirect(url_for('benefits_page', year=year))

    as_of = _parse_form_date('as_of') or datetime.now()
    db.contribution_overrides.update_one(
        {'account_id': account_id, 'year': year},
        {'$set': {
            'account_id': account_id,
            'year': year,
            'ytd_amount': amount,
            'as_of': as_of,
            'note': (request.form.get('note') or '').strip() or None,
            'updated_at': datetime.now(),
        }},
        upsert=True,
    )
    return redirect(url_for('benefits_page', year=year))


@app.route('/benefits/card', methods=['POST'])
def benefits_card_save():
    """Set a card's annual fee and the month it posts."""
    card_id = safe_objectid(request.form.get('card_id', ''))
    year = _form_int('year') or datetime.now().year
    if not card_id:
        return redirect(url_for('benefits_page', year=year))
    db.cc_cards.update_one({'_id': card_id}, {'$set': {
        'annual_fee': _form_float('annual_fee') or 0.0,
        'fee_month': _form_int('fee_month'),
    }})
    return redirect(url_for('benefits_page', year=year))


@app.route('/benefits/perk', methods=['POST'])
def benefits_perk_save():
    """Add, edit, or delete one statement credit on a card."""
    card_id = safe_objectid(request.form.get('card_id', ''))
    year = _form_int('year') or datetime.now().year
    if not card_id:
        return redirect(url_for('benefits_page', year=year))
    card = db.cc_cards.find_one({'_id': card_id})
    if not card:
        return redirect(url_for('benefits_page', year=year))

    perks = list(card.get('perks') or [])
    perk_id = (request.form.get('perk_id') or '').strip()

    if request.form.get('delete') == '1':
        perks = [p for p in perks if p.get('id') != perk_id]
        db.cc_cards.update_one({'_id': card_id}, {'$set': {'perks': perks}})
        db.cc_perk_usage.delete_many({'card_id': card_id, 'perk_id': perk_id})
        return redirect(url_for('benefits_page', year=year))

    name = (request.form.get('name') or '').strip()
    if not name:
        return redirect(url_for('benefits_page', year=year))
    cadence = request.form.get('cadence')
    if cadence not in benefits.CADENCE_PERIODS_PER_YEAR:
        cadence = 'annual'
    basis = request.form.get('reset_basis')
    if basis not in dict(benefits.RESET_BASES):
        basis = 'calendar'

    entry = {
        'name': name,
        'amount': _form_float('amount') or 0.0,
        'cadence': cadence,
        'reset_basis': basis,
        'ends_on': (request.form.get('ends_on') or '').strip() or None,
        'notes': (request.form.get('notes') or '').strip() or None,
    }
    for existing in perks:
        if existing.get('id') == perk_id:
            existing.update(entry)
            break
    else:
        entry['id'] = secrets.token_hex(6)
        perks.append(entry)

    db.cc_cards.update_one({'_id': card_id}, {'$set': {'perks': perks}})
    return redirect(url_for('benefits_page', year=year))


@app.route('/benefits/perk-template', methods=['POST'])
def benefits_perk_template():
    """Load a starter perk set onto a card, replacing whatever it has."""
    card_id = safe_objectid(request.form.get('card_id', ''))
    year = _form_int('year') or datetime.now().year
    template = benefits.PERK_TEMPLATES.get(request.form.get('template', ''))
    if not card_id or not template:
        return redirect(url_for('benefits_page', year=year))

    perks = []
    for p in template['perks']:
        entry = dict(p)
        entry['id'] = secrets.token_hex(6)
        entry.setdefault('reset_basis', 'calendar')
        perks.append(entry)
    benefit_items = []
    for b in template.get('benefits') or []:
        item = dict(b)
        item['id'] = secrets.token_hex(6)
        benefit_items.append(item)

    db.cc_cards.update_one({'_id': card_id}, {'$set': {
        'perks': perks,
        'benefits': benefit_items,
        'annual_fee': template['annual_fee'],
        # Recorded so detection can fall back to the template's merchant
        # patterns even after the template is improved.
        'template_key': request.form.get('template', ''),
    }})
    return redirect(url_for('benefits_page', year=year))


@app.route('/benefits/card-benefit', methods=['POST'])
def benefits_card_benefit():
    """Add or remove one non-credit benefit on a card.

    These are reference only — lounge access, status, insurance — so they are
    never scored against the annual fee and never alerted on.
    """
    card_id = safe_objectid(request.form.get('card_id', ''))
    year = _form_int('year') or datetime.now().year
    if not card_id:
        return redirect(url_for('benefits_page', year=year))
    card = db.cc_cards.find_one({'_id': card_id})
    if not card:
        return redirect(url_for('benefits_page', year=year))

    items = list(card.get('benefits') or [])
    benefit_id = (request.form.get('benefit_id') or '').strip()

    if request.form.get('delete') == '1':
        items = [b for b in items if b.get('id') != benefit_id]
        db.cc_cards.update_one({'_id': card_id}, {'$set': {'benefits': items}})
        return redirect(url_for('benefits_page', year=year))

    name = (request.form.get('name') or '').strip()
    if not name:
        return redirect(url_for('benefits_page', year=year))
    category = request.form.get('category')
    if category not in benefits.BENEFIT_CATEGORY_LABELS:
        category = 'other'

    entry = {
        'name': name,
        'category': category,
        'notes': (request.form.get('notes') or '').strip() or None,
    }
    for existing in items:
        if existing.get('id') == benefit_id:
            existing.update(entry)
            break
    else:
        entry['id'] = secrets.token_hex(6)
        items.append(entry)

    db.cc_cards.update_one({'_id': card_id}, {'$set': {'benefits': items}})
    return redirect(url_for('benefits_page', year=year))


@app.route('/benefits/perk-usage', methods=['POST'])
def benefits_perk_usage():
    """Mark a credit used (in full or partially) for one reset period."""
    card_id = safe_objectid(request.form.get('card_id', ''))
    perk_id = (request.form.get('perk_id') or '').strip()
    period = (request.form.get('period') or '').strip()
    if not card_id or not perk_id or not period:
        return jsonify({'ok': False, 'error': 'missing card, perk, or period'}), 400

    data = request.get_json(silent=True) or {}
    raw = data.get('amount_used', request.form.get('amount_used'))
    try:
        amount = float(raw)
    except (TypeError, ValueError):
        amount = 0.0

    if amount <= 0:
        db.cc_perk_usage.delete_one({'card_id': card_id, 'perk_id': perk_id, 'period': period})
    else:
        db.cc_perk_usage.update_one(
            {'card_id': card_id, 'perk_id': perk_id, 'period': period},
            {'$set': {'card_id': card_id, 'perk_id': perk_id, 'period': period,
                      'amount_used': amount, 'updated_at': datetime.now()}},
            upsert=True,
        )

    if request.is_json:
        return jsonify({'ok': True, 'amount_used': amount})
    year = _form_int('year') or datetime.now().year
    return redirect(url_for('benefits_page', year=year))


# Start background stock refresh scheduler
schedule_stock_refresh()

if __name__ == '__main__':
    debug_mode = os.environ.get('FLASK_DEBUG', 'false').lower() == 'true'
    app.run(host='0.0.0.0', port=5000, debug=debug_mode)
