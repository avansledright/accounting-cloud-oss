from datetime import datetime
from bson import ObjectId

class Transaction:
    def __init__(self, description, amount, date, transaction_type, account_id=None, _id=None):
        self._id = ObjectId(_id) if _id else ObjectId()
        self.description = description
        self.amount = amount
        self.date = date
        self.transaction_type = transaction_type # 'expense' or 'income'
        self.account_id = ObjectId(account_id) if account_id else None  # Links to Account._id

    def to_dict(self):
        return {
            "_id": str(self._id),
            "description": self.description,
            "amount": self.amount,
            "date": self.date.isoformat(),
            "transaction_type": self.transaction_type,
            "account_id": str(self.account_id) if self.account_id else None
        }

class RecurringTransaction:
    def __init__(self, description, amount, frequency, start_date, transaction_type,
                 end_date=None, day_of_month=None, category=None, notes=None,
                 account_number=None, auto_pay=False, payment_method=None,
                 account_id=None, _id=None):
        self._id = ObjectId(_id) if _id else ObjectId()
        self.description = description
        self.amount = amount
        self.frequency = frequency  # e.g., 'weekly', 'monthly', 'bi-monthly', 'yearly'
        self.start_date = start_date
        self.transaction_type = transaction_type  # 'expense' or 'income'
        self.end_date = end_date
        self.day_of_month = day_of_month  # e.g., "1" or "1,15" for multiple days
        self.category = category  # e.g., 'Housing', 'Utilities', 'Income'
        self.notes = notes
        self.account_number = account_number
        self.auto_pay = auto_pay
        self.payment_method = payment_method  # e.g., 'Checking', 'Credit Card'
        self.account_id = ObjectId(account_id) if account_id else None  # Links to Account._id

    def to_dict(self):
        return {
            "_id": str(self._id),
            "description": self.description,
            "amount": self.amount,
            "frequency": self.frequency,
            "start_date": self.start_date.isoformat(),
            "transaction_type": self.transaction_type,
            "end_date": self.end_date.isoformat() if self.end_date else None,
            "day_of_month": self.day_of_month,
            "category": self.category,
            "notes": self.notes,
            "account_number": self.account_number,
            "auto_pay": self.auto_pay,
            "payment_method": self.payment_method,
            "account_id": str(self.account_id) if self.account_id else None
        }


class ProjectedOverride:
    """Stores amount overrides for specific occurrences of recurring transactions."""
    def __init__(self, recurring_id, date, amount, _id=None):
        self._id = ObjectId(_id) if _id else ObjectId()
        self.recurring_id = ObjectId(recurring_id) if recurring_id else None
        self.date = date  # The specific date of the projected occurrence
        self.amount = amount  # The overridden amount

    def to_dict(self):
        return {
            "_id": str(self._id),
            "recurring_id": str(self.recurring_id) if self.recurring_id else None,
            "date": self.date.isoformat() if hasattr(self.date, 'isoformat') else self.date,
            "amount": self.amount
        }


# Unified spending categories (used by both budget and CC transactions)
CATEGORIES = [
    'Dining', 'Groceries', 'Gas & Fuel', 'Travel', 'Shopping',
    'Entertainment', 'Subscriptions', 'Utilities', 'Phone', 'Healthcare',
    'Transportation', 'Home', 'Housing', 'Insurance', 'Debt Payments',
    'Personal', 'Fees & Interest', 'Savings', 'Investments', 'Other'
]

# Aliases so existing references keep working
EXPENSE_CATEGORIES = CATEGORIES

INCOME_CATEGORIES = [
    'Salary',
    'Freelance',
    'Investments',
    'Rental Income',
    'Other Income'
]

# Account types
ACCOUNT_TYPES = [
    ('checking', 'Checking'),
    ('savings', 'Savings'),
    ('ira', 'Traditional IRA'),
    ('roth_ira', 'Roth IRA'),
    ('brokerage', 'Brokerage'),
    ('401k', '401(k)'),
    ('hsa', 'HSA'),
    ('deferred_comp', 'Deferred Comp / Employer Plan'),
    ('other', 'Other')
]

# Account types that hold securities and get return-rate / projection treatment.
INVESTMENT_ACCOUNT_TYPES = ['ira', 'roth_ira', 'brokerage', '401k', 'hsa', 'deferred_comp']

# Account types that hold spendable cash for the ledger/outlook.
CASH_ACCOUNT_TYPES = ['checking', 'savings']

# Debt types
DEBT_TYPES = [
    ('credit_card', 'Credit Card'),
    ('personal_loan', 'Personal Loan'),
    ('student_loan', 'Student Loan'),
    ('medical', 'Medical'),
    ('other', 'Other')
]

