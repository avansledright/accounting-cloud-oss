# AI features (optional)

Accounting Cloud works fully without AI. Turning it on adds three things:

| Feature | What it does | Where |
|---|---|---|
| Sorting card purchases | Purchases the built-in keyword rules leave as "Other" are sorted into a category after a statement import. They get a small **AI** mark so you can check them. | Credit Cards |
| Spending tips | A few suggestions to save money, based on your card spending. | Credit Cards → Insights |
| Advisor reports | Weekly, monthly and portfolio briefings about your money. On demand, or automatically every Monday and on the 1st at 6 am. | Advisor |

You bring your own AI account and pay the provider directly, or run a free
model on your own computer:

| Service | Cost | Notes |
|---|---|---|
| **Claude** (Anthropic) | Prepaid credit; a few dollars lasts months | Not used for training; deleted within 30 days |
| **ChatGPT** (OpenAI) | $5 minimum prepaid credit | Not used for training; kept up to 30 days for abuse checks |
| **Gemini** (Google) | Free tier, or $5 minimum prepaid | **Use the paid tier**: on the free tier Google may use and humans may read your data |
| **Ollama** | Free | Runs on your own computer; needs a capable machine |

Everything is set up on one page: **Admin → AI settings** (`/settings/ai`).
The page has the same step-by-step instructions as below.

