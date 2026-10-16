"""Obviously-fake demo data for trying the app or taking screenshots.

Every name, institution and number here is invented ("Demo Bank",
"Example Credit Union", "Alex & Sam Example"). Used by the first-run setup
wizard's "load demo data" button and by ``scripts/seed_demo.py``.

Never point it at a database holding real data: ``force=True`` deletes ALL
documents in the collections it seeds.
"""
from datetime import datetime, timedelta

from dateutil.relativedelta import relativedelta

from models import (
    Account, Asset, Debt, Holding, RecurringTransaction, RecurringTransfer,
    SavingsGoal, Transaction,
)

SEEDED = [
    'accounts', 'transactions', 'recurring_transactions', 'recurring_transfers',
    'debts', 'assets', 'holdings', 'savings_goals', 'budget_targets',
    'cc_cards', 'cc_transactions',
]


class DemoDataRefused(RuntimeError):
    """Raised when the database already has accounts and force is not set."""


def load_demo_data(db, force=False):
    """Fill ``db`` with the demo household.

    Refuses (DemoDataRefused) when accounts already exist, unless ``force``,
    which first wipes every collection in SEEDED.
    """
    if db.accounts.count_documents({}) and not force:
        raise DemoDataRefused('Database already has accounts; refusing to load demo data.')
    if force:
        for name in SEEDED:
            db[name].delete_many({})

    now = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = now.replace(day=1)
    year_start = now.replace(month=1, day=1)

    # ---- Accounts --------------------------------------------------------
    checking = Account('Demo Checking', 'checking', 'Demo Bank', balance=6200.00,
                       is_primary=True, balance_as_of_date=now)
    savings = Account('Demo High-Yield Savings', 'savings', 'Example Online Bank', balance=18500.00,
                      balance_as_of_date=now)
    k401 = Account('Alex 401(k)', '401k', 'Example Retirement Co', balance=64000.00,
                   estimated_return_rate=6.0, management_type='managed', balance_as_of_date=now)
    roth = Account('Sam Roth IRA', 'roth_ira', 'Sample Brokerage', balance=21000.00,
                   estimated_return_rate=7.0, management_type='self_managed', balance_as_of_date=now)
    brokerage = Account('Joint Brokerage', 'brokerage', 'Sample Brokerage', balance=12500.00,
                        estimated_return_rate=6.5, management_type='self_managed', balance_as_of_date=now)
    for a in (checking, savings, k401, roth, brokerage):
        db.accounts.insert_one(a.__dict__)

    # ---- Recurring budget items -------------------------------------------
    def rec(desc, amount, ttype, category, day, freq='monthly', account=checking, **kw):
        r = RecurringTransaction(desc, amount, freq, year_start, ttype, day_of_month=day,
                                 category=category, account_id=str(account._id), **kw)
        db.recurring_transactions.insert_one(r.__dict__)
        return r

    rec('Alex Paycheck', 2850.00, 'income', 'Salary', '15,30')
    rec('Sam Paycheck', 2100.00, 'income', 'Salary', '1,15')
    rec('Mortgage Payment', 2150.00, 'expense', 'Housing', '1', auto_pay=True)
    rec('Electric Utility', 120.00, 'expense', 'Utilities', '12')
    rec('Internet Service', 70.00, 'expense', 'Utilities', '18', auto_pay=True)
    rec('Phone Plan', 85.00, 'expense', 'Phone', '20', auto_pay=True)
    rec('Car Payment', 410.00, 'expense', 'Debt Payments', '5', auto_pay=True)
    rec('Student Loan Payment', 230.00, 'expense', 'Debt Payments', '22')
    rec('Car Insurance', 640.00, 'expense', 'Insurance', '10', freq='yearly')
    rec('Credit Card Payment', 1400.00, 'expense', 'Debt Payments', '25')

    for rt in (
        RecurringTransfer(str(checking._id), str(savings._id), 400.00, 'monthly', year_start,
                          day_of_month='16', description='Monthly savings sweep'),
        RecurringTransfer(str(checking._id), str(roth._id), 250.00, 'monthly', year_start,
                          day_of_month='2', description='Roth IRA contribution'),
    ):
        db.recurring_transfers.insert_one(rt.__dict__)

    # ---- A few actual ledger transactions this month ----------------------
    for desc, amount, ttype, day in (
        ('Sam Paycheck', 2100.00, 'income', 1),
        ('Mortgage Payment', 2150.00, 'expense', 1),
        ('Car Payment', 410.00, 'expense', 5),
    ):
        d = month_start + timedelta(days=day - 1)
        if d <= now:
            t = Transaction(desc, amount, d, ttype, account_id=str(checking._id))
            db.transactions.insert_one(t.__dict__)

    # ---- Debts and assets ------------------------------------------------
    for d in (
        Debt('Student Loan (Sam)', 'student_loan', 17800.00, interest_rate=4.5, minimum_payment=230.00,
             creditor='Example Loan Servicer', payment_match=['Student Loan Payment']),
        Debt('Demo Rewards Card', 'credit_card', 0.00, interest_rate=24.99, minimum_payment=0,
             creditor='Demo Bank', notes='Paid in full monthly'),
    ):
        db.debts.insert_one(d.__dict__)

    for a in (
        Asset('Demo House', 'real_estate', 410000.00, purchase_date=datetime(2021, 6, 1),
              purchase_price=360000.00, liability_balance=298000.00, liability_type='mortgage',
              address='123 Example Street, Anytown', interest_rate=6.0, loan_term_months=360,
              monthly_payment=2150.00, payment_start_date=datetime(2021, 7, 1),
              property_tax_annual=4800.00, homeowners_insurance_annual=1500.00,
              payment_match=['Mortgage Payment']),
        Asset('Demo Hatchback', 'vehicle', 16500.00, purchase_date=datetime(2023, 3, 1),
              purchase_price=24000.00, liability_balance=11800.00, liability_type='auto_loan',
              interest_rate=5.5, loan_term_months=60, monthly_payment=410.00,
              payment_start_date=datetime(2023, 4, 5), payment_match=['Car Payment']),
    ):
        db.assets.insert_one(a.__dict__)

    # ---- Holdings (real tickers so live prices work) -----------------------
    for acct, sym, qty, cost in (
        (brokerage, 'VTI', 25, 5200.00),
        (brokerage, 'SCHD', 60, 4300.00),
        (roth, 'VXUS', 80, 4600.00),
        (roth, 'BND', 50, 3700.00),
    ):
        db.holdings.insert_one(Holding(str(acct._id), sym, qty, cost_basis=cost, notes='demo').__dict__)

    # ---- Goals and budget targets ----------------------------------------
    db.savings_goals.insert_one(SavingsGoal('Emergency fund (6 months)', 0, goal_type='emergency',
                                            months_target=6, linked_account_ids=[str(savings._id)]).__dict__)
    db.savings_goals.insert_one(SavingsGoal('Kitchen refresh', 15000.00, current_amount=4200.00,
                                            target_date=now + relativedelta(months=14),
                                            category='purchase').__dict__)
    for cat, target in (('Groceries', 900), ('Dining', 350), ('Gas & Fuel', 180),
                        ('Shopping', 300), ('Entertainment', 120), ('Subscriptions', 60)):
        db.budget_targets.update_one({'category': cat}, {'$set': {'category': cat, 'target': float(target)}},
                                     upsert=True)

    # ---- Credit card + last month's statement ------------------------------
    db.cc_cards.insert_one({'name': 'Demo Rewards Card', 'issuer': 'other', 'created_at': now})
    last_month = month_start - relativedelta(months=1)
    stmt = last_month.strftime('%Y-%m')
    for day, desc, amount, cat in (
        (2, 'WHOLE FOODS MARKET #000', 142.18, 'Groceries'),
        (3, 'SHELL OIL 0000000', 48.20, 'Gas & Fuel'),
        (5, 'NETFLIX.COM', 15.49, 'Entertainment'),
        (7, 'TRADER JOE S #000', 96.40, 'Groceries'),
        (9, 'CHIPOTLE 0000', 27.85, 'Dining'),
        (12, 'AMAZON MKTPLACE PMTS', 63.99, 'Shopping'),
        (14, 'EXAMPLE NEIGHBORHOOD CAFE', 18.75, 'Dining'),
        (16, 'KROGER #000', 121.62, 'Groceries'),
        (19, 'SPOTIFY USA', 11.99, 'Entertainment'),
        (21, 'CVS/PHARMACY #0000', 22.40, 'Healthcare'),
        (24, 'TARGET 00000000', 88.10, 'Groceries'),
        (26, 'AMC THEATRE 0000', 34.00, 'Entertainment'),
        (27, 'ITEM RETURN CREDIT', -25.00, 'Credit'),
    ):
        db.cc_transactions.insert_one({
            'date': last_month + timedelta(days=day - 1), 'description': desc, 'amount': amount,
            'category': cat, 'card_issuer': 'other', 'card_name': 'Demo Rewards Card',
            'original_category': '', 'statement_date': stmt,
        })