# Asset types
ASSET_TYPES = [
    ('real_estate', 'Real Estate'),
    ('vehicle', 'Vehicle'),
    ('other', 'Other')
]

# Liability types
LIABILITY_TYPES = [
    ('mortgage', 'Mortgage'),
    ('auto_loan', 'Auto Loan'),
    ('heloc', 'HELOC'),
    ('other', 'Other')
]

# Credit card issuers
CARD_ISSUERS = [
    ('chase', 'Chase'),
    ('citi', 'Citi'),
    ('amex', 'American Express'),
    ('other', 'Other')
]

# CC transactions share the budget categories, plus a 'Credit' bucket for
# merchant credits/refunds (which are stored as negative amounts).
CC_CATEGORIES = CATEGORIES + ['Credit']

# Chase bank category → CC_CATEGORIES mapping
CHASE_CATEGORY_MAP = {
    'Food & Drink': 'Dining',
    'Groceries': 'Groceries',
    'Gas': 'Gas & Fuel',
    'Travel': 'Travel',
    'Shopping': 'Shopping',
    'Entertainment': 'Entertainment',
    'Bills & Utilities': 'Utilities',
    'Health & Wellness': 'Healthcare',
    'Home': 'Home',
    'Personal': 'Personal',
    'Fees & Adjustments': 'Fees & Interest',
}

# Disambiguation rules, checked *before* MERCHANT_CATEGORY_RULES and in this order.
# These exist because a broader keyword below would otherwise win and get it wrong.
# Add entries from how your own statements are actually categorized, not from
# intuition: each one overrides every keyword rule.
MERCHANT_OVERRIDES = [
    # Warehouse club: the fuel pump is its own merchant string.
    ('COSTCO GAS', 'Gas & Fuel'),
    # ...and the warehouse itself is treated as Groceries, however mixed the basket.
    ('COSTCO', 'Groceries'),
    # Transit before Dining's 'SUBWAY' sandwich rule.
    ('SUBWAY TRANSIT', 'Transportation'),
    # Toast card-present POS prefix: overwhelmingly restaurants.
    ('TST*', 'Dining'),
    ('TST *', 'Dining'),
    # Merchant strings that run the brand together and so miss the spaced keyword.
    ('WHOLEFDS', 'Groceries'),
    ('BESTBUY', 'Shopping'),
    ('OLDNAVY', 'Shopping'),
    ('CIRCLEK', 'Gas & Fuel'),
    ('BP#', 'Gas & Fuel'),
    # The .com storefront is household goods; the physical store is Shopping.
    ('HOMEDEPOT.COM', 'Home'),
]

