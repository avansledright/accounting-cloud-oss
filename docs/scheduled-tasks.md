# Scheduled tasks

Recurring financial chores that the app runs for you on a schedule: a yearly
rebalance check, a quarterly reminder, an extra advisor report. Each run is
saved, so you can open the result later. The page is **Scheduled Tasks** in
the sidebar (`/scheduled-tasks`).

The logic is in `agent/scheduled_tasks.py` (schedule math, the task types,
the runner, validation, and the helpers the MCP tools call). The routes in
`app.py` only parse forms and render.

The examples on this page use invented figures.

## Task types

| Type | Needs AI? | What it does |
|---|---|---|
| **Rebalance check** (`rebalance_check`) | No | Compares each account's holdings with the target weights you set and says whether anything has drifted too far, with the trades that would bring it back. |
| **Reminder** (`reminder`) | No | Records a message and a checklist on schedule. |
| **Advisor report** (`report`) | **Yes** | Writes one of the [advisor reports](advisor-agent.md) (weekly, monthly or portfolio) on your own schedule. |

### AI is optional

Rebalance checks and reminders are plain arithmetic and text. They work with
AI turned off and never contact a provider.

An advisor-report task needs a model. While AI is off (the default, see
[ai.md](ai.md)) the task is **skipped, not failed**: nothing is called, no
empty report is stored, and the task shows the status `skipped` with the
reason ("Advisor reports need AI, which is turned off..."). The task list
marks such a task **needs AI**, the form says so before you save, and the MCP
tools return the same message. Turn AI on under **Admin → AI settings** with
*Advisor reports* ticked and the task runs at its next scheduled time.

