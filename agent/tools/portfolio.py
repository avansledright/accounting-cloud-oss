import logging
from datetime import datetime

import requests
from bson import ObjectId

from agent.db import db

logger = logging.getLogger(__name__)


def fetch_live_price(symbol: str) -> float | None:
    """Lightweight live-price fetch (Yahoo, the web app's primary source).

    Mirrors the main app's Yahoo call but kept here to avoid importing
    app.py into the MCP server. Only the current price is needed — the
    app-side fetch_yahoo also pulls dividend history.
    """
    try:
        resp = requests.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
            "?range=1d&interval=1d",
            headers={'User-Agent': 'Mozilla/5.0'},
            timeout=8,
        )
        resp.raise_for_status()
        result = resp.json().get('chart', {}).get('result') or []
        if not result:
            return None
        return result[0].get('meta', {}).get('regularMarketPrice')
    except Exception:
        logger.warning("Live price fetch failed for %s", symbol, exc_info=True)
        return None


def get_portfolio_summary(account_id: str = "") -> str:
    """Get portfolio summary: live market value, cost basis, gain/loss, YTD dividends, holding count.

    Args:
        account_id: Filter to a specific investment account. Leave empty for all.
    """
    query = {}
    if account_id:
        query['account_id'] = ObjectId(account_id)

    holdings = list(db.holdings.find(query))
    if not holdings:
        return "No holdings found."

    # Build account lookup
    accts = {str(a['_id']): a['name'] for a in db.accounts.find({}, {'name': 1})}

    count = len(holdings)
    symbols = sorted({(h.get('symbol') or '').upper() for h in holdings if h.get('symbol')})
    prices = {sym: fetch_live_price(sym) for sym in symbols}

    def _value(h):
        price = prices.get((h.get('symbol') or '').upper())
        return price * (h.get('quantity', 0) or 0) if price is not None else None

    total_cost = sum(h.get('cost_basis', 0) or 0 for h in holdings)
    priced = [h for h in holdings if _value(h) is not None]
    unpriced = count - len(priced)
    total_value = sum(_value(h) for h in priced)
    # Gain only over holdings we could price, so a missing quote isn't a "loss"
    priced_cost = sum(h.get('cost_basis', 0) or 0 for h in priced)

    # Get YTD dividends
    year_start = datetime(datetime.now().year, 1, 1)
    div_query = {'date': {'$gte': year_start}}
    if account_id:
        holding_ids = [h['_id'] for h in holdings]
        div_query['holding_id'] = {'$in': holding_ids}
    ytd_divs = sum(d.get('amount', 0) for d in db.dividends.find(div_query))

    lines = [f"Portfolio summary ({count} holdings):"]
    if priced:
        note = f" ({unpriced} holding(s) without a live price excluded)" if unpriced else ""
        lines.append(f"  Market value (live): ${total_value:,.2f}{note}")
    else:
        lines.append("  Market value: unavailable (no live prices)")
    lines.append(f"  Total cost basis: ${total_cost:,.2f}")
    if priced and priced_cost > 0:
        gain = total_value - priced_cost
        lines.append(f"  Unrealized gain/loss: ${gain:+,.2f} ({gain / priced_cost * 100:+.1f}%)")
    lines.append(f"  YTD dividends: ${ytd_divs:,.2f}")

    # Group by account
    by_account = {}
    for h in holdings:
        aid = str(h.get('account_id', ''))
        if aid not in by_account:
            by_account[aid] = []
        by_account[aid].append(h)

    if len(by_account) > 1:
        lines.append("\nBy account:")
        for aid, acct_holdings in by_account.items():
            name = accts.get(aid, 'Unknown')
            acct_cost = sum(h.get('cost_basis', 0) or 0 for h in acct_holdings)
            acct_values = [_value(h) for h in acct_holdings if _value(h) is not None]
            value_str = f"${sum(acct_values):,.2f} market value, " if acct_values else ""
            lines.append(f"  {name}: {len(acct_holdings)} holdings, {value_str}${acct_cost:,.2f} cost basis")

    return "\n".join(lines)


def get_holdings(account_id: str = "") -> str:
    """Get all stock/ETF holdings with symbol, quantity, and cost basis.

    Args:
        account_id: Filter to a specific investment account. Leave empty for all.
    """
    query = {}
    if account_id:
        query['account_id'] = ObjectId(account_id)

    holdings = list(db.holdings.find(query).sort('symbol', 1))
    if not holdings:
        return "No holdings found."

    accts = {str(a['_id']): a['name'] for a in db.accounts.find({}, {'name': 1})}

    lines = []
    for h in holdings:
        symbol = h.get('symbol', '?')
        qty = h.get('quantity', 0)
        cost = h.get('cost_basis', 0) or 0
        acct_name = accts.get(str(h.get('account_id', '')), '')
        acct_str = f" ({acct_name})" if acct_name else ""
        lines.append(f"  {symbol}: {qty:,.4f} shares, cost basis ${cost:,.2f}{acct_str}")

    return f"{len(holdings)} holdings:\n" + "\n".join(lines)


def get_dividend_history(symbol: str = "", limit: int = 20) -> str:
    """Get recent dividend payments.

    Args:
        symbol: Filter by stock symbol (e.g., 'AAPL'). Leave empty for all.
        limit: Maximum number of results (default 20).
    """
    query = {}
    if symbol:
        query['symbol'] = symbol.upper()

    dividends = list(db.dividends.find(query).sort('date', -1).limit(limit))
    if not dividends:
        return "No dividends found."

    lines = []
    total = 0.0
    for d in dividends:
        dt = d['date']
        if isinstance(dt, datetime):
            dt = dt.strftime('%Y-%m-%d')
        amount = d.get('amount', 0)
        total += amount
        lines.append(f"  {dt}  {d.get('symbol', '?')}: ${amount:,.2f}")

    return f"Recent dividends ({len(dividends)} shown, total ${total:,.2f}):\n" + "\n".join(lines)


def get_ytd_dividends() -> str:
    """Get year-to-date total dividend income, broken down by symbol."""
    year_start = datetime(datetime.now().year, 1, 1)

    pipeline = [
        {'$match': {'date': {'$gte': year_start}}},
        {'$group': {'_id': '$symbol', 'total': {'$sum': '$amount'}}},
        {'$sort': {'total': -1}},
    ]

    results = list(db.dividends.aggregate(pipeline))
    if not results:
        return f"No dividends received in {datetime.now().year} yet."

    lines = [f"YTD dividends for {datetime.now().year}:"]
    grand_total = 0.0
    for r in results:
        lines.append(f"  {r['_id']}: ${r['total']:,.2f}")
        grand_total += r['total']
    lines.append(f"\nTotal YTD: ${grand_total:,.2f}")
    return "\n".join(lines)