# Keyword rules for auto-categorizing CC transactions (matched against uppercase description).
# Dict order is precedence: the first category with a matching keyword wins.
MERCHANT_CATEGORY_RULES = {
    'Dining': ['MCDONALD', 'STARBUCKS', 'CHIPOTLE', 'CHICK-FIL', 'DUNKIN', 'PANERA',
               'TACO BELL', 'WENDY', 'BURGER KING', 'DOMINO', 'PIZZA HUT', 'GRUBHUB',
               'DOORDASH', 'UBER EATS', 'SUBWAY', 'POPEYE', 'IHOP', 'DENNY', 'APPLEBEE',
               'CHILI', 'OLIVE GARDEN', 'OUTBACK', 'CRACKER BARREL', 'WINGSTOP',
               'PANDA EXPRESS', 'FIVE GUYS', 'SONIC DRIVE',
               'BUFFALO WILD', 'RED ROBIN', 'WAFFLE HOUSE', 'RESTAURANT', 'CAFE', 'DINER',
               'BAKERY', 'SUSHI', 'PIZZA', 'GRILL', 'KITCHEN', 'BISTRO', 'EATERY',
               'TAQUERIA', 'BBQ', 'BARBEQUE',
               'JIMMY JOHN', 'FIRST WATCH', 'AUNTIE ANNE', 'CINNABON',
               'COFFEE', 'ESPRESSO', 'ROASTERS', 'DONUT', 'CREAMERY', 'BAGEL',
               'PASTRY', 'DELI', 'NOODLE', 'PIZZERIA', 'STEAKHOUSE', 'CANTINA',
               'TAVERN', ' PUB', 'BREWING', 'BREWERY', 'VENDING',
               # Square card-present prefix: what reaches here is usually a restaurant
               'SQ *'],
    'Groceries': ['WALMART', 'KROGER', 'PUBLIX', 'SAFEWAY', 'WHOLE FOODS', 'TRADER JOE',
                  'ALDI', 'COSTCO', 'SAM\'S CLUB', 'TARGET', 'HEB', 'FOOD LION',
                  'WEGMANS', 'MEIJER', 'STOP & SHOP', 'GIANT', 'SPROUTS', 'MARKET',
                  'GROCERY', 'FRESH', 'WINN DIXIE', 'PIGGLY', 'HARRIS TEETER',
                  'ALBERTSONS', 'LIDL', 'INSTACART', 'PRODUCE', 'BUTCHER'],
    'Gas & Fuel': ['SHELL', 'EXXON', 'CHEVRON', 'BP ', 'SUNOCO', 'MARATHON', 'SPEEDWAY',
                   'WAWA', 'CIRCLE K', 'QUIKTRIP', 'RACETRAC', 'VALERO', 'CITGO',
                   'MURPHY', 'SHEETZ', 'LOVES TRAVEL', 'PILOT', 'FUEL', 'GASOLINE',
                   'KWIK TRIP', 'CASEY', 'CUMBERLAND FARMS', 'BUCEE', 'GAS'],
    'Travel': ['AIRLINE', 'UNITED AIR', 'DELTA AIR', 'AMERICAN AIR', 'SOUTHWEST',
               'JETBLUE', 'SPIRIT AIR', 'FRONTIER', 'HOTEL', 'MARRIOTT', 'HILTON',
               'HYATT', 'AIRBNB', 'VRBO', 'EXPEDIA', 'BOOKING.COM', 'KAYAK',
               'ENTERPRISE', 'HERTZ', 'AVIS', 'RENTAL CAR', 'LYFT', 'UBER',
               # Parking and tolls live under Transportation, not here.
               'RESORT', 'LODGE', 'MOTEL',
               # American Airlines ticket numbers arrive as AMERICAN00<13 digits>
               'AMERICAN00'],
    'Shopping': ['AMAZON', 'AMZN', 'EBAY', 'ETSY', 'BEST BUY', 'HOME DEPOT',
                 'LOWES', 'IKEA', 'WAYFAIR', 'NORDSTROM', 'MACY', 'KOHL',
                 'OLD NAVY', 'GAP', 'NIKE', 'ADIDAS', 'MARSHALL', 'TJ MAXX',
                 'ROSS', 'DOLLAR TREE', 'DOLLAR GENERAL', 'FIVE BELOW',
                 'BED BATH', 'APPLE.COM', 'APPLE STORE', 'DICK\'S SPORT',
                 'POSHMARK', 'BLOOMINGDALE', 'REI.COM', 'DUTY FREE'],
    'Entertainment': ['NETFLIX', 'HULU', 'DISNEY+', 'HBO', 'SPOTIFY', 'APPLE MUSIC',
                      'YOUTUBE', 'PARAMOUNT', 'PEACOCK', 'AMC THEATRE', 'REGAL',
                      'CINEMARK', 'FANDANGO', 'STUBHUB', 'TICKETMASTER',
                      'MOVIE', 'THEATRE', 'THEATER', 'STEAM', 'PLAYSTATION',
                      'XBOX', 'NINTENDO', 'TICKETWEB', 'CONCERT', 'KINDLE'],
    'Subscriptions': ['SUBSCRIPTION', 'RECURRING', 'MEMBER', 'MONTHLY',
                      'ADOBE', 'MICROSOFT', 'GOOGLE STORAGE', 'ICLOUD',
                      'DROPBOX', 'GITHUB', 'AWS', 'CHATGPT', 'OPENAI',
                      'PATREON', 'NOTION', 'ZOOM', '1PASSWORD', 'LASTPASS',
                      'NORDVPN', 'EXPRESSVPN', 'GYM', 'FITNESS', 'PLANET FIT',
                      'ANYTIME FIT', 'CRUNCH', 'YMCA'],
    'Utilities': ['ELECTRIC', 'POWER', 'WATER BILL', 'WATER DEPT', 'WATER UTIL', 'WATER SVC', 'SEWER', 'GAS BILL', 'UTILITY',
                  'COMCAST', 'XFINITY', 'SPECTRUM', 'INTERNET', 'CABLE',
                  'WASTE MGMT', 'TRASH'],
    'Phone': ['GOOGLE *FI', 'GOOGLE FI', 'GOOGLE*FI', 'PHONE BILL', 'T-MOBILE', 'TMOBILE',
              'VERIZON', 'AT&T', 'MINT MOBILE', 'BOOST MOBILE', 'STRAIGHT TALK', 'US CELLULAR',
              'XFINITY MOBILE', 'CONSUMER CELLULAR', 'TRACFONE'],
    'Healthcare': ['CVS', 'WALGREENS', 'PHARMACY', 'MEDICAL', 'DOCTOR', 'DENTAL',
                   'HOSPITAL', 'CLINIC', 'URGENT CARE', 'OPTOM', 'VISION',
                   'HEALTH', 'LABCORP', 'QUEST DIAG', 'RITE AID', 'PRESCRIPTION',
                   'DIAGNOSTIC', 'ORTHO', 'DERMATOL', 'OPTICAL'],
    'Transportation': ['AUTO', 'JIFFY LUBE', 'AUTOZONE', 'O\'REILLY', 'ADVANCE AUTO',
                       'NAPA', 'TIRE', 'CARWASH', 'OIL CHANGE', 'MECHANIC',
                       'TRANSIT', 'METRO', 'BUS', 'TRAIN', 'SUBWAY TRANSIT',
                       'PARKING', 'PARKI', 'TOLL'],
    'Home': ['HOME IMPROVEMENT', 'PLUMB', 'ACE HARDWARE',
             'TRUE VALUE', 'MENARDS', 'CARPET', 'PAINT', 'LAWN',
             'GARDEN', 'POOL', 'PEST CONTROL', 'CLEANING',
             'TASKRABBIT', 'USPS', 'HARDWARE', 'FURNITURE', 'MATTRESS'],
    'Personal': ['SALON', 'BARBER', 'SPA', 'NAIL', 'BEAUTY', 'SEPHORA',
                 'ULTA', 'BATH & BODY', 'LAUNDRY', 'DRY CLEAN', 'CHEWY', 'PETCO', 'PETSMART'],
    'Fees & Interest': ['INTEREST CHARGE', 'LATE FEE', 'ANNUAL FEE', 'FINANCE CHARGE',
                        'FOREIGN TRANSACTION', 'CASH ADVANCE FEE', 'OVER LIMIT'],
}

