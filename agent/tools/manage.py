"""The rest of what the web app's forms can do, for the MCP server.

`writes.py` covers adding things and the common edits. This module closes the
gap to the web app: full edits, deletes, transfers, loan links, payment plans,
skips, snapshots, and a few read tools that need IDs or loan math.

Conventions for "leave unchanged", shared by every update_* here:
- text: "" leaves it, "none" clears it (where the field may be empty)
- numbers: -1 leaves it (0 is a real value for a rate or a payment)
- yes/no: "" leaves it, "true" / "false" sets it

Every tool returns a human-readable status string, like writes.py.
"""

from datetime import datetime

import loans
from agent.db import db
from agent.tools.writes import (
    RECURRING_FREQUENCIES, _account_exists, _oid, _parse_date,
)
from models import (
    ACCOUNT_TYPES, ASSET_TYPES, CATEGORIES, DEBT_TYPES, GOAL_CATEGORIES,
    NetWorthSnapshot, PaymentPlan,
)

UNCHANGED = -1
_CLEAR = ('none', 'clear', 'remove')


# ---------- helpers ----------

def _text(update, key, value, nullable=True):
    """Apply a text argument: '' leaves it, 'none' clears it."""
    value = (value or '').strip()
    if not value:
        return
    if value.lower() in _CLEAR:
        if nullable:
            update[key] = None
        return
    update[key] = value


def _number(update, key, value, minimum=0.0):
    """Apply a numeric argument: -1 leaves it. Returns an error string or None."""
    if value is None or value == UNCHANGED:
        return None
    if value < minimum:
        return f"Error: {key} can't be below {minimum:g}."
    update[key] = float(value)
    return None


def _flag(update, key, value):
    """Apply a yes/no argument given as text. Returns an error string or None."""
    value = (value or '').strip().lower()
    if not value:
        return None
    if value not in ('true', 'false', 'yes', 'no'):
        return f"Error: {key} must be \"true\" or \"false\"."
    update[key] = value in ('true', 'yes')
    return None


def _date_arg(update, key, value):
    """Apply a YYYY-MM-DD argument: '' leaves it, 'none' clears it."""
    value = (value or '').strip()
    if not value:
        return None
    if value.lower() in _CLEAR:
        update[key] = None
        return None
    dt = _parse_date(value)
    if not dt:
        return f"Error: {key} must be YYYY-MM-DD."
    update[key] = dt
    return None


def _match_list(raw):
    """The payment-match field: comma or newline separated ledger descriptions."""
    return [p.strip() for p in (raw or '').replace('\n', ',').split(',') if p.strip()]


def _day(value):
    return value.strftime('%Y-%m-%d') if isinstance(value, datetime) else str(value or '')


def _keys(values):
    return {key for key, _label in values}


# ---------- debts ----------

def update_debt(debt_id: str, name: str = "", debt_type: str = "",
                interest_rate: float = UNCHANGED, minimum_payment: float = UNCHANGED,
                creditor: str = "", account_number: str = "", notes: str = "",
                deferment_end_date: str = "", subsidized: str = "",
                payment_match: str = "") -> str:
    """Edit a debt: everything the web app's edit-debt form has except the
    starting balance (update_debt_balance)."""
    oid = _oid(debt_id)
    if not oid:
        return "Error: invalid debt_id."
    update = {}
    _text(update, 'name', name, nullable=False)
    if debt_type:
        if debt_type not in _keys(DEBT_TYPES):
            return f"Error: debt_type must be one of {sorted(_keys(DEBT_TYPES))}."
        update['debt_type'] = debt_type
    for key, value in (('interest_rate', interest_rate), ('minimum_payment', minimum_payment)):
        err = _number(update, key, value)
        if err:
            return err
    if update.get('interest_rate', 0) >= 100:
        return "Error: interest_rate is a percent, e.g. 4.5."
    _text(update, 'creditor', creditor)
    _text(update, 'account_number', account_number)
    _text(update, 'notes', notes)
    err = _date_arg(update, 'deferment_end_date', deferment_end_date) or _flag(
        update, 'subsidized', subsidized)
    if err:
        return err
    if payment_match.strip():
        update['payment_match'] = ([] if payment_match.strip().lower() in _CLEAR
                                   else _match_list(payment_match))
    if not update:
        return "Nothing to update."
    res = db.debts.update_one({'_id': oid}, {'$set': update})
    if not res.matched_count:
        return "Debt not found."
    return "Updated " + ", ".join(sorted(update)) + "."


