"""AI settings page, key storage and the provider calls (all mocked: CI never
calls a real provider)."""
import json
from types import SimpleNamespace
from unittest import mock

import pytest


@pytest.fixture
def secret(monkeypatch):
    monkeypatch.setenv('SECRET_KEY', 'test-secret-key')
    for var in ('OLLAMA_URL', 'AGENT_OLLAMA_HOST', 'ADVISOR_ENABLED'):
        monkeypatch.delenv(var, raising=False)


def _claude_response(text, stop_reason='end_turn'):
    return SimpleNamespace(stop_reason=stop_reason,
                           content=[SimpleNamespace(type='thinking', thinking=''),
                                    SimpleNamespace(type='text', text=text)])


def _form(**overrides):
    data = {
        'provider': 'anthropic', 'api_key': 'sk-ant-api03-abcdefghijklmnop',
        'model_fast_anthropic': 'claude-haiku-5-5', 'model_report_anthropic': 'claude-opus-5-5',
        'feature_categorize': 'on', 'feature_tips': 'on', 'feature_report': 'on',
        'consent': 'on', 'action': 'save',
    }
    data.update(overrides)
    return {k: v for k, v in data.items() if v is not None}


# ---- key storage

def test_key_is_encrypted_and_tied_to_secret_key(app_module, secret, monkeypatch):
    import ai
    token = ai.encrypt_key('sk-ant-secret-value-1234')
    assert 'sk-ant' not in token
    assert ai.decrypt_key(token) == 'sk-ant-secret-value-1234'
    monkeypatch.setenv('SECRET_KEY', 'a-different-secret')
    assert ai.decrypt_key(token) == ''          # unreadable, not a crash


def test_export_leaves_out_ai_settings(app_module, client, db, secret):
    import ai
    ai.save_settings({'provider': 'anthropic', 'api_key_enc': ai.encrypt_key('sk-ant-x' * 3)})
    data = client.get('/export').get_json()
    assert 'ai_settings' not in data


def test_import_does_not_touch_ai_settings(app_module, client, db, secret):
    import io
    import ai
    ai.save_settings({'provider': 'anthropic'})
    backup = json.dumps({'_meta': {}, 'ai_settings': [], 'accounts': []}).encode()
    client.post('/import', data={'file': (io.BytesIO(backup), 'b.json')},
                content_type='multipart/form-data')
    assert ai.load_settings()['provider'] == 'anthropic'


# ---- settings page

def test_page_renders_with_walkthroughs(client, secret):
    page = client.get('/settings/ai')
    assert page.status_code == 200
    for text in (b'Claude', b'ChatGPT', b'Gemini', b'Ollama', b'API key', b'What gets sent'):
        assert text in page.data


def test_save_encrypts_key_and_tests_connection(app_module, client, db, secret):
    import ai
    with mock.patch('anthropic.Anthropic') as cls:
        cls.return_value.messages.create.return_value = _claude_response('{"answer": "OK"}')
        resp = client.post('/settings/ai', data=_form())
    assert resp.status_code == 302
    saved = ai.load_settings()
    assert saved['provider'] == 'anthropic'
    assert 'sk-ant' not in json.dumps(saved, default=str)
    assert saved['api_key_hint'] == 'mnop'
    assert saved['last_test']['ok'] is True
    assert ai.enabled('categorize') and ai.enabled('report')
    assert ai.get_config('categorize')['model'] == 'claude-haiku-5-5'
    assert ai.get_config('report')['model'] == 'claude-opus-5-5'
    page = client.get(resp.headers['Location'])
    assert b'Connected to Claude' in page.data
    assert b'sk-ant-api03' not in page.data      # the key is never shown back


def test_save_requires_consent_for_cloud(app_module, client, db, secret):
    import ai
    resp = client.post('/settings/ai', data=_form(consent=None))
    assert resp.status_code == 400
    assert b'Tick the box' in resp.data
    assert ai.load_settings() is None


def test_save_rejects_wrong_kind_of_key(app_module, client, db, secret):
    resp = client.post('/settings/ai', data=_form(api_key='AIzaSyNotAClaudeKey12345'))
    assert resp.status_code == 400
    assert b'start with sk-ant-' in resp.data