# Keywords for card payments (paying the bill) — dropped entirely during import,
# since they are neither spending nor a merchant credit.
PAYMENT_KEYWORDS = ['PAYMENT', 'AUTOPAY', 'AUTO-PAY', 'THANK YOU', 'PYMT']

# Keywords for merchant credits/refunds — kept during import but stored as a
# negative amount so they net against spending instead of showing as a charge.
CREDIT_KEYWORDS = ['CREDIT', 'REFUND', 'REBATE', 'CASHBACK REWARD']

SUBSCRIPTION_GROUPS = {
    'Video Streaming': ['NETFLIX', 'HULU', 'DISNEY', 'HBO', 'PARAMOUNT', 'PEACOCK',
                        'APPLE TV', 'PRIME VIDEO', 'YOUTUBE PREMIUM', 'CRUNCHYROLL'],
    'Music Streaming': ['SPOTIFY', 'APPLE MUSIC', 'YOUTUBE MUSIC', 'TIDAL',
                        'AMAZON MUSIC', 'PANDORA', 'DEEZER'],
    'Cloud Storage': ['ICLOUD', 'GOOGLE STORAGE', 'GOOGLE ONE', 'DROPBOX', 'ONEDRIVE'],
    'Password Manager': ['1PASSWORD', 'LASTPASS', 'BITWARDEN', 'DASHLANE'],
    'VPN': ['NORDVPN', 'EXPRESSVPN', 'SURFSHARK', 'PROTONVPN'],
    'Fitness': ['PLANET FIT', 'ANYTIME FIT', 'CRUNCH', 'LA FITNESS', 'YMCA',
                'PELOTON', 'APPLE FITNESS'],
    'News/Media': ['NEW YORK TIMES', 'WASHINGTON POST', 'WSJ', 'MEDIUM', 'SUBSTACK'],
    'Food Delivery': ['DOORDASH', 'UBER EATS', 'GRUBHUB', 'INSTACART'],
}


class Account:
    def __init__(self, name, account_type, institution, balance=0,
                 account_number=None, notes=None, is_primary=False,
                 estimated_return_rate=None, balance_as_of_date=None,
                 management_type=None, _id=None):
        self._id = ObjectId(_id) if _id else ObjectId()
        self.name = name
        self.account_type = account_type  # 'checking', 'savings', 'ira', 'roth_ira', 'brokerage', '401k'
        self.institution = institution
        self.balance = balance
        self.account_number = account_number
        self.notes = notes
        self.is_primary = is_primary  # Primary account for ledger calculations
        self.estimated_return_rate = estimated_return_rate  # Annual % (e.g. 3.0 for 3%)
        self.balance_as_of_date = balance_as_of_date  # Date balance was last confirmed
        self.management_type = management_type  # 'managed' or 'self_managed'

    def to_dict(self):
        d = {
            "_id": str(self._id),
            "name": self.name,
            "account_type": self.account_type,
            "institution": self.institution,
            "balance": self.balance,
            "account_number": self.account_number,
            "notes": self.notes,
            "is_primary": self.is_primary,
            "estimated_return_rate": self.estimated_return_rate,
            "balance_as_of_date": self.balance_as_of_date,
            "management_type": self.management_type,
        }
        return d


