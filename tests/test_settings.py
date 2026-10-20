"""wf's settings file, ~/.wf/config.yaml (settings.py), in the WF_HOME conftest makes per test."""

import re

import pytest

from workflow import settings


def test_a_missing_or_empty_file_has_no_settings(wf_home):
    assert settings.get("max_agents") is None
    (wf_home / "config.yaml").write_text("")
    assert settings.get("max_agents") is None


def test_each_feature_reads_its_own_setting(monkeypatch, wf_home):
    monkeypatch.setattr(settings, "KNOWN", ("max_agents", "tools"))
    (wf_home / "config.yaml").write_text("tools: [git]\nmax_agents: 2\n")
    assert settings.get("max_agents") == 2
    assert settings.get("tools") == ["git"]
    (wf_home / "config.yaml").write_text("tools: [git]\n")
    assert settings.get("max_agents") is None


@pytest.mark.parametrize("text, problem", [
    ("max_agent: 4\n", "unknown setting max_agent (known: max_agents, cursor_runtime, cursor_api_key_file)"),
    ("- 4\n", "must hold settings by name"),
    ("max_agents: [\n", "can't read"),
])
def test_a_wrong_file_fails_every_read(wf_home, text, problem):
    (wf_home / "config.yaml").write_text(text)
    with pytest.raises(settings.SettingsError, match=re.escape(problem)):
        settings.get("max_agents")


def test_only_a_known_setting_can_be_read():
    with pytest.raises(ValueError):
        settings.get("max_agent")


def test_a_setting_written_with_no_value_is_there(wf_home):
    (wf_home / "config.yaml").write_text("max_agents:\n")
    assert settings.get("max_agents", missing="unset") is None
    (wf_home / "config.yaml").write_text("")
    assert settings.get("max_agents", missing="unset") == "unset"
