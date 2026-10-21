"""Optional AI features, in one place.

Every AI feature goes through this module:

    categorize  merchant categorization of card transactions (tiny, structured)
    tips        spending tips on the Cards page (medium, structured)
    report      advisor reports (larger, Markdown)

Providers: Claude (Anthropic), ChatGPT (OpenAI), Gemini (Google) and Ollama
(a model you run yourself). Nothing is sent anywhere unless a provider is set.

Configuration lives in the ``ai_settings`` collection (one document,
``_id: 'ai'``), edited on the AI settings page. The API key is stored
encrypted with a key derived from SECRET_KEY, and the collection is left out
of the JSON export, so a backup file never carries a usable key.

Installs configured before the settings page used environment variables
(OLLAMA_URL, AGENT_OLLAMA_HOST, ...). Those still work: they apply only while
no settings have been saved on the page.
"""
import base64
import hashlib
import json
import logging
import os
from datetime import datetime

import requests

from agent.db import db

logger = logging.getLogger(__name__)

PURPOSES = ('categorize', 'tips', 'report')

# Provider catalogue: labels, models offered on the settings page and the
# defaults. 'fast' is used for categorization and tips, 'report' for advisor
# reports. Prices are USD per million input / output tokens, shown on the page.
PROVIDERS = {
    'anthropic': {
        'label': 'Claude (Anthropic)',
        'short': 'Claude',
        'company': 'Anthropic',
        'key_prefix': ('sk-ant-',),
        'models': [
            {'id': 'claude-opus-5-5', 'label': 'Claude Opus 5.5 (most capable)', 'price': (4.00, 20.00)},
            {'id': 'claude-sonnet-5-5', 'label': 'Claude Sonnet 5.5 (balanced)', 'price': (2.00, 10.00)},
            {'id': 'claude-haiku-5-5', 'label': 'Claude Haiku 5.5 (cheapest, fastest)', 'price': (0.10, 0.50)},
        ],
        'default_fast': 'claude-opus-5-5',
        'default_report': 'claude-opus-5-5',
    },
    'openai': {
        'label': 'ChatGPT (OpenAI)',
        'short': 'ChatGPT',
        'company': 'OpenAI',
        'key_prefix': ('sk-',),
        'models': [
            {'id': 'gpt-6-luna', 'label': 'GPT-6 Luna (cheapest, fastest)', 'price': (0.10, 0.50)},
            {'id': 'gpt-6.1-sol', 'label': 'GPT-6.1 Sol (more capable)', 'price': (2.00, 10.00)},
        ],
        'default_fast': 'gpt-6-luna',
        'default_report': 'gpt-6.1-sol',
    },
    'gemini': {
        'label': 'Gemini (Google)',
        'short': 'Gemini',
        'company': 'Google',
        # New AI Studio keys start with AQ. (since May 2026); older ones with AIza.
        'key_prefix': ('AQ.', 'AIza'),
        'models': [
            {'id': 'gemini-3.5-flash-lite', 'label': 'Gemini 3.5 Flash-Lite (fast, low cost)', 'price': (0.30, 2.50)},
            {'id': 'gemini-3.1-flash-lite', 'label': 'Gemini 3.1 Flash-Lite (cheapest)', 'price': (0.25, 1.50)},
            {'id': 'gemini-3.8-flash', 'label': 'Gemini 3.8 Flash (more capable)', 'price': (1.50, 7.50)},
        ],
        'default_fast': 'gemini-3.5-flash-lite',
        'default_report': 'gemini-3.8-flash',
    },
    'ollama': {
        'label': 'Ollama (runs on your own computer)',
        'short': 'Ollama',
        'company': None,
        'key_prefix': None,
        'models': [],          # free text: whatever the user has pulled
        'default_fast': 'granite4.2:3b',
        'default_report': 'gemma4:e4b',
    },
}

CLOUD_PROVIDERS = ('anthropic', 'openai', 'gemini')

# Per-purpose request shape. effort applies to providers with a reasoning
# control; max_tokens leaves room for reasoning on models that think.
_PURPOSE_LIMITS = {
    'categorize': {'max_tokens': 2000, 'effort': 'low', 'timeout': 60},
    'tips': {'max_tokens': 4000, 'effort': 'low', 'timeout': 120},
    'report': {'max_tokens': 16000, 'effort': 'medium', 'timeout': 600},
}


class AIError(Exception):
    """A request to the AI provider failed. ``str(e)`` is safe to show users."""


# ---------------------------------------------------------------- key storage

