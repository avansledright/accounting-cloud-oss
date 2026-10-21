"""Standalone MCP server for Accounting Cloud — read + write tools.

Run with:
    python mcp_server.py

Listens on MCP_HOST:MCP_PORT (default 0.0.0.0:8765) at:
    http://host:port/http       (streamable HTTP, stateless — preferred)
    http://host:port/sse        (legacy SSE event stream)
    http://host:port/messages/  (legacy SSE JSON-RPC POSTs)

Point clients at /http. SSE only works against a single replica.
"""

import hmac
import os

from mcp.server.fastmcp import FastMCP
from pymongo import MongoClient

import agent.db as agent_db

# ----- DB init (must come before importing tool modules) -----
mongo_uri = os.environ.get("MONGO_URI", "mongodb://localhost:27017/")
client = MongoClient(mongo_uri)
db = client[os.environ.get("MONGO_DB_NAME", "accounting_cloud")]
agent_db.init(db)

# ----- Read tools (existing) -----
from agent.tools.accounts import get_account_balances, get_account_summary
from agent.tools.transactions import (
    search_transactions, search_cc_transactions, get_spending_by_category,
)
from agent.tools.budget import get_budget_status, get_recurring_items
from agent.tools.cashflow import get_cash_flow_outlook, get_negative_balance_months
from agent.tools.portfolio import (
    get_portfolio_summary, get_holdings, get_dividend_history, get_ytd_dividends,
)
from agent.tools.networth import get_current_net_worth, get_net_worth_history
from agent.tools.debts import get_debts, get_total_debt

# ----- Write tools -----
from agent.tools import writes as W

# ----- Async report generation -----
from agent import reports as advisor_reports

MCP_HOST = os.environ.get("MCP_HOST", "0.0.0.0")
MCP_PORT = int(os.environ.get("MCP_PORT", "8765"))
# Public mount path when behind an ingress that strips a prefix (e.g.
# "/accounting-mcp"). Empty string for "no prefix" (direct access).
MCP_MOUNT_PATH = os.environ.get("MCP_MOUNT_PATH", "") or None

mcp = FastMCP(
    "Accounting Cloud",
    instructions=(
        "Household personal finance tools. Read tools query "
        "accounts, ledger and credit card transactions, budgets, cash flow, "
        "portfolio, net worth, and debts. Write tools create and update transactions, recurring "
        "items, transfers, accounts, assets, debts, budgets, goals, and "
        "holdings. Use the list_* tools first to discover IDs before calling "
        "update_* or delete_* tools. All dates are YYYY-MM-DD."
    ),
    host=MCP_HOST,
    port=MCP_PORT,
    # Streamable HTTP at /http, stateless: every request is self-contained,
    # so it works behind a multi-replica Service. SSE can't — its session
    # lives in one process's memory and a load balancer may route the
    # follow-up POSTs to a different replica.
    streamable_http_path="/http",
    stateless_http=True,
)


# ============================================================
# READ TOOLS
# ============================================================

@mcp.tool()
def account_balances() -> str:
    """All account balances: name, type, institution, current balance."""
    return get_account_balances()

@mcp.tool()
def account_summary() -> str:
    """Total balances grouped: cash vs investment vs other."""
    return get_account_summary()

@mcp.tool()
def list_accounts() -> str:
    """List every account WITH ITS ID — call this first when you need an account_id."""
    return W.list_accounts()

@mcp.tool()
def transactions(description: str = "", date_from: str = "", date_to: str = "",
                 transaction_type: str = "", limit: int = 20, account_id: str = "") -> str:
    """Search ledger transactions. Each row shows its ID (for update/delete) and account.
    Covers EVERY account unless account_id is given.

    Args:
        description: Substring (case-insensitive).
        date_from: YYYY-MM-DD.
        date_to: YYYY-MM-DD.
        transaction_type: 'expense' or 'income'.
        limit: Max results.
        account_id: Restrict to one account (see list_accounts).
    """
    return search_transactions(description, date_from, date_to, transaction_type, limit, account_id)

@mcp.tool()
def cc_transactions(description: str = "", card: str = "", date_from: str = "",
                    date_to: str = "", category: str = "", limit: int = 20) -> str:
    """Search credit card transactions (imported statements), newest first.

    Args:
        description: Merchant substring (case-insensitive).
        card: Card name or issuer substring, e.g. 'amex'.
        date_from: YYYY-MM-DD.
        date_to: YYYY-MM-DD.
        category: Category name.
        limit: Max results.
    """
    return search_cc_transactions(description, card, date_from, date_to, category, limit)

@mcp.tool()
def spending_by_category(month: str = "") -> str:
    """CC spending breakdown by category. month=YYYY-MM (default: current)."""
    return get_spending_by_category(month)

