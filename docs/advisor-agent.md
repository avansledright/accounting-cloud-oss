# Advisor reports

The "AI Advisor" is a **report generator**, not an interactive chat. On a
schedule, or when you ask for one, it gathers a text snapshot of your data,
sends it to an LLM with a fixed prompt, and stores the Markdown answer. You
read the result on the Reports page (`/reports`) and in the dashboard's
"Advisor Brief" card. There is no back-and-forth conversation; to ask
follow-up questions about your data, connect an AI client to the
[MCP server](mcp-server.md) instead.

Everything lives in the `agent/` package:

| File | Role |
|---|---|
| `agent/db.py` | Shared database handle (a proxy, see [The agent.db proxy](#the-agentdb-proxy)). |
| `agent/tools/*.py` | Read functions that turn collections into plain-text summaries. Shared with the MCP server's read tools. |
| `agent/prompts.py` | Household profile loading and the three prompt templates. |
| `agent/reports.py` | `generate_weekly_report()`, `generate_monthly_report()`, `generate_portfolio_report()` and the LLM call. |
| `agent/scheduler.py` | Background thread that generates missing reports on a schedule. |
| `agent/household_profile.example.md` | Template for your own household profile. |

The feature is optional. With no LLM configured the rest of the app works
normally; the scheduler stays off unless you enable it.

## Report types

All three report types are stored in the `reports` collection and **upserted
by period** (`replace_one({'type': ..., 'period': ...}, report, upsert=True)`),
so regenerating a report replaces that period's copy instead of adding a
duplicate.

| Type | Period key | Data gathered | Prompt |
|---|---|---|---|
| `weekly` | ISO week, e.g. `2030-W08` (`_week_key`) | `get_spending_by_category()`, `get_budget_status()`, `get_recurring_items()` (current month) | `WEEKLY_INSIGHT_PROMPT` |
| `monthly` | Month, e.g. `2030-02` (`_month_key`) | Account balances and summary, spending by category, budget status, recurring items, 12-month cash flow outlook, negative-balance months, portfolio summary, holdings, YTD dividends, current net worth, net worth history, debts | `MONTHLY_INSIGHT_PROMPT` |
| `portfolio` | Month | Holdings priced live (`_build_portfolio_data`), YTD dividends, the last 20 dividend payments, investment-account balances (`_get_investment_account_balances`, filtered by `INVESTMENT_ACCOUNT_TYPES`) | `PORTFOLIO_INSIGHT_PROMPT` |

Each stored document has this shape:

```python
{
    'type': 'weekly' | 'monthly' | 'portfolio',
    'date': datetime,        # when it was generated
    'period': '2030-W08' or '2030-02',
    'data': {...},           # the text sections that were sent to the model
    'insights': str | None,  # the model's Markdown answer, None if the call failed
}
```

The data sections come from the same `agent/tools` functions that back the
MCP read tools, so the caveats listed in
[mcp-server.md](mcp-server.md#read-tools) apply: spending and budget figures
come from credit card transactions, debt balances are the stored values, and
the cash flow outlook counts the same recurring occurrence dates as the ledger
(`recurrence.occurrence_dates()`) but, unlike `calculate_yearly_outlook()`,
starts from the stored account balance and ignores actual transactions,
skips, overrides and the loan payoff simulation.

The portfolio and monthly reports fetch live prices. Both go through
`fetch_live_price()` in `agent/tools/portfolio.py` (kept out of `app.py` so
the MCP server need not import it), which calls Yahoo Finance's chart
endpoint once per symbol. The monthly report gets them through the
`portfolio_summary` tool's live market value and gain/loss; the portfolio
report (`_fetch_yahoo_price` in `agent/reports.py` is an alias for the same
function) computes market value and unrealised gain or loss per holding. If a price fetch fails, that holding is listed as
"live price unavailable" and left out of the totals. The prompt tells the
model to trust live market value over imported cost basis when they disagree.
See [portfolio.md](portfolio.md) for how holdings and prices work in the app.

## Prompts

`agent/prompts.py` builds each prompt from three parts:

1. `HOUSEHOLD_BASELINE`: the household profile (below).
2. `ADVISOR_ROLE`: the advisor's job and tone (notice what changed, judge
   decisions against goals, give 2 to 4 specific recommendations with dollar
   amounts, do not manufacture concerns, no generic advice).
3. The report-specific template: the data sections with `{placeholders}`,
   then the required Markdown structure of the answer (for example
   "What I'm seeing / Are they on track? / Recommended actions / One thing to
   watch" for the weekly briefing).

The templates are filled with `str.format()` in `agent/reports.py`.

## LLM call

> Reports now run on whichever AI service is set up on **Admin → AI
> settings**: Claude, ChatGPT, Gemini or Ollama. See [ai.md](ai.md). The
> `_call_ollama()` name is kept, but it calls `ai.generate('report', prompt)`.
> The environment variables below are the older configuration, used only
> while no settings have been saved on that page.

With the environment-variable configuration, `_call_ollama()` posts to any
server that implements the Ollama-compatible `/api/chat` endpoint:

```python
POST {AGENT_OLLAMA_HOST}/api/chat
{
  "model": AGENT_OLLAMA_MODEL,
  "messages": [{"role": "user", "content": prompt}],
  "stream": false,
  "options": {"temperature": 0.4, "num_ctx": 8192}
}
```

It returns `response.json()['message']['content']`. Any exception (connection
refused, timeout, HTTP error, unexpected JSON) is logged with
`logger.exception("Ollama request failed")` and the function returns `None`.
The report is still saved, with `insights: None`, so the data snapshot is kept
but the page shows no write-up.

| Variable | Default | Purpose |
|---|---|---|
| `AGENT_OLLAMA_HOST` | empty (AI off) | Base URL of the Ollama-compatible server (trailing slash stripped). From inside Docker, a host-run Ollama is usually `http://host.docker.internal:11434`. |
| `AGENT_OLLAMA_MODEL` | `gemma4:e4b` | Model name. |
| `AGENT_OLLAMA_API_KEY` | empty | When set, sent as `Authorization: Bearer <key>`. Leave blank for plain Ollama. |
| `AGENT_OLLAMA_TIMEOUT` | `3600` | Request timeout in seconds. |

These are separate from `OLLAMA_URL` / `OLLAMA_MODEL` / `OLLAMA_API_KEY`,
which `app.py` uses for credit card categorisation (see
[credit-cards.md](credit-cards.md)). Reports run in the background and nobody
waits on them, so they can use a bigger, slower model than the categoriser.

## Scheduler

`agent.scheduler.schedule_reports()` is called once when `app.py` is imported.
It starts a daemon thread that checks, at startup and at each wake-up, whether
scheduled reports are on (**Admin → AI settings → "Also write them
automatically"**, or `ADVISOR_ENABLED=true` with the environment-variable
configuration). When they are off it skips the run, so a fresh install without
AI does not fill the `reports` collection with insight-less entries.

When enabled, the thread:

1. **Catches up at startup.** `_check_and_generate()` looks for the current
   week's `weekly` report and the current month's `monthly` and `portfolio`
   reports, and generates any that are missing.
2. **Sleeps until the next trigger**, whichever comes first:
   - the next Monday at 06:00 (weekly cadence), or
   - the 1st of the next month at 06:00 (monthly cadence).
3. **Runs `_check_and_generate()` again** and loops.

Because every wake-up runs the same "generate whatever is missing for the
current period" check, Monday 06:00 produces the new weekly report and the 1st
at 06:00 produces the new monthly and portfolio reports (plus a weekly report
if that week's is somehow missing). Times are the server's local time
(`datetime.now()`); in a container that is usually UTC unless you set `TZ`.

The scheduler lives in the web process. The Docker image runs gunicorn with
one worker on purpose (see the comment in the `Dockerfile`): the scheduler
thread, report-job state and price cache are in process memory, and more
workers or replicas would each start their own scheduler. The MCP server does
not start a scheduler.

Each generation is wrapped in `try/except` and logged, so one failing report
does not stop the others or kill the thread.

## Generating reports on demand

### From the Reports page

`/reports` shows the latest report of each type, rendered from Markdown with
the `markdown` package (`extra`, `sane_lists`, `nl2br` extensions), and a
history list of the 40 most recent reports. `/reports/<report_id>` shows one
past report.

The "Regenerate now" form posts `report_type` to `POST /api/reports/generate`:

| `report_type` | Generates |
|---|---|
| `weekly` | Weekly briefing |
| `monthly` | Monthly review |
| `portfolio` | Portfolio review |
| `both` | Weekly and monthly |
| `all` | All three |

Generation is asynchronous. `_run_report_async()` in `app.py` starts a daemon
thread per report type and records it in the in-memory `_REPORT_JOBS` dict
(guarded by a lock). A second request for a type that is already running is
ignored. The route redirects straight back to `/reports`, which shows a
"Generating" banner and polls `GET /api/reports/status` every few seconds:

```json
{"jobs": {"monthly": {"started": "2030-02-01T09:15:02", "elapsed_seconds": 412, "error": null}}}
```

When the job list empties, the page reloads and shows the new report. If the
generation function raises, the error is shown in the banner for 60 seconds.
Note that an LLM failure does not raise (it returns `None`), so it shows up as
a finished report with no insights rather than as an error; check the web
app's logs.

On-demand generation works whether or not `ADVISOR_ENABLED` is set; that flag
only controls the scheduler.

### From an MCP client

The [MCP server](mcp-server.md#report-tools) exposes
`generate_weekly_advisor_report` and `generate_monthly_advisor_report`. These
run synchronously inside the MCP request, do not appear in the web page's
"Generating" banner, and there is no MCP tool for the portfolio report.

## Household profile

Every prompt starts with a household profile: durable context (who the
clients are, income, accounts, debts, goals, standing notes) that the model
should not have to re-derive from raw data each run.

- **Default.** With `ADVISOR_PROFILE_FILE` unset, `DEFAULT_HOUSEHOLD_PROFILE`
  is used. It is generic: it tells the model to work only from the data, not
  to invent names, employers, accounts or goals, that mid-month credit card
  totals can read low because statements are uploaded after they close, and
  not to repeat the same recommendation every week.
- **Your own profile.** Set `ADVISOR_PROFILE_FILE` to a Markdown or plain-text
  file. Start from `agent/household_profile.example.md`, which describes a
  fictional couple ("Alex and Sam Example") with sections for income, cash and
  emergency fund, retirement and investments, real assets and debt, goals, and
  standing advisor notes. Copy it to a path outside version control
  (`household_profile.md` is already in `.gitignore`) and replace the
  contents.
- **Fallback.** If the file is missing, unreadable or empty, a warning is
  logged and the default profile is used.
- **Brace escaping.** The profile is concatenated in front of `str.format()`
  templates, so `_load_household_profile()` output has every `{` and `}`
  doubled. You can use literal braces in the profile safely
  (`tests/test_prompts.py` covers this).
- **Read once at startup.** `HOUSEHOLD_BASELINE` is built when
  `agent.prompts` is imported. After editing the file, restart the web app
  (and the MCP server, if you use its report tools). Update the profile when
  major life events happen: a new job, a paid-off loan, a new dependent.

### With Docker

Mount the file into the container and point the variable at the mounted path,
for example in a `docker-compose.override.yml`:

```yaml
services:
  web:
    volumes:
      - ./household_profile.md:/app/household_profile.md:ro
    environment:
      ADVISOR_PROFILE_FILE: /app/household_profile.md
```

Add the same mount and variable to the `mcp` service if you trigger reports
through MCP. In Kubernetes, a ConfigMap or Secret mounted as a file works the
same way; see [deployment.md](deployment.md).

The profile is sent to whatever LLM endpoint you configure. If that is a
hosted service rather than a local Ollama, the profile and your financial
data leave your network.

## The agent.db proxy

`agent/tools/*.py` and `agent/reports.py` do `from agent.db import db` at
import time, before any database exists. `agent/db.py` makes that work with a
small proxy:

- `db` is a `_DBProxy` instance whose `__getattr__` forwards attribute access
  (`db.accounts`, `db.reports`, ...) to the real pymongo `Database`.
- `agent.db.init(real_db)` sets the target. `app.py` calls
  `agent_db.init(db)` right after connecting; `mcp_server.py` does the same
  before importing the tool modules.
- Using the proxy before `init()` raises
  `RuntimeError("agent.db not initialized ...")`.

So the agent code never opens its own MongoDB connection; it always uses the
host process's database.

## Choosing a model

- **Small CPU models are slow.** The monthly prompt is long (a dozen data
  sections plus the profile and instructions, within an 8,192-token context).
  A mid-size model on CPU can run below 1 token per second, so one report can
  take tens of minutes. That is why `AGENT_OLLAMA_TIMEOUT` defaults to one
  hour and generation runs in background threads.
- **Timeouts.** If reports keep coming back empty, check the logs for
  `Ollama request failed`. Raise `AGENT_OLLAMA_TIMEOUT`, use a smaller or
  quantised model, or point `AGENT_OLLAMA_HOST` at a machine with a GPU. The
  web app's gunicorn `--timeout 120` does not apply, because generation runs
  outside the request. The MCP report tools do block the MCP request, so your
  MCP client's timeout matters there.
- **Context size.** `num_ctx` is fixed at 8192. Very large holdings lists or
  long profiles can push the monthly and portfolio prompts past that; keep
  the profile concise.
- **Cold starts.** The first request after Ollama loads a model is slower.
  The Reports page warns about this while a job runs.
- **Quality.** The prompts ask for specific, numerate recommendations.
  Smaller models tend to drift toward generic advice or misread figures; a
  larger model gives noticeably better reports. Always check numbers against
  the app before acting on them.

## Related docs

- [mcp-server.md](mcp-server.md): the report tools and the shared read tools
- [architecture.md](architecture.md): where the scheduler and agent package fit
- [deployment.md](deployment.md): environment variables in Docker and Kubernetes
- [features.md](features.md#reports-reports): the Reports page in the feature tour
