from agent.db import db
from models import INVESTMENT_ACCOUNT_TYPES


def get_account_balances() -> str:
    """Get all account balances including name, type, institution, and current balance."""
    accounts = list(db.accounts.find({}, {
        'name': 1, 'account_type': 1, 'institution': 1, 'balance': 1, 'is_primary': 1,
    }))
    if not accounts:
        return "No accounts found."
    lines = []
    for a in accounts:
        primary = " [PRIMARY]" if a.get('is_primary') else ""
        lines.append(
            f"{a['name']} ({a.get('account_type', 'N/A')}): "
            f"${a.get('balance', 0):,.2f} — {a.get('institution', 'N/A')}{primary}"
        )
    return "\n".join(lines)


def get_account_summary() -> str:
    """Get a summary of total balances by account type: cash (checking/savings) vs investment accounts."""
    accounts = list(db.accounts.find({}, {'account_type': 1, 'balance': 1, 'name': 1}))
    if not accounts:
        return "No accounts found."

    cash_types = {'checking', 'savings'}
    investment_types = set(INVESTMENT_ACCOUNT_TYPES)

    cash_total = 0.0
    investment_total = 0.0
    other_total = 0.0

    for a in accounts:
        bal = a.get('balance', 0) or 0
        atype = a.get('account_type', 'other')
        if atype in cash_types:
            cash_total += bal
        elif atype in investment_types:
            investment_total += bal
        else:
            other_total += bal

    lines = [
        f"Cash accounts (checking + savings): ${cash_total:,.2f}",
        f"Investment accounts: ${investment_total:,.2f}",
    ]
    if other_total:
        lines.append(f"Other accounts: ${other_total:,.2f}")
    lines.append(f"Total across all accounts: ${cash_total + investment_total + other_total:,.2f}")
    return "\n".join(lines)