def delete_debt(debt_id: str) -> str:
    """Delete a debt. Its linked ledger payments stay on the ledger, unlinked."""
    oid = _oid(debt_id)
    if not oid:
        return "Error: invalid debt_id."
    debt = db.debts.find_one({'_id': oid}, {'name': 1})
    if not debt:
        return "Debt not found."
    db.debts.delete_one({'_id': oid})
    res = db.transactions.update_many({'debt_id': oid}, {'$unset': {'debt_id': ''}})
    return f"Deleted debt {debt.get('name')}; {res.modified_count} ledger payment(s) unlinked."


def loan_payments(loan_ref: str, limit: int = 12) -> str:
    """The payments linked to a loan, newest first, each split into interest,
    escrow and principal, with the balance after it. loan_ref is 'debt:<id>'
    or 'asset:<id>'."""
    kind, _, raw = (loan_ref or '').partition(':')
    oid = _oid(raw)
    if kind not in ('debt', 'asset') or not oid:
        return "Error: loan_ref must be 'debt:<id>' or 'asset:<id>'."
    if kind == 'debt':
        loan = db.debts.find_one({'_id': oid})
        pay = loans.payments_by_link(db, 'debt_id')
        result = loans.amortize_debt(loan, pay.get(str(oid), [])) if loan else None
        start = (loan or {}).get('balance') or 0
    else:
        loan = db.assets.find_one({'_id': oid})
        pay = loans.payments_by_link(db, 'asset_liability_id')
        result = loans.amortize_asset_liability(loan, pay.get(str(oid), [])) if loan else None
        start = (loan or {}).get('liability_balance') or 0
    if not loan:
        return "Loan not found."
    lines = [f"{loan.get('name')}: owed now ${result['current_balance']:,.2f} "
             f"(starting ${start:,.2f}); paid so far: principal "
             f"${result['principal_paid']:,.2f}, interest ${result['interest_paid']:,.2f}"
             + (f", escrow ${result['escrow_paid']:,.2f}" if result['escrow_paid'] else "")]
    rows = list(reversed(result['rows']))[:max(1, limit)]
    if not rows:
        lines.append("No ledger payments are linked to it yet.")
    for r in rows:
        lines.append(
            f"{r['_id']} | {_day(r['date'])}  ${r['amount']:,.2f}  {r.get('description') or ''}: "
            f"principal ${r['principal']:,.2f}, interest ${r['interest']:,.2f}"
            + (f", escrow ${r['escrow']:,.2f}" if r['escrow'] else "")
            + (f", reserve credit ${r['credit']:,.2f}" if r['credit'] else "")
            + f" -> ${r['balance_after']:,.2f}")
    return "\n".join(lines)


