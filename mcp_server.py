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
from agent.tools import manage as M

# ----- Async report generation -----
from agent import reports as advisor_reports

# ----- Scheduled tasks and price alerts. The web app's polling loops run
# them; this server only manages them (and can run one task on request). -----
from agent import scheduled_tasks
from agent import price_alerts

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
        "holdings, and manage scheduled tasks (rebalance checks, reminders, "
        "advisor reports that run on a schedule) and price alerts. Use the list_* tools first to discover IDs before calling "
        "update_* or delete_* tools. All dates are YYYY-MM-DD. In update_* "
        "tools, leave an argument out to keep the field as it is: text \"none\" "
        "clears a field, and a number argument that defaults to -1 accepts 0 "
        "as a real value."
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


def _combine(*results: str) -> str:
    """One update tool can span two writers. Report what each did; "Nothing to
    update." only when neither had anything to do."""
    said = [r for r in results if r and r != "Nothing to update."]
    return " ".join(dict.fromkeys(said)) if said else "Nothing to update."


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
                     account_id: str = "", start_date: str = "",
                     end_date: str = "", transaction_type: str = "",
                     auto_pay: str = "", payment_method: str = "",
                     notes: str = "", account_number: str = "") -> str:
    """Update a recurring item (a bill or a paycheck). Pass only what changes.
    end_date "none" makes it open-ended again; auto_pay is "true" or "false"."""
    return _combine(
        W.update_recurring(recurring_id, description, amount, frequency,
                           category, day_of_month, account_id),
        M.update_recurring_details(recurring_id, start_date, end_date, transaction_type,
                                   auto_pay, payment_method, notes, account_number))

@mcp.tool()
def delete_recurring(recurring_id: str) -> str:
    """Delete a recurring item."""
    return W.delete_recurring(recurring_id)

@mcp.tool()
def override_recurring_amount(recurring_id: str, date: str, amount: float) -> str:
    """Override the projected amount for one specific occurrence."""
    return W.override_recurring_amount(recurring_id, date, amount)

@mcp.tool()
def skip_projected(description: str, date: str) -> str:
    """Skip ONE projected occurrence of a recurring item or transfer, so the
    outlook stops counting it. description exactly as the ledger shows it
    (a transfer reads "Transfer to: <name>"); date is that occurrence's date."""
    return M.skip_projected(description, date)

@mcp.tool()
def unskip_projected(description: str, date: str) -> str:
    """Undo skip_projected."""
    return M.unskip_projected(description, date)

@mcp.tool()
def list_skipped(date_from: str = "") -> str:
    """Skipped occurrences on or after date_from (default today)."""
    return M.list_skipped(date_from)


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

@mcp.tool()
def list_transfers(limit: int = 20) -> str:
    """One-time transfers WITH IDs, newest first. Recurring ones: list_recurring."""
    return M.list_transfers(limit)

@mcp.tool()
def delete_transfer(transfer_id: str) -> str:
    """Delete a one-time transfer and the two ledger rows it posted."""
    return M.delete_transfer(transfer_id)

@mcp.tool()
def update_recurring_transfer(transfer_id: str, amount: float = -1,
                              frequency: str = "", start_date: str = "",
                              end_date: str = "", day_of_month: str = "",
                              from_account_id: str = "", to_account_id: str = "",
                              description: str = "", notes: str = "") -> str:
    """Edit a recurring transfer. Pass only what changes; end_date "none" clears it."""
    return M.update_recurring_transfer(transfer_id, amount, frequency, start_date,
                                       end_date, day_of_month, from_account_id,
                                       to_account_id, description, notes)


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
                   is_primary: bool = False, account_type: str = "",
                   notes: str = "", account_number: str = "",
                   management_type: str = "", balance_as_of_date: str = "") -> str:
    """Update an account's details (not its balance: update_account_balance).
    is_primary=true makes it the primary account and demotes the other."""
    return _combine(
        W.update_account(account_id, name, institution, estimated_return_rate, is_primary),
        M.update_account_details(account_id, account_type, notes, account_number,
                                 management_type, balance_as_of_date))

