"""Write operations for the MCP server.

All write tools return a human-readable status string.
Dates are stored as datetime.datetime (BSON requirement) — never use
datetime.date for storage.
"""

from datetime import datetime
from bson import ObjectId
from bson.errors import InvalidId

from agent.db import db
from models import ACCOUNT_TYPES, EXTRA_DEBT_PAYMENT_DESC, Transaction, Transfer

# Same values the web app's budget/transfer forms offer.
RECURRING_FREQUENCIES = ('weekly', 'biweekly', 'monthly', 'bi-monthly', 'yearly')


# ---------- helpers ----------

def _oid(value):
    """Return ObjectId or None if invalid/empty."""
    if not value:
        return None
    try:
        return ObjectId(value)
    except (InvalidId, TypeError):
        return None


def _parse_date(value):
    """Parse YYYY-MM-DD or full ISO into datetime. Empty/None returns None."""
    if not value:
        return None
    try:
        if 'T' in value:
            return datetime.fromisoformat(value)
        return datetime.fromisoformat(value)  # YYYY-MM-DD parses fine
    except ValueError:
        return None


def _account_exists(account_id):
    oid = _oid(account_id)
    return bool(oid and db.accounts.find_one({'_id': oid}, {'_id': 1}))


def _primary_account_id():
    """The primary account's id as a string, or None — the web app's default."""
    primary = db.accounts.find_one({'is_primary': True}, {'_id': 1})
    return str(primary['_id']) if primary else None


def _account_name(account_id):
    oid = _oid(account_id)
    if not oid:
        return None
    a = db.accounts.find_one({'_id': oid}, {'name': 1})
    return a['name'] if a else None


# ---------- listing helpers (with IDs) ----------

def list_accounts() -> str:
    """List every account with its ID — needed before calling tools that take account_id."""
    accounts = list(db.accounts.find({}))
    if not accounts:
        return "No accounts."
    lines = []
    for a in accounts:
        primary = ' [PRIMARY]' if a.get('is_primary') else ''
        lines.append(
            f"{a['_id']} | {a['name']} ({a.get('account_type','?')}, "
            f"{a.get('institution','?')}) — ${a.get('balance',0):,.2f}{primary}"
        )
    return "\n".join(lines)


def list_recurring(item_type: str = "") -> str:
    """List recurring transactions with their IDs. Filter by 'expense', 'income', or 'transfer'."""
    lines = []
    if item_type != 'transfer':
        q = {}
        if item_type in ('expense', 'income'):
            q['transaction_type'] = item_type
        for r in db.recurring_transactions.find(q).sort('description', 1):
            lines.append(
                f"{r['_id']} | [{r.get('transaction_type','?')}] {r.get('description','?')}: "
                f"${r.get('amount',0):,.2f} {r.get('frequency','?')} ({r.get('category','-')})"
            )
    if item_type in ('', 'transfer'):
        for r in db.recurring_transfers.find().sort('description', 1):
            lines.append(
                f"{r['_id']} | [transfer] {r.get('description','?')}: "
                f"${r.get('amount',0):,.2f} {r.get('frequency','?')}"
            )
    return "\n".join(lines) if lines else "No recurring items."


def list_assets() -> str:
    out = []
    for a in db.assets.find():
        out.append(
            f"{a['_id']} | {a['name']} ({a.get('asset_type','?')}): "
            f"value=${a.get('estimated_value',0):,.2f}, "
            f"liability=${a.get('liability_balance') or 0:,.2f}"
        )
    return "\n".join(out) if out else "No assets."


def list_debts() -> str:
    out = []
    for d in db.debts.find():
        out.append(
            f"{d['_id']} | {d['name']} ({d.get('debt_type','?')}): "
            f"${d.get('balance',0):,.2f} @ {d.get('interest_rate',0)}% "
            f"min ${d.get('minimum_payment',0)}"
        )
    return "\n".join(out) if out else "No debts."


def list_goals() -> str:
    out = []
    for g in db.savings_goals.find():
        out.append(
            f"{g['_id']} | {g['name']}: ${g.get('current_amount',0):,.2f}/"
            f"${g.get('target_amount',0):,.2f}"
        )
    return "\n".join(out) if out else "No goals."