def link_transaction_to_loan(transaction_id: str, loan_ref: str = "") -> str:
    """Count a ledger transaction as a payment on a loan, or stop counting it.

    loan_ref is 'debt:<id>' or 'asset:<id>'; empty unlinks. A linked payment
    lowers what the loan shows as owed. The web app archives or restores a
    paid-off loan's budget line the next time its Debts page loads.
    """
    tid = _oid(transaction_id)
    if not tid:
        return "Error: invalid transaction_id."
    txn = db.transactions.find_one({'_id': tid}, {'description': 1})
    if not txn:
        return "Transaction not found."
    ref = (loan_ref or '').strip()
    if not ref:
        db.transactions.update_one({'_id': tid},
                                   {'$unset': {'debt_id': '', 'asset_liability_id': ''}})
        return f"Unlinked '{txn.get('description')}' from any loan."
    kind, _, raw = ref.partition(':')
    oid = _oid(raw)
    if kind not in ('debt', 'asset') or not oid:
        return "Error: loan_ref must be 'debt:<id>' or 'asset:<id>'."
    loan = (db.debts if kind == 'debt' else db.assets).find_one({'_id': oid}, {'name': 1})
    if not loan:
        return "Loan not found."
    field, other = (('debt_id', 'asset_liability_id') if kind == 'debt'
                    else ('asset_liability_id', 'debt_id'))
    db.transactions.update_one({'_id': tid}, {'$set': {field: oid}, '$unset': {other: ''}})
    return f"Linked '{txn.get('description')}' as a payment on {loan.get('name')}."


# ---------- accounts ----------

def update_account_details(account_id: str, account_type: str = "", notes: str = "",
                           account_number: str = "", management_type: str = "",
                           balance_as_of_date: str = "") -> str:
    """The account fields update_account does not cover."""
    oid = _oid(account_id)
    if not oid:
        return "Error: invalid account_id."
    update = {}
    if account_type:
        if account_type not in _keys(ACCOUNT_TYPES):
            return f"Error: account_type must be one of {sorted(_keys(ACCOUNT_TYPES))}."
        update['account_type'] = account_type
    _text(update, 'notes', notes)
    _text(update, 'account_number', account_number)
    _text(update, 'management_type', management_type)
    err = _date_arg(update, 'balance_as_of_date', balance_as_of_date)
    if err:
        return err
    if not update:
        return "Nothing to update."
    res = db.accounts.update_one({'_id': oid}, {'$set': update})
    return ("Updated " + ", ".join(sorted(update)) + ".") if res.matched_count else "Account not found."


def delete_account(account_id: str, force: bool = False) -> str:
    """Delete an account. Refuses while anything still points at it (ledger
    transactions, recurring items, transfers, holdings) unless force is true;
    the web app deletes without that check and leaves those rows orphaned."""
    oid = _oid(account_id)
    if not oid:
        return "Error: invalid account_id."
    acct = db.accounts.find_one({'_id': oid})
    if not acct:
        return "Account not found."
    either = {'$in': [oid, str(oid)]}
    ends = {'$or': [{'from_account_id': either}, {'to_account_id': either}]}
    used = {
        'ledger transactions': db.transactions.count_documents({'account_id': either}),
        'recurring items': db.recurring_transactions.count_documents({'account_id': either}),
        'transfers': db.transfers.count_documents(ends),
        'recurring transfers': db.recurring_transfers.count_documents(ends),
        'holdings': db.holdings.count_documents({'account_id': either}),
    }
    used = {k: v for k, v in used.items() if v}
    if used and not force:
        return (f"Not deleted: {acct.get('name')} is still used by "
                + ", ".join(f"{v} {k}" for k, v in used.items())
                + ". Move or delete those first, or pass force=true to delete it anyway.")
    if acct.get('is_primary') and not force:
        return "Not deleted: this is the primary account. Make another account primary first."
    db.accounts.delete_one({'_id': oid})
    return f"Deleted account {acct.get('name')}."


# ---------- assets ----------

