"""Card perks: fee/anniversary from the statement fee charge, and template matching."""
from datetime import datetime

from bson import ObjectId

import benefits


def _fee(card_name, amount, when, desc='ANNUAL FEE'):
    return {'card_name': card_name, 'amount': amount, 'date': when, 'description': desc}


# ---------------------------------------------------------------- 15. fee detection
def test_detect_annual_fee_takes_latest_charge():
    card = {'name': 'Amex Platinum'}
    txns = [
        _fee('Amex Platinum', 695.0, datetime(2025, 9, 3), 'PLATINUM ANNUAL MEMBERSHIP FEE'),
        _fee('Amex Platinum', 895.0, datetime(2026, 9, 4), 'ANNUAL MEMBERSHIP FEE'),
        _fee('Amex Platinum', -895.0, datetime(2026, 9, 20), 'ANNUAL MEMBERSHIP FEE REVERSAL'),
        _fee('Other Card', 95.0, datetime(2026, 11, 1)),
        {'card_name': 'Amex Platinum', 'amount': 30.0, 'date': datetime(2026, 10, 1),
         'description': 'LATE FEE'},
    ]
    assert benefits.detect_annual_fee(card, txns) == (895.0, 9)
    assert benefits.detect_annual_fee({'name': 'No Fee Card'}, txns) == (None, None)


def test_build_card_perks_fills_missing_fee_from_statement():
    card = {'_id': ObjectId(), 'name': 'Citi AAdvantage Executive', 'issuer': 'citi'}
    perks = benefits.build_card_perks([card], [], 2026, today=datetime(2026, 10, 16),
                                      fee_transactions=[_fee(card['name'], 595.0, datetime(2025, 11, 2))])
    result = perks[0]
    assert result['annual_fee'] == 595.0 and result['fee_detected']
    assert result['fee_month'] == 11 and result['fee_month_detected']
    assert 'annual_fee' not in card  # derived in memory, never written back


def test_build_card_perks_never_overrides_hand_entered_values():
    card = {'_id': ObjectId(), 'name': 'Sapphire', 'annual_fee': 95.0, 'fee_month': 3}
    perks = benefits.build_card_perks([card], [], 2026, today=datetime(2026, 10, 16),
                                      fee_transactions=[_fee('Sapphire', 550.0, datetime(2026, 7, 1))])
    assert perks[0]['annual_fee'] == 95.0 and perks[0]['fee_month'] == 3
    assert not perks[0]['fee_detected'] and not perks[0]['fee_month_detected']


def test_build_card_perks_fills_only_the_missing_month():
    card = {'_id': ObjectId(), 'name': 'Sapphire', 'annual_fee': 95.0}
    perks = benefits.build_card_perks([card], [], 2026, today=datetime(2026, 10, 16),
                                      fee_transactions=[_fee('Sapphire', 550.0, datetime(2026, 7, 1))])
    assert perks[0]['annual_fee'] == 95.0 and not perks[0]['fee_detected']
    assert perks[0]['fee_month'] == 7 and perks[0]['fee_month_detected']


def test_benefits_page_reads_fee_charge_from_a_previous_year(client, db):
    db.cc_cards.insert_one({'name': 'Citi Exec', 'issuer': 'citi'})
    db.cc_transactions.insert_one(_fee('Citi Exec', 595.0, datetime(2025, 11, 2)))
    resp = client.get('/benefits?year=2026')
    assert resp.status_code == 200
    assert b'(from statement)' in resp.data
    assert db.cc_cards.find_one()['name'] == 'Citi Exec'
    assert 'annual_fee' not in db.cc_cards.find_one()


# ---------------------------------------------------------------- 16. template matching
def test_plain_citi_cards_are_not_offered_the_aadvantage_template():
    for name in ('Citi Double Cash', 'Citi Custom Cash', 'Citi Premier', 'Citi'):
        assert benefits.suggest_template({'name': name, 'issuer': 'citi'}) is None, name


def test_aadvantage_cards_still_match():
    for name in ('Citi AAdvantage Executive', 'AAdvantage Exec', 'Citi Executive',
                 'American Airlines Executive'):
        assert benefits.suggest_template({'name': name, 'issuer': 'citi'}) == 'citi_aadvantage_executive', name


def test_other_templates_unchanged():
    assert benefits.suggest_template({'name': 'Platinum', 'issuer': 'amex'}) == 'amex_platinum'
    assert benefits.suggest_template({'name': 'Chase Sapphire', 'issuer': 'chase'}) == 'chase_sapphire_preferred'