def test_switching_provider_drops_the_old_key(app_module, client, db, secret):
    import ai
    with mock.patch('anthropic.Anthropic') as cls:
        cls.return_value.messages.create.return_value = _claude_response('{"answer": "OK"}')
        client.post('/settings/ai', data=_form())
    # Same provider, blank key field: the saved key is kept.
    with mock.patch('anthropic.Anthropic') as cls:
        cls.return_value.messages.create.return_value = _claude_response('{"answer": "OK"}')
        resp = client.post('/settings/ai', data=_form(api_key=''))
    assert resp.status_code == 302
    assert ai.get_config('categorize')['api_key'] == 'sk-ant-api03-abcdefghijklmnop'
    # Different provider, blank key field: refused, the Claude key is not reused.
    resp = client.post('/settings/ai', data=_form(provider='gemini', api_key=''))
    assert resp.status_code == 400
    assert b'Paste your Gemini API key' in resp.data


def test_turn_off_and_remove_key(app_module, client, db, secret):
    import ai
    with mock.patch('anthropic.Anthropic') as cls:
        cls.return_value.messages.create.return_value = _claude_response('{"answer": "OK"}')
        client.post('/settings/ai', data=_form())
    client.post('/settings/ai', data={'action': 'off'})
    assert not ai.enabled('categorize')
    assert ai.load_settings()['api_key_enc']           # kept for turning back on
    client.post('/settings/ai', data={'action': 'remove_key'})
    assert not ai.load_settings()['api_key_enc']


def test_features_can_be_turned_off_individually(app_module, client, db, secret):
    import ai
    with mock.patch('anthropic.Anthropic') as cls:
        cls.return_value.messages.create.return_value = _claude_response('{"answer": "OK"}')
        client.post('/settings/ai', data=_form(feature_report=None, feature_scheduled_reports=None))
    assert ai.enabled('categorize') and not ai.enabled('report')
    assert not ai.scheduled_reports_enabled()


def test_failed_test_is_reported(app_module, client, db, secret):
    import anthropic
    import ai
    err = anthropic.AuthenticationError(
        'invalid x-api-key', response=mock.Mock(status_code=401, headers={}), body=None)
    with mock.patch('anthropic.Anthropic') as cls:
        cls.return_value.messages.create.side_effect = err
        resp = client.post('/settings/ai', data=_form())
    page = client.get(resp.headers['Location'])
    assert b'rejected the API key' in page.data
    assert ai.load_settings()['last_test']['ok'] is False


def test_ollama_needs_no_key(app_module, client, db, secret):
    import ai
    ok = mock.Mock(status_code=200)
    ok.json.return_value = {'message': {'content': '{"answer": "OK"}'}}
    with mock.patch('requests.post', return_value=ok) as post:
        resp = client.post('/settings/ai', data={
            'provider': 'ollama', 'ollama_url': 'http://localhost:11434/',
            'model_fast_ollama': 'granite4.2:3b', 'model_report_ollama': 'gemma4:e4b',
            'feature_categorize': 'on', 'action': 'save'})
    assert resp.status_code == 302
    assert post.call_args.args[0] == 'http://localhost:11434/api/chat'
    assert ai.get_config('categorize')['base_url'] == 'http://localhost:11434'


# ---- provider calls

def test_claude_request_shape(app_module, db, secret):
    import ai
    cfg = {'provider': 'anthropic', 'model': 'claude-opus-5-5', 'api_key': 'sk-ant-k', 'timeout': 60}
    with mock.patch('anthropic.Anthropic') as cls:
        cls.return_value.beta.messages.create.return_value = _claude_response('{"category": "Dining"}')
        out = ai.generate('categorize', 'Merchant: JOES CAFE',
                          schema={'type': 'object'}, config=cfg)
    assert out == {'category': 'Dining'}
    kwargs = cls.return_value.beta.messages.create.call_args.kwargs
    assert kwargs['model'] == 'claude-opus-5-5'
    assert kwargs['fallbacks'] == 'default'
    assert kwargs['betas'] == ['server-side-fallback-2026-07-01']
    assert kwargs['output_config'] == {'effort': 'low',
                                       'format': {'type': 'json_schema', 'schema': {'type': 'object'}}}
    assert 'temperature' not in kwargs and 'thinking' not in kwargs