def list_holdings_with_ids(account_id: str = "") -> str:
    q = {}
    if account_id:
        oid = _oid(account_id)
        if not oid:
            return "Invalid account_id."
        q['account_id'] = oid
    out = []
    for h in db.holdings.find(q):
        out.append(
            f"{h['_id']} | {h.get('symbol','?')} qty={h.get('quantity',0)} "
            f"acct={h.get('account_id')}"
        )
    return "\n".join(out) if out else "No holdings."


# ---------- transactions ----------

def add_transaction(description: str, amount: float, date: str,
                    transaction_type: str, account_id: str = "") -> str:
    """Add a single ledger transaction (income or expense).

    transaction_type must be 'income' or 'expense'. date is YYYY-MM-DD.
    account_id is optional — if omitted, posts to the primary account.
    """
    if transaction_type not in ('income', 'expense'):
        return "Error: transaction_type must be 'income' or 'expense'."
    dt = _parse_date(date)
    if not dt:
        return "Error: date must be YYYY-MM-DD."
    if amount <= 0:
        return "Error: amount must be positive."

    if account_id:
        if not _oid(account_id):
            return "Error: invalid account_id."
        if not _account_exists(account_id):
            return "Error: account not found."
    else:
        # Same default as the web app's POST /transactions
        account_id = _primary_account_id()

    txn = Transaction(
        description=description,
        amount=float(amount),
        date=dt,
        transaction_type=transaction_type,
        account_id=account_id,
    )
    db.transactions.insert_one(txn.__dict__)
    return f"Added transaction {txn._id}: {transaction_type} ${amount:,.2f} — {description}"


def update_transaction(transaction_id: str, description: str = "",
                       amount: float = 0, date: str = "",
                       transaction_type: str = "",
                       account_id: str = "") -> str:
    """Update fields on a transaction. Pass only the fields you want to change."""
    oid = _oid(transaction_id)
    if not oid:
        return "Error: invalid transaction_id."
    update = {}
    if description: update['description'] = description
    if amount: update['amount'] = float(amount)
    if date:
        dt = _parse_date(date)
        if not dt:
            return "Error: invalid date."
        update['date'] = dt
    if transaction_type:
        if transaction_type not in ('income', 'expense'):
            return "Error: transaction_type must be 'income' or 'expense'."
        update['transaction_type'] = transaction_type
    if account_id:
        acct_oid = _oid(account_id)
        if not acct_oid or not _account_exists(account_id):
            return "Error: invalid account_id."
        update['account_id'] = acct_oid
    if not update:
        return "Nothing to update."
    res = db.transactions.update_one({'_id': oid}, {'$set': update})
    return f"Updated {res.modified_count} transaction." if res.matched_count else "Transaction not found."


def delete_transaction(transaction_id: str) -> str:
    oid = _oid(transaction_id)
    if not oid:
        return "Error: invalid transaction_id."
    transaction = db.transactions.find_one({'_id': oid})
    if not transaction:
        return "Transaction not found."
    # Like the web app's delete: record a skip so the projected occurrence
    # this transaction stood in for doesn't reappear in the outlook.
    transaction_date = transaction['date'].strftime('%Y-%m-%d') if isinstance(transaction['date'], datetime) else transaction['date']
    db.skipped_transactions.update_one(
        {'description': transaction['description'], 'date': transaction_date},
        {'$set': {'description': transaction['description'], 'date': transaction_date}},
        upsert=True
    )
    db.transactions.delete_one({'_id': oid})
    return "Deleted."


# ---------- recurring transactions ----------