class Asset:
    def __init__(self, name, asset_type, estimated_value,
                 purchase_date=None, purchase_price=None,
                 liability_balance=None, liability_type=None,
                 address=None, notes=None,
                 interest_rate=None, loan_term_months=None,
                 monthly_payment=None,
                 payment_start_date=None, payment_frequency=None,
                 property_tax_annual=None, homeowners_insurance_annual=None,
                 pmi_monthly=None, hoa_monthly=None,
                 escrow_balance=None, payment_match=None,
                 _id=None):
        self._id = ObjectId(_id) if _id else ObjectId()
        self.name = name
        self.asset_type = asset_type  # 'real_estate', 'vehicle', 'other'
        self.estimated_value = estimated_value
        self.purchase_date = purchase_date
        self.purchase_price = purchase_price
        self.liability_balance = liability_balance
        self.liability_type = liability_type
        self.address = address
        self.notes = notes
        self.interest_rate = interest_rate  # APR %
        self.loan_term_months = loan_term_months
        self.monthly_payment = monthly_payment
        self.payment_start_date = payment_start_date  # When loan payments began/begin
        self.payment_frequency = payment_frequency or 'monthly'  # 'monthly' or 'biweekly'
        self.property_tax_annual = property_tax_annual
        self.homeowners_insurance_annual = homeowners_insurance_annual
        self.pmi_monthly = pmi_monthly
        self.hoa_monthly = hoa_monthly
        self.escrow_balance = escrow_balance
        self.payment_match = payment_match or []  # Ledger descriptions that auto-link as loan payments

    def to_dict(self):
        return {
            "_id": str(self._id),
            "name": self.name,
            "asset_type": self.asset_type,
            "estimated_value": self.estimated_value,
            "purchase_date": self.purchase_date.isoformat() if self.purchase_date else None,
            "purchase_price": self.purchase_price,
            "liability_balance": self.liability_balance,
            "liability_type": self.liability_type,
            "address": self.address,
            "notes": self.notes,
            "interest_rate": self.interest_rate,
            "loan_term_months": self.loan_term_months,
            "monthly_payment": self.monthly_payment,
            "payment_start_date": self.payment_start_date.isoformat() if self.payment_start_date else None,
            "payment_frequency": self.payment_frequency,
            "property_tax_annual": self.property_tax_annual,
            "homeowners_insurance_annual": self.homeowners_insurance_annual,
            "pmi_monthly": self.pmi_monthly,
            "hoa_monthly": self.hoa_monthly,
            "escrow_balance": self.escrow_balance,
            "payment_match": self.payment_match
        }


# Savings Goal categories
GOAL_CATEGORIES = [
    ('emergency', 'Emergency Fund'),
    ('travel', 'Travel'),
    ('purchase', 'Major Purchase'),
    ('education', 'Education'),
    ('retirement', 'Retirement'),
    ('other', 'Other')
]

# Goal types — how a goal's progress is measured.
#   manual      — hand-tracked via the Contribute box (original behavior)
#   account     — auto-tracks the live combined balance of linked accounts
#   networth    — auto-tracks live net worth (accounts + assets − all debts)
#   emergency   — target = N months of expenses; tracks liquid savings coverage
#   debt_free   — global: pay off all standalone debt (excludes asset-backed loans)
#   debt_payoff — pay off one specific debt
#   retirement  — link retirement accounts to a target, with a projected ETA
GOAL_TYPES = [
    ('manual', 'Manual (hand-tracked)'),
    ('account', 'Account Balance (live)'),
    ('emergency', 'Emergency Fund (months of expenses)'),
    ('retirement', 'Retirement Target'),
    ('networth', 'Net Worth Milestone'),
    ('debt_free', 'Debt Free (all standalone debt)'),
    ('debt_payoff', 'Pay Off a Specific Debt'),
]

# Goal types whose progress is computed from live data rather than the
# manual Contribute box.
LIVE_GOAL_TYPES = {'account', 'emergency', 'retirement', 'networth', 'debt_free', 'debt_payoff'}

# Goal types that represent money set aside (vs. debt/net-worth milestones),
# used for the "Total Saved / Target" summary on the goals page.
SAVINGS_GOAL_TYPES = {'manual', 'account', 'emergency', 'retirement'}


