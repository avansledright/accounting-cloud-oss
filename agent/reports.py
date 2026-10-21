"""Generate weekly and monthly financial reports."""

import logging
import os
from datetime import datetime, timedelta

import ai
from agent.db import db
from models import INVESTMENT_ACCOUNT_TYPES
from agent.tools.accounts import get_account_balances, get_account_summary
from agent.tools.transactions import get_spending_by_category
from agent.tools.budget import get_budget_status, get_recurring_items
from agent.tools.cashflow import get_cash_flow_outlook, get_negative_balance_months
from agent.tools.portfolio import (
    get_portfolio_summary, get_holdings, get_ytd_dividends,
    get_dividend_history, fetch_live_price,
)
from agent.tools.networth import get_current_net_worth, get_net_worth_history
from agent.tools.debts import get_debts
from agent.prompts import (
    WEEKLY_INSIGHT_PROMPT, MONTHLY_INSIGHT_PROMPT, PORTFOLIO_INSIGHT_PROMPT,
)

logger = logging.getLogger(__name__)

# Reports go through ai.py, which picks the provider (Claude, ChatGPT, Gemini or
# Ollama) from the AI settings page. Optional: with AI off, reports are off.

def llm_configured() -> bool:
    """True when advisor reports have an AI provider to run on."""
    return ai.enabled('report')


def _call_ollama(prompt: str) -> str | None:
    """Run a report prompt on the configured AI provider; None on failure.

    (Name kept from when reports only ran on Ollama.) Generation runs in a
    daemon thread (see app._run_report_async), so wall time doesn't matter.
    """
    try:
        return ai.generate('report', prompt)
    except ai.AIError as e:
        logger.warning("Advisor report request failed: %s", e)
        return None
    except Exception:
        logger.exception("Advisor report request failed")
        return None


def _week_key(dt: datetime) -> str:
    """Return ISO week key like '2026-W08'."""
    return f"{dt.isocalendar()[0]}-W{dt.isocalendar()[1]:02d}"


def _month_key(dt: datetime) -> str:
    """Return month key like '2026-02'."""
    return dt.strftime('%Y-%m')


def generate_weekly_report() -> dict:
    """Generate a weekly spending/budget report and store it in the DB."""
    now = datetime.now()
    period = _week_key(now)

    # Spending by category for current month
    spending = get_spending_by_category()

    # Budget status for current month
    budget = get_budget_status()

    # Recurring items summary
    recurring = get_recurring_items()

    data = {
        'spending_by_category': spending,
        'budget_status': budget,
        'recurring_items': recurring,
    }

    # Build prompt and call Ollama for insights
    prompt = WEEKLY_INSIGHT_PROMPT.format(
        period=period,
        spending=spending,
        budget=budget,
        recurring=recurring,
    )
    insights = _call_ollama(prompt)

    report = {
        'type': 'weekly',
        'date': now,
        'period': period,
        'data': data,
        'insights': insights,
    }

    # Upsert by period to avoid duplicates
    db.reports.replace_one(
        {'type': 'weekly', 'period': period},
        report,
        upsert=True,
    )

    logger.info("Weekly report generated for %s", period)
    return report


# Live price for the portfolio report; shared with the portfolio_summary tool.
_fetch_yahoo_price = fetch_live_price


def _build_portfolio_data() -> dict:
    """Build cost/value/gain totals + per-holding lines using live prices.

    Returned strings replace the agent-tool versions in the prompt so
    the model can actually reason about gain/loss instead of guessing
    from cost basis alone.
    """
    from bson import ObjectId  # local import to avoid agent-tool churn
    holdings = list(db.holdings.find().sort('symbol', 1))
    accts = {str(a['_id']): a['name']
             for a in db.accounts.find({}, {'name': 1})}

    if not holdings:
        return {
            'summary': "No holdings found.",
            'holdings_text': "No holdings found.",
            'total_value': 0.0,
            'total_cost': 0.0,
        }

    # Fetch current prices once per unique symbol
    symbols = sorted({h.get('symbol', '').upper() for h in holdings if h.get('symbol')})
    prices = {sym: _fetch_yahoo_price(sym) for sym in symbols}

    total_value = 0.0
    total_cost = 0.0
    lines = []
    for h in holdings:
        symbol = h.get('symbol', '?').upper()
        qty = h.get('quantity', 0) or 0
        cost = h.get('cost_basis', 0) or 0
        price = prices.get(symbol)
        value = (price * qty) if price is not None else None
        acct_name = accts.get(str(h.get('account_id', '')), '')
        acct_str = f" ({acct_name})" if acct_name else ""

        if value is not None:
            total_value += value
            total_cost += cost
            if cost > 0:
                gain = value - cost
                gain_pct = (gain / cost) * 100
                gain_str = f", gain ${gain:+,.2f} ({gain_pct:+.1f}%)"
            else:
                gain_str = ", gain n/a (no cost basis)"
            lines.append(
                f"  {symbol}: {qty:,.4f} sh @ ${price:,.2f} = ${value:,.2f}, "
                f"cost ${cost:,.2f}{gain_str}{acct_str}"
            )
        else:
            lines.append(
                f"  {symbol}: {qty:,.4f} sh, cost ${cost:,.2f} "
                f"(live price unavailable){acct_str}"
            )

    holdings_text = f"{len(holdings)} holdings:\n" + "\n".join(lines)

    grand_gain = total_value - total_cost
    grand_pct = (grand_gain / total_cost * 100) if total_cost > 0 else 0.0

    # YTD dividends
    year_start = datetime(datetime.now().year, 1, 1)
    ytd_div = sum(
        d.get('amount', 0)
        for d in db.dividends.find({'date': {'$gte': year_start}})
    )

    summary = (
        f"Portfolio summary ({len(holdings)} holdings):\n"
        f"  Market value (live): ${total_value:,.2f}\n"
        f"  Total cost basis: ${total_cost:,.2f}\n"
        f"  Unrealized gain/loss: ${grand_gain:+,.2f} ({grand_pct:+.1f}%)\n"
        f"  YTD dividends: ${ytd_div:,.2f}"
    )

    return {
        'summary': summary,
        'holdings_text': holdings_text,
        'total_value': total_value,
        'total_cost': total_cost,
    }


