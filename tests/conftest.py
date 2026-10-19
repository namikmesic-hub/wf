"""Shared test setup: wf's own folder (~/.wf) goes to a fresh temp folder in every test,
outside the test's repo, as the real ~/.wf is."""

import pytest


@pytest.fixture(autouse=True)
def wf_home(tmp_path_factory, monkeypatch):
    home = tmp_path_factory.mktemp("wf-home")
    monkeypatch.setenv("WF_HOME", str(home))
    return home