@mcp.tool()
def budget_status(month: str = "") -> str:
    """Budget targets vs actual spending. month=YYYY-MM (default: current)."""
    return get_budget_status(month)

@mcp.tool()
def recurring_items(item_type: str = "") -> str:
    """All recurring items. Filter: 'expense' | 'income' | 'transfer'."""
    return get_recurring_items(item_type)

@mcp.tool()
def list_recurring(item_type: str = "") -> str:
    """List recurring items WITH IDs (needed for update/delete tools)."""
    return W.list_recurring(item_type)

@mcp.tool()
def cash_flow_outlook(account_id: str = "") -> str:
    """12-month projected cash flow. Defaults to primary account."""
    return get_cash_flow_outlook(account_id)

@mcp.tool()
def negative_balance_months(account_id: str = "") -> str:
    """Months projected to dip below zero."""
    return get_negative_balance_months(account_id)

@mcp.tool()
def portfolio_summary(account_id: str = "") -> str:
    """Portfolio totals: live market value, cost basis, gain/loss, YTD dividends, holding count."""
    return get_portfolio_summary(account_id)

@mcp.tool()
def holdings(account_id: str = "") -> str:
    """Stock/ETF holdings with symbol, quantity, cost basis."""
    return get_holdings(account_id)

@mcp.tool()
def list_holdings(account_id: str = "") -> str:
    """Holdings WITH IDs (needed for update/delete tools)."""
    return W.list_holdings_with_ids(account_id)

@mcp.tool()
def dividend_history(symbol: str = "", limit: int = 20) -> str:
    """Recent dividend payments."""
    return get_dividend_history(symbol, limit)

@mcp.tool()
def ytd_dividends() -> str:
    """Year-to-date dividend totals by symbol."""
    return get_ytd_dividends()

@mcp.tool()
def current_net_worth() -> str:
    """Current net worth breakdown."""
    return get_current_net_worth()

@mcp.tool()
def net_worth_history(limit: int = 12) -> str:
    """Recent net-worth snapshots."""
    return get_net_worth_history(limit)

@mcp.tool()
def debts() -> str:
    """All debts."""
    return get_debts()

@mcp.tool()
def list_debts() -> str:
    """Debts WITH IDs."""
    return W.list_debts()

@mcp.tool()
def total_debt() -> str:
    """Sum of all debt balances."""
    return get_total_debt()

@mcp.tool()
def list_assets() -> str:
    """All assets WITH IDs."""
    return W.list_assets()

@mcp.tool()
def list_goals() -> str:
    """All savings goals WITH IDs."""
    return W.list_goals()


# ============================================================
# WRITE TOOLS — TRANSACTIONS
# ============================================================

@mcp.tool()
def add_transaction(description: str, amount: float, date: str,
                    transaction_type: str, account_id: str = "") -> str:
    """Add a ledger transaction. transaction_type: 'income'|'expense'. date: YYYY-MM-DD.
    Omit account_id to post to the primary account."""
    return W.add_transaction(description, amount, date, transaction_type, account_id)

@mcp.tool()
def update_transaction(transaction_id: str, description: str = "",
                       amount: float = 0, date: str = "",
                       transaction_type: str = "", account_id: str = "") -> str:
    """Update fields on a transaction. Pass only what changes."""
    return W.update_transaction(transaction_id, description, amount, date,
                                transaction_type, account_id)

@mcp.tool()
def delete_transaction(transaction_id: str) -> str:
    """Permanently delete a transaction."""
    return W.delete_transaction(transaction_id)


# ============================================================
# WRITE TOOLS — RECURRING
# ============================================================

@mcp.tool()
def add_recurring(description: str, amount: float, frequency: str,
                  start_date: str, transaction_type: str,
                  category: str = "", day_of_month: str = "",
                  account_id: str = "", auto_pay: bool = False,
                  payment_method: str = "", notes: str = "") -> str:
    """Add a recurring transaction (bill or paycheck).
    frequency: 'weekly'|'biweekly' (every 14 days)|'monthly'|'bi-monthly'
    (every OTHER month)|'yearly'. A twice-a-month payday is 'monthly' with
    day_of_month '15,30'.
    day_of_month: '1' or '1,15' for split-month posting."""
    return W.add_recurring(description, amount, frequency, start_date,
                           transaction_type, category, day_of_month,
                           account_id, auto_pay, payment_method, notes)

@mcp.tool()
def update_recurring(recurring_id: str, description: str = "",
                     amount: float = 0, frequency: str = "",
                     category: str = "", day_of_month: str = "",
                     account_id: str = "") -> str:
    """Update a recurring item."""
    return W.update_recurring(recurring_id, description, amount, frequency,
                              category, day_of_month, account_id)