class SavingsGoal:
    def __init__(self, name, target_amount, current_amount=0, target_date=None,
                 category=None, notes=None, created_date=None,
                 goal_type='manual', linked_account_ids=None, linked_debt_id=None,
                 baseline_amount=None, months_target=None, _id=None):
        self._id = ObjectId(_id) if _id else ObjectId()
        self.name = name
        self.target_amount = target_amount
        self.current_amount = current_amount
        self.target_date = target_date
        self.category = category
        self.notes = notes
        self.created_date = created_date or datetime.now()
        self.goal_type = goal_type or 'manual'
        self.linked_account_ids = linked_account_ids or []  # list of account id strings
        self.linked_debt_id = linked_debt_id                # single debt id string
        self.baseline_amount = baseline_amount              # starting debt for debt goals
        self.months_target = months_target                  # months of expenses (emergency)

    def to_dict(self):
        return {
            "_id": str(self._id),
            "name": self.name,
            "target_amount": self.target_amount,
            "current_amount": self.current_amount,
            "target_date": self.target_date.isoformat() if self.target_date else None,
            "category": self.category,
            "notes": self.notes,
            "created_date": self.created_date.isoformat() if self.created_date else None,
            "goal_type": self.goal_type,
            "linked_account_ids": self.linked_account_ids,
            "linked_debt_id": self.linked_debt_id,
            "baseline_amount": self.baseline_amount,
            "months_target": self.months_target
        }


class Transfer:
    def __init__(self, from_account_id, to_account_id, amount, date,
                 description=None, notes=None, _id=None):
        self._id = ObjectId(_id) if _id else ObjectId()
        self.from_account_id = ObjectId(from_account_id) if from_account_id else None
        self.to_account_id = ObjectId(to_account_id) if to_account_id else None
        self.amount = amount
        self.date = date
        self.description = description or 'Account Transfer'
        self.notes = notes

    def to_dict(self):
        return {
            "_id": str(self._id),
            "from_account_id": str(self.from_account_id) if self.from_account_id else None,
            "to_account_id": str(self.to_account_id) if self.to_account_id else None,
            "amount": self.amount,
            "date": self.date.isoformat() if hasattr(self.date, 'isoformat') else self.date,
            "description": self.description,
            "notes": self.notes
        }


class RecurringTransfer:
    def __init__(self, from_account_id, to_account_id, amount, frequency,
                 start_date, day_of_month=None, end_date=None,
                 description=None, notes=None, _id=None):
        self._id = ObjectId(_id) if _id else ObjectId()
        self.from_account_id = ObjectId(from_account_id) if from_account_id else None
        self.to_account_id = ObjectId(to_account_id) if to_account_id else None
        self.amount = amount
        self.frequency = frequency  # 'monthly', 'bi-monthly', 'yearly'
        self.start_date = start_date
        self.day_of_month = day_of_month  # e.g., "6" or "6,21" for multiple days
        self.end_date = end_date
        self.description = description or 'Recurring Transfer'
        self.notes = notes

    def to_dict(self):
        return {
            "_id": str(self._id),
            "from_account_id": str(self.from_account_id) if self.from_account_id else None,
            "to_account_id": str(self.to_account_id) if self.to_account_id else None,
            "amount": self.amount,
            "frequency": self.frequency,
            "start_date": self.start_date.isoformat() if hasattr(self.start_date, 'isoformat') else self.start_date,
            "day_of_month": self.day_of_month,
            "end_date": self.end_date.isoformat() if self.end_date else None,
            "description": self.description,
            "notes": self.notes
        }


class Debt:
    def __init__(self, name, debt_type, balance, interest_rate=0,
                 minimum_payment=0, creditor=None, account_number=None,
                 notes=None, deferment_end_date=None, subsidized=False,
                 payment_match=None, _id=None):
        self._id = ObjectId(_id) if _id else ObjectId()
        self.name = name
        self.debt_type = debt_type
        self.balance = balance
        self.interest_rate = interest_rate  # APR %
        self.minimum_payment = minimum_payment
        self.creditor = creditor
        self.account_number = account_number
        self.notes = notes
        self.deferment_end_date = deferment_end_date  # Student loan deferment end
        self.subsidized = subsidized  # If True, no interest accrues during deferment
        self.payment_match = payment_match or []  # Ledger descriptions that auto-link as payments

    def to_dict(self):
        return {
            "_id": str(self._id),
            "name": self.name,
            "debt_type": self.debt_type,
            "balance": self.balance,
            "interest_rate": self.interest_rate,
            "minimum_payment": self.minimum_payment,
            "creditor": self.creditor,
            "account_number": self.account_number,
            "notes": self.notes,
            "deferment_end_date": self.deferment_end_date.isoformat() if self.deferment_end_date else None,
            "subsidized": self.subsidized,
            "payment_match": self.payment_match
        }


