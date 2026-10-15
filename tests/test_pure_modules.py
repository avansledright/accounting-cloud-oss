"""Unit tests for the modules that need no database.

app.py connects to MongoDB at import time, so route-level behaviour is
covered by the Docker Compose smoke test in CI rather than here.
"""
from datetime import datetime

import benefits
import home_move
import payroll_validator


# ---- home_move -------------------------------------------------------------

RATES = {'rate30': 6.0, 'rate15': 5.5, 'arm': 5.6, 'live': False}


def _params(**overrides):
    p = home_move.parse_params({})
    p.update(overrides)
    return p


def test_payment_for_matches_standard_amortization():
    # $300k at 6% over 30 years is the textbook $1,798.65/month.
    assert round(home_move.payment_for(300_000, 6.0, 360), 2) == 1798.65


def test_payment_for_zero_rate_is_straight_line():
    assert home_move.payment_for(12_000, 0, 12) == 1000


def test_transfer_taxes_are_configurable():
    p = _params(sale_price=400_000, purchase_price=500_000,
                seller_transfer_tax_per_1000=2.0, buyer_transfer_tax_per_1000=1.0,
                closing_date='2030-06-15')
    r = home_move.calculate_home_move(p, RATES)
    seller = dict((n, a) for n, a, _ in r['sale']['costs'])
    buyer = dict((n, a) for n, a, _ in r['purchase']['closing_costs'])
    assert seller['Transfer taxes ($2 per $1,000)'] == 800.0
    assert buyer['Buyer transfer taxes ($1 per $1,000)'] == 500.0


def test_pmi_only_above_80_ltv():
    low_down = home_move.calculate_home_move(_params(purchase_price=400_000, down_payment_pct=10), RATES)
    big_down = home_move.calculate_home_move(_params(purchase_price=400_000, down_payment_pct=20), RATES)
    assert low_down['purchase']['pmi_applies'] is True
    assert big_down['purchase']['pmi_applies'] is False


def test_solve_price_for_payment_stays_under_target():
    p = _params(target_payment=3000, solve_for='payment')
    price, result = home_move.solve_price_for_payment(p, RATES)
    assert price and price > 0
    assert result['monthly']['total'] <= 3000


# ---- benefits --------------------------------------------------------------

def test_ira_limit_with_catch_up():
    settings = benefits.get_settings({'age_50_plus': True})
    assert benefits.limit_for('ira', 2026, settings) == 7500 + 1100


def test_limit_override_wins():
    settings = benefits.get_settings({'limit_overrides': {'2026': {'401k': 30000}}})
    assert benefits.limit_for('401k', 2026, settings) == 30000


def test_unknown_year_has_no_limit():
    assert benefits.limit_for('401k', 1999, benefits.get_settings({})) is None


def test_suggest_template_matches_public_card_names():
    assert benefits.suggest_template({'name': 'Amex Platinum'}) == 'amex_platinum'
    assert benefits.suggest_template({'name': 'My Sapphire card'}) == 'chase_sapphire_preferred'
    assert benefits.suggest_template({'name': 'Debit card'}) is None


def test_every_perk_template_has_a_checked_date():
    for key, tpl in benefits.PERK_TEMPLATES.items():
        assert tpl.get('checked'), f'{key} is missing its research date'


def test_monthly_periods_cover_the_year():
    assert len(benefits.periods_in_year('monthly', datetime.now().year)) == 12


# ---- payroll_validator -----------------------------------------------------

def test_ssn_validation():
    assert payroll_validator.validate_ssn('123-45-6789') == (True, '')
    assert payroll_validator.validate_ssn('666-12-3456')[0] is False
    assert payroll_validator.validate_ssn('912-34-5678')[0] is False
    assert payroll_validator.validate_ssn('123')[0] is False


def test_ein_validation():
    assert payroll_validator.validate_ein('12-3456789')[0] is True
    assert payroll_validator.validate_ein('000000000')[0] is False