- [Set it up](#set-it-up)
- [What gets sent](#what-gets-sent)
- [What it costs](#what-it-costs)
- [Your API key](#your-api-key)
- [Troubleshooting](#troubleshooting)
- [How it works (for developers)](#how-it-works-for-developers)

## Set it up

1. Open the app, go to **Admin**, and click **Set up AI (step by step)**.
2. Pick a service.
3. Follow the steps for that service below to get an API key (about five
   minutes).
4. Paste the key, choose models (the defaults are fine), tick the features
   you want, read **What gets sent** and tick the box to confirm.
5. Click **Save and test**. The app sends one tiny test message and tells you
   whether the key works.

### Claude (Anthropic)

1. Go to [platform.claude.com](https://platform.claude.com) (the Claude
   Console) and sign up or log in. This is separate from a Claude.ai chat
   subscription, which doesn't include API use.
2. Open [Settings → Billing](https://platform.claude.com/settings/billing),
   add a payment card and click **Buy credits**. A few dollars is plenty to
   start. Leave **auto-reload** off if you want a hard cap.
3. Open [Settings → API keys](https://platform.claude.com/settings/keys) and
   click **Create key**. Name it `Accounting Cloud` and create it.
4. Copy the key. It starts with `sk-ant-` and is shown only once.
5. Optional: on the Billing page, **Spend limits → Set limit** caps monthly
   spend.

Models offered: Claude Opus 5.5 (default; most capable), Sonnet 5.5, and
Haiku 5.5 (cheapest). Opus and Sonnet requests turn on Anthropic's
server-side refusal fallback, so a request a safety classifier declines is
retried on Anthropic's recommended fallback model instead of failing.

### ChatGPT (OpenAI)

1. Go to [platform.openai.com](https://platform.openai.com) and sign up or
   log in. A ChatGPT Plus subscription doesn't include API use.
2. Open [Settings → Billing](https://platform.openai.com/settings/organization/billing),
   add a payment card and buy credit (minimum $5). Auto-recharge is on by
   default; turn it off for a hard cap. Without credit the key doesn't work.
3. Open [API keys](https://platform.openai.com/api-keys), click **Create new
   secret key**, name it `Accounting Cloud` and keep the default project and
   permissions.
4. Copy the key (it starts with `sk-`; it's shown only once).
5. Optional: [Settings → Limits](https://platform.openai.com/settings/organization/limits)
   → **Edit spend limit**, then turn on **Enforce a hard limit**.

Models offered: GPT-6 Luna (default for everyday tasks; cheapest) and GPT-6.1
Sol (default for reports).

### Gemini (Google)

> [!WARNING]
> Use the paid tier for financial data. On the free tier Google may use what
> you send to improve its products and human reviewers may read it, and
> Google's own terms say not to send personal information to it. Prepaying
> (minimum $5) switches that off.

1. Go to [aistudio.google.com/apikey](https://aistudio.google.com/apikey) and
   sign in with your Google account. Accepting the terms creates a project and
   an API key automatically.
2. On the API keys page, click **Set up billing** in your project's
   **Billing tier** column and prepay at least $5. Leave auto-reload off for a
   hard cap.
3. If no key is listed, click **Create API key** and choose your project.
4. Copy the key. New keys start with `AQ.`; older ones with `AIza`.
5. Optional: [aistudio.google.com/spend](https://aistudio.google.com/spend) →
   **Edit spend cap**. Prepaid credit is a hard limit in itself.

Models offered: Gemini 3.5 Flash-Lite (default for everyday tasks), 3.1
Flash-Lite (cheapest) and 3.8 Flash (default for reports).

Google's terms describe the Gemini API as meant for developers building for
professional or business use, so check they suit you before using a personal
account for this.

### Ollama (your own computer)

For people comfortable installing software. Nothing leaves your network and
there is no bill, but it needs a reasonably powerful computer (16 GB of
memory or more; a graphics card or an Apple Silicon Mac makes it much faster).

1. Install Ollama from [ollama.com/download](https://ollama.com/download).
2. Download the default models:
   ```bash
   ollama pull granite4.2:3b
   ollama pull gemma4:e4b
   ```
   Any other model works; enter its name on the settings page.
3. Let the app (which runs in Docker) reach Ollama:
   - **Windows and Mac:** in Ollama's settings, turn on **Expose Ollama to the
     network** and restart Ollama.
   - **Linux:** `sudo systemctl edit ollama`, add
     ```ini
     [Service]
     Environment="OLLAMA_HOST=0.0.0.0"
     ```
     then `sudo systemctl restart ollama`.

   Only do this on a network you trust: anyone on it can use your Ollama.
4. On the settings page, use the address `http://host.docker.internal:11434`
   for Ollama on the same computer (the Compose file makes this work on Linux
   too), or the other machine's address, e.g. `http://192.168.1.20:11434`.

## What gets sent

Each feature sends only what it needs, only when it runs, and only to the
service you chose:

| Feature | Sent |
|---|---|
| Sorting purchases | One merchant name from a card transaction (e.g. `JOES CAFE`, with processor prefixes and store numbers stripped) and the category list. No amounts, dates or account details. Two requests per merchant: the category is only used when both answers agree. |
| Spending tips | Card spending totals by category per month, and the top 20 merchants by total with counts. |
| Advisor reports | A summary of your finances: account balances, budget and spending by category, recurring bills and income, debts, investments, net worth history, and the household profile if you configured one (`ADVISOR_PROFILE_FILE`). No account numbers or passwords. |

The settings page asks you to confirm this for each cloud service before it
saves.

## What it costs

The settings page shows a rough monthly estimate next to each model, for a
typical household (a few card statements, weekly and monthly reports). With
the cheaper models it's a few cents a month; with the most capable ones,
roughly $1–3. Your provider's usage page shows the real figure, and every
provider above lets you set a spending limit.

## Your API key

- It's stored in the app's database encrypted with a key derived from
  `SECRET_KEY` (Fernet). Without `SECRET_KEY` it can't be read back, so the
  page refuses to save a key when `SECRET_KEY` isn't set. The installer sets
  it for you.
- If `SECRET_KEY` changes, the saved key becomes unreadable and AI turns off
  until you paste the key again.
- The `ai_settings` collection is left out of **Admin → Full Backup (JSON)**
  and is never overwritten by an import. After restoring a backup, set AI up
  again. (`mongodump` backups do contain it, still encrypted.)
- The page never shows the key again, only its last four characters.
- A saved key is only used for the service it was entered for. Switching
  services asks for that service's key.
- **Turn off and delete the saved key** removes it. If a key ever leaks,
  delete it on the provider's website as well.
- The app has no login: anyone who can open it can use the AI features (and
  spend your credit), though they can't read the key. Keep the app private
  (see [deployment.md](deployment.md#reverse-proxy-and-sso)) and set a spend
  limit with your provider.

## Troubleshooting

"Save and test" (and **Test the connection again**) show the provider's
answer in plain words. The common ones:

| Message | Fix |
|---|---|
| rejected the API key | Copy the whole key again; make sure it's for the service you picked. |
| no credit / prepaid credit has run out | Buy credit on the provider's billing page. |
| spend limit has been reached | Raise the limit, or wait for next month. |
| does not know that model | Pick another model in step 3. |
| rate limiting this key | Wait a minute. New accounts start with low limits. |
| Could not reach Ollama | Check Ollama is running and exposed to the network (step 3 of its guide), and the address. |
| Ollama does not have the model | Run `ollama pull <model>` on the Ollama computer. |

Background failures (categorization, tips, reports) are logged with
`docker compose logs web`; the feature simply does nothing that time.

## How it works (for developers)

- `ai.py` is the only place that talks to a provider. `ai.generate(purpose,
  prompt, system=None, schema=None)` returns text, or a parsed object when a
  JSON schema is given. Purposes are `categorize`, `tips` and `report`, each
  with its own model (`model_fast` / `model_report`), output-token budget,
  reasoning effort and timeout. Errors raise `ai.AIError` with a user-safe
  message.
- Providers:
  - **Claude**: the official `anthropic` SDK, `output_config` with `effort`
    and `format: json_schema`; Opus/Sonnet use `fallbacks: "default"` (beta
    `server-side-fallback-2026-07-01`). Refusals raise `AIError`.
  - **OpenAI**: Chat Completions (no conversation state stored by OpenAI),
    `response_format: json_schema` with `strict: true`, `reasoning_effort`
    (`none` for categorization on GPT-6 Luna). No `temperature`.
  - **Gemini**: `v1beta/models/{model}:generateContent` with the
    `x-goog-api-key` header, `generationConfig.responseFormat` for JSON (falling
    back to `responseMimeType` + `responseJsonSchema` if rejected) and
    `thinkingConfig.thinkingLevel` (`minimal` on Flash-Lite for small tasks).
    Thought parts are skipped.
  - **Ollama**: `/api/chat` with `format: <schema>` and `think: false`.
- Settings: the `ai_settings` collection, one document `_id: 'ai'` with
  `provider`, `api_key_enc`, `api_key_hint`, `model_fast`, `model_report`,
  `ollama_url`, `features` (`categorize`, `tips`, `report`,
  `scheduled_reports`), `consent_provider`, `consent_at`, `last_test`.
  The web app and the MCP server read the same document (both need
  `SECRET_KEY`).
- Before the settings page existed, AI was configured with environment
  variables (`OLLAMA_URL`, `OLLAMA_MODEL`, `OLLAMA_API_KEY`, `CC_LLM_ENABLED`,
  `CC_LLM_TIMEOUT`, `AGENT_OLLAMA_HOST`, `AGENT_OLLAMA_MODEL`,
  `AGENT_OLLAMA_API_KEY`, `AGENT_OLLAMA_TIMEOUT`, `ADVISOR_ENABLED`). They
  still apply while no settings have been saved; the first save on the page
  takes over.
- The report scheduler thread always runs and checks
  `ai.scheduled_reports_enabled()` at each wake-up (Mondays and the 1st,
  06:00, plus a catch-up at startup).
- Tests (`tests/test_ai_settings.py`, `tests/test_ai_optional.py`) mock every
  provider; CI never calls a real one.
- To add a provider: add it to `PROVIDERS` (models with prices, defaults, key
  prefixes), write a `_call_<name>(cfg, prompt, *, system, schema,
  max_tokens, effort)` that returns text and raises `AIError`, register it in
  `_CALLERS`, and add a guide template `templates/ai_guide_<name>.html`.