class PaymentPlan:
    """No-interest installment plan (e.g., medical bill) charged to a credit card."""
    def __init__(self, vendor, original_amount, monthly_payment,
                 start_date=None, card_name=None, category=None,
                 notes=None, status='active', _id=None):
        self._id = ObjectId(_id) if _id else ObjectId()
        self.vendor = vendor
        self.original_amount = original_amount
        self.monthly_payment = monthly_payment
        self.start_date = start_date
        self.card_name = card_name  # Optional CC the plan bills to
        self.category = category    # Optional category to apply when linking transactions
        self.notes = notes
        self.status = status        # 'active' or 'closed'

    def to_dict(self):
        return {
            "_id": str(self._id),
            "vendor": self.vendor,
            "original_amount": self.original_amount,
            "monthly_payment": self.monthly_payment,
            "start_date": self.start_date.isoformat() if self.start_date and hasattr(self.start_date, 'isoformat') else self.start_date,
            "card_name": self.card_name,
            "category": self.category,
            "notes": self.notes,
            "status": self.status,
        }


class NetWorthSnapshot:
    def __init__(self, date, total_assets, total_liabilities, net_worth,
                 breakdown=None, _id=None):
        self._id = ObjectId(_id) if _id else ObjectId()
        self.date = date
        self.total_assets = total_assets
        self.total_liabilities = total_liabilities
        self.net_worth = net_worth
        self.breakdown = breakdown or {}

    def to_dict(self):
        return {
            "_id": str(self._id),
            "date": self.date.isoformat() if self.date else None,
            "total_assets": self.total_assets,
            "total_liabilities": self.total_liabilities,
            "net_worth": self.net_worth,
            "breakdown": self.breakdown
        }


class Holding:
    def __init__(self, account_id, symbol, quantity, cost_basis=None,
                 date_added=None, notes=None, _id=None):
        self._id = ObjectId(_id) if _id else ObjectId()
        self.account_id = ObjectId(account_id) if account_id else None
        self.symbol = symbol.upper()
        self.quantity = quantity
        self.cost_basis = cost_basis
        self.date_added = date_added or datetime.utcnow()
        self.notes = notes

    def to_dict(self):
        return {
            "_id": str(self._id),
            "account_id": str(self.account_id) if self.account_id else None,
            "symbol": self.symbol,
            "quantity": self.quantity,
            "cost_basis": self.cost_basis,
            "date_added": self.date_added,
            "notes": self.notes,
        }


# ============ PAYROLL (Household Employer) ============

# Default payroll tax constants — stored in db.payroll_settings and editable via UI
PAYROLL_DEFAULTS = {
    'ss_rate': 0.062,              # Social Security rate (each side)
    'medicare_rate': 0.0145,       # Medicare rate (each side)
    'additional_medicare_rate': 0.009,
    'additional_medicare_threshold': 200000,
    'ss_wage_base': 184500,        # 2026 SS wage base (verify with SSA); update yearly
    'fica_annual_threshold': 2900, # 2026 cash wages threshold (verify Pub 926); household employee FICA trigger
    'futa_rate': 0.006,            # Net FUTA after state credit
    'futa_wage_base': 7000,
    'futa_quarterly_threshold': 1000,
    # Two-letter state code and per-state unemployment (SUTA) settings. Left
    # blank on purpose: set your state, SUTA rate and wage base in
    # Payroll Settings (your state agency assigns the actual rate).
    'state': '',
    'state_configs': {},
}

# Pay frequencies (for payroll)
PAY_FREQUENCIES = [
    ('weekly', 'Weekly'),
    ('biweekly', 'Bi-weekly'),
    ('semimonthly', 'Semi-monthly'),
    ('monthly', 'Monthly'),
]


