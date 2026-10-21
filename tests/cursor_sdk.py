"""The real Cursor host on a fake Cursor SDK (fake_cursor_sdk/), for the tests of Cursor's runtime.

host_copy() makes a folder with the host's files and the fake as node_modules/@cursor/sdk, and a
lockfile that pins the fake, so the harness finds what it needs installed. use() points the
`cursor` harness at such a folder with a scenario for the fake (see fake_cursor_sdk/index.js),
and keeps the fake's API key in the test's Keychain. The fake's log says what the host asked of
the SDK: entries(log, "create") are the options of each Agent.create, and so on.
"""

import json
import shutil
from pathlib import Path

import keyring
import keyring.backend
import pytest

from workflow import config, harnesses
from workflow.harnesses import Unavailable, cursor

FAKE = Path(__file__).parent / "fake_cursor_sdk"
KEY = "key_test"
REAL_WF_HOME = config.home()  # read before conftest moves WF_HOME for each test


def _node_ok() -> bool:
    try:
        cursor.find_node("node")  # on PATH: no test reads your config.toml
    except Unavailable:
        return False
    return True


needs_node = pytest.mark.skipif(not _node_ok(), reason="the Cursor host needs Node.js 22.18 or later")


def host_copy(folder: Path) -> Path:
    for name in (cursor.ENTRY, "package.json"):
        shutil.copy(cursor.HOST / name, folder / name)
    shutil.copytree(cursor.HOST / "src", folder / "src")
    shutil.copytree(FAKE, folder / "node_modules" / "@cursor" / "sdk")
    (folder / "package-lock.json").write_text(json.dumps(
        {"lockfileVersion": 3, "packages": {"": {"name": "wf-cursor-host"}, "node_modules/@cursor/sdk": {"version": "1.0.38"}}}))
    return folder


def use(monkeypatch, folder: Path, host: Path, *turns, key: str | None = KEY, acp=None, timeout: float = 10,
        **scenario) -> Path:
    """Run `cursor` sessions on the host in `host`, with the fake playing these turns. Returns its log."""
    path, log = folder / "sdk-scenario.json", folder / "sdk.jsonl"
    path.write_text(json.dumps({"turns": list(turns), **scenario}))
    monkeypatch.setenv("FAKE_CURSOR_SDK_SCENARIO", str(path))
    monkeypatch.setenv("FAKE_CURSOR_SDK_LOG", str(log))
    monkeypatch.setitem(harnesses.FACTORIES, "cursor", lambda: cursor.Cursor(host=host, request_timeout=timeout, acp=acp))
    if key is not None:
        keyring.set_password(*cursor.KEYCHAIN, key)
    return log


def entries(log: Path, kind: str) -> list:
    lines = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    return [e["data"] for e in lines if e["kind"] == kind]


def real_key() -> str | None:
    """The Cursor API key this machine has for wf, for the opt-in tests. It comes from the file that
    cursor.api_key_file in your config.toml names, or from your real Keychain. conftest gives each
    test an empty keyring."""
    try:
        file = config.load(REAL_WF_HOME).cursor.api_key_file
    except config.ConfigError:
        file = None
    if file is not None:
        try:
            return file.read_text(encoding="utf-8").strip() or None
        except OSError:
            return None
    real = [k for k in keyring.backend.get_all_keyring() if type(k).__name__ != "FakeKeyring"]
    if not real:
        return None
    try:
        return max(real, key=keyring.backend.by_priority).get_password(*cursor.KEYCHAIN) or None
    except Exception:
        return None


@pytest.fixture
def live_key(fake_keyring):
    """Your Cursor API key, put in the test's Keychain. The test skips without one."""
    key = real_key()
    if not key:
        pytest.skip(f"no Cursor API key for wf: {cursor.FIX}")
    fake_keyring.items[cursor.KEYCHAIN] = key
    return key