def test_claude_haiku_has_no_fallbacks(app_module, db, secret):
    import ai
    cfg = {'provider': 'anthropic', 'model': 'claude-haiku-5-5', 'api_key': 'sk-ant-k', 'timeout': 60}
    with mock.patch('anthropic.Anthropic') as cls:
        cls.return_value.messages.create.return_value = _claude_response('Report text')
        assert ai.generate('report', 'Write a report', config=cfg) == 'Report text'
    kwargs = cls.return_value.messages.create.call_args.kwargs
    assert 'fallbacks' not in kwargs
    assert kwargs['output_config'] == {'effort': 'medium'}


def test_claude_refusal_is_an_error(app_module, db, secret):
    import ai
    cfg = {'provider': 'anthropic', 'model': 'claude-haiku-5-5', 'api_key': 'sk-ant-k', 'timeout': 60}
    with mock.patch('anthropic.Anthropic') as cls:
        cls.return_value.messages.create.return_value = _claude_response('', stop_reason='refusal')
        with pytest.raises(ai.AIError, match='declined'):
            ai.generate('report', 'x', config=cfg)


def test_categorization_uses_configured_provider(app_module, client, db, secret):
    with mock.patch('anthropic.Anthropic') as cls:
        cls.return_value.messages.create.return_value = _claude_response('{"answer": "OK"}')
        client.post('/settings/ai', data=_form())
    with mock.patch('anthropic.Anthropic') as cls:
        cls.return_value.messages.create.return_value = _claude_response('{"category": "Dining"}')
        assert app_module.categorize_with_llm('SQ *JOES CAFE 12345') == 'Dining'
    assert cls.return_value.messages.create.call_count == 2      # two votes


def test_savings_tips_use_configured_provider(app_module, client, db, secret):
    with mock.patch('anthropic.Anthropic') as cls:
        cls.return_value.messages.create.return_value = _claude_response('{"answer": "OK"}')
        client.post('/settings/ai', data=_form())
    tips = {'tips': [{'title': 'Cancel a streaming service', 'detail': 'Two overlap.',
                      'potential_savings': '$15/month'}]}
    with mock.patch('anthropic.Anthropic') as cls:
        cls.return_value.messages.create.return_value = _claude_response(json.dumps(tips))
        assert app_module.get_ai_savings_tips({}, '2026-10') == tips['tips']


def _resp(status, payload):
    r = mock.Mock(status_code=status, text=json.dumps(payload))
    r.json.return_value = payload
    return r


def test_openai_request_shape(app_module, db, secret):
    import ai
    cfg = {'provider': 'openai', 'model': 'gpt-6-luna', 'api_key': 'sk-proj-k', 'timeout': 60}
    ok = _resp(200, {'choices': [{'finish_reason': 'stop',
                                  'message': {'content': '{"category": "Dining"}', 'refusal': None}}]})
    with mock.patch('requests.post', return_value=ok) as post:
        out = ai.generate('categorize', 'Merchant: JOES CAFE', schema={'type': 'object'}, config=cfg)
    assert out == {'category': 'Dining'}
    url, body = post.call_args.args[0], post.call_args.kwargs['json']
    assert url == 'https://api.openai.com/v1/chat/completions'
    assert post.call_args.kwargs['headers'] == {'Authorization': 'Bearer sk-proj-k'}
    assert body['reasoning_effort'] == 'none'
    assert body['response_format']['json_schema']['strict'] is True
    assert 'temperature' not in body


def test_openai_report_effort_on_sol(app_module, db, secret):
    import ai
    cfg = {'provider': 'openai', 'model': 'gpt-6.1-sol', 'api_key': 'sk-k', 'timeout': 60}
    ok = _resp(200, {'choices': [{'message': {'content': 'Report'}}]})
    with mock.patch('requests.post', return_value=ok) as post:
        assert ai.generate('report', 'x', config=cfg) == 'Report'
    assert post.call_args.kwargs['json']['reasoning_effort'] == 'medium'
    assert 'response_format' not in post.call_args.kwargs['json']