def _fernet():
    from cryptography.fernet import Fernet
    secret = os.environ.get('SECRET_KEY', '')
    if not secret:
        return None
    key = base64.urlsafe_b64encode(hashlib.sha256(('ai-settings:' + secret).encode()).digest())
    return Fernet(key)


def secret_key_configured():
    """False when SECRET_KEY is unset: the app then makes a random one at each
    start, so a stored API key could not be read back after a restart."""
    return bool(os.environ.get('SECRET_KEY', ''))


def encrypt_key(plain):
    f = _fernet()
    if not f:
        raise AIError('SECRET_KEY is not set, so the API key cannot be stored safely.')
    return f.encrypt(plain.encode()).decode()


def decrypt_key(token):
    if not token:
        return ''
    f = _fernet()
    if not f:
        return ''
    try:
        return f.decrypt(token.encode()).decode()
    except Exception:
        # SECRET_KEY changed since the key was saved.
        return ''


def key_hint(plain):
    return plain[-4:] if plain and len(plain) > 8 else ''


# ------------------------------------------------------------------- settings

def load_settings():
    """The saved settings document, or None if the page was never saved."""
    return db.ai_settings.find_one({'_id': 'ai'})


def save_settings(fields):
    fields = dict(fields, updated_at=datetime.now())
    db.ai_settings.update_one({'_id': 'ai'}, {'$set': fields}, upsert=True)


def default_features():
    return {'categorize': True, 'tips': True, 'report': True, 'scheduled_reports': False}


def _env_config(purpose):
    """Pre-settings-page configuration from environment variables (Ollama only)."""
    if purpose == 'report':
        host = os.environ.get('AGENT_OLLAMA_HOST', '').strip().rstrip('/')
        if not host:
            return None
        return {'provider': 'ollama', 'base_url': host,
                'model': os.environ.get('AGENT_OLLAMA_MODEL', 'gemma4:e4b'),
                'api_key': os.environ.get('AGENT_OLLAMA_API_KEY', ''),
                'timeout': int(os.environ.get('AGENT_OLLAMA_TIMEOUT', '3600'))}
    url = os.environ.get('OLLAMA_URL', '').strip().rstrip('/')
    if not url:
        return None
    if purpose == 'categorize' and os.environ.get('CC_LLM_ENABLED', 'true').lower() != 'true':
        return None
    timeout = int(os.environ.get('CC_LLM_TIMEOUT', '60')) if purpose == 'categorize' else 90
    return {'provider': 'ollama', 'base_url': url,
            'model': os.environ.get('OLLAMA_MODEL', 'granite4.2:3b'),
            'api_key': os.environ.get('OLLAMA_API_KEY', ''), 'timeout': timeout}


def config_from_settings(s, purpose):
    """Resolve one purpose's request config from a settings document (saved or
    proposed). Returns None when that purpose is off."""
    provider = (s or {}).get('provider') or 'none'
    if provider not in PROVIDERS:
        return None
    features = {**default_features(), **(s.get('features') or {})}
    if not features.get(purpose):
        return None
    info = PROVIDERS[provider]
    model = s.get('model_report' if purpose == 'report' else 'model_fast') \
        or info['default_report' if purpose == 'report' else 'default_fast']
    if not model:
        return None
    cfg = {'provider': provider, 'model': model, 'timeout': _PURPOSE_LIMITS[purpose]['timeout']}
    if provider == 'ollama':
        url = (s.get('ollama_url') or '').strip().rstrip('/')
        if not url:
            return None
        cfg.update(base_url=url, api_key=decrypt_key(s.get('api_key_enc')))
        if purpose == 'report':
            cfg['timeout'] = 3600       # local models on CPU can be very slow
    else:
        if s.get('consent_provider') != provider:
            return None                 # the privacy notice was not accepted for this provider
        key = s.get('api_key') or decrypt_key(s.get('api_key_enc'))
        if not key:
            return None
        cfg['api_key'] = key
    return cfg


def get_config(purpose):
    s = load_settings()
    if s is None:
        return _env_config(purpose)
    return config_from_settings(s, purpose)


def enabled(purpose):
    return get_config(purpose) is not None


def scheduled_reports_enabled():
    s = load_settings()
    if s is None:
        return (os.environ.get('ADVISOR_ENABLED', 'false').lower() == 'true'
                and _env_config('report') is not None)
    features = {**default_features(), **(s.get('features') or {})}
    return bool(features.get('scheduled_reports')) and get_config('report') is not None


