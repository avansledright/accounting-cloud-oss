"""
Home Move planner: "sell my home for X, buy another for Y" estimator.

Models a typical US residential sale + purchase end-to-end:

  * Sale side   — commission, owner's title policy (seller-paid in many
                  states), survey, seller-side transfer taxes, property tax
                  proration credit to the buyer (for areas that bill in
                  arrears), mortgage payoff -> net proceeds.
  * Purchase side — down payment, lender fees & points, lender's title policy,
                  closing/escrow fee, recording, buyer-side transfer taxes,
                  prepaid interest, prepaid insurance, initial escrow
                  reserve -> cash to close.
  * Monthly     — P&I (fixed or ARM), property tax, insurance, PMI, HOA -> PITI,
                  compared to the current home's payment.
  * Loan        — yearly amortization summary, total interest, ARM adjustment
                  scenarios (expected & worst case under the caps).

Local customs vary a lot (who pays transfer tax and title insurance, whether
property tax is billed in arrears). Every rule of thumb is exposed as an input
so it can be set for your own state/county.
Live rates come from the Freddie Mac PMMS survey via FRED (no API key needed).
"""
import csv
import io
import logging
import threading
import time
from datetime import date, timedelta

import requests

logger = logging.getLogger(__name__)

# Default assumptions (all overridable from the form)
DEFAULTS = {
    # --- current home / sale ---
    'sale_price': 0.0,
    'mortgage_payoff': 0.0,
    'current_monthly_housing': 0.0,   # current P&I + escrow, for comparison
    'current_tax_annual': 0.0,        # most recent annual tax bill on current home
    'commission_pct': 5.0,            # total (listing + buyer agent), negotiable
    'seller_title_insurance': None,   # None -> estimated from price
    'seller_survey': 550.0,
    'seller_misc': 500.0,             # local certificates, lien release recording, wires, courier
    'tax_proration_pct': 100.0,       # % of last annual bill credited to buyer, prorated to closing (0 if taxes are paid in advance)
    'seller_transfer_tax_per_1000': 1.0,  # seller-side transfer/stamp taxes per $1,000 of price (varies by state/county/city)
    'tax_proration_credit': None,     # None -> computed from bill × pct × days elapsed
    'closing_date': None,             # ISO date; None -> 60 days out
    # --- new home / purchase ---
    'purchase_price': 0.0,
    'solve_for': 'price',             # 'price' (normal) | 'payment' (reverse: solve price from target payment)
    'target_payment': 0.0,            # desired total monthly (PITI + HOA) when solve_for == 'payment'
    'down_mode': 'pct',               # 'pct' | 'amount' | 'proceeds'
    'down_payment_pct': 20.0,
    'down_payment_amount': 0.0,
    'extra_cash': 0.0,                # additional cash from savings toward the deal
    'loan_type': 'fixed30',           # fixed30 | fixed15 | arm5 | arm7 | arm10
    'rate': None,                     # None -> live rate for loan type
    'points_pct': 0.0,                # discount points, % of loan
    'buydown_year1': 0.0,             # temporary buydown: rate reduction (pts) in year 1 (e.g. 2 for a 2-1)
    'buydown_year2': 0.0,             # rate reduction (pts) in year 2 (e.g. 1 for a 2-1)
    'buydown_payer': 'buyer',         # 'buyer' (added to closing costs) | 'seller' (seller concession)
    'arm_first_cap': 2.0,             # %-pts, first adjustment
    'arm_periodic_cap': 1.0,          # %-pts, each later adjustment
    'arm_lifetime_cap': 5.0,          # %-pts over intro rate
    'arm_expected_rate': None,        # None -> intro + 1.0
    'tax_mode': 'rate',               # 'rate' (% of price) | 'amount'
    'tax_rate_pct': 1.1,              # effective property tax rate, % of price per year (US average is roughly 1%)
    'tax_annual': 0.0,
    'insurance_mode': 'rate',
    'insurance_rate_pct': 0.5,        # homeowner's insurance, % of price per year
    'insurance_annual': 0.0,
    'pmi_rate_pct': 0.6,              # % of loan per year when LTV > 80%
    'hoa_monthly': 0.0,
    'lender_origination': 1500.0,
    'appraisal': 600.0,
    'lender_misc': 150.0,             # credit report, flood cert, tax service
    'lender_title_policy': 750.0,
    'closing_escrow_fee': 1200.0,     # title company settlement/closing fee (buyer share)
    'recording_fees': 250.0,
    'inspection': 500.0,
    'buyer_misc': 300.0,
    'escrow_reserve_months': 3,       # initial escrow cushion (tax + insurance)
    'prepaid_insurance_months': 12,
    'seller_credit': 0.0,             # concessions negotiated from the seller of the new home
    'buyer_transfer_tax_per_1000': 0.0,   # buyer-side transfer/stamp taxes per $1,000 (some cities/states charge the buyer)
}