def update_asset_details(asset_id: str, name: str = "", asset_type: str = "",
                         address: str = "", notes: str = "", payment_match: str = "",
                         loan_term_months: int = UNCHANGED,
                         payment_start_date: str = "", payment_frequency: str = "",
                         purchase_price: float = UNCHANGED, purchase_date: str = "",
                         property_tax_annual: float = UNCHANGED,
                         homeowners_insurance_annual: float = UNCHANGED,
                         pmi_monthly: float = UNCHANGED, hoa_monthly: float = UNCHANGED,
                         escrow_balance: float = UNCHANGED) -> str:
    """The asset fields update_asset does not cover: naming, the loan's terms
    and the escrow amounts that are taken out of each payment before principal."""
    oid = _oid(asset_id)
    if not oid:
        return "Error: invalid asset_id."
    update = {}
    _text(update, 'name', name, nullable=False)
    if asset_type:
        if asset_type not in _keys(ASSET_TYPES):
            return f"Error: asset_type must be one of {sorted(_keys(ASSET_TYPES))}."
        update['asset_type'] = asset_type
    _text(update, 'address', address)
    _text(update, 'notes', notes)
    if payment_match.strip():
        update['payment_match'] = ([] if payment_match.strip().lower() in _CLEAR
                                   else _match_list(payment_match))
    if loan_term_months != UNCHANGED:
        if loan_term_months < 0:
            return "Error: loan_term_months can't be negative."
        update['loan_term_months'] = int(loan_term_months) or None
    if payment_frequency:
        if payment_frequency not in ('monthly', 'biweekly'):
            return "Error: payment_frequency must be 'monthly' or 'biweekly'."
        update['payment_frequency'] = payment_frequency
    for key, value in (('purchase_price', purchase_price),
                       ('property_tax_annual', property_tax_annual),
                       ('homeowners_insurance_annual', homeowners_insurance_annual),
                       ('pmi_monthly', pmi_monthly), ('hoa_monthly', hoa_monthly),
                       ('escrow_balance', escrow_balance)):
        err = _number(update, key, value)
        if err:
            return err
        if key in update and not update[key]:
            update[key] = None       # the web form stores an empty amount as null
    err = (_date_arg(update, 'payment_start_date', payment_start_date)
           or _date_arg(update, 'purchase_date', purchase_date))
    if err:
        return err
    if not update:
        return "Nothing to update."
    res = db.assets.update_one({'_id': oid}, {'$set': update})
    return ("Updated " + ", ".join(sorted(update)) + ".") if res.matched_count else "Asset not found."


def delete_asset(asset_id: str) -> str:
    """Delete an asset and its loan. Linked ledger payments stay, unlinked."""
    oid = _oid(asset_id)
    if not oid:
        return "Error: invalid asset_id."
    asset = db.assets.find_one({'_id': oid}, {'name': 1})
    if not asset:
        return "Asset not found."
    db.assets.delete_one({'_id': oid})
    res = db.transactions.update_many({'asset_liability_id': oid},
                                      {'$unset': {'asset_liability_id': ''}})
    return f"Deleted asset {asset.get('name')}; {res.modified_count} ledger payment(s) unlinked."


# ---------- goals ----------

def update_goal(goal_id: str, name: str = "", target_amount: float = UNCHANGED,
                target_date: str = "", category: str = "", notes: str = "") -> str:
    """Edit a savings goal's name, target, date, category or notes."""
    oid = _oid(goal_id)
    if not oid:
        return "Error: invalid goal_id."
    update = {}
    _text(update, 'name', name, nullable=False)
    err = _number(update, 'target_amount', target_amount) or _date_arg(
        update, 'target_date', target_date)
    if err:
        return err
    if category:
        if category.lower() in _CLEAR:
            update['category'] = None
        elif category not in _keys(GOAL_CATEGORIES):
            return f"Error: category must be one of {sorted(_keys(GOAL_CATEGORIES))}."
        else:
            update['category'] = category
    _text(update, 'notes', notes)
    if not update:
        return "Nothing to update."
    res = db.savings_goals.update_one({'_id': oid}, {'$set': update})
    return ("Updated " + ", ".join(sorted(update)) + ".") if res.matched_count else "Goal not found."


def contribute_to_goal(goal_id: str, amount: float) -> str:
    """Add to (or, with a negative amount, take from) a goal's saved amount."""
    oid = _oid(goal_id)
    if not oid:
        return "Error: invalid goal_id."
    if not amount:
        return "Error: amount is required."
    res = db.savings_goals.update_one({'_id': oid}, {'$inc': {'current_amount': float(amount)}})
    if not res.matched_count:
        return "Goal not found."
    goal = db.savings_goals.find_one({'_id': oid})
    return (f"{goal.get('name')}: ${goal.get('current_amount', 0):,.2f} of "
            f"${goal.get('target_amount', 0):,.2f}.")