A rebalance check can add two or three sentences of AI commentary to its
result ("Add a short AI summary"). That is extra: it is requested only when
AI is on, after the result has been saved, in its own thread, through `ai.py`
like every other AI feature. If AI is off or the provider does not answer,
the result simply has no commentary. What it sends is listed in
[ai.md](ai.md#what-gets-sent).

### Rebalance check

For each account in the task:

1. Lots of the same fund are added together and valued at the live price
   (`agent.tools.portfolio.fetch_live_price`). With no live price the cost
   basis is used and the fund is marked **stale**; with neither, the fund is
   left out of the account total and a warning says so.
2. Each fund's share of the account is compared with its target. A fund you
   hold but did not list counts as a 0% target.
3. If any fund is further from its target than the drift threshold (5 points
   by default), the check lists sells and buys, in dollars and approximate
   shares, that bring **every** fund back to target.
4. Trades in an IRA, Roth IRA or 401(k) are noted as having no tax impact. In
   an account marked **Taxable**, each sell carries an estimated gain
   (pro-rata from the total cost basis).
5. With **Scan taxable accounts for tax-loss harvesting** ticked, a taxable
   account also lists funds whose live value is below their cost basis, and
   warns about the wash-sale rule when the same fund is held in *any* other
   account or when this same check suggests buying it elsewhere.

Targets must add up to 100% (within 0.5). **Scan only** skips the drift math
for an account and only runs the tax-loss scan, for an account you do not
keep targets for.

Example: an account holding $7,800 of fund A and $2,200 of fund B, with
targets of 70% and 30%, is 78/22. Both are 8 points off, more than the 5
point threshold, so the check suggests selling $800 of A and buying $800 of
B.

This is arithmetic on the numbers you entered, not advice. Check the figures
before you trade.

## Schedules

| Repeats | Fields | Notes |
|---|---|---|
| Weekly | weekday, time | `weekday` 0 = Monday |
| Monthly | day, time | A day past the end of a month (the 31st) runs on that month's last day |
| Quarterly | first month, day, time | Every three months from the month you pick |
| Yearly | month, day, time | Feb 29 runs on Feb 28 in other years |
| One time | date, time | The task turns itself off after it runs |

Times are whole hours in the server's time zone (the `TZ` variable, UTC
unless you set it; see [deployment.md](deployment.md#configuration-reference)).

## How tasks run

A background thread in the web app, started by
`agent.scheduler.schedule_reports()`, checks every five minutes
(`SCHEDULED_TASKS_POLL_SECONDS`) for enabled tasks whose `next_run` has
passed. It starts whether or not AI is on.

- **A due task is claimed before it runs.** `claim_task()` is one
  `find_one_and_update` that matches the `next_run` it read and moves it
  forward. The app is meant to run as one process, but if two are pointed at
  the same database (two replicas, or a restart overlapping the old process)
  only the one whose update matched runs the task. Never run a due task
  without claiming it first.
- **Catch-up runs once.** The new `next_run` is worked out from *now*, not
  from the missed time. If the app was off for three months, a monthly task
  runs once at startup and then waits for its next date.
- **One failure does not stop the rest.** A task that raises is recorded as
  `error` with the message, its `next_run` still moves on, and the loop
  continues.
- **Run now** runs a task in a background thread without changing its
  schedule.

The MCP server does not run this loop. It can create and edit tasks, and
`run_scheduled_task` runs one on request.

## Results

Each run is stored in the `reports` collection with `type: 'scheduled'`:

| Field | Meaning |
|---|---|
| `task_id`, `task_name`, `task_type` | The task that ran (the name is kept so the result still reads well after the task is deleted) |
| `trigger` | `schedule` or `manual` |
| `date`, `period` | When it ran (`period` is the day, `YYYY-MM-DD`) |
| `data` | The figures, by task type |
| `summary_md` | The result as Markdown (what the MCP tools return) |
| `insights` | The optional AI commentary, or `null` |

Results appear on the task's own page, in a **Scheduled Task Results** card
and the history list on the Advisor page, and through the MCP tools
`latest_scheduled_task_result`, `list_reports` and `get_report`. They are not
advisor briefs, so the dashboard's "Latest Advisor Brief" card ignores them.

Anything that lists `reports` must allow for this type: it has no
`insights` guarantee and a different `data` shape.

## The `scheduled_tasks` collection

| Field | Type | Meaning |
|---|---|---|
| `name` | str | Shown everywhere |
| `task_type` | str | `rebalance_check`, `report` or `reminder` |
| `enabled` | bool | Disabled tasks are never run by the loop |
| `schedule` | dict | `frequency` (`weekly`, `monthly`, `quarterly`, `yearly`, `once`), `hour`, and `weekday` / `day` / `month` / `date` as the frequency needs. `date` is a `datetime` |
| `config` | dict | By type. Rebalance: `accounts` (`account_id`, `targets` `{SYMBOL: percent}`, `taxable`, `scan_only`), `drift_threshold_pct`, `include_tax_loss_harvest`, `ai_narrative`. Report: `report_type`. Reminder: `message`, `checklist` |
| `next_run` | datetime or null | The next time the loop will run it |
| `last_run`, `last_status`, `last_error`, `last_report_id` | | The last run: `ok`, `error` or `skipped`, the reason for the last two, and the stored result |
| `running_since` | datetime or null | Set while a run is in progress |
| `notes`, `created_at`, `updated_at` | | |

It is included in the JSON backup (`/export`) like every other collection.

## Routes

| Route | Purpose |
|---|---|
| `GET /scheduled-tasks` | List and the create/edit form (`?edit=<id>` loads a task into the form) |
| `POST /scheduled-tasks/save` | Create, or update when `task_id` is posted. A validation error re-renders the form with status 400 |
| `POST /scheduled-tasks/<id>/toggle` | Enable or disable. Enabling re-computes `next_run` from now |
| `POST /scheduled-tasks/<id>/run` | Run now, in the background |
| `POST /scheduled-tasks/<id>/delete` | Delete the task. Past results are kept |
| `GET /scheduled-tasks/<id>` | The task's latest result and its past runs |
| `GET /scheduled-tasks/results/<report_id>` | One stored result |

## MCP tools

`list_scheduled_tasks`, `add_scheduled_task`, `update_scheduled_task`,
`delete_scheduled_task`, `run_scheduled_task` and
`latest_scheduled_task_result`. See
[mcp-server.md](mcp-server.md#scheduled-tasks).

## Adding a task type

Register it in `TASK_TYPES` in `agent/scheduled_tasks.py`:

```python
TASK_TYPES['my_type'] = {
    'label': 'My type',
    'run': run_my_type,            # run(config, task) -> {'data': {...}, 'summary_md': '...'}
    'validate': validate_my_type,  # validate(config) -> (clean_config, error_or_None)
    # 'needs_ai': True,            # skipped with a clear message while AI is off
}
```

Then add its form fields to `fields_from_form()` and
`templates/scheduled_tasks.html`, and a branch in
`templates/scheduled_task_result.html` if the Markdown summary is not enough.
A type that needs a model must go through `ai.py`, set `needs_ai`, and raise
`TaskSkipped` when `ai_available()` is false.

## Tests

`tests/test_scheduled_tasks.py` covers the schedule math, the atomic claim
and catch-up, the rebalance arithmetic, the AI-off behaviour, the MCP tools
and the pages, against mongomock. It replaces
`agent.scheduled_tasks.fetch_live_price` and the AI calls, so it never uses
the network.

## Related docs

- [advisor-agent.md](advisor-agent.md): the reports a `report` task writes
- [ai.md](ai.md): turning AI on, and what is sent
- [price-alerts.md](price-alerts.md): the other background check
- [mcp-server.md](mcp-server.md): the MCP tools
