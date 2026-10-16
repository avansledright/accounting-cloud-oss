"""Home Move planner: payment math, temporary buydowns and the reverse (price-from-payment) solver."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import home_move as hm  # noqa: E402

# Fixed rates so no test touches the network.
RATES = {'rate30': 6.5, 'rate15': 5.85, 'arm': 6.05, 'live': False, 'source': 'test', 'as_of': None}


def params(**overrides):
    p = hm.parse_params({})
    p.update(rate=6.5, closing_date='2027-01-15', tax_mode='amount', tax_annual=3600.0,
             insurance_mode='amount', insurance_annual=480.0)
    p.update(overrides)
    return p


# ---------------------------------------------------------------- payment math
def test_payment_matches_reference_amortization():
    # $100k / 6% / 30yr is the textbook $599.55.
    assert hm.payment_for(100_000, 6, 360) == pytest.approx(599.55, abs=0.01)
    assert hm.payment_for(120_000, 0, 360) == pytest.approx(333.33, abs=0.01)


def test_two_one_buydown_reference_values():
    # $300k at 7%: full $1,995.91, year 1 at 5% $1,610.46, year 2 at 6% $1,798.65.
    b = hm.temporary_buydown(300_000, 7, 360, 2, 1)
    assert b['full_payment'] == pytest.approx(1995.91, abs=0.01)
    assert [round(y['payment'], 2) for y in b['years']] == [1610.46, 1798.65]
    # Escrow = 12 months of each year's shortfall.
    assert b['cost'] == pytest.approx((385.44 + 197.26) * 12, abs=0.5)
    assert b['label'] == '2-1'


def test_one_year_buydown_label_and_cost():
    b = hm.temporary_buydown(300_000, 7, 360, 1, 0)
    assert b['label'] == '1-0'
    assert len(b['years']) == 1
    assert b['cost'] == pytest.approx((1995.91 - hm.payment_for(300_000, 6, 360)) * 12, abs=0.5)


def test_by_year_payments_step_up_to_note_rate():
    r = hm.calculate_home_move(params(purchase_price=400_000, buydown_year1=2, buydown_year2=1), RATES)
    by_year = r['monthly']['by_year']
    assert [y['label'] for y in by_year] == ['Year 1', 'Year 2', 'Year 3+']
    assert by_year[0]['total'] < by_year[1]['total'] < by_year[2]['total']
    assert by_year[2]['total'] == pytest.approx(r['monthly']['total'])
    assert r['monthly']['first_year_total'] == pytest.approx(by_year[0]['total'])


def test_no_buydown_has_single_payment():
    r = hm.calculate_home_move(params(purchase_price=400_000), RATES)
    assert len(r['monthly']['by_year']) == 1
    assert r['monthly']['first_year_total'] == pytest.approx(r['monthly']['total'])


def test_buyer_paid_buydown_adds_to_closing_costs_seller_paid_does_not():
    base = params(purchase_price=400_000, buydown_year1=2, buydown_year2=1)
    buyer = hm.calculate_home_move(dict(base, buydown_payer='buyer'), RATES)['purchase']
    seller = hm.calculate_home_move(dict(base, buydown_payer='seller'), RATES)['purchase']
    assert buyer['cash_to_close'] - seller['cash_to_close'] == pytest.approx(buyer['buydown']['cost'], abs=0.01)


# ---------------------------------------------------------------- reverse solver
def test_solver_hits_note_rate_target():
    price, r = hm.solve_price_for_payment(params(solve_for='payment', target_payment=3000), RATES)
    assert price and r['monthly']['total'] <= 3000
    # Rounded down to $1,000, so the next $1,000 up would exceed the target.
    above = hm.calculate_home_move(params(purchase_price=price + 1000), RATES)['monthly']['total']
    assert above > 3000 - 15


def test_year1_target_with_buydown_buys_more_house():
    common = dict(solve_for='payment', target_payment=3000, buydown_year1=2, buydown_year2=1)
    note_price, _ = hm.solve_price_for_payment(params(target_basis='note', **common), RATES)
    y1_price, r = hm.solve_price_for_payment(params(target_basis='year1', **common), RATES)
    assert y1_price > note_price
    assert r['monthly']['first_year_total'] <= 3000
    assert r['monthly']['total'] > 3000  # payment steps up once the buydown ends


def test_year1_basis_without_buydown_matches_note_basis():
    a, _ = hm.solve_price_for_payment(params(target_payment=3000, target_basis='note'), RATES)
    b, _ = hm.solve_price_for_payment(params(target_payment=3000, target_basis='year1'), RATES)
    assert a == b


def test_compare_buydowns_orders_by_reach():
    rows = hm.compare_buydowns(params(target_payment=3000), RATES)
    assert [r['label'] for r in rows] == ['No buydown', '1-0', '2-1']
    prices = [r['price'] for r in rows]
    assert prices == sorted(prices) and prices[0] < prices[-1]
    for r in rows:
        assert r['by_year'][0]['total'] <= 3000
    assert rows[0]['cost'] == 0 and rows[2]['cost'] > rows[1]['cost'] > 0


def test_unreachable_target_returns_none():
    # Fixed costs alone exceed the target.
    assert hm.solve_price_for_payment(params(target_payment=50, hoa_monthly=400), RATES) == (None, None)


def test_parse_params_rejects_bad_target_basis():
    assert hm.parse_params({'target_basis': 'bogus'})['target_basis'] == 'note'
    assert hm.parse_params({'target_basis': 'year1'})['target_basis'] == 'year1'