def add_recurring(description: str, amount: float, frequency: str,
                  start_date: str, transaction_type: str,
                  category: str = "", day_of_month: str = "",
                  account_id: str = "", auto_pay: bool = False,
                  payment_method: str = "", notes: str = "") -> str:
    """Add a recurring transaction (a bill or a paycheck).

    frequency: 'weekly' | 'biweekly' (every 14 days) | 'monthly' |
               'bi-monthly' (every other month) | 'yearly'
    day_of_month: '1' or '1,15' for split months.
    """
    if transaction_type not in ('income', 'expense'):
        return "Error: transaction_type must be 'income' or 'expense'."
    if frequency not in RECURRING_FREQUENCIES:
        return "Error: bad frequency."
    sd = _parse_date(start_date)
    if not sd:
        return "Error: start_date must be YYYY-MM-DD."
    acct_oid = _oid(account_id) if account_id else None
    if account_id and not acct_oid:
        return "Error: invalid account_id."

    doc = {
        'description': description,
        'amount': float(amount),
        'frequency': frequency,
        'start_date': sd,
        'transaction_type': transaction_type,
        'category': category or None,
        'day_of_month': day_of_month or None,
        'account_id': acct_oid,
        'auto_pay': bool(auto_pay),
        'payment_method': payment_method or None,
        'notes': notes or None,
        'end_date': None,
    }
    res = db.recurring_transactions.insert_one(doc)
    return f"Added recurring {res.inserted_id}: {description} ${amount:,.2f}/{frequency}"


def update_recurring(recurring_id: str, description: str = "",
                     amount: float = 0, frequency: str = "",
                     category: str = "", day_of_month: str = "",
                     account_id: str = "") -> str:
    """Update a recurring transaction. Pass only the fields you want to change."""
    oid = _oid(recurring_id)
    if not oid:
        return "Error: invalid recurring_id."
    update = {}
    if description: update['description'] = description
    if amount: update['amount'] = float(amount)
    if frequency:
        if frequency not in RECURRING_FREQUENCIES:
            return "Error: bad frequency."
        update['frequency'] = frequency
    if category: update['category'] = category
    if day_of_month: update['day_of_month'] = day_of_month
    if account_id:
        acct_oid = _oid(account_id)
        if not acct_oid:
            return "Error: invalid account_id."
        update['account_id'] = acct_oid
    if not update:
        return "Nothing to update."
    res = db.recurring_transactions.update_one({'_id': oid}, {'$set': update})
    return "Updated." if res.matched_count else "Recurring item not found."


def delete_recurring(recurring_id: str) -> str:
    oid = _oid(recurring_id)
    if not oid:
        return "Error: invalid recurring_id."
    res = db.recurring_transactions.delete_one({'_id': oid})
    return "Deleted." if res.deleted_count else "Not found."


def override_recurring_amount(recurring_id: str, date: str, amount: float) -> str:
    """Override the projected amount for a single occurrence of a recurring item."""
    oid = _oid(recurring_id)
    if not oid:
        return "Error: invalid recurring_id."
    dt = _parse_date(date)
    if not dt:
        return "Error: bad date."
    # The outlook keys overrides on the 'YYYY-MM-DD' string, as the web app stores it.
    date_str = dt.strftime('%Y-%m-%d')
    db.projected_overrides.update_one(
        {'recurring_id': oid, 'date': date_str},
        {'$set': {'recurring_id': oid, 'date': date_str, 'amount': float(amount)}},
        upsert=True,
    )
    return f"Override saved: {date} → ${amount:,.2f}"


# ---------- transfers ----------

def add_transfer(from_account_id: str, to_account_id: str,
                 amount: float, date: str,
                 description: str = "", notes: str = "") -> str:
    """Record a one-time account-to-account transfer."""
    fr = _oid(from_account_id); to = _oid(to_account_id)
    if not fr or not to:
        return "Error: invalid account ids."
    if not _account_exists(from_account_id) or not _account_exists(to_account_id):
        return "Error: account not found."
    if fr == to:
        return "Error: can't transfer to the same account."
    if amount <= 0:
        return "Error: amount must be positive."
    dt = _parse_date(date)
    if not dt:
        return "Error: bad date."
    # Mirrors the web app's POST /transfers/add: the transfer record plus a
    # paired expense/income on the ledger, which is what moves the balances.
    description = description or 'Account Transfer'
    transfer = Transfer(
        from_account_id=from_account_id, to_account_id=to_account_id,
        amount=float(amount), date=dt,
        description=description, notes=notes or None,
    )
    db.transfers.insert_one(transfer.__dict__)
    db.transactions.insert_one(Transaction(
        description=f"Transfer to: {description}", amount=float(amount), date=dt,
        transaction_type='expense', account_id=from_account_id,
    ).__dict__)
    db.transactions.insert_one(Transaction(
        description=f"Transfer from: {description}", amount=float(amount), date=dt,
        transaction_type='income', account_id=to_account_id,
    ).__dict__)
    return f"Transfer {transfer._id}: ${amount:,.2f} {_account_name(from_account_id)} → {_account_name(to_account_id)}"


