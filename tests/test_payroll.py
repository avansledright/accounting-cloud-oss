"""Household payroll: Pub 926 age/relationship exemptions and the FICA threshold catch-up."""
from datetime import datetime

import pytest
from bson import ObjectId

SETTINGS = {
    'ss_rate': 0.062, 'medicare_rate': 0.0145, 'additional_medicare_rate': 0.009,
    'additional_medicare_threshold': 200000, 'ss_wage_base': 184500,
    'fica_annual_threshold': 2900, 'futa_rate': 0.006, 'futa_wage_base': 7000,
    'state': 'XX', 'state_configs': {'XX': {'suta_rate': 0.0, 'suta_wage_base': 0}},
}


def _employee(db, **fields):
    doc = {'_id': ObjectId(), 'name': 'Sam', **fields}
    db.household_employees.insert_one(doc)
    return doc


def _pay(db, emp, gross, when):
    db.payroll_entries.insert_one({'employee_id': emp['_id'], 'pay_date': when,
                                   'gross_wages': gross})


# ---------------------------------------------------------------- 13. exemptions
def test_child_aged_18_to_20_is_exempt_from_fica_and_futa(app_module, db):
    # under_21 but NOT under_18: the case the old under_18 gate got wrong
    emp = _employee(db, relationship='daughter', under_21=True, under_18=False)
    calc = app_module.calculate_payroll(emp, 4000.0, datetime(2026, 6, 1), SETTINGS)
    assert calc['ee_ss'] == calc['er_ss'] == calc['ee_medicare'] == calc['er_medicare'] == 0
    assert calc['er_futa'] == 0
    assert 'FICA exempt: child under 21' in calc['notes']


def test_child_flagged_only_under_18_still_qualifies(app_module, db):
    emp = _employee(db, relationship='son', under_18=True)
    calc = app_module.calculate_payroll(emp, 4000.0, datetime(2026, 6, 1), SETTINGS)
    assert calc['ee_ss'] == 0 and calc['er_futa'] == 0


def test_unrelated_under_18_is_fica_exempt_but_owes_futa(app_module, db):
    emp = _employee(db, relationship='unrelated', under_18=True)
    calc = app_module.calculate_payroll(emp, 4000.0, datetime(2026, 6, 1), SETTINGS)
    assert calc['ee_ss'] == 0 and calc['ee_medicare'] == 0
    assert calc['er_futa'] == pytest.approx(24.0)  # 0.6% of 4,000


def test_unrelated_under_21_gets_no_child_exemption(app_module, db):
    emp = _employee(db, relationship='unrelated', under_21=True)
    calc = app_module.calculate_payroll(emp, 4000.0, datetime(2026, 6, 1), SETTINGS)
    assert calc['ee_ss'] == pytest.approx(248.0)
    assert calc['er_futa'] == pytest.approx(24.0)


def test_parent_is_futa_exempt(app_module, db):
    emp = _employee(db, relationship='mother')
    calc = app_module.calculate_payroll(emp, 4000.0, datetime(2026, 6, 1), SETTINGS)
    assert calc['er_futa'] == 0 and calc['ee_ss'] == 0


# ---------------------------------------------------------------- 14. threshold catch-up
def test_crossing_threshold_catches_up_fica_on_earlier_wages(app_module, db):
    emp = _employee(db, relationship='unrelated')
    _pay(db, emp, 1500.0, datetime(2026, 1, 31))
    _pay(db, emp, 1000.0, datetime(2026, 2, 28))

    # Below the threshold: nothing yet
    early = app_module.calculate_payroll(emp, 1000.0, datetime(2026, 2, 28), SETTINGS)
    assert early['ee_ss'] == 0

    # YTD 2,500 + 1,000 = 3,500 >= 2,900: all 3,500 is now FICA wages
    calc = app_module.calculate_payroll(emp, 1000.0, datetime(2026, 3, 31), SETTINGS)
    assert calc['ee_ss'] == calc['er_ss'] == pytest.approx(round(3500 * 0.062, 2))
    assert calc['ee_medicare'] == calc['er_medicare'] == pytest.approx(round(3500 * 0.0145, 2))
    assert any('catch-up on $2,500.00' in n for n in calc['notes'])

    # The run after: only its own wages
    _pay(db, emp, 1000.0, datetime(2026, 3, 31))
    later = app_module.calculate_payroll(emp, 1000.0, datetime(2026, 4, 30), SETTINGS)
    assert later['ee_ss'] == pytest.approx(62.0)


def test_schedule_h_counts_all_wages_once_fica_triggers(app_module, db):
    emp = _employee(db, relationship='unrelated')
    db.payroll_entries.insert_many([
        {'employee_id': emp['_id'], 'pay_date': datetime(2026, 1, 31), 'gross_wages': 2500.0,
         'ee_ss': 0, 'ee_medicare': 0},
        {'employee_id': emp['_id'], 'pay_date': datetime(2026, 3, 31), 'gross_wages': 1000.0,
         'ee_ss': 217.0, 'er_ss': 217.0, 'ee_medicare': 50.75, 'er_medicare': 50.75},
    ])
    totals, per_emp = app_module._schedule_h_totals(2026, SETTINGS)
    assert totals['cash_wages_fica'] == 3500.0
    assert per_emp == [{'name': 'Sam', 'gross': 3500.0, 'fica_wages': 3500.0}]


def test_schedule_h_route_renders(app_module, client, db):
    assert client.get('/payroll/schedule-h/2026').status_code == 200