@mcp.tool()
def delete_account(account_id: str, force: bool = False) -> str:
    """Delete an account. Refuses, and says what still uses it, while ledger
    transactions, recurring items, transfers or holdings point at it; force=true
    deletes anyway and leaves those rows without an account."""
    return M.delete_account(account_id, force)


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
                 monthly_payment: float = 0,
                 interest_rate: float = 0) -> str:
    """Update common asset fields. 0 = leave unchanged.
    interest_rate is the loan's APR in percent (5.19 = 5.19%).
    liability_balance is the loan's STARTING balance (the web app's field):
    linked payments are subtracted from it to give what is owed now
    (list_assets shows both). Don't set it to a statement's current payoff:
    that subtracts every linked payment a second time."""
    return W.update_asset(asset_id, estimated_value, liability_balance,
                          monthly_payment, interest_rate)

@mcp.tool()
def update_asset_details(asset_id: str, name: str = "", asset_type: str = "",
                         address: str = "", notes: str = "", payment_match: str = "",
                         loan_term_months: int = -1, payment_start_date: str = "",
                         payment_frequency: str = "", purchase_price: float = -1,
                         purchase_date: str = "", property_tax_annual: float = -1,
                         homeowners_insurance_annual: float = -1,
                         pmi_monthly: float = -1, hoa_monthly: float = -1,
                         escrow_balance: float = -1) -> str:
    """The rest of an asset's fields (update_asset has value, loan balance,
    payment and rate). The tax, insurance, PMI and HOA amounts are the
    escrow taken out of each loan payment before principal, so they change what
    the loan shows as owed. payment_match: ledger descriptions, comma separated,
    that auto-link as payments on this loan. payment_frequency: monthly|biweekly."""
    return M.update_asset_details(asset_id, name, asset_type, address, notes,
                                  payment_match, loan_term_months, payment_start_date,
                                  payment_frequency, purchase_price, purchase_date,
                                  property_tax_annual, homeowners_insurance_annual,
                                  pmi_monthly, hoa_monthly, escrow_balance)

@mcp.tool()
def delete_asset(asset_id: str) -> str:
    """Delete an asset and its loan. Linked ledger payments stay, unlinked."""
    return M.delete_asset(asset_id)

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
def update_debt(debt_id: str, name: str = "", debt_type: str = "",
                interest_rate: float = -1, minimum_payment: float = -1,
                creditor: str = "", account_number: str = "", notes: str = "",
                deferment_end_date: str = "", subsidized: str = "",
                payment_match: str = "") -> str:
    """Edit a debt's details: minimum payment, rate (APR percent), name, type,
    creditor, notes, deferment end date, subsidized ("true"/"false") and
    payment_match (ledger descriptions, comma separated, that auto-link as
    payments). Pass only what changes. The starting balance is update_debt_balance."""
    return M.update_debt(debt_id, name, debt_type, interest_rate, minimum_payment,
                         creditor, account_number, notes, deferment_end_date,
                         subsidized, payment_match)

@mcp.tool()
def delete_debt(debt_id: str) -> str:
    """Delete a debt. Its linked ledger payments stay on the ledger, unlinked."""
    return M.delete_debt(debt_id)

@mcp.tool()
def make_debt_payment(debt_id: str, amount: float, date: str = "",
                      account_id: str = "", description: str = "") -> str:
    """Log a payment toward a debt as a ledger expense linked to it (like the
    web app's log-payment). date: YYYY-MM-DD, default today. Omit account_id
    for the primary account. description defaults to 'Extra Debt Payment';
    pass the loan's budget-line description for a regular scheduled payment."""
    return W.make_debt_payment(debt_id, amount, date, account_id, description)

@mcp.tool()
def link_transaction_to_loan(transaction_id: str, loan_ref: str = "") -> str:
    """Count an existing ledger transaction as a payment on a loan. loan_ref is
    'debt:<id>' (list_debts) or 'asset:<id>' (list_assets); empty unlinks it.
    Linked payments are what lower a loan's balance."""
    return M.link_transaction_to_loan(transaction_id, loan_ref)

@mcp.tool()
def loan_payments(loan_ref: str, limit: int = 12) -> str:
    """A loan's linked payments, newest first, each split into principal,
    interest and escrow with the balance after it. loan_ref: 'debt:<id>' or
    'asset:<id>'."""
    return M.loan_payments(loan_ref, limit)

@mcp.tool()
def list_payment_plans() -> str:
    """No-interest installment plans (e.g. a medical bill paid monthly on a
    card) WITH IDs and what is left on each."""
    return M.list_payment_plans()

@mcp.tool()
def add_payment_plan(vendor: str, original_amount: float, monthly_payment: float,
                     start_date: str = "", card_name: str = "", category: str = "",
                     notes: str = "") -> str:
    """Add a no-interest installment plan."""
    return M.add_payment_plan(vendor, original_amount, monthly_payment, start_date,
                              card_name, category, notes)