LOAN_TYPES = {
    'fixed30': {'label': '30-year fixed', 'term': 360, 'arm_fixed_years': 0, 'rate_key': 'rate30'},
    'fixed15': {'label': '15-year fixed', 'term': 180, 'arm_fixed_years': 0, 'rate_key': 'rate15'},
    'arm5':    {'label': '5/1 ARM (30-yr)', 'term': 360, 'arm_fixed_years': 5, 'rate_key': 'arm'},
    'arm7':    {'label': '7/1 ARM (30-yr)', 'term': 360, 'arm_fixed_years': 7, 'rate_key': 'arm'},
    'arm10':   {'label': '10/1 ARM (30-yr)', 'term': 360, 'arm_fixed_years': 10, 'rate_key': 'arm'},
}

# ---------------------------------------------------------------------------
# Live rates (Freddie Mac PMMS via FRED public CSV, cached 12h)
# ---------------------------------------------------------------------------
_RATE_CACHE = {'data': None, 'ts': 0}
_RATE_LOCK = threading.Lock()
_RATE_TTL = 12 * 3600
_FRED_CSV = 'https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}'
FALLBACK_RATES = {'rate30': 6.50, 'rate15': 5.85}
ARM_SPREAD_VS_30YR = -0.45  # PMMS stopped publishing ARM rates; 5/1 ARMs typically price ~0.3–0.6 below 30-yr fixed


def _fred_latest(series):
    resp = requests.get(_FRED_CSV.format(series=series), timeout=8)
    resp.raise_for_status()
    rows = [r for r in csv.reader(io.StringIO(resp.text)) if len(r) == 2]
    for d, v in reversed(rows[1:]):
        try:
            return d, float(v)
        except ValueError:
            continue
    return None, None


def fetch_mortgage_rates(force=False):
    """Return {'rate30','rate15','arm','as_of','source','live'} with caching."""
    with _RATE_LOCK:
        if not force and _RATE_CACHE['data'] and time.time() - _RATE_CACHE['ts'] < _RATE_TTL:
            return _RATE_CACHE['data']
    data = {'live': False, 'source': 'fallback defaults', 'as_of': None}
    try:
        d30, r30 = _fred_latest('MORTGAGE30US')
        d15, r15 = _fred_latest('MORTGAGE15US')
        if r30:
            data.update({'rate30': r30, 'rate15': r15 or FALLBACK_RATES['rate15'],
                         'as_of': d30, 'live': True,
                         'source': 'Freddie Mac PMMS (via FRED)'})
    except Exception as e:  # network down, FRED changed, etc.
        logger.warning(f'Mortgage rate fetch failed: {e}')
    if not data['live']:
        data.update(FALLBACK_RATES)
    data['arm'] = round(data['rate30'] + ARM_SPREAD_VS_30YR, 2)
    data['arm_note'] = f'estimated: 30-yr fixed {ARM_SPREAD_VS_30YR:+.2f} (survey no longer publishes ARM rates)'
    with _RATE_LOCK:
        _RATE_CACHE['data'] = data
        _RATE_CACHE['ts'] = time.time()
    return data


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _f(v, default=0.0):
    """Parse a float form value; blanks -> default."""
    if v is None or v == '':
        return default
    try:
        return float(str(v).replace(',', '').replace('$', '').replace('%', ''))
    except (TypeError, ValueError):
        return default