@mcp.tool()
def delete_recurring(recurring_id: str) -> str:
    """Delete a recurring item."""
    return W.delete_recurring(recurring_id)

@mcp.tool()
def override_recurring_amount(recurring_id: str, date: str, amount: float) -> str:
    """Override the projected amount for one specific occurrence."""
    return W.override_recurring_amount(recurring_id, date, amount)


# ============================================================
# WRITE TOOLS — TRANSFERS
# ============================================================

@mcp.tool()
def add_transfer(from_account_id: str, to_account_id: str,
                 amount: float, date: str,
                 description: str = "", notes: str = "") -> str:
    """Record a one-time transfer between accounts. Also posts the paired
    'Transfer to:'/'Transfer from:' ledger rows on both accounts, as the web app does."""
    return W.add_transfer(from_account_id, to_account_id, amount, date,
                          description, notes)

@mcp.tool()
def add_recurring_transfer(from_account_id: str, to_account_id: str,
                           amount: float, frequency: str, start_date: str,
                           day_of_month: str = "", description: str = "") -> str:
    """Set up a recurring transfer between two accounts."""
    return W.add_recurring_transfer(from_account_id, to_account_id, amount,
                                    frequency, start_date, day_of_month,
                                    description)

@mcp.tool()
def delete_recurring_transfer(transfer_id: str) -> str:
    """Delete a recurring transfer."""
    return W.delete_recurring_transfer(transfer_id)


# ============================================================
# WRITE TOOLS — ACCOUNTS
# ============================================================

@mcp.tool()
def add_account(name: str, account_type: str, institution: str,
                balance: float = 0, is_primary: bool = False,
                estimated_return_rate: float = 0,
                management_type: str = "") -> str:
    """Create an account. account_type: checking|savings|ira|roth_ira|brokerage|401k|other."""
    return W.add_account(name, account_type, institution, balance,
                         is_primary, estimated_return_rate, management_type)

@mcp.tool()
def update_account_balance(account_id: str, balance: float) -> str:
    """Set an account's balance and stamp the as-of date."""
    return W.update_account_balance(account_id, balance)

@mcp.tool()
def update_account(account_id: str, name: str = "", institution: str = "",
                   estimated_return_rate: float = 0,
                   is_primary: bool = False) -> str:
    """Update account metadata."""
    return W.update_account(account_id, name, institution,
                            estimated_return_rate, is_primary)


# ============================================================
# WRITE TOOLS — ASSETS / DEBTS
# ============================================================

@mcp.tool()
def add_asset(name: str, asset_type: str, estimated_value: float,
              liability_balance: float = 0, interest_rate: float = 0,
              monthly_payment: float = 0, address: str = "",
              notes: str = "") -> str:
    """Add an asset. asset_type: real_estate|vehicle|other."""
    return W.add_asset(name, asset_type, estimated_value, liability_balance,
                       interest_rate, monthly_payment, address, notes)

@mcp.tool()
def update_asset(asset_id: str, estimated_value: float = 0,
                 liability_balance: float = 0,
                 monthly_payment: float = 0) -> str:
    """Update common asset fields. 0 = leave unchanged."""
    return W.update_asset(asset_id, estimated_value, liability_balance,
                          monthly_payment)

@mcp.tool()
def add_debt(name: str, debt_type: str, balance: float,
             interest_rate: float = 0, minimum_payment: float = 0,
             creditor: str = "", notes: str = "") -> str:
    """Add a debt. debt_type: credit_card|personal_loan|student_loan|medical|other."""
    return W.add_debt(name, debt_type, balance, interest_rate,
                      minimum_payment, creditor, notes)

@mcp.tool()
def update_debt_balance(debt_id: str, balance: float) -> str:
    """Set a debt's STARTING balance (the web app's "Starting Balance").
    The current balance is this minus linked ledger payments, so don't use
    this to record a payment — use make_debt_payment."""
    return W.update_debt_balance(debt_id, balance)

@mcp.tool()
def make_debt_payment(debt_id: str, amount: float, date: str = "",
                      account_id: str = "", description: str = "") -> str:
    """Log a payment toward a debt as a ledger expense linked to it (like the
    web app's log-payment). date: YYYY-MM-DD, default today. Omit account_id
    for the primary account. description defaults to 'Extra Debt Payment';
    pass the loan's budget-line description for a regular scheduled payment."""
    return W.make_debt_payment(debt_id, amount, date, account_id, description)


# ============================================================
# WRITE TOOLS — BUDGETS / GOALS
# ============================================================

@mcp.tool()
def set_budget_target(category: str, amount: float, month: str = "") -> str:
    """Set a category's budget target. Omit month for default; pass YYYY-MM
    for a per-month override. amount=0 clears the target."""
    return W.set_budget_target(category, amount, month)

