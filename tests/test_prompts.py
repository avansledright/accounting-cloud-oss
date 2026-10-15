import importlib


def test_default_profile_is_generic(monkeypatch):
    monkeypatch.delenv('ADVISOR_PROFILE_FILE', raising=False)
    import agent.prompts as prompts
    importlib.reload(prompts)
    text = prompts.WEEKLY_INSIGHT_PROMPT.format(period='2030-W01', spending='-', budget='-', recurring='-')
    assert 'No written profile has been provided' in text


def test_profile_file_with_braces_is_escaped(tmp_path, monkeypatch):
    f = tmp_path / 'profile.md'
    f.write_text('## Household Profile\nClients: Pat {and} Lee\n')
    monkeypatch.setenv('ADVISOR_PROFILE_FILE', str(f))
    import agent.prompts as prompts
    importlib.reload(prompts)
    text = prompts.WEEKLY_INSIGHT_PROMPT.format(period='p', spending='s', budget='b', recurring='r')
    assert 'Clients: Pat {and} Lee' in text