# ---------- recurring items ----------

def update_recurring_details(recurring_id: str, start_date: str = "", end_date: str = "",
                             transaction_type: str = "", auto_pay: str = "",
                             payment_method: str = "", notes: str = "",
                             account_number: str = "") -> str:
    """The recurring-item fields update_recurring does not cover. end_date
    "none" makes the item open-ended again."""
    oid = _oid(recurring_id)
    if not oid:
        return "Error: invalid recurring_id."
    update = {}
    err = _date_arg(update, 'start_date', start_date) or _date_arg(update, 'end_date', end_date)
    if err:
        return err
    if 'start_date' in update and update['start_date'] is None:
        return "Error: start_date can't be cleared."
    if transaction_type:
        if transaction_type not in ('income', 'expense'):
            return "Error: transaction_type must be 'income' or 'expense'."
        update['transaction_type'] = transaction_type
    err = _flag(update, 'auto_pay', auto_pay)
    if err:
        return err
    _text(update, 'payment_method', payment_method)
    _text(update, 'notes', notes)
    _text(update, 'account_number', account_number)
    if not update:
        return "Nothing to update."
    res = db.recurring_transactions.update_one({'_id': oid}, {'$set': update})
    return ("Updated " + ", ".join(sorted(update)) + ".") if res.matched_count else "Recurring item not found."


# ---------- ledger: skipped occurrences ----------

def _skip_pair(description):
    """A transfer shows on both accounts: skipping one side skips the other."""
    for mine, theirs in (('Transfer to: ', 'Transfer from: '), ('Transfer from: ', 'Transfer to: ')):
        if description.startswith(mine):
            return [description, theirs + description[len(mine):]]
    return [description]


def skip_projected(description: str, date: str) -> str:
    """Skip one projected occurrence of a recurring item or transfer, so the
    outlook stops counting it. description is the item's description exactly as
    the ledger shows it; date is that occurrence's date (YYYY-MM-DD)."""
    description = (description or '').strip()
    dt = _parse_date(date)
    if not description or not dt:
        return "Error: description and a YYYY-MM-DD date are required."
    day = dt.strftime('%Y-%m-%d')
    for desc in _skip_pair(description):
        db.skipped_transactions.update_one(
            {'description': desc, 'date': day},
            {'$set': {'description': desc, 'date': day}}, upsert=True)
    return f"Skipped '{description}' on {day}."


def unskip_projected(description: str, date: str) -> str:
    """Undo skip_projected: the occurrence is projected again."""
    description = (description or '').strip()
    dt = _parse_date(date)
    if not description or not dt:
        return "Error: description and a YYYY-MM-DD date are required."
    day = dt.strftime('%Y-%m-%d')
    res = db.skipped_transactions.delete_many(
        {'description': {'$in': _skip_pair(description)}, 'date': day})
    return f"Restored '{description}' on {day}." if res.deleted_count else "No such skip."


def list_skipped(date_from: str = "") -> str:
    """Skipped occurrences on or after date_from (default: today)."""
    start = (_parse_date(date_from) or datetime.now()).strftime('%Y-%m-%d')
    rows = sorted((s for s in db.skipped_transactions.find() if str(s.get('date')) >= start),
                  key=lambda s: (str(s.get('date')), s.get('description') or ''))
    return "\n".join(f"{s.get('date')}  {s.get('description')}" for s in rows) or "Nothing is skipped."


# ---------- transfers ----------

