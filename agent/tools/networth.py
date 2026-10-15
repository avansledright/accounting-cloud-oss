from agent.db import db


def get_current_net_worth() -> str:
    """Get current net worth breakdown: accounts, assets, liabilities, debts."""
    # Account balances
    accounts = list(db.accounts.find({}, {'name': 1, 'account_type': 1, 'balance': 1}))
    account_total = sum(a.get('balance', 0) or 0 for a in accounts)

    # Assets
    assets = list(db.assets.find({}, {'name': 1, 'asset_type': 1, 'estimated_value': 1, 'liability_balance': 1}))
    asset_value = sum(a.get('estimated_value', 0) or 0 for a in assets)
    asset_liabilities = sum(a.get('liability_balance', 0) or 0 for a in assets)

    # Debts
    debts = list(db.debts.find({}, {'name': 1, 'debt_type': 1, 'balance': 1}))
    debt_total = sum(d.get('balance', 0) or 0 for d in debts)

    total_assets = account_total + asset_value
    total_liabilities = asset_liabilities + debt_total
    net_worth = total_assets - total_liabilities

    lines = [
        "Current net worth breakdown:",
        f"\nAssets:",
        f"  Account balances: ${account_total:,.2f}",
    ]

    if assets:
        for a in assets:
            lines.append(f"  {a['name']} ({a.get('asset_type', '')}): ${a.get('estimated_value', 0):,.2f}")
    lines.append(f"  Total assets: ${total_assets:,.2f}")

    lines.append(f"\nLiabilities:")
    if assets:
        for a in assets:
            lb = a.get('liability_balance', 0) or 0
            if lb > 0:
                lines.append(f"  {a['name']} loan: ${lb:,.2f}")
    if debts:
        for d in debts:
            lines.append(f"  {d['name']} ({d.get('debt_type', '')}): ${d.get('balance', 0):,.2f}")
    lines.append(f"  Total liabilities: ${total_liabilities:,.2f}")

    lines.append(f"\nNet worth: ${net_worth:,.2f}")
    return "\n".join(lines)


def get_net_worth_history(limit: int = 12) -> str:
    """Get historical net worth snapshots.

    Args:
        limit: Number of recent snapshots to return (default 12).
    """
    snapshots = list(
        db.networth_snapshots.find().sort('date', -1).limit(limit)
    )
    if not snapshots:
        return "No net worth history found."

    snapshots.reverse()  # Chronological order

    lines = ["Net worth history:"]
    for s in snapshots:
        d = s['date']
        if hasattr(d, 'strftime'):
            d = d.strftime('%Y-%m-%d')
        lines.append(
            f"  {d}: ${s.get('net_worth', 0):,.2f} "
            f"(assets ${s.get('total_assets', 0):,.2f}, "
            f"liabilities ${s.get('total_liabilities', 0):,.2f})"
        )

    if len(snapshots) >= 2:
        first = snapshots[0].get('net_worth', 0)
        last = snapshots[-1].get('net_worth', 0)
        change = last - first
        lines.append(f"\nChange over period: ${change:+,.2f}")

    return "\n".join(lines)
