import re
from datetime import datetime

from bson import ObjectId

from agent.db import db


def search_transactions(
    description: str = "",
    date_from: str = "",
    date_to: str = "",
    transaction_type: str = "",
    limit: int = 20,
    account_id: str = "",
) -> str:
    """Search ledger transactions by description, date range, type and/or account.

    Args:
        description: Partial match on transaction description (case-insensitive). Leave empty to skip.
        date_from: Start date in YYYY-MM-DD format. Leave empty to skip.
        date_to: End date in YYYY-MM-DD format. Leave empty to skip.
        transaction_type: Filter by 'expense' or 'income'. Leave empty for both.
        limit: Maximum number of results to return (default 20).
        account_id: Only this account's transactions. Leave empty for every account.
    """
    query = {}
    if account_id:
        try:
            acct_oid = ObjectId(account_id)
        except Exception:
            return f"Invalid account_id: {account_id}"
        acct = db.accounts.find_one({'_id': acct_oid}, {'is_primary': 1})
        if not acct:
            return f"No account with id {account_id}."
        # Transactions with no account_id belong to the primary account.
        match = [acct_oid, account_id]
        if acct.get('is_primary'):
            match.append(None)
        query['account_id'] = {'$in': match}
    if description:
        query['description'] = {'$regex': re.escape(description), '$options': 'i'}
    if transaction_type:
        query['transaction_type'] = transaction_type

    date_filter = {}
    if date_from:
        date_filter['$gte'] = datetime.fromisoformat(date_from)
    if date_to:
        date_filter['$lte'] = datetime.fromisoformat(date_to + "T23:59:59")
    if date_filter:
        query['date'] = date_filter

    transactions = list(
        db.transactions.find(query).sort('date', -1).limit(limit)
    )
    if not transactions:
        return "No transactions found matching the criteria."

    # A transaction with no account_id belongs to the primary account.
    accts = {str(a['_id']): a['name'] for a in db.accounts.find({}, {'name': 1})}
    primary = db.accounts.find_one({'is_primary': True}, {'name': 1})
    primary_name = primary['name'] if primary else 'Primary'

    lines = []
    total = 0.0
    for t in transactions:
        d = t['date']
        if isinstance(d, datetime):
            d = d.strftime('%Y-%m-%d')
        amount = t.get('amount', 0)
        ttype = t.get('transaction_type', 'expense')
        sign = '+' if ttype == 'income' else '-'
        total += amount if ttype == 'income' else -amount
        acct = accts.get(str(t['account_id']), 'Unknown') if t.get('account_id') else primary_name
        lines.append(f"{t['_id']} | {d}  {sign}${amount:,.2f}  {t.get('description', 'N/A')} [{acct}]")

    lines.append(f"\n{len(transactions)} transactions shown. Net: ${total:,.2f}")
    return "\n".join(lines)


def get_spending_by_category(month: str = "") -> str:
    """Get expense breakdown by category for a given month from credit card transactions.

    Args:
        month: Month in YYYY-MM format (e.g., '2026-01'). Leave empty for current month.
    """
    if not month:
        now = datetime.now()
        month = now.strftime('%Y-%m')

    year, mo = int(month.split('-')[0]), int(month.split('-')[1])
    start = datetime(year, mo, 1)
    if mo == 12:
        end = datetime(year + 1, 1, 1)
    else:
        end = datetime(year, mo + 1, 1)

    pipeline = [
        {'$match': {'date': {'$gte': start, '$lt': end}}},
        {'$group': {'_id': '$category', 'total': {'$sum': '$amount'}}},
        {'$sort': {'total': -1}},
    ]

    results = list(db.cc_transactions.aggregate(pipeline))
    if not results:
        return f"No credit card spending found for {month}."

    lines = [f"Spending by category for {month}:"]
    grand_total = 0.0
    for r in results:
        cat = r['_id'] or 'Uncategorized'
        total = r['total']
        grand_total += total
        lines.append(f"  {cat}: ${total:,.2f}")
    lines.append(f"\nTotal spending: ${grand_total:,.2f}")
    return "\n".join(lines)


def search_cc_transactions(
    description: str = "",
    card: str = "",
    date_from: str = "",
    date_to: str = "",
    category: str = "",
    limit: int = 20,
) -> str:
    """Search credit card transactions (imported statements), newest first.

    Args:
        description: Partial match on merchant description (case-insensitive). Leave empty to skip.
        card: Partial match on card name or issuer, e.g. 'amex' or 'sapphire'. Leave empty for all cards.
        date_from: Start date in YYYY-MM-DD format. Leave empty to skip.
        date_to: End date in YYYY-MM-DD format. Leave empty to skip.
        category: Exact category name (case-insensitive). Leave empty to skip.
        limit: Maximum number of results to return (default 20).
    """
    query = {}
    if description:
        query['description'] = {'$regex': re.escape(description), '$options': 'i'}
    if card:
        pattern = {'$regex': re.escape(card), '$options': 'i'}
        query['$or'] = [{'card_name': pattern}, {'card_issuer': pattern}]
    if category:
        query['category'] = {'$regex': f'^{re.escape(category)}$', '$options': 'i'}

    date_filter = {}
    if date_from:
        date_filter['$gte'] = datetime.fromisoformat(date_from)
    if date_to:
        date_filter['$lte'] = datetime.fromisoformat(date_to + "T23:59:59")
    if date_filter:
        query['date'] = date_filter

    transactions = list(
        db.cc_transactions.find(query).sort('date', -1).limit(limit)
    )
    if not transactions:
        return "No credit card transactions found matching the criteria."

    lines = []
    total = 0.0
    for t in transactions:
        d = t['date']
        if isinstance(d, datetime):
            d = d.strftime('%Y-%m-%d')
        # Charges are stored positive; refunds/credits are stored negative.
        amount = t.get('amount', 0)
        total += amount
        lines.append(
            f"{d}  ${amount:,.2f}  {t.get('description', 'N/A')}  "
            f"[{t.get('card_name', '?')}, {t.get('category', 'Other')}]"
        )

    lines.append(f"\n{len(transactions)} transactions shown. Net (credits negative): ${total:,.2f}")
    return "\n".join(lines)
