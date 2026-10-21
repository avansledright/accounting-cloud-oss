"""The AI features are optional: with no LLM endpoint configured, nothing
tries to reach one and the pages still work."""
from unittest import mock


def test_llm_off_without_an_endpoint(app_module, monkeypatch):
    # The test environment sets no OLLAMA_URL / AGENT_OLLAMA_HOST.
    assert app_module.OLLAMA_URL == ''
    assert app_module.CC_LLM_ENABLED is False
    import agent.reports
    assert agent.reports.llm_configured() is False


def test_no_llm_calls_without_an_endpoint(app_module, db):
    import agent.reports
    with mock.patch('requests.post') as post, mock.patch('requests.get') as get:
        assert app_module.get_ollama_savings_tips({}, '2026-10') == []
        assert agent.reports._call_ollama('hello') is None
        app_module._categorize_leftovers_async()
    post.assert_not_called()
    get.assert_not_called()


def test_reports_page_explains_how_to_enable(client):
    page = client.get('/reports')
    assert page.status_code == 200
    assert b'optional and turned off' in page.data
    assert b'Regenerate now' not in page.data


def test_generate_does_nothing_without_an_endpoint(app_module, client):
    with mock.patch.object(app_module, '_run_report_async') as run:
        resp = client.post('/api/reports/generate', data={'report_type': 'all'})
    assert resp.status_code == 302
    run.assert_not_called()


def test_reports_page_shows_generate_when_configured(app_module, client):
    import agent.reports
    with mock.patch.object(agent.reports, 'OLLAMA_HOST', 'http://ollama:11434'):
        page = client.get('/reports')
    assert b'Regenerate now' in page.data