def parse_params(source):
    """Build a params dict from a request.args / form-like mapping."""
    p = {}
    for key, default in DEFAULTS.items():
        raw = source.get(key)
        if key in ('down_mode', 'loan_type', 'tax_mode', 'insurance_mode', 'closing_date', 'buydown_payer', 'solve_for'):
            p[key] = raw if raw else default
        elif default is None:
            p[key] = _f(raw, None) if raw not in (None, '') else None
        elif isinstance(default, int) and not isinstance(default, bool):
            p[key] = int(_f(raw, default))
        else:
            p[key] = _f(raw, default)
    if p['loan_type'] not in LOAN_TYPES:
        p['loan_type'] = 'fixed30'
    return p


def payment_for(principal, annual_rate_pct, n_months):
    if n_months <= 0 or principal <= 0:
        return 0.0
    r = annual_rate_pct / 100 / 12
    if r == 0:
        return principal / n_months
    return principal * r / (1 - (1 + r) ** (-n_months))


def buydown_cost(principal, note_rate_pct, term_months, year1_pts, year2_pts):
    return temporary_buydown(principal, note_rate_pct, term_months, year1_pts, year2_pts)['cost']


def temporary_buydown(principal, note_rate_pct, term_months, year1_pts, year2_pts):
    """
    Temporary (e.g. 2-1) buydown: the borrower pays P&I at a reduced rate for
    the first one or two years while an escrow funded at closing covers the
    difference. Cost = sum of the monthly payment shortfalls. The loan itself
    still amortizes at the note rate.
    """
    full = payment_for(principal, note_rate_pct, term_months)
    years = []
    cost = 0.0
    for yr, pts in ((1, year1_pts or 0), (2, year2_pts or 0)):
        if pts <= 0:
            continue
        reduced_rate = max(note_rate_pct - pts, 0)
        pmt = payment_for(principal, reduced_rate, term_months)
        years.append({'year': yr, 'pts': pts, 'rate': reduced_rate, 'payment': pmt, 'monthly_saving': full - pmt})
        cost += (full - pmt) * 12
    label = ('-'.join(f"{y['pts']:g}" for y in years) + '-0') if years else 'none'
    return {'years': years, 'cost': cost, 'full_payment': full, 'label': label}


def estimate_owner_title_policy(price):
    """Rough owner's title policy premium. Tiered approximation; rates are
    state-regulated, so override with a real quote when you have one."""
    if price <= 0:
        return 0.0
    # Roughly: $1,700 on the first $250k, then ~$2.25 per $1,000 above that.
    base = 1700.0
    if price > 250_000:
        base += (price - 250_000) / 1000 * 2.25
    return round(base, 2)


def _yearly_schedule(principal, rate_pct, term_months, arm=None):
    """
    Amortize month by month; return (years, total_interest, arm_info).
    `arm` = {'fixed_years', 'adjusted_rate'} switches rate after the intro
    period and re-amortizes the remaining balance over the remaining term.
    """
    balance = principal
    rate = rate_pct
    pmt = payment_for(balance, rate, term_months)
    years = []
    total_interest = 0.0
    yr_int = yr_prin = 0.0
    adjusted_payment = None
    balance_at_adjust = None
    for m in range(1, term_months + 1):
        if arm and m == arm['fixed_years'] * 12 + 1 and arm['adjusted_rate'] != rate:
            rate = arm['adjusted_rate']
            pmt = payment_for(balance, rate, term_months - m + 1)
            adjusted_payment = pmt
            balance_at_adjust = balance
        interest = balance * rate / 100 / 12
        principal_paid = min(pmt - interest, balance)
        balance -= principal_paid
        total_interest += interest
        yr_int += interest
        yr_prin += principal_paid
        if m % 12 == 0 or m == term_months or balance <= 0.005:
            years.append({
                'year': (m + 11) // 12,
                'principal': round(yr_prin, 2),
                'interest': round(yr_int, 2),
                'balance': round(max(balance, 0), 2),
                'rate': round(rate, 3),
            })
            yr_int = yr_prin = 0.0
            if balance <= 0.005:
                break
    return years, total_interest, {'adjusted_payment': adjusted_payment,
                                   'balance_at_adjust': balance_at_adjust}


