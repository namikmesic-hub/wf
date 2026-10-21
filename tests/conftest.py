"""Shared test setup:
- wf's own folder (~/.wf) goes to a fresh temp folder in every test, outside the
  test's repo, as the real ~/.wf is;
- a cap on agent sessions set where the tests run (WF_MAX_AGENTS) doesn't apply to them;
- runs go in the repo's .wf/runs unless a test sets a runs folder itself;
- the runner's git and the tests' own git commands read none of your global or system
  config, so a global config that signs commits with a key that waits for a touch doesn't
  stop the tests' commits (the agents' git tool drops these variables, so it still reads
  your ~/.gitconfig);
- the keyring is an empty one in memory, so no test reads or writes your real Keychain.

pytest skips each test marked docker where `docker info` or `docker compose version` fails, or where
it can't reach grafana.com. The metrics stack downloads a Grafana plugin from there at each start."""

import shutil
import subprocess
import urllib.request

import keyring
import pytest
from keyring.backend import KeyringBackend


@pytest.hookimpl(trylast=True)  # after -m deselects tests, so a run without the docker tests never calls docker
def pytest_collection_modifyitems(items):
    marked = [item for item in items if item.get_closest_marker("docker")]
    lacking = _what_docker_tests_lack() if marked else None
    if lacking:
        for item in marked:
            item.add_marker(pytest.mark.skip(reason=f"needs {lacking}"))


def _what_docker_tests_lack() -> str | None:
    if shutil.which("docker") is None:
        return "Docker with Compose"
    try:
        if not all(subprocess.run(["docker", *args], capture_output=True, timeout=30).returncode == 0
                   for args in (["info"], ["compose", "version"])):
            return "Docker with Compose, and a Docker daemon that answers"
    except (OSError, subprocess.TimeoutExpired):
        return "Docker with Compose, and a Docker daemon that answers"
    try:
        urllib.request.urlopen("https://grafana.com/api/plugins/grafana-clickhouse-datasource", timeout=10).close()
    except OSError:
        return "the internet: the metrics stack downloads Grafana's ClickHouse plugin from grafana.com at each start"
    return None


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