def test_openai_out_of_credit_message(app_module, db, secret):
    import ai
    cfg = {'provider': 'openai', 'model': 'gpt-6-luna', 'api_key': 'sk-k', 'timeout': 60}
    err = _resp(429, {'error': {'message': 'no credit', 'type': 'insufficient_quota',
                                'code': 'credit_balance_exhausted'}})
    with mock.patch('requests.post', return_value=err):
        with pytest.raises(ai.AIError, match='no credit left'):
            ai.generate('report', 'x', config=cfg)


def test_gemini_request_shape(app_module, db, secret):
    import ai
    cfg = {'provider': 'gemini', 'model': 'gemini-3.5-flash-lite', 'api_key': 'AQ.key', 'timeout': 60}
    ok = _resp(200, {'candidates': [{'finishReason': 'STOP', 'content': {'parts': [
        {'text': 'thinking...', 'thought': True}, {'text': '{"category": "Dining"}'}]}}]})
    with mock.patch('requests.post', return_value=ok) as post:
        out = ai.generate('categorize', 'Merchant', schema={'type': 'object'}, config=cfg,
                          system='Be brief')
    assert out == {'category': 'Dining'}
    url, body = post.call_args.args[0], post.call_args.kwargs['json']
    assert url.endswith('/v1beta/models/gemini-3.5-flash-lite:generateContent')
    assert post.call_args.kwargs['headers'] == {'x-goog-api-key': 'AQ.key'}
    gen = body['generationConfig']
    assert gen['thinkingConfig'] == {'thinkingLevel': 'minimal'}
    assert gen['responseFormat']['text']['mimeType'] == 'APPLICATION_JSON'
    assert body['systemInstruction'] == {'parts': [{'text': 'Be brief'}]}
    assert 'temperature' not in gen


def test_gemini_falls_back_to_older_schema_fields(app_module, db, secret):
    import ai
    cfg = {'provider': 'gemini', 'model': 'gemini-3.8-flash', 'api_key': 'AIza-key', 'timeout': 60}
    bad = _resp(400, {'error': {'code': 400, 'status': 'INVALID_ARGUMENT',
                                'message': 'Unknown name "responseFormat"'}})
    ok = _resp(200, {'candidates': [{'content': {'parts': [{'text': '{"answer": "OK"}'}]}}]})
    with mock.patch('requests.post', side_effect=[bad, ok]) as post:
        assert ai.generate('tips', 'x', schema={'type': 'object'}, config=cfg) == {'answer': 'OK'}
    gen = post.call_args.kwargs['json']['generationConfig']
    assert gen['responseMimeType'] == 'application/json' and 'responseFormat' not in gen
    assert gen['thinkingConfig'] == {'thinkingLevel': 'low'}       # Flash: no 'minimal'


def test_gemini_bad_key_message(app_module, db, secret):
    import ai
    cfg = {'provider': 'gemini', 'model': 'gemini-3.8-flash', 'api_key': 'AQ.bad', 'timeout': 60}
    bad = _resp(400, {'error': {'code': 400, 'status': 'INVALID_ARGUMENT', 'message': 'API key not valid.',
                                'details': [{'reason': 'API_KEY_INVALID'}]}})
    with mock.patch('requests.post', return_value=bad):
        with pytest.raises(ai.AIError, match='rejected the API key'):
            ai.generate('report', 'x', config=cfg)


def test_gemini_accepts_new_and_old_key_formats(app_module, client, db, secret):
    ok = _resp(200, {'candidates': [{'content': {'parts': [{'text': '{"answer": "OK"}'}]}}]})
    for key in ('AQ.Ab12345678901234', 'AIzaSy12345678901234'):
        with mock.patch('requests.post', return_value=ok):
            resp = client.post('/settings/ai', data={
                'provider': 'gemini', 'api_key_gemini': key, 'consent': 'on', 'action': 'save',
                'model_fast_gemini': 'gemini-3.1-flash-lite', 'model_report_gemini': 'gemini-3.8-flash',
                'feature_categorize': 'on'})
        assert resp.status_code == 302, resp.data[:300]
