"""Shared test setup: wf's own folder (~/.wf) goes to a fresh temp folder in every test,
outside the test's repo, as the real ~/.wf is. A cap on agent sessions set where the
tests run (WF_MAX_AGENTS) doesn't apply to them. Runs go in the repo's .wf/runs unless a
test sets a runs folder itself. git reads none of your config, so a global config that signs
commits with a key that waits for a touch doesn't stop the tests' commits."""

import pytest


@pytest.fixture(autouse=True)
def wf_home(tmp_path_factory, monkeypatch):
    home = tmp_path_factory.mktemp("wf-home")
    monkeypatch.setenv("WF_HOME", str(home))
    monkeypatch.delenv("WF_MAX_AGENTS", raising=False)
    monkeypatch.delenv("WF_RUNS_DIR", raising=False)
    return home


@pytest.fixture(autouse=True)
def no_git_config(tmp_path_factory, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path_factory.mktemp("git") / "config"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for name in ("GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS"):  # config set in the environment
        monkeypatch.delenv(name, raising=False)