def list_transfers(limit: int = 20) -> str:
    """One-time transfers with their IDs, newest first."""
    names = {a['_id']: a.get('name') for a in db.accounts.find({}, {'name': 1})}
    lines = []
    for t in db.transfers.find().sort('date', -1).limit(max(1, limit)):
        lines.append(
            f"{t['_id']} | {_day(t.get('date'))}  ${t.get('amount', 0):,.2f}  "
            f"{names.get(t.get('from_account_id'), '?')} -> "
            f"{names.get(t.get('to_account_id'), '?')}  {t.get('description') or ''}")
    return "\n".join(lines) or "No transfers."


def delete_transfer(transfer_id: str) -> str:
    """Delete a one-time transfer and the two ledger rows it posted."""
    oid = _oid(transfer_id)
    if not oid:
        return "Error: invalid transfer_id."
    transfer = db.transfers.find_one({'_id': oid})
    if not transfer:
        return "Transfer not found."
    description = transfer.get('description') or 'Account Transfer'
    removed = 0
    for prefix, account in (('Transfer to: ', transfer.get('from_account_id')),
                            ('Transfer from: ', transfer.get('to_account_id'))):
        removed += db.transactions.delete_one({
            'description': f"{prefix}{description}", 'date': transfer.get('date'),
            'account_id': {'$in': [account, str(account)]}}).deleted_count
    db.transfers.delete_one({'_id': oid})
    return f"Deleted the transfer and {removed} ledger row(s)."


def update_recurring_transfer(transfer_id: str, amount: float = UNCHANGED,
                              frequency: str = "", start_date: str = "",
                              end_date: str = "", day_of_month: str = "",
                              from_account_id: str = "", to_account_id: str = "",
                              description: str = "", notes: str = "") -> str:
    """Edit a recurring transfer (IDs from list_recurring with item_type='transfer')."""
    oid = _oid(transfer_id)
    if not oid:
        return "Error: invalid transfer_id."
    current = db.recurring_transfers.find_one({'_id': oid})
    if not current:
        return "Recurring transfer not found."
    update = {}
    if amount != UNCHANGED:
        if amount <= 0:
            return "Error: amount must be positive."
        update['amount'] = float(amount)
    if frequency:
        if frequency not in RECURRING_FREQUENCIES:
            return "Error: bad frequency."
        update['frequency'] = frequency
    err = _date_arg(update, 'start_date', start_date) or _date_arg(update, 'end_date', end_date)
    if err:
        return err
    if 'start_date' in update and update['start_date'] is None:
        return "Error: start_date can't be cleared."
    _text(update, 'day_of_month', day_of_month)
    for key, value in (('from_account_id', from_account_id), ('to_account_id', to_account_id)):
        if value:
            if not _account_exists(value):
                return f"Error: {key} is not an account."
            update[key] = _oid(value)
    if (update.get('from_account_id', current.get('from_account_id'))
            == update.get('to_account_id', current.get('to_account_id'))):
        return "Error: can't transfer to the same account."
    _text(update, 'description', description, nullable=False)
    _text(update, 'notes', notes)
    if not update:
        return "Nothing to update."
    db.recurring_transfers.update_one({'_id': oid}, {'$set': update})
    return "Updated " + ", ".join(sorted(update)) + "."


# ---------- dividends ----------

def list_dividends(symbol: str = "", limit: int = 20) -> str:
    """Recorded dividends with their IDs, newest first."""
    query = {'symbol': symbol.upper()} if symbol else {}
    lines = [f"{d['_id']} | {_day(d.get('date'))}  {d.get('symbol', '?')}: "
             f"${d.get('amount', 0):,.2f} ({d.get('source') or 'manual'})"
             for d in db.dividends.find(query).sort('date', -1).limit(max(1, limit))]
    return "\n".join(lines) or "No dividends."


def delete_dividend(dividend_id: str) -> str:
    oid = _oid(dividend_id)
    if not oid:
        return "Error: invalid dividend_id."
    res = db.dividends.delete_one({'_id': oid})
    return "Deleted." if res.deleted_count else "Not found."


# ---------- budget targets ----------

