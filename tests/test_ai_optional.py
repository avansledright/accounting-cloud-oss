"""The AI features are optional: with no provider configured, nothing tries
to reach one and the pages still work."""
from unittest import mock

import pytest


@pytest.fixture
def no_ai_env(monkeypatch):
    for var in ('OLLAMA_URL', 'AGENT_OLLAMA_HOST', 'ADVISOR_ENABLED'):
        monkeypatch.delenv(var, raising=False)


def test_ai_off_by_default(app_module, db, no_ai_env):
    import ai
    assert ai.load_settings() is None
    for purpose in ai.PURPOSES:
        assert ai.enabled(purpose) is False
    assert ai.scheduled_reports_enabled() is False
    assert ai.status()['provider'] == 'none'


def test_no_ai_calls_when_off(app_module, db, no_ai_env):
    import agent.reports
    with mock.patch('requests.post') as post, mock.patch('requests.get') as get, \
            mock.patch('anthropic.Anthropic') as claude:
        assert app_module.get_ai_savings_tips({}, '2026-10') == []
        assert agent.reports._call_ollama('hello') is None
        assert app_module._llm_vote('JOES CAFE', app_module.CC_LLM_VOTE_PROMPTS[0]) is None
        app_module._categorize_leftovers_async()
    post.assert_not_called()
    get.assert_not_called()
    claude.assert_not_called()


def test_reports_page_explains_how_to_enable(client, no_ai_env):
    page = client.get('/reports')
    assert page.status_code == 200
    assert b'optional and turned off' in page.data
    assert b'Regenerate now' not in page.data


def test_generate_does_nothing_when_off(app_module, client, no_ai_env):
    with mock.patch.object(app_module, '_run_report_async') as run:
        resp = client.post('/api/reports/generate', data={'report_type': 'all'})
    assert resp.status_code == 302
    run.assert_not_called()


def test_env_ollama_still_works_without_saved_settings(app_module, db, monkeypatch):
    import ai
    monkeypatch.setenv('AGENT_OLLAMA_HOST', 'http://ollama:11434')
    monkeypatch.delenv('OLLAMA_URL', raising=False)
    assert ai.enabled('report') is True
    assert ai.enabled('categorize') is False
    assert ai.get_config('report')['base_url'] == 'http://ollama:11434'


def test_saved_settings_override_env(app_module, db, monkeypatch):
    import ai
    monkeypatch.setenv('OLLAMA_URL', 'http://ollama:11434')
    ai.save_settings({'provider': 'none'})
    assert ai.enabled('categorize') is False


def test_reports_page_shows_generate_when_configured(client, monkeypatch):
    monkeypatch.setenv('AGENT_OLLAMA_HOST', 'http://ollama:11434')
    page = client.get('/reports')
    assert b'Regenerate now' in page.data