class HouseholdEmployee:
    """A household employee (caregiver, housekeeper, etc.) paid by the user as household employer."""
    def __init__(self, name, ssn=None, address=None, hire_date=None,
                 termination_date=None, pay_type='salary', pay_rate=0,
                 pay_frequency='monthly', funding_account_id=None,
                 fit_per_period=0, sit_per_period=0,
                 state=None, relationship=None,
                 lives_with_employer=False, under_18=False,
                 notes=None, active=True, _id=None):
        self._id = ObjectId(_id) if _id else ObjectId()
        self.name = name
        self.ssn = ssn
        self.address = address
        self.hire_date = hire_date
        self.termination_date = termination_date
        self.pay_type = pay_type  # 'salary' (fixed per period) or 'hourly'
        self.pay_rate = pay_rate  # gross per period if salary; hourly rate if hourly
        self.pay_frequency = pay_frequency
        self.funding_account_id = ObjectId(funding_account_id) if funding_account_id else None
        self.fit_per_period = fit_per_period  # Flat federal income tax withholding per period (optional)
        self.sit_per_period = sit_per_period  # Flat state income tax withholding per period (optional)
        self.state = state  # Overrides global state setting if set
        self.relationship = relationship  # e.g., 'parent', 'in-law', 'unrelated' — affects FICA exemptions
        self.lives_with_employer = lives_with_employer
        self.under_18 = under_18
        self.notes = notes
        self.active = active

    def to_dict(self):
        return {
            "_id": str(self._id),
            "name": self.name,
            "ssn": self.ssn,
            "address": self.address,
            "hire_date": self.hire_date.isoformat() if self.hire_date else None,
            "termination_date": self.termination_date.isoformat() if self.termination_date else None,
            "pay_type": self.pay_type,
            "pay_rate": self.pay_rate,
            "pay_frequency": self.pay_frequency,
            "funding_account_id": str(self.funding_account_id) if self.funding_account_id else None,
            "fit_per_period": self.fit_per_period,
            "sit_per_period": self.sit_per_period,
            "state": self.state,
            "relationship": self.relationship,
            "lives_with_employer": self.lives_with_employer,
            "under_18": self.under_18,
            "notes": self.notes,
            "active": self.active,
        }


class PayrollEntry:
    """A single payroll run for a household employee."""
    def __init__(self, employee_id, pay_date, period_start, period_end,
                 gross_wages=0, hours=None, rate=None,
                 ee_ss=0, ee_medicare=0, ee_addl_medicare=0,
                 ee_fit=0, ee_sit=0,
                 er_ss=0, er_medicare=0, er_futa=0, er_suta=0,
                 net_pay=0, funding_account_id=None, transaction_id=None,
                 state=None, notes=None, _id=None):
        self._id = ObjectId(_id) if _id else ObjectId()
        self.employee_id = ObjectId(employee_id) if employee_id else None
        self.pay_date = pay_date
        self.period_start = period_start
        self.period_end = period_end
        self.gross_wages = gross_wages
        self.hours = hours
        self.rate = rate
        self.ee_ss = ee_ss
        self.ee_medicare = ee_medicare
        self.ee_addl_medicare = ee_addl_medicare
        self.ee_fit = ee_fit
        self.ee_sit = ee_sit
        self.er_ss = er_ss
        self.er_medicare = er_medicare
        self.er_futa = er_futa
        self.er_suta = er_suta
        self.net_pay = net_pay
        self.funding_account_id = ObjectId(funding_account_id) if funding_account_id else None
        self.transaction_id = ObjectId(transaction_id) if transaction_id else None
        self.state = state
        self.notes = notes

    def to_dict(self):
        return {
            "_id": str(self._id),
            "employee_id": str(self.employee_id) if self.employee_id else None,
            "pay_date": self.pay_date.isoformat() if hasattr(self.pay_date, 'isoformat') else self.pay_date,
            "period_start": self.period_start.isoformat() if hasattr(self.period_start, 'isoformat') else self.period_start,
            "period_end": self.period_end.isoformat() if hasattr(self.period_end, 'isoformat') else self.period_end,
            "gross_wages": self.gross_wages,
            "hours": self.hours,
            "rate": self.rate,
            "ee_ss": self.ee_ss,
            "ee_medicare": self.ee_medicare,
            "ee_addl_medicare": self.ee_addl_medicare,
            "ee_fit": self.ee_fit,
            "ee_sit": self.ee_sit,
            "er_ss": self.er_ss,
            "er_medicare": self.er_medicare,
            "er_futa": self.er_futa,
            "er_suta": self.er_suta,
            "net_pay": self.net_pay,
            "funding_account_id": str(self.funding_account_id) if self.funding_account_id else None,
            "transaction_id": str(self.transaction_id) if self.transaction_id else None,
            "state": self.state,
            "notes": self.notes,
        }


class Dividend:
    """Dividend payment record for a holding."""
    def __init__(self, symbol, amount, date, holding_id=None, source='api', _id=None):
        self._id = ObjectId(_id) if _id else ObjectId()
        self.holding_id = ObjectId(holding_id) if holding_id else None
        self.symbol = symbol.upper()
        self.amount = amount  # total dividend received (per-share * quantity)
        self.date = date
        self.source = source  # 'api' or 'manual'

    def to_dict(self):
        return {
            "_id": str(self._id),
            "holding_id": str(self.holding_id) if self.holding_id else None,
            "symbol": self.symbol,
            "amount": self.amount,
            "date": self.date,
            "source": self.source,
        }