def reset_month_budget_targets(month: str) -> str:
    """Drop every per-month override for YYYY-MM, so that month uses the default targets."""
    if not _parse_date(f"{month}-01"):
        return "Error: month must be YYYY-MM."
    res = db.monthly_budget_targets.delete_many({'month': month})
    return f"Cleared {res.deleted_count} override(s) for {month}."


# ---------- net worth ----------

def take_net_worth_snapshot() -> str:
    """Record today's net worth in the history, like the Net Worth page's button."""
    accounts = sum(a.get('balance', 0) or 0 for a in db.accounts.find())
    assets = list(db.assets.find())
    asset_value = sum(a.get('estimated_value', 0) or 0 for a in assets)
    apay = loans.payments_by_link(db, 'asset_liability_id')
    asset_loans = sum(loans.current_asset_liability(a, apay) for a in assets)
    dpay = loans.payments_by_link(db, 'debt_id')
    debts = sum(loans.current_debt_balance(d, dpay) for d in db.debts.find())
    total_assets, liabilities = accounts + asset_value, asset_loans + debts
    snapshot = NetWorthSnapshot(
        date=datetime.now(), total_assets=total_assets, total_liabilities=liabilities,
        net_worth=total_assets - liabilities,
        breakdown={'accounts': accounts, 'assets': asset_value,
                   'asset_liabilities': asset_loans, 'standalone_debts': debts,
                   'liabilities': liabilities})
    db.networth_snapshots.insert_one(snapshot.__dict__)
    return (f"Snapshot recorded: net worth ${snapshot.net_worth:,.2f} "
            f"(assets ${total_assets:,.2f}, liabilities ${liabilities:,.2f}).")


# ---------- payment plans (no-interest installments on a card) ----------

def list_payment_plans() -> str:
    """Payment plans with IDs and what is left on each."""
    lines = []
    for p in db.payment_plans.find().sort('vendor', 1):
        paid = sum(t.get('amount') or 0
                   for t in db.cc_transactions.find({'payment_plan_id': p['_id']}))
        left = max(0.0, (p.get('original_amount') or 0) - paid)
        lines.append(
            f"{p['_id']} | {p.get('vendor')}: ${left:,.2f} left of "
            f"${p.get('original_amount', 0):,.2f}, ${p.get('monthly_payment', 0):,.2f}/mo"
            + (f" on {p['card_name']}" if p.get('card_name') else "")
            + f" [{p.get('status') or 'active'}]")
    return "\n".join(lines) or "No payment plans."


def add_payment_plan(vendor: str, original_amount: float, monthly_payment: float,
                     start_date: str = "", card_name: str = "", category: str = "",
                     notes: str = "") -> str:
    """Add a no-interest installment plan (e.g. a medical bill paid monthly on a card)."""
    vendor = (vendor or '').strip()
    if not vendor or original_amount <= 0 or monthly_payment <= 0:
        return "Error: vendor, original_amount and monthly_payment are required."
    start = None
    if start_date:
        start = _parse_date(start_date)
        if not start:
            return "Error: start_date must be YYYY-MM-DD."
    if category and category not in CATEGORIES:
        return f"Error: category must be one of {CATEGORIES}."
    plan = PaymentPlan(vendor=vendor, original_amount=float(original_amount),
                       monthly_payment=float(monthly_payment), start_date=start,
                       card_name=card_name or None, category=category or None,
                       notes=notes or None, status='active')
    db.payment_plans.insert_one(plan.__dict__)
    return f"Added payment plan {plan._id}: {vendor} ${original_amount:,.2f}"