def _get_investment_account_balances() -> str:
    """Return live balances for investment-type accounts only."""
    investment_types = set(INVESTMENT_ACCOUNT_TYPES)
    accounts = list(db.accounts.find(
        {'account_type': {'$in': list(investment_types)}},
        {'name': 1, 'account_type': 1, 'institution': 1, 'balance': 1},
    ))
    if not accounts:
        return "No investment accounts found."
    lines = []
    for a in accounts:
        lines.append(
            f"{a['name']} ({a.get('account_type', 'N/A')}): "
            f"${a.get('balance', 0):,.2f} — {a.get('institution', 'N/A')}"
        )
    return "\n".join(lines)


def generate_portfolio_report() -> dict:
    """Generate a portfolio-focused review and store it in the DB.

    Distinct from the broader monthly review: zooms in on self-managed
    holdings (composition, dividends, tax-placement) without retreading
    the household-wide cash flow / debt picture.
    """
    now = datetime.now()
    period = _month_key(now)

    enriched = _build_portfolio_data()
    summary = enriched['summary']
    holdings = enriched['holdings_text']
    dividends = get_ytd_dividends()
    recent_dividends = get_dividend_history(limit=20)
    investment_balances = _get_investment_account_balances()

    data = {
        'portfolio_summary': summary,
        'holdings': holdings,
        'ytd_dividends': dividends,
        'recent_dividends': recent_dividends,
        'investment_balances': investment_balances,
    }

    prompt = PORTFOLIO_INSIGHT_PROMPT.format(
        period=period,
        summary=summary,
        holdings=holdings,
        dividends=dividends,
        recent_dividends=recent_dividends,
        investment_balances=investment_balances,
    )
    insights = _call_ollama(prompt)

    report = {
        'type': 'portfolio',
        'date': now,
        'period': period,
        'data': data,
        'insights': insights,
    }

    db.reports.replace_one(
        {'type': 'portfolio', 'period': period},
        report,
        upsert=True,
    )

    logger.info("Portfolio report generated for %s", period)
    return report


def generate_monthly_report() -> dict:
    """Generate a full monthly financial overview and store it in the DB."""
    now = datetime.now()
    period = _month_key(now)

    # Gather all data sections
    balances = get_account_balances()
    summary = get_account_summary()
    spending = get_spending_by_category()
    budget = get_budget_status()
    recurring = get_recurring_items()
    cashflow = get_cash_flow_outlook()
    negative_months = get_negative_balance_months()
    portfolio = get_portfolio_summary()
    holdings = get_holdings()
    dividends = get_ytd_dividends()
    net_worth = get_current_net_worth()
    nw_history = get_net_worth_history()
    debts_text = get_debts()

    data = {
        'account_balances': balances,
        'account_summary': summary,
        'spending_by_category': spending,
        'budget_status': budget,
        'recurring_items': recurring,
        'cash_flow_outlook': cashflow,
        'negative_balance_months': negative_months,
        'portfolio_summary': portfolio,
        'holdings': holdings,
        'ytd_dividends': dividends,
        'net_worth': net_worth,
        'net_worth_history': nw_history,
        'debts': debts_text,
    }

    # Build prompt and call Ollama
    prompt = MONTHLY_INSIGHT_PROMPT.format(
        period=period,
        balances=balances,
        summary=summary,
        spending=spending,
        budget=budget,
        cashflow=cashflow,
        negative_months=negative_months,
        portfolio=portfolio,
        holdings=holdings,
        dividends=dividends,
        net_worth=net_worth,
        nw_history=nw_history,
        debts=debts_text,
    )
    insights = _call_ollama(prompt)

    report = {
        'type': 'monthly',
        'date': now,
        'period': period,
        'data': data,
        'insights': insights,
    }

    db.reports.replace_one(
        {'type': 'monthly', 'period': period},
        report,
        upsert=True,
    )

    logger.info("Monthly report generated for %s", period)
    return report