def add_recurring_transfer(from_account_id: str, to_account_id: str,
                           amount: float, frequency: str, start_date: str,
                           day_of_month: str = "", description: str = "") -> str:
    """Set up a recurring transfer between accounts."""
    fr = _oid(from_account_id); to = _oid(to_account_id)
    if not fr or not to:
        return "Error: invalid account ids."
    sd = _parse_date(start_date)
    if not sd:
        return "Error: bad start_date."
    if frequency not in RECURRING_FREQUENCIES:
        return "Error: bad frequency."
    doc = {
        'from_account_id': fr, 'to_account_id': to,
        'amount': float(amount), 'frequency': frequency,
        'start_date': sd, 'day_of_month': day_of_month or None,
        'end_date': None, 'description': description or 'Recurring Transfer',
        'notes': None,
    }
    res = db.recurring_transfers.insert_one(doc)
    return f"Recurring transfer {res.inserted_id}: ${amount:,.2f}/{frequency}"


def delete_recurring_transfer(transfer_id: str) -> str:
    oid = _oid(transfer_id)
    if not oid:
        return "Error: invalid id."
    res = db.recurring_transfers.delete_one({'_id': oid})
    return "Deleted." if res.deleted_count else "Not found."


# ---------- accounts ----------

def add_account(name: str, account_type: str, institution: str,
                balance: float = 0, is_primary: bool = False,
                estimated_return_rate: float = 0,
                management_type: str = "") -> str:
    """Create a new account. account_type: checking | savings | ira | roth_ira | brokerage | 401k | other."""
    valid = {t for t, _label in ACCOUNT_TYPES}
    if account_type not in valid:
        return f"Error: account_type must be one of {sorted(valid)}."
    if is_primary:
        db.accounts.update_many({}, {'$set': {'is_primary': False}})
    doc = {
        'name': name, 'account_type': account_type,
        'institution': institution, 'balance': float(balance),
        'is_primary': bool(is_primary),
        'estimated_return_rate': float(estimated_return_rate) if estimated_return_rate else None,
        'balance_as_of_date': datetime.now(),
        'management_type': management_type or None,
    }
    res = db.accounts.insert_one(doc)
    return f"Added account {res.inserted_id}: {name}"


def update_account_balance(account_id: str, balance: float) -> str:
    """Set an account's current balance and stamp the as-of date to now."""
    oid = _oid(account_id)
    if not oid:
        return "Error: invalid account_id."
    res = db.accounts.update_one(
        {'_id': oid},
        {'$set': {'balance': float(balance), 'balance_as_of_date': datetime.now()}},
    )
    return f"Balance updated to ${balance:,.2f}." if res.matched_count else "Account not found."


def update_account(account_id: str, name: str = "", institution: str = "",
                   estimated_return_rate: float = 0,
                   is_primary: bool = False) -> str:
    """Update account metadata. Setting is_primary=true demotes any other primary."""
    oid = _oid(account_id)
    if not oid:
        return "Error: invalid account_id."
    update = {}
    if name: update['name'] = name
    if institution: update['institution'] = institution
    if estimated_return_rate:
        update['estimated_return_rate'] = float(estimated_return_rate)
    if is_primary:
        db.accounts.update_many({}, {'$set': {'is_primary': False}})
        update['is_primary'] = True
    if not update:
        return "Nothing to update."
    res = db.accounts.update_one({'_id': oid}, {'$set': update})
    return "Updated." if res.matched_count else "Account not found."


# ---------- assets ----------

def add_asset(name: str, asset_type: str, estimated_value: float,
              liability_balance: float = 0, interest_rate: float = 0,
              monthly_payment: float = 0, address: str = "",
              notes: str = "") -> str:
    """Add an asset (real_estate | vehicle | other) with optional liability."""
    if asset_type not in ('real_estate', 'vehicle', 'other'):
        return "Error: bad asset_type."
    doc = {
        'name': name, 'asset_type': asset_type,
        'estimated_value': float(estimated_value),
        'liability_balance': float(liability_balance) if liability_balance else None,
        'interest_rate': float(interest_rate) if interest_rate else None,
        'monthly_payment': float(monthly_payment) if monthly_payment else None,
        'address': address or None, 'notes': notes or None,
    }
    res = db.assets.insert_one(doc)
    return f"Added asset {res.inserted_id}: {name}"