@mcp.tool()
def add_goal(name: str, target_amount: float, current_amount: float = 0,
             target_date: str = "", category: str = "", notes: str = "") -> str:
    """Create a savings goal. target_date: YYYY-MM-DD."""
    return W.add_goal(name, target_amount, current_amount, target_date,
                      category, notes)

@mcp.tool()
def update_goal_amount(goal_id: str, current_amount: float) -> str:
    """Update a goal's current amount."""
    return W.update_goal_amount(goal_id, current_amount)

@mcp.tool()
def delete_goal(goal_id: str) -> str:
    """Delete a goal."""
    return W.delete_goal(goal_id)


# ============================================================
# WRITE TOOLS — HOLDINGS / DIVIDENDS
# ============================================================

@mcp.tool()
def add_holding(account_id: str, symbol: str, quantity: float,
                cost_basis: float = 0, notes: str = "") -> str:
    """Add a stock/ETF holding to an investment account."""
    return W.add_holding(account_id, symbol, quantity, cost_basis, notes)

@mcp.tool()
def update_holding(holding_id: str, quantity: float = 0,
                   cost_basis: float = 0) -> str:
    """Update a holding's quantity and/or cost basis."""
    return W.update_holding(holding_id, quantity, cost_basis)

@mcp.tool()
def delete_holding(holding_id: str) -> str:
    """Delete a holding."""
    return W.delete_holding(holding_id)

@mcp.tool()
def add_dividend(symbol: str, amount: float, date: str,
                 holding_id: str = "") -> str:
    """Manually record a dividend payment."""
    return W.add_dividend(symbol, amount, date, holding_id)


# ============================================================
# ADVISOR REPORTS
# ============================================================

@mcp.tool()
def generate_weekly_advisor_report() -> str:
    """Trigger generation of the weekly AI advisor report.
    Blocks until the LLM finishes (minutes on a CPU-only model). Returns a status string."""
    if not advisor_reports.llm_configured():
        return "Advisor reports are off: set AGENT_OLLAMA_HOST to an Ollama-compatible endpoint to use them."
    r = advisor_reports.generate_weekly_report()
    n = len((r or {}).get('insights') or '')
    return f"Weekly report generated. Insights: {n} chars."

@mcp.tool()
def generate_monthly_advisor_report() -> str:
    """Trigger generation of the monthly AI advisor report.
    Blocks until the LLM finishes (can be many minutes). Returns a status string."""
    if not advisor_reports.llm_configured():
        return "Advisor reports are off: set AGENT_OLLAMA_HOST to an Ollama-compatible endpoint to use them."
    r = advisor_reports.generate_monthly_report()
    n = len((r or {}).get('insights') or '')
    return f"Monthly report generated. Insights: {n} chars."


class BearerAuthASGI:
    """Require `Authorization: Bearer <token>` on every request.

    This surface exposes WRITE tools over the same MongoDB as the web app --
    it can create and delete transactions, accounts, debts and holdings. The
    web app is expected to sit behind a reverse proxy with SSO, but an MCP
    client usually cannot complete an interactive browser login, so this
    surface is authenticated with a static bearer token instead.

    Written as raw ASGI rather than Starlette's BaseHTTPMiddleware on purpose:
    BaseHTTPMiddleware buffers the response body, which breaks SSE streaming.

    Fails CLOSED -- with no token configured it refuses everything, because a
    missing secret must never silently expose financial write access.
    """

    def __init__(self, app, token: str):
        self.app = app
        self.token = token

    async def _reject(self, send, status: int, message: str):
        body = f'{{"error":"{message}"}}'.encode()
        await send({
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        })
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if not self.token:
            await self._reject(send, 503, "mcp token not configured")
            return
        supplied = ""
        for name, value in scope.get("headers", []):
            if name == b"authorization":
                scheme, _, supplied = value.decode("latin-1").partition(" ")
                if scheme.lower() != "bearer":
                    supplied = ""
                break
        # compare_digest keeps the check constant-time.
        if not supplied or not hmac.compare_digest(supplied, self.token):
            await self._reject(send, 401, "unauthorized")
            return
        await self.app(scope, receive, send)


if __name__ == "__main__":
    import uvicorn

    # The streamable HTTP app owns the lifespan that runs its session
    # manager, so the SSE routes are grafted onto it rather than the reverse.
    starlette_app = mcp.streamable_http_app()
    starlette_app.router.routes.extend(mcp.sse_app(mount_path=MCP_MOUNT_PATH).routes)
    app = BearerAuthASGI(starlette_app, os.environ.get("MCP_API_TOKEN", ""))
    uvicorn.run(app, host=MCP_HOST, port=MCP_PORT)