def update_payment_plan(plan_id: str, vendor: str = "", original_amount: float = UNCHANGED,
                        monthly_payment: float = UNCHANGED, start_date: str = "",
                        card_name: str = "", category: str = "", notes: str = "",
                        status: str = "") -> str:
    """Edit a payment plan. status is 'active' or 'closed'."""
    oid = _oid(plan_id)
    if not oid:
        return "Error: invalid plan_id."
    update = {}
    _text(update, 'vendor', vendor, nullable=False)
    for key, value in (('original_amount', original_amount), ('monthly_payment', monthly_payment)):
        err = _number(update, key, value, minimum=0.01)
        if err:
            return err
    err = _date_arg(update, 'start_date', start_date)
    if err:
        return err
    _text(update, 'card_name', card_name)
    _text(update, 'category', category)
    if update.get('category') and update['category'] not in CATEGORIES:
        return f"Error: category must be one of {CATEGORIES}."
    _text(update, 'notes', notes)
    if status:
        if status not in ('active', 'closed'):
            return "Error: status must be 'active' or 'closed'."
        update['status'] = status
    if not update:
        return "Nothing to update."
    res = db.payment_plans.update_one({'_id': oid}, {'$set': update})
    return ("Updated " + ", ".join(sorted(update)) + ".") if res.matched_count else "Plan not found."


def delete_payment_plan(plan_id: str) -> str:
    """Delete a payment plan. Card charges linked to it stay, unlinked."""
    oid = _oid(plan_id)
    if not oid:
        return "Error: invalid plan_id."
    res = db.payment_plans.delete_one({'_id': oid})
    if not res.deleted_count:
        return "Plan not found."
    unlinked = db.cc_transactions.update_many({'payment_plan_id': oid},
                                              {'$unset': {'payment_plan_id': ''}})
    return f"Deleted; {unlinked.modified_count} card charge(s) unlinked."


# ---------- credit card transactions ----------

def _cc_txn(cc_transaction_id):
    oid = _oid(cc_transaction_id)
    return oid, (db.cc_transactions.find_one({'_id': oid}) if oid else None)


def recategorize_cc_transaction(cc_transaction_id: str, category: str) -> str:
    """Set a credit card charge's category (IDs from cc_transactions)."""
    if category not in CATEGORIES:
        return f"Error: category must be one of {CATEGORIES}."
    oid, txn = _cc_txn(cc_transaction_id)
    if not txn:
        return "Credit card transaction not found."
    # Like the web app: a correction by hand settles the row, so the mark that
    # flags an automatic category for review is cleared.
    db.cc_transactions.update_one({'_id': oid}, {'$set': {'category': category},
                                                 '$unset': {'categorized_by': ''}})
    return f"'{txn.get('description')}' is now {category}."


def link_cc_transaction_to_plan(cc_transaction_id: str, plan_id: str = "") -> str:
    """Count a card charge as a payment on a payment plan; empty plan_id unlinks."""
    oid, txn = _cc_txn(cc_transaction_id)
    if not txn:
        return "Credit card transaction not found."
    if not plan_id:
        db.cc_transactions.update_one({'_id': oid}, {'$unset': {'payment_plan_id': ''}})
        return "Unlinked."
    pid = _oid(plan_id)
    plan = db.payment_plans.find_one({'_id': pid}, {'vendor': 1}) if pid else None
    if not plan:
        return "Plan not found."
    db.cc_transactions.update_one({'_id': oid}, {'$set': {'payment_plan_id': pid}})
    return f"Linked '{txn.get('description')}' to the {plan.get('vendor')} plan."


# ---------- advisor reports ----------

def _report_head(r):
    return " ".join(str(x) for x in (r.get('type') or 'report', r.get('task_name') or '',
                                     r.get('period') or '') if x)


def list_reports(limit: int = 10) -> str:
    """Saved advisor and scheduled-task reports with IDs, newest first."""
    lines = [f"{r['_id']} | {_day(r.get('date'))}  {_report_head(r)}"
             for r in db.reports.find().sort('date', -1).limit(max(1, limit))]
    return "\n".join(lines) or "No reports."


def get_report(report_id: str) -> str:
    """The text of one saved report."""
    oid = _oid(report_id)
    report = db.reports.find_one({'_id': oid}) if oid else None
    if not report:
        return "Report not found."
    body = report.get('insights') or report.get('summary_md') or ''
    return f"{_report_head(report)} ({_day(report.get('date'))})\n\n{body or '(no text stored)'}"