@mcp.tool()
def update_payment_plan(plan_id: str, vendor: str = "", original_amount: float = -1,
                        monthly_payment: float = -1, start_date: str = "",
                        card_name: str = "", category: str = "", notes: str = "",
                        status: str = "") -> str:
    """Edit a payment plan. status: active|closed."""
    return M.update_payment_plan(plan_id, vendor, original_amount, monthly_payment,
                                 start_date, card_name, category, notes, status)

@mcp.tool()
def delete_payment_plan(plan_id: str) -> str:
    """Delete a payment plan. Card charges linked to it stay, unlinked."""
    return M.delete_payment_plan(plan_id)

@mcp.tool()
def link_cc_transaction_to_plan(cc_transaction_id: str, plan_id: str = "") -> str:
    """Count a credit card charge (ID from cc_transactions) as a payment on a
    payment plan; empty plan_id unlinks it."""
    return M.link_cc_transaction_to_plan(cc_transaction_id, plan_id)

@mcp.tool()
def recategorize_cc_transaction(cc_transaction_id: str, category: str) -> str:
    """Change a credit card charge's category (ID from cc_transactions)."""
    return M.recategorize_cc_transaction(cc_transaction_id, category)


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
def update_goal(goal_id: str, name: str = "", target_amount: float = -1,
                target_date: str = "", category: str = "", notes: str = "") -> str:
    """Edit a goal's name, target amount, target date, category or notes."""
    return M.update_goal(goal_id, name, target_amount, target_date, category, notes)

@mcp.tool()
def contribute_to_goal(goal_id: str, amount: float) -> str:
    """Add to a goal's saved amount (negative takes money back out)."""
    return M.contribute_to_goal(goal_id, amount)

@mcp.tool()
def reset_month_budget_targets(month: str) -> str:
    """Drop every per-month budget override for YYYY-MM, so the month goes back
    to the default targets."""
    return M.reset_month_budget_targets(month)

@mcp.tool()
def take_net_worth_snapshot() -> str:
    """Record today's net worth in the history (the Net Worth page's button)."""
    return M.take_net_worth_snapshot()

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

@mcp.tool()
def list_dividends(symbol: str = "", limit: int = 20) -> str:
    """Recorded dividends WITH IDs, newest first."""
    return M.list_dividends(symbol, limit)

@mcp.tool()
def delete_dividend(dividend_id: str) -> str:
    """Delete a recorded dividend."""
    return M.delete_dividend(dividend_id)


# ============================================================
# ADVISOR REPORTS
# ============================================================

@mcp.tool()
def generate_weekly_advisor_report() -> str:
    """Trigger generation of the weekly AI advisor report.
    Blocks until the LLM finishes (minutes on a CPU-only model). Returns a status string."""
    if not advisor_reports.llm_configured():
        return "Advisor reports are off. Turn them on in the app under Admin -> AI settings."
    r = advisor_reports.generate_weekly_report()
    n = len((r or {}).get('insights') or '')
    return f"Weekly report generated. Insights: {n} chars."

@mcp.tool()
def generate_monthly_advisor_report() -> str:
    """Trigger generation of the monthly AI advisor report.
    Blocks until the LLM finishes (can be many minutes). Returns a status string."""
    if not advisor_reports.llm_configured():
        return "Advisor reports are off. Turn them on in the app under Admin -> AI settings."
    r = advisor_reports.generate_monthly_report()
    n = len((r or {}).get('insights') or '')
    return f"Monthly report generated. Insights: {n} chars."


@mcp.tool()
def list_reports(limit: int = 10) -> str:
    """Saved advisor reports WITH IDs, newest first."""
    return M.list_reports(limit)

@mcp.tool()
def get_report(report_id: str) -> str:
    """The text of one saved report."""
    return M.get_report(report_id)


# ============================================================
# SCHEDULED TASKS
# ============================================================

@mcp.tool()
def list_scheduled_tasks() -> str:
    """List scheduled tasks WITH IDs: type, schedule, enabled, next/last run and status."""
    return scheduled_tasks.list_tasks_text()