def update_asset(asset_id: str, estimated_value: float = 0,
                 liability_balance: float = 0,
                 monthly_payment: float = 0) -> str:
    """Update common asset fields. 0 means leave unchanged."""
    oid = _oid(asset_id)
    if not oid:
        return "Error: invalid asset_id."
    update = {}
    if estimated_value: update['estimated_value'] = float(estimated_value)
    if liability_balance: update['liability_balance'] = float(liability_balance)
    if monthly_payment: update['monthly_payment'] = float(monthly_payment)
    if not update:
        return "Nothing to update."
    res = db.assets.update_one({'_id': oid}, {'$set': update})
    return "Updated." if res.matched_count else "Asset not found."


# ---------- debts ----------

def add_debt(name: str, debt_type: str, balance: float,
             interest_rate: float = 0, minimum_payment: float = 0,
             creditor: str = "", notes: str = "") -> str:
    """Add a debt. debt_type: credit_card | personal_loan | student_loan | medical | other."""
    if debt_type not in ('credit_card', 'personal_loan', 'student_loan', 'medical', 'other'):
        return "Error: bad debt_type."
    doc = {
        'name': name, 'debt_type': debt_type,
        'balance': float(balance),
        'interest_rate': float(interest_rate),
        'minimum_payment': float(minimum_payment),
        'creditor': creditor or None, 'notes': notes or None,
    }
    res = db.debts.insert_one(doc)
    return f"Added debt {res.inserted_id}: {name} ${balance:,.2f}"


def update_debt_balance(debt_id: str, balance: float) -> str:
    """Set a debt's STARTING balance — the web app's edit-debt "Starting Balance" field.

    The current balance is derived: linked ledger payments are amortized
    against this figure. To record a payment, use make_debt_payment instead.
    """
    oid = _oid(debt_id)
    if not oid:
        return "Error: invalid debt_id."
    res = db.debts.update_one({'_id': oid}, {'$set': {'balance': float(balance)}})
    if not res.matched_count:
        return "Debt not found."
    return (f"Starting balance set to ${balance:,.2f}. Linked payments are "
            f"subtracted from this to give the current balance.")


def make_debt_payment(debt_id: str, amount: float, date: str = "",
                      account_id: str = "", description: str = "") -> str:
    """Log a payment toward a debt, the way the web app's "log payment" does.

    Posts an expense on the ledger (default: primary account, today) linked
    to the debt via debt_id. The debt's starting balance is left alone; its
    current balance drops because linked payments are amortized against it.
    description defaults to the payoff plan's 'Extra Debt Payment' line (so
    the outlook dedups that month's projected extra); pass the loan's regular
    budget-line description for a scheduled payment instead.
    """
    oid = _oid(debt_id)
    if not oid:
        return "Error: invalid debt_id."
    if amount <= 0:
        return "Error: amount must be positive."
    d = db.debts.find_one({'_id': oid})
    if not d:
        return "Debt not found."
    if date:
        dt = _parse_date(date)
        if not dt:
            return "Error: date must be YYYY-MM-DD."
    else:
        dt = datetime.combine(datetime.now().date(), datetime.min.time())
    if account_id:
        if not _account_exists(account_id):
            return "Error: invalid account_id."
    else:
        account_id = _primary_account_id()

    txn = Transaction(
        description=description or EXTRA_DEBT_PAYMENT_DESC,
        amount=round(float(amount), 2),
        date=dt,
        transaction_type='expense',
        account_id=account_id,
    )
    doc = txn.__dict__
    doc['debt_id'] = oid
    db.transactions.insert_one(doc)
    return (f"Logged ${amount:,.2f} payment on {d['name']} as ledger transaction "
            f"{txn._id} ({dt:%Y-%m-%d}, {_account_name(account_id) or 'no account'}).")


# ---------- budget targets ----------