# ---------------------------------------------------------------------------
# Main calculation
# ---------------------------------------------------------------------------
def calculate_home_move(p, rates=None, monthly_income=None):
    rates = rates or fetch_mortgage_rates()
    lt = LOAN_TYPES[p['loan_type']]
    today = date.today()

    # Closing date
    try:
        closing = date.fromisoformat(p['closing_date']) if p['closing_date'] else today + timedelta(days=60)
    except ValueError:
        closing = today + timedelta(days=60)

    # ---------------- Sale side ----------------
    sale = p['sale_price']
    commission = sale * p['commission_pct'] / 100
    title_ins = p['seller_title_insurance'] if p['seller_title_insurance'] is not None else estimate_owner_title_policy(sale)
    seller_transfer = sale / 1000 * p['seller_transfer_tax_per_1000']
    days_elapsed = (closing - date(closing.year, 1, 1)).days + 1
    if p['tax_proration_credit'] is not None:
        proration = p['tax_proration_credit']
    else:
        # Where property tax is billed in arrears, the seller owes the buyer the
        # current year's taxes accrued to closing (plus any unpaid prior-year
        # balance, ignored here). Set tax_proration_pct to 0 if not applicable.
        proration = p['current_tax_annual'] * p['tax_proration_pct'] / 100 * days_elapsed / 365
    seller_costs = [
        ('Agent commission', commission, f"{p['commission_pct']:.2f}% of sale price"),
        (f"Transfer taxes (${p['seller_transfer_tax_per_1000']:g} per $1,000)", seller_transfer, 'seller-paid'),
        ("Owner's title insurance", title_ins, 'seller-paid'),
        ('Survey', p['seller_survey'], ''),
        ('Property tax proration credit to buyer', proration,
         f"{p['tax_proration_pct']:.0f}% of ${p['current_tax_annual']:,.0f} × {days_elapsed}/365 days"),
        ('Misc (certificates, lien release recording, wires)', p['seller_misc'], ''),
    ]
    seller_total = sum(c[1] for c in seller_costs)
    net_proceeds = sale - p['mortgage_payoff'] - seller_total
    equity = sale - p['mortgage_payoff']

    # ---------------- Purchase side ----------------
    price = p['purchase_price']
    rate_key = lt['rate_key']
    live_rate = rates.get(rate_key, FALLBACK_RATES['rate30'])
    rate = p['rate'] if p['rate'] is not None else live_rate
    term = lt['term']

    tax_annual = p['tax_annual'] if p['tax_mode'] == 'amount' else price * p['tax_rate_pct'] / 100
    ins_annual = p['insurance_annual'] if p['insurance_mode'] == 'amount' else price * p['insurance_rate_pct'] / 100
    tax_m = tax_annual / 12
    ins_m = ins_annual / 12
    hoa_m = p['hoa_monthly']
    buyer_transfer = price / 1000 * p['buyer_transfer_tax_per_1000']
    # Prepaid interest: from closing through end of month
    if closing.month == 12:
        month_end = date(closing.year + 1, 1, 1)
    else:
        month_end = date(closing.year, closing.month + 1, 1)
    prepaid_days = (month_end - closing).days
    prepaid_ins = ins_annual / 12 * p['prepaid_insurance_months']
    escrow_reserve = (tax_m + ins_m) * p['escrow_reserve_months']
    fixed_buyer_costs = (p['lender_origination'] + p['appraisal'] + p['lender_misc'] + p['lender_title_policy']
                         + p['closing_escrow_fee'] + p['recording_fees'] + buyer_transfer
                         + p['inspection'] + p['buyer_misc'] + prepaid_ins + escrow_reserve - p['seller_credit'])
    cash_available = net_proceeds + p['extra_cash']

    def loan_dependent_costs(loan_amt):
        cost = loan_amt * p['points_pct'] / 100 + loan_amt * rate / 100 / 365 * prepaid_days
        if p['buydown_payer'] == 'buyer':
            cost += buydown_cost(loan_amt, rate, term, p['buydown_year1'], p['buydown_year2'])
        return cost

    if p['down_mode'] == 'amount':
        down = min(p['down_payment_amount'], price)
    elif p['down_mode'] == 'proceeds':
        # Put every available dollar toward the purchase: down payment = cash on
        # hand minus closing costs. Points/prepaid interest depend on the loan
        # size, so iterate to a fixed point (converges in a couple of passes).
        down = max(cash_available - fixed_buyer_costs, 0)
        for _ in range(5):
            down = max(cash_available - fixed_buyer_costs - loan_dependent_costs(max(price - down, 0)), 0)
        down = min(down, price)
    else:
        down = price * p['down_payment_pct'] / 100
    down = max(down, 0)
    loan = max(price - down, 0)
    ltv = loan / price * 100 if price > 0 else 0
    down_pct = down / price * 100 if price > 0 else 0
    pi = payment_for(loan, rate, term)

    pmi_monthly = loan * p['pmi_rate_pct'] / 100 / 12 if ltv > 80 and loan > 0 else 0.0
    piti = pi + tax_m + ins_m + pmi_monthly + hoa_m

    # Buyer closing costs
    points = loan * p['points_pct'] / 100
    buydown = temporary_buydown(loan, rate, term, p['buydown_year1'], p['buydown_year2'])
    buydown_buyer_cost = buydown['cost'] if (buydown['cost'] and p['buydown_payer'] == 'buyer') else 0.0
    prepaid_interest = loan * rate / 100 / 365 * prepaid_days

    buyer_costs = [
        ('Lender origination / underwriting', p['lender_origination'], ''),
        (f"Discount points ({p['points_pct']:.3g}% of loan)", points, ''),
        (f"Rate buydown escrow ({buydown['label']})", buydown_buyer_cost, 'buyer-paid'),
        ('Appraisal', p['appraisal'], ''),
        ('Credit report, flood cert, tax service', p['lender_misc'], ''),
        ("Lender's title policy", p['lender_title_policy'], ''),
        ('Title settlement / closing fee', p['closing_escrow_fee'], ''),
        ('Recording fees', p['recording_fees'], ''),
        (f"Buyer transfer taxes (${p['buyer_transfer_tax_per_1000']:g} per $1,000)", buyer_transfer, ''),
        ('Inspection', p['inspection'], ''),
        ('Misc', p['buyer_misc'], ''),
    ]
    prepaids = [
        (f'Prepaid interest ({prepaid_days} days to month end)', prepaid_interest, ''),
        (f"Homeowner's insurance ({p['prepaid_insurance_months']} months prepaid)", prepaid_ins, ''),
        (f"Initial escrow reserve ({p['escrow_reserve_months']} mo tax + insurance)", escrow_reserve, ''),
    ]
    closing_costs_total = sum(c[1] for c in buyer_costs)
    prepaids_total = sum(c[1] for c in prepaids)
    cash_to_close = down + closing_costs_total + prepaids_total - p['seller_credit']
    cash_after = cash_available - cash_to_close

    # ---------------- Loan life / ARM ----------------
    arm = None
    arm_info = None
    if lt['arm_fixed_years']:
        expected = p['arm_expected_rate'] if p['arm_expected_rate'] is not None else rate + 1.0
        expected = min(expected, rate + p['arm_lifetime_cap'])
        # First adjustment is capped; the worst case then climbs by the periodic
        # cap each year until it hits the lifetime cap.
        worst_first = rate + min(p['arm_first_cap'], p['arm_lifetime_cap'])
        worst_max = rate + p['arm_lifetime_cap']
        arm = {'fixed_years': lt['arm_fixed_years'], 'adjusted_rate': expected}
        years_w, _, info_w = _yearly_schedule(loan, rate, term,
                                              {'fixed_years': lt['arm_fixed_years'], 'adjusted_rate': worst_first})
        remaining_after_fixed = term - lt['arm_fixed_years'] * 12
        bal_adj = info_w['balance_at_adjust'] or loan
        arm_info = {
            'fixed_years': lt['arm_fixed_years'],
            'intro_rate': rate,
            'intro_payment': pi,
            'expected_rate': expected,
            'worst_first_rate': worst_first,
            'worst_first_payment': info_w['adjusted_payment'] or pi,
            'worst_max_rate': worst_max,
            'worst_max_payment': payment_for(bal_adj, worst_max, remaining_after_fixed),
            'balance_at_adjust': bal_adj,
            'first_cap': p['arm_first_cap'], 'periodic_cap': p['arm_periodic_cap'],
            'lifetime_cap': p['arm_lifetime_cap'],
        }
    years, total_interest, info = _yearly_schedule(loan, rate, term, arm)
    if arm_info:
        arm_info['expected_payment'] = info['adjusted_payment'] or pi

    payoff_year = closing.year + (term // 12)
    # Balance after 5 years for equity-at-a-glance
    bal_5 = next((y['balance'] for y in years if y['year'] == 5), None)

    # ---------------- Comparison ----------------
    current = p['current_monthly_housing']
    delta = piti - current if current else None
    ratio = piti / monthly_income * 100 if monthly_income else None

    return {
        'closing_date': closing.isoformat(),
        'closing_date_display': closing.strftime('%b %d, %Y'),
        'rates': rates,
        'loan_label': lt['label'],
        'sale': {
            'price': sale, 'payoff': p['mortgage_payoff'], 'equity': equity,
            'costs': [(n, round(a, 2), note) for n, a, note in seller_costs],
            'costs_total': seller_total, 'net_proceeds': net_proceeds,
            'costs_pct': seller_total / sale * 100 if sale else 0,
        },
        'purchase': {
            'price': price, 'down': down, 'down_pct': down_pct, 'loan': loan, 'ltv': ltv,
            'rate': rate, 'rate_is_live': p['rate'] is None, 'live_rate': live_rate,
            'term_years': term // 12,
            'closing_costs': [(n, round(a, 2), note) for n, a, note in buyer_costs],
            'closing_costs_total': closing_costs_total,
            'prepaids': [(n, round(a, 2), note) for n, a, note in prepaids],
            'prepaids_total': prepaids_total,
            'seller_credit': p['seller_credit'],
            'buydown': buydown, 'buydown_payer': p['buydown_payer'],
            'cash_to_close': cash_to_close,
            'cash_available': cash_available, 'extra_cash': p['extra_cash'],
            'cash_after': cash_after,
            'shortfall': -cash_after if cash_after < 0 else 0,
            'pmi_applies': pmi_monthly > 0,
        },
        'monthly': {
            'pi': pi, 'tax': tax_m, 'insurance': ins_m, 'pmi': pmi_monthly, 'hoa': hoa_m,
            'total': piti, 'tax_annual': tax_annual, 'insurance_annual': ins_annual,
            'current': current, 'delta': delta, 'annual_delta': delta * 12 if delta is not None else None,
            'income': monthly_income, 'housing_ratio': ratio,
        },
        'loan': {
            'years': years, 'total_interest': total_interest,
            'total_paid': loan + total_interest, 'payoff_year': payoff_year,
            'balance_year5': bal_5, 'arm': arm_info,
        },
    }


def solve_price_for_payment(p, rates=None, monthly_income=None, max_price=20_000_000):
    """
    Reverse calculator: find the purchase price whose total monthly payment
    (PITI + HOA, at the note rate) equals p['target_payment'], holding every
    other input fixed (rate, loan type, down-payment mode, sale side, etc.).
    Total payment is monotonic in price, so bisection converges quickly.
    Returns (price, result) or (None, None) if the target is unreachable.
    """
    target = p.get('target_payment') or 0
    if target <= 0:
        return None, None
    rates = rates or fetch_mortgage_rates()

    def total_at(price):
        q = dict(p, purchase_price=price)
        return calculate_home_move(q, rates, monthly_income)['monthly']['total']

    lo, hi = 0.0, float(max_price)
    if total_at(hi) < target:
        return None, None
    for _ in range(60):
        mid = (lo + hi) / 2
        if total_at(mid) > target:
            hi = mid
        else:
            lo = mid
        if hi - lo < 50:
            break
    price = int(lo // 1000) * 1000  # round down to a clean number that stays under target
    if price <= 0:
        return None, None
    q = dict(p, purchase_price=price)
    return price, calculate_home_move(q, rates, monthly_income)