def status():
    """Summary for pages: which provider is on and for what."""
    s = load_settings()
    if s is None:
        source = 'env' if (_env_config('categorize') or _env_config('report')) else None
        provider = 'ollama' if source else 'none'
    else:
        source = 'settings'
        provider = s.get('provider') or 'none'
    return {
        'provider': provider,
        'label': PROVIDERS.get(provider, {}).get('label', 'Off'),
        'source': source,
        'purposes': {p: enabled(p) for p in PURPOSES},
        'scheduled_reports': scheduled_reports_enabled(),
    }


# ---------------------------------------------------------------- generation

def generate(purpose, prompt, *, system=None, schema=None, config=None):
    """Run one request. Returns the parsed JSON object when ``schema`` is
    given, else the response text. Raises AIError (user-safe message), or
    AIError('off') style errors when the purpose is not enabled."""
    cfg = config or get_config(purpose)
    if not cfg:
        raise AIError('AI is turned off for this feature.')
    limits = _PURPOSE_LIMITS[purpose]
    call = _CALLERS[cfg['provider']]
    text = call(cfg, prompt, system=system, schema=schema,
                max_tokens=limits['max_tokens'], effort=limits['effort'])
    if schema is None:
        return text
    try:
        return json.loads(_strip_fences(text))
    except (TypeError, ValueError):
        raise AIError('The AI reply was not valid JSON.')


def _strip_fences(text):
    t = (text or '').strip()
    if t.startswith('```'):
        t = t.split('\n', 1)[1] if '\n' in t else ''
        if t.rstrip().endswith('```'):
            t = t.rstrip()[:-3]
    return t.strip()


TEST_SCHEMA = {
    'type': 'object',
    'properties': {'answer': {'type': 'string', 'enum': ['OK', 'NO']}},
    'required': ['answer'],
    'additionalProperties': False,
}


def test_connection(settings):
    """Send one tiny structured request with the given (unsaved) settings.
    Returns (ok, message)."""
    cfg = config_from_settings(dict(settings, features={'categorize': True}), 'categorize')
    if not cfg:
        return False, 'Choose a provider and enter its API key (and accept the privacy notice) first.'
    try:
        out = generate('categorize', 'Reply with the answer OK.', schema=TEST_SCHEMA, config=cfg)
    except AIError as e:
        return False, str(e)
    except Exception as e:      # unexpected: keep the page working
        logger.exception('AI test failed')
        return False, f'Unexpected error: {e}'
    if isinstance(out, dict) and out.get('answer') in ('OK', 'NO'):
        return True, f"Connected to {PROVIDERS[cfg['provider']]['short']} ({cfg['model']})."
    return False, 'The provider answered, but not in the expected format.'


# ------------------------------------------------------------------ providers

def _http_error(provider, resp):
    """Turn a provider's HTTP error into a message a non-technical user can act on."""
    name = PROVIDERS[provider]['short']
    try:
        detail = resp.json()
        detail = (detail.get('error') or {}).get('message') if isinstance(detail.get('error'), dict) \
            else detail.get('error') or detail.get('message')
    except Exception:
        detail = (resp.text or '')[:200]
    code = resp.status_code
    if code in (401, 403):
        hint = f'{name} rejected the API key. Check that you copied the whole key.'
    elif code == 402:
        hint = f'{name} says there is a billing problem. Add credit or a payment method to your account.'
    elif code == 404:
        hint = f'{name} does not know that model, or your account cannot use it. Pick another model.'
    elif code == 429:
        hint = (f'{name} is rate limiting this key or the account is out of credit/quota. '
                'Check billing, or wait a minute and try again.')
    elif code >= 500:
        hint = f'{name} is having problems right now. Try again later.'
    else:
        hint = f'{name} returned an error ({code}).'
    return AIError(f'{hint} ({detail})' if detail else hint)


def _call_ollama(cfg, prompt, *, system, schema, max_tokens, effort):
    messages = ([{'role': 'system', 'content': system}] if system else []) + \
        [{'role': 'user', 'content': prompt}]
    body = {
        'model': cfg['model'],
        'messages': messages,
        'stream': False,
        # Reasoning tokens dominate the cost on small local models and nothing
        # here needs them (measured; see app.py categorization notes).
        'think': False,
        'options': {'temperature': 0 if schema else 0.4, 'num_ctx': 8192 if effort == 'medium' else 2048},
    }
    if schema:
        body['format'] = schema
    headers = {'Authorization': f"Bearer {cfg['api_key']}"} if cfg.get('api_key') else {}
    try:
        resp = requests.post(f"{cfg['base_url']}/api/chat", json=body, headers=headers,
                             timeout=cfg['timeout'])
    except requests.RequestException as e:
        raise AIError(f"Could not reach Ollama at {cfg['base_url']} ({e.__class__.__name__}).")
    if resp.status_code != 200:
        raise _ollama_error(cfg, resp)
    return (resp.json().get('message') or {}).get('content', '')


