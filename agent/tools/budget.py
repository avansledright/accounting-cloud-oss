from datetime import datetime

from agent.db import db


def get_budget_status(month: str = "") -> str:
    """Get budget targets vs actual spending for each category.

    Args:
        month: Month in YYYY-MM format (e.g., '2026-02'). Leave empty for current month.
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

    # Get default targets
    targets = {}
    for bt in db.budget_targets.find():
        targets[bt['category']] = bt['target']

    # Apply monthly overrides
    for mbt in db.monthly_budget_targets.find({'month': month}):
        targets[mbt['category']] = mbt['target']

    if not targets:
        return f"No budget targets configured for {month}."

    # Get actual spending from cc_transactions
    pipeline = [
        {'$match': {'date': {'$gte': start, '$lt': end}}},
        {'$group': {'_id': '$category', 'total': {'$sum': '$amount'}}},
    ]
    actuals = {}
    for r in db.cc_transactions.aggregate(pipeline):
        cat = r['_id'] or 'Uncategorized'
        actuals[cat] = r['total']

    lines = [f"Budget status for {month}:"]
    total_budget = 0.0
    total_spent = 0.0
    for cat in sorted(targets.keys()):
        target = targets[cat]
        spent = actuals.get(cat, 0)
        remaining = target - spent
        pct = (spent / target * 100) if target > 0 else 0
        status = "OVER" if remaining < 0 else "OK"
        total_budget += target
        total_spent += spent
        lines.append(
            f"  {cat}: ${spent:,.2f} / ${target:,.2f} ({pct:.0f}%) [{status}]"
        )

    lines.append(f"\nTotal: ${total_spent:,.2f} / ${total_budget:,.2f}")
    return "\n".join(lines)


def get_recurring_items(item_type: str = "") -> str:
    """Get all recurring transactions (bills/income) and recurring transfers.

    Args:
        item_type: Filter by 'expense', 'income', or 'transfer'. Leave empty for all.
    """
    lines = []

    # Recurring transactions
    if item_type != 'transfer':
        query = {'archived': {'$ne': True}}  # paid-off loan lines are archived
        if item_type in ('expense', 'income'):
            query['transaction_type'] = item_type
        items = list(db.recurring_transactions.find(query).sort('description', 1))
        if items:
            lines.append("Recurring transactions:")
            for rt in items:
                amount = rt.get('amount', 0)
                freq = rt.get('frequency', 'monthly')
                # Calculate monthly equivalent
                if freq == 'weekly':
                    monthly = amount * 52 / 12
                elif freq == 'bi-monthly':
                    monthly = amount * 2
                elif freq == 'yearly':
                    monthly = amount / 12
                elif freq == 'monthly':
                    # day_of_month may list several days, e.g. "1,15" for a semi-monthly payday
                    monthly = amount * (len([d for d in str(rt.get('day_of_month') or '').split(',') if d.strip()]) or 1)
                else:
                    monthly = amount
                ttype = rt.get('transaction_type', 'expense')
                cat = rt.get('category', '')
                cat_str = f" [{cat}]" if cat else ""
                lines.append(
                    f"  {rt['description']}: ${amount:,.2f}/{freq} "
                    f"(~${monthly:,.2f}/mo) — {ttype}{cat_str}"
                )

    # Recurring transfers
    if item_type in ('', 'transfer'):
        transfers = list(db.recurring_transfers.find().sort('description', 1))
        if transfers:
            # Build account name lookup
            accts = {str(a['_id']): a['name'] for a in db.accounts.find({}, {'name': 1})}
            lines.append("\nRecurring transfers:")
            for rt in transfers:
                amount = rt.get('amount', 0)
                freq = rt.get('frequency', 'monthly')
                from_name = accts.get(str(rt.get('from_account_id', '')), 'Unknown')
                to_name = accts.get(str(rt.get('to_account_id', '')), 'Unknown')
                if freq == 'weekly':
                    monthly = amount * 52 / 12
                elif freq == 'bi-monthly':
                    monthly = amount * 2
                elif freq == 'yearly':
                    monthly = amount / 12
                else:
                    monthly = amount
                lines.append(
                    f"  {rt.get('description', 'Transfer')}: ${amount:,.2f}/{freq} "
                    f"(~${monthly:,.2f}/mo) — {from_name} → {to_name}"
                )

    if not lines:
        return "No recurring items found."
    return "\n".join(lines)