def set_budget_target(category: str, amount: float, month: str = "") -> str:
    """Set the budget target for a category.

    If month is YYYY-MM, sets a per-month override. Empty month sets the
    default template that applies whenever no override exists.
    """
    amount = float(amount)
    if month:
        if amount == 0:
            db.monthly_budget_targets.delete_one({'month': month, 'category': category})
            return f"Cleared override for {category} in {month}."
        db.monthly_budget_targets.update_one(
            {'month': month, 'category': category},
            {'$set': {'target': amount, 'category': category, 'month': month}},
            upsert=True,
        )
        return f"Set {month} override: {category} = ${amount:,.2f}"
    if amount == 0:
        db.budget_targets.delete_one({'category': category})
        return f"Cleared default target for {category}."
    db.budget_targets.update_one(
        {'category': category},
        {'$set': {'target': amount, 'category': category}},
        upsert=True,
    )
    return f"Set default target: {category} = ${amount:,.2f}"


# ---------- savings goals ----------

def add_goal(name: str, target_amount: float,
             current_amount: float = 0, target_date: str = "",
             category: str = "", notes: str = "") -> str:
    """Create a savings goal. target_date is YYYY-MM-DD."""
    td = _parse_date(target_date) if target_date else None
    doc = {
        'name': name, 'target_amount': float(target_amount),
        'current_amount': float(current_amount), 'target_date': td,
        'category': category or None, 'notes': notes or None,
        'created_date': datetime.now(),
    }
    res = db.savings_goals.insert_one(doc)
    return f"Added goal {res.inserted_id}: {name}"


def update_goal_amount(goal_id: str, current_amount: float) -> str:
    oid = _oid(goal_id)
    if not oid:
        return "Error: invalid goal_id."
    res = db.savings_goals.update_one(
        {'_id': oid}, {'$set': {'current_amount': float(current_amount)}}
    )
    return f"Goal updated to ${current_amount:,.2f}." if res.matched_count else "Goal not found."


def delete_goal(goal_id: str) -> str:
    oid = _oid(goal_id)
    if not oid:
        return "Error: invalid goal_id."
    res = db.savings_goals.delete_one({'_id': oid})
    return "Deleted." if res.deleted_count else "Not found."


# ---------- holdings ----------

def add_holding(account_id: str, symbol: str, quantity: float,
                cost_basis: float = 0, notes: str = "") -> str:
    """Add a stock/ETF holding to an investment account."""
    oid = _oid(account_id)
    if not oid or not _account_exists(account_id):
        return "Error: invalid or unknown account_id."
    doc = {
        'account_id': oid, 'symbol': symbol.upper(),
        'quantity': float(quantity),
        'cost_basis': float(cost_basis) if cost_basis else None,
        'date_added': datetime.utcnow(),
        'notes': notes or None,
    }
    res = db.holdings.insert_one(doc)
    return f"Added holding {res.inserted_id}: {quantity} {symbol.upper()}"


def update_holding(holding_id: str, quantity: float = 0,
                   cost_basis: float = 0) -> str:
    oid = _oid(holding_id)
    if not oid:
        return "Error: invalid holding_id."
    update = {}
    if quantity: update['quantity'] = float(quantity)
    if cost_basis: update['cost_basis'] = float(cost_basis)
    if not update:
        return "Nothing to update."
    res = db.holdings.update_one({'_id': oid}, {'$set': update})
    return "Updated." if res.matched_count else "Holding not found."


def delete_holding(holding_id: str) -> str:
    oid = _oid(holding_id)
    if not oid:
        return "Error: invalid holding_id."
    res = db.holdings.delete_one({'_id': oid})
    return "Deleted." if res.deleted_count else "Not found."


# ---------- dividends ----------

def add_dividend(symbol: str, amount: float, date: str,
                 holding_id: str = "") -> str:
    """Manually record a dividend payment."""
    dt = _parse_date(date)
    if not dt:
        return "Error: bad date."
    doc = {
        'symbol': symbol.upper(), 'amount': float(amount),
        'date': dt, 'source': 'manual',
        'holding_id': _oid(holding_id) if holding_id else None,
    }
    res = db.dividends.insert_one(doc)
    return f"Added dividend {res.inserted_id}: {symbol.upper()} ${amount:,.2f} on {date}"