@mcp.tool()
def add_scheduled_task(name: str, task_type: str, frequency: str,
                       month: int = 0, day: int = 0, weekday: int = -1,
                       hour: int = 8, config_json: str = "{}", notes: str = "",
                       date: str = "") -> str:
    """Create a scheduled task that runs inside the web app.

    task_type: 'rebalance_check' | 'report' | 'reminder'.
    frequency: 'weekly' (weekday 0=Mon..6) | 'monthly' (day) | 'quarterly'
    (month = first quarter month, day) | 'yearly' (month, day) | 'once' (date
    YYYY-MM-DD, or month+day for the next occurrence). hour: 0-23 in the
    server's time zone. Days past a month's end (31st) run on its last day.

    config_json by type:
      rebalance_check: {"accounts": [{"account_id": "...", "targets": {"VTI": 60,
        "BND": 40}, "taxable": false}], "drift_threshold_pct": 5,
        "include_tax_loss_harvest": true, "ai_narrative": true}
        Targets must sum to 100 (within 0.5). Add "scan_only": true (targets may be
        {}) for an account that should only be scanned for tax-loss harvesting
        and wash-sale conflicts. Use list_accounts for investment account IDs.
        Needs no AI; the optional narrative is added only when AI is on.
      report: {"report_type": "weekly"|"monthly"|"portfolio"}
        Needs AI turned on in the app (Admin -> AI settings); skipped otherwise.
      reminder: {"message": "...", "checklist": ["item", ...]}  (no AI)
    """
    return scheduled_tasks.mcp_add_task(name, task_type, frequency, month, day, weekday,
                                        hour, config_json, notes, date)

@mcp.tool()
def update_scheduled_task(task_id: str, name: str = "", task_type: str = "",
                          frequency: str = "", month: int = 0, day: int = 0,
                          weekday: int = -1, hour: int = -1, config_json: str = "",
                          notes: str = "", enabled: str = "", date: str = "") -> str:
    """Update a scheduled task. Pass only what changes (0 / -1 / "" = unchanged).
    config_json is merged key-by-key into the stored config (a given "accounts"
    list replaces the old one). enabled: "true" | "false". Changing the schedule
    or re-enabling re-computes the next run from now."""
    return scheduled_tasks.mcp_update_task(task_id, name, task_type, frequency, month, day,
                                           weekday, hour, config_json, notes, enabled, date)

@mcp.tool()
def delete_scheduled_task(task_id: str) -> str:
    """Delete a scheduled task. Its past results are kept."""
    return scheduled_tasks.mcp_delete_task(task_id)

@mcp.tool()
def run_scheduled_task(task_id: str) -> str:
    """Run a scheduled task now (does not change its next run). rebalance_check and
    reminder run synchronously and return the summary; report tasks start in the
    background (fetch them later with latest_scheduled_task_result), or are
    skipped with a message while AI is turned off."""
    return scheduled_tasks.mcp_run_task(task_id)

@mcp.tool()
def latest_scheduled_task_result(task_id: str) -> str:
    """The most recent stored result (markdown summary + AI narrative if any) of a task."""
    return scheduled_tasks.mcp_latest_result(task_id)


# ============================================================
# PRICE ALERTS
# ============================================================

@mcp.tool()
def list_price_alerts() -> str:
    """Price alerts WITH IDs: triggers (and which already fired), last price, basis
    per share and gain vs basis, plus the latest firings."""
    return price_alerts.mcp_list()

@mcp.tool()
def add_price_alert(symbol: str, ladder_pct: str = "", target_price: float = 0.0,
                    daily_jump_pct: float = 0.0, basis_price: float = 0.0,
                    notes: str = "") -> str:
    """Record an alert when a stock goes up. Set at least one trigger:
    ladder_pct: comma list of % gains over cost basis, each alerting once (e.g. "10,25,50,100");
    target_price: alert once when the price reaches it;
    daily_jump_pct: alert once per trading day when up this % on the previous close.
    basis_price: per-share basis; leave 0 to use the holdings' cost_basis / quantity.
    Checked every 5 minutes in US market hours by the web app. Firings show on the
    Portfolio page, and are also posted to Slack when the app has Slack configured."""
    return price_alerts.mcp_add(symbol, ladder_pct, target_price, daily_jump_pct, basis_price, notes)

@mcp.tool()
def delete_price_alert(alert_id: str) -> str:
    """Delete a price alert (IDs from list_price_alerts)."""
    return price_alerts.mcp_delete(alert_id)

@mcp.tool()
def reset_price_alert(alert_id: str) -> str:
    """Re-arm every trigger of a price alert so ladder steps and the target can alert again."""
    return price_alerts.mcp_reset(alert_id)


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
