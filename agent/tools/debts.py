from datetime import datetime

from agent.db import db


def get_debts() -> str:
    """Get all debts with name, type, balance, interest rate, and minimum payment."""
    debts = list(db.debts.find().sort('balance', -1))
    if not debts:
        return "No debts found."

    lines = ["Current debts:"]
    for d in debts:
        rate = d.get('interest_rate', 0) or 0
        min_pay = d.get('minimum_payment', 0) or 0
        deferment = ""
        if d.get('deferment_end_date'):
            end = d['deferment_end_date']
            if isinstance(end, datetime):
                end = end.strftime('%Y-%m-%d')
            sub = " (subsidized)" if d.get('subsidized') else ""
            deferment = f" — deferred until {end}{sub}"

        lines.append(
            f"  {d['name']} ({d.get('debt_type', 'N/A')}): "
            f"${d.get('balance', 0):,.2f} at {rate:.1f}% APR, "
            f"min payment ${min_pay:,.2f}{deferment}"
        )

    return "\n".join(lines)


def get_total_debt() -> str:
    """Get the total sum of all debt balances."""
    debts = list(db.debts.find({}, {'balance': 1, 'name': 1}))
    if not debts:
        return "No debts found. You're debt-free!"

    total = sum(d.get('balance', 0) or 0 for d in debts)
    lines = [f"Total debt: ${total:,.2f}"]
    lines.append(f"Across {len(debts)} debt(s):")
    for d in debts:
        lines.append(f"  {d['name']}: ${d.get('balance', 0):,.2f}")
    return "\n".join(lines)
