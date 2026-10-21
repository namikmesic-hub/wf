"""Shared test setup:
- wf's own folder (~/.wf) goes to a fresh temp folder in every test, outside the
  test's repo, as the real ~/.wf is. So no test reads your config.toml.
- The environment variables of config.toml's keys (WF_MAX_AGENTS, WF_RUNS_DIR, ...) that
  you set where the tests run don't apply to them.
- Runs go in the repo's .wf/runs unless a test sets a runs folder itself.
- Each test starts with umask 022, whatever the umask of your shell. wf refuses a config.toml
  that other users can change, so under umask 002 it would refuse each one that a test writes.
- The runner's git and the tests' own git commands read none of your global or system
  config. So a global config that signs commits with a key that waits for a touch doesn't
  stop the tests' commits. The agents' git tool drops these variables, so it still reads
  your ~/.gitconfig.
- The keyring is an empty one in memory, so no test reads or writes your real Keychain."""

import os

import keyring
import pytest
from keyring.backend import KeyringBackend

from workflow import config


@pytest.fixture(autouse=True)
def usual_umask():
    before = os.umask(0o022)
    yield
    os.umask(before)


@pytest.fixture(autouse=True)
def wf_home(tmp_path_factory, monkeypatch):
    home = tmp_path_factory.mktemp("wf-home")
    monkeypatch.setenv("WF_HOME", str(home))
    for key in config.KEYS.values():
        if key.variable:
            monkeypatch.delenv(key.variable, raising=False)
    return home


@pytest.fixture(autouse=True)
def no_git_config(tmp_path_factory, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path_factory.mktemp("git") / "config"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for name in ("GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS"):  # config set in the environment
        monkeypatch.delenv(name, raising=False)


class FakeKeyring(KeyringBackend):
    priority = 1

    def __init__(self):
        super().__init__()
        self.items: dict[tuple[str, str], str] = {}

    def get_password(self, service, username):
        return self.items.get((service, username))

    def set_password(self, service, username, password):
        self.items[(service, username)] = password

    def delete_password(self, service, username):
        self.items.pop((service, username), None)


@pytest.fixture(autouse=True)
def fake_keyring():
    previous = keyring.get_keyring()
    fake = FakeKeyring()
    keyring.set_keyring(fake)
    yield fake
    keyring.set_keyring(previous)


@pytest.fixture(scope="session")
def cursor_host(tmp_path_factory):
    """A copy of the Cursor host with the fake Cursor SDK beside it (tests/cursor_sdk.py)."""
    from cursor_sdk import host_copy

    return host_copy(tmp_path_factory.mktemp("cursor-host"))


from cursor_sdk import live_key  # noqa: E402,F401  (a fixture of the opt-in Cursor tests)