def _ollama_error(cfg, resp):
    if resp.status_code == 404:
        return AIError(f"Ollama does not have the model '{cfg['model']}'. "
                       f"Run 'ollama pull {cfg['model']}' on that computer.")
    return AIError(f'Ollama returned an error ({resp.status_code}): {(resp.text or "")[:200]}')


# Claude models that accept server-side refusal fallbacks ("default" form).
_ANTHROPIC_FALLBACK_MODELS = ('claude-opus-5-5', 'claude-sonnet-5-5')


def _call_anthropic(cfg, prompt, *, system, schema, max_tokens, effort):
    import anthropic

    client = anthropic.Anthropic(api_key=cfg['api_key'], timeout=cfg['timeout'], max_retries=2)
    output_config = {'effort': effort}
    if schema:
        output_config['format'] = {'type': 'json_schema', 'schema': schema}
    kwargs = {
        'model': cfg['model'],
        'max_tokens': max_tokens,
        'messages': [{'role': 'user', 'content': prompt}],
        'output_config': output_config,
    }
    if system:
        kwargs['system'] = system
    try:
        if cfg['model'] in _ANTHROPIC_FALLBACK_MODELS:
            # If a safety classifier declines, re-run on Anthropic's
            # recommended fallback model instead of failing.
            response = client.beta.messages.create(
                betas=['server-side-fallback-2026-07-01'], fallbacks='default', **kwargs)
        else:
            response = client.messages.create(**kwargs)
    except anthropic.AuthenticationError:
        raise AIError('Claude rejected the API key. Check that you copied the whole key (it starts with sk-ant-).')
    except anthropic.PermissionDeniedError as e:
        raise AIError(f'This API key is not allowed to use {cfg["model"]}. ({e.message})')
    except anthropic.NotFoundError:
        raise AIError(f'Claude does not know the model {cfg["model"]}. Pick another model.')
    except anthropic.RateLimitError:
        raise AIError('Claude is rate limiting this key. Wait a minute and try again.')
    except anthropic.BadRequestError as e:
        msg = e.message or ''
        if 'usage limits' in msg.lower():
            raise AIError('Your Claude spend limit has been reached. Raise it under Billing → Spend limits '
                          'in the Claude Console, or wait for next month.')
        if 'credit' in msg.lower() or 'billing' in msg.lower():
            raise AIError('Your Anthropic account has no credit. Buy credits under Billing in the Claude Console.')
        raise AIError(f'Claude rejected the request: {msg}')
    except anthropic.APIStatusError as e:
        if e.status_code == 402:
            raise AIError('Your Anthropic account has a billing problem. Check Billing in the Claude Console.')
        raise AIError(f'Claude is having problems right now ({e.status_code}). Try again later.')
    except anthropic.APIConnectionError:
        raise AIError('Could not reach Claude. Check this computer\'s internet connection.')
    if response.stop_reason == 'refusal':
        raise AIError('Claude declined this request.')
    if response.stop_reason == 'max_tokens':
        logger.warning('Claude reply hit max_tokens (%s)', max_tokens)
    return ''.join(b.text for b in response.content if b.type == 'text')


def _post_json(provider, url, body, headers, timeout):
    name = PROVIDERS[provider]['short']
    try:
        resp = requests.post(url, json=body, headers=headers, timeout=timeout)
    except requests.Timeout:
        raise AIError(f'{name} took too long to answer. Try again later.')
    except requests.RequestException:
        raise AIError(f"Could not reach {name}. Check this computer's internet connection.")
    return resp


# OpenAI: Chat Completions, which keeps no conversation state on OpenAI's side
# (the Responses API stores it for 30 days unless told not to). Reasoning
# effort: gpt-6-luna accepts 'none'; gpt-6.1-sol starts at 'low'.
_OPENAI_NO_REASONING_MODELS = ('gpt-6-luna',)


