# Price alerts

Get told when a stock you watch goes up. Alerts live in a **Price Alerts**
section on the Portfolio page (`/portfolio#price-alerts`). The logic is in
`agent/price_alerts.py`.

Every alert that fires is recorded and listed on the Portfolio page. Posting
it to Slack as well is optional (see [Slack](#slack-optional)).

The examples on this page use invented figures.

## Triggers

An alert watches one symbol and has at least one of:

| Trigger | Fires | Example |
|---|---|---|
| **Gain ladder** | Once per step, the first time the price is that far above your cost per share | Steps `10, 25, 50`: at +27% the +25 step fires (and +10 is marked as passed); the +50 step is still armed |
| **Target price** | Once, when the price reaches it | `40` fires at $40.00 or more |
| **Daily jump** | At most once per trading day, when the price is up that much on the previous close | `15` fires at +15% on the day |

**Cost per share** comes from the alert's own "Basis / share" if you set one,
otherwise from your holdings of that symbol across all accounts (total cost
basis ÷ total quantity, counting only lots whose cost is known). With no
basis from either, the gain ladder cannot fire; the page shows "no cost
basis" and the MCP tool says so when you add the alert.

The **re-arm** button (↻) clears what has fired so every step and the target
can fire again, for example after you sell and buy back.

## When alerts are checked

A background thread in the web app, started by
`agent.scheduler.schedule_reports()`, checks every five minutes
(`PRICE_ALERT_POLL_SECONDS`) during the regular US trading session: Monday to
Friday, 09:30 to 16:10 New York time, whatever the server's `TZ` is. The few
minutes after the close let the closing price be seen once. It needs no AI.

- Prices come from Yahoo Finance (`fetch_quote`), the same unofficial source
  as the rest of the portfolio; expect occasional gaps.
- Market holidays are harmless: the quote does not move, and the daily jump
  is keyed on the quote's own trading day, not today's date.
- **Check now** on the page checks immediately, also outside market hours
  (the price may then be the last close).
- Symbols on other exchanges are still checked, but only during US hours.

**Each firing is claimed before it is recorded.** `_claim()` is one
`find_one_and_update` that marks the step, target or day as fired. The app is
meant to run as one process, but if two are pointed at the same database only
the one whose update matched records and sends the alert. Never send a firing
without claiming it first.

The MCP server does not run this loop; it only manages alerts.

## Slack (optional)

With no Slack settings, alerts are recorded and shown in the app, nothing is
sent anywhere, and nothing fails.

To also post each alert to a Slack channel:

1. Create a Slack app at <https://api.slack.com/apps> in your workspace and
   give it the **`chat:write`** bot scope. Install it to the workspace and
   copy the **Bot User OAuth Token** (it starts with `xoxb-`).
2. Invite the bot to the channel you want (`/invite @your-bot-name`).
3. Set two environment variables for the web app and restart it:

   ```bash
   SLACK_BOT_TOKEN=xoxb-your-token
   SLACK_CHANNEL=#your-channel
   ```

   In Docker Compose put them in `.env`. In Kubernetes put the token in the
   Secret and the channel in the ConfigMap (both are in
   `deploy/kubernetes/`, commented out).

Both must be set; with only one, nothing is sent. The message says what
happened, the price, and what your position is worth, so only use a channel
whose members may see that.

If Slack refuses a message (a wrong channel name, or the bot is not in the
channel), the alert is still recorded, and the page shows "not sent to
Slack" with Slack's reason next to it. A firing is not retried.

## Collections

`price_alerts`:

| Field | Type | Meaning |
|---|---|---|
| `symbol` | str | Upper-cased ticker |
| `ladder_pct` | list of float, or null | Gain steps in percent, sorted |
| `target_price` | float or null | |
| `daily_jump_pct` | float or null | |
| `basis_price` | float or null | Cost per share override |
| `enabled` | bool | |
| `ladder_fired` | list of float | Steps that have fired |
| `target_fired` | bool | |
| `daily_fired_on` | str or null | Trading day (`YYYY-MM-DD`) the daily jump last fired |
| `last_price`, `last_checked` | float, datetime | From the last check |
| `notes`, `created_at` | | |

`price_alert_events`, one document per firing:

| Field | Type | Meaning |
|---|---|---|
| `alert_id`, `symbol` | | The alert that fired |
| `kind` | str | `ladder`, `target` or `daily` |
| `message` | str | The text (what Slack receives) |
| `price` | float | The price at the time |
| `at` | datetime | When it fired |
| `delivered` | bool | True when Slack accepted it |
| `error` | str or null | Slack's reason when it refused; null when Slack is not configured |

Both are included in the JSON backup (`/export`).

## Routes

| Route | Purpose |
|---|---|
| `POST /portfolio/alerts/add` | Add an alert (`symbol`, `ladder_pct`, `target_price`, `daily_jump_pct`, `basis_price`) |
| `POST /portfolio/alerts/<id>/reset` | Re-arm every trigger |
| `POST /portfolio/alerts/<id>/delete` | Delete the alert |
| `POST /portfolio/alerts/check` | Check now |

## MCP tools

`list_price_alerts`, `add_price_alert`, `delete_price_alert` and
`reset_price_alert`. See [mcp-server.md](mcp-server.md#price-alerts).

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `SLACK_BOT_TOKEN` | empty | Slack bot token (`xoxb-...`). Empty: nothing is sent |
| `SLACK_CHANNEL` | empty | Channel to post to, e.g. `#your-channel` or a channel ID. Empty: nothing is sent |
| `PRICE_ALERT_POLL_SECONDS` | `300` | Seconds between checks |

## Tests

`tests/test_price_alerts.py` covers the triggers, one-time firing, the claim,
market hours in New York time, the optional Slack delivery and the page,
against mongomock. It replaces `fetch_quote` and never posts to Slack.

## Related docs

- [portfolio.md](portfolio.md): holdings, cost basis and the price service
- [scheduled-tasks.md](scheduled-tasks.md): the other background check
- [mcp-server.md](mcp-server.md): the MCP tools