def _call_openai(cfg, prompt, *, system, schema, max_tokens, effort):
    model = cfg['model']
    if effort == 'low' and max_tokens <= 2000 and model in _OPENAI_NO_REASONING_MODELS:
        effort = 'none'                 # categorization: no reasoning needed
    body = {
        'model': model,
        'messages': ([{'role': 'system', 'content': system}] if system else [])
                    + [{'role': 'user', 'content': prompt}],
        'max_completion_tokens': max_tokens,
        'reasoning_effort': effort,
        # No temperature: reasoning models reject it.
    }
    if schema:
        body['response_format'] = {'type': 'json_schema',
                                   'json_schema': {'name': 'result', 'strict': True, 'schema': schema}}
    resp = _post_json('openai', 'https://api.openai.com/v1/chat/completions', body,
                      {'Authorization': f"Bearer {cfg['api_key']}"}, cfg['timeout'])
    if resp.status_code != 200:
        raise _openai_error(resp)
    choice = (resp.json().get('choices') or [{}])[0]
    message = choice.get('message') or {}
    if message.get('refusal'):
        raise AIError('ChatGPT declined this request.')
    if choice.get('finish_reason') == 'length':
        logger.warning('ChatGPT reply hit max_completion_tokens (%s)', max_tokens)
    return message.get('content') or ''


def _openai_error(resp):
    try:
        err = resp.json().get('error') or {}
    except Exception:
        err = {}
    code = err.get('code') or err.get('type') or ''
    if resp.status_code == 429 and code in ('credit_balance_exhausted', 'insufficient_quota'):
        return AIError('Your OpenAI account has no credit left. Add credit under Settings → Billing '
                       'on platform.openai.com.')
    if resp.status_code == 429 and code.endswith('spend_limit_exceeded'):
        return AIError('Your OpenAI spending limit has been reached. Raise it under Settings → Limits, '
                       'or wait for next month.')
    if resp.status_code == 403:
        return AIError(f"OpenAI refused the request: {err.get('message') or 'not available'}")
    return _http_error('openai', resp)


# Gemini: generateContent (v1beta). Gemini 3 models always think a little;
# Flash-Lite accepts 'minimal', Flash starts at 'low'. Temperature is left at
# Google's recommended default.
_GEMINI_URL = 'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent'


def _call_gemini(cfg, prompt, *, system, schema, max_tokens, effort):
    model = cfg['model']
    level = effort
    if 'flash-lite' in model and effort == 'low':
        level = 'minimal'
    generation = {'maxOutputTokens': max_tokens, 'thinkingConfig': {'thinkingLevel': level}}
    body = {'contents': [{'role': 'user', 'parts': [{'text': prompt}]}], 'generationConfig': generation}
    if system:
        body['systemInstruction'] = {'parts': [{'text': system}]}
    if schema:
        generation['responseFormat'] = {'text': {'mimeType': 'APPLICATION_JSON', 'schema': schema}}
    url = _GEMINI_URL.format(model=model)
    headers = {'x-goog-api-key': cfg['api_key']}
    resp = _post_json('gemini', url, body, headers, cfg['timeout'])
    if schema and resp.status_code == 400 and 'responseFormat' in (resp.text or ''):
        # Older API surface: the previous structured-output fields.
        generation.pop('responseFormat')
        generation.update(responseMimeType='application/json', responseJsonSchema=schema)
        resp = _post_json('gemini', url, body, headers, cfg['timeout'])
    if resp.status_code != 200:
        raise _gemini_error(resp)
    data = resp.json()
    if (data.get('promptFeedback') or {}).get('blockReason'):
        raise AIError('Gemini declined this request.')
    candidate = (data.get('candidates') or [{}])[0]
    if candidate.get('finishReason') in ('SAFETY', 'RECITATION', 'PROHIBITED_CONTENT', 'BLOCKLIST'):
        raise AIError('Gemini declined this request.')
    if candidate.get('finishReason') == 'MAX_TOKENS':
        logger.warning('Gemini reply hit maxOutputTokens (%s)', max_tokens)
    parts = (candidate.get('content') or {}).get('parts') or []
    return ''.join(p.get('text', '') for p in parts if not p.get('thought'))


def _gemini_error(resp):
    try:
        err = resp.json().get('error') or {}
    except Exception:
        err = {}
    reasons = {d.get('reason') for d in err.get('details') or [] if isinstance(d, dict)}
    status = err.get('status') or ''
    if 'API_KEY_INVALID' in reasons or resp.status_code == 401 or 'ACCESS_TOKEN_TYPE_UNSUPPORTED' in reasons:
        return AIError('Gemini rejected the API key. Check that you copied the whole key from Google AI Studio.')
    if resp.status_code == 402:
        return AIError('Your Gemini prepaid credit has run out. Buy more at aistudio.google.com/billing.')
    if status == 'FAILED_PRECONDITION':
        return AIError('Gemini needs billing set up for this account (the free tier is not available '
                       'in every country). Set up billing in Google AI Studio.')
    return _http_error('gemini', resp)


_CALLERS = {
    'ollama': _call_ollama,
    'anthropic': _call_anthropic,
    'openai': _call_openai,
    'gemini': _call_gemini,
}
