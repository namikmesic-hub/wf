"""The Cursor host as a program: its protocol, how it is installed and found, which adapter runs
`cursor`, and its own TypeScript tests. The host runs on the fake Cursor SDK (tests/cursor_sdk.py)."""

import asyncio
import json
import os
import shutil
import stat
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import jsonschema
import keyring
import pytest

from cursor_sdk import KEY, entries, needs_node, use
from workflow import cli, harnesses, runner, schemas
from workflow.harnesses import Unavailable, cursor, remote
from workflow.harnesses.base import SessionSpec
from workflow.harnesses.cursor_acp import CursorAcp
from workflow.output_tool import ResultTool

PROTOCOL = cursor.HOST / "protocol"
VALIDATOR = jsonschema.Draft202012Validator(json.loads((PROTOCOL / "wf-session-0.schema.json").read_text()))


def problems(frame: dict) -> list[str]:
    return [error.message for error in VALIDATOR.iter_errors(frame)]


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / "repo").mkdir()
    monkeypatch.chdir(tmp_path / "repo")
    monkeypatch.setattr(runner, "interactive", lambda: False)
    subprocess.run(["git", "init", "-q"], check=True)
    return tmp_path


# ---------- the protocol ----------


def test_the_schema_is_valid_and_every_example_frame_meets_it():
    jsonschema.Draft202012Validator.check_schema(VALIDATOR.schema)
    examples = [json.loads(line) for line in (PROTOCOL / "examples.jsonl").read_text().splitlines()]
    assert {frame["t"] for frame in examples} == {"hello", "welcome", "req", "res", "ev", "call", "ret"}
    for frame in examples:
        assert problems(frame) == [], frame


@pytest.mark.parametrize("frame", [
    {"t": "ev", "seq": 1, "at": "x", "session": "ses_a", "kind": "turn.text", "data": {"text": "hi"}},  # no turn
    {"t": "res", "id": "r1", "ok": True},
    {"t": "req", "id": "r1", "op": "turn.start", "args": {"session": "ses_a"}},
    {"t": "hello", "protocol": "wf-session/1", "client": {"name": "wf"}},
    {"t": "res", "id": "r1", "ok": False, "error": {"class": "oops", "code": "x", "message": "x", "retryable": False}},
])
def test_the_schema_refuses_a_frame_that_breaks_the_protocol(frame):
    assert problems(frame)


@needs_node
def test_every_frame_of_a_run_meets_the_schema(repo, monkeypatch, cursor_host):
    frames = []
    use(monkeypatch, repo, cursor_host, [{"thinking": ["Hm."], "thinking_ms": 20}, {"text": "Looking."},
                                         {"tool": {"name": "git", "arguments": {"args": ["status"]}}},
                                         {"tool": {"name": "read_file", "arguments": {"path": "flow.yaml"}}},
                                         {"request": "req-from-the-stream"}, {"thinking": ["Cut off"]}],
        [{"tool": {"name": "submit_result", "arguments": {"summary": "hi"}}}])
    monkeypatch.setattr(remote.RemoteHarness, "tap", staticmethod(lambda way, frame: frames.append((way, frame))))
    Path("flow.yaml").write_text("""
version: 1
name: cu
defaults: {harness: cursor, harnesses: {cursor: {model: gpt-5.6-sol-high}}}
start: {input: {}}
nodes:
  ask: {needs: [start], instruction: Say hi}
end: {output: {out: "{{ ask.summary }}"}}
""")
    assert cli.main(["run", "flow.yaml"]) == 0
    kinds = {(way, frame["t"]) for way, frame in frames}
    assert kinds == {("out", "hello"), ("in", "welcome"), ("out", "req"), ("in", "res"), ("in", "ev"), ("in", "call"), ("out", "ret")}
    assert {frame["op"] for way, frame in frames if frame["t"] == "req"} == {
        "session.open", "turn.start", "usage.read", "session.close"}
    events = [frame for way, frame in frames if frame["t"] == "ev"]
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))  # numbered, in order
    assert {"turn.thinking", "turn.request"} <= {e["kind"] for e in events}
    assert all(e["session"].startswith("ses_") for e in events)
    after = [frame for way, frame in frames if frame["t"] not in ("hello", "welcome")
             and not (frame["t"] == "req" and frame["op"] == "session.open")]
    assert all(frame["session"].startswith("ses_") for frame in after)  # every frame names its session
    starts = [frame["args"]["kind"] for way, frame in frames if frame["t"] == "req" and frame["op"] == "turn.start"]
    assert starts == ["instruction", "reminder"]
    for way, frame in frames:
        assert problems(frame) == [], (way, frame)


@needs_node
def test_a_host_that_speaks_another_protocol_is_unavailable(tmp_path):
    host = tmp_path / "host"
    host.mkdir()
    (host / cursor.ENTRY).write_text(
        "process.stdin.once('data', () => process.stdout.write(JSON.stringify("
        "{t: 'welcome', protocol: 'wf-session/9', server: {name: 'x', version: '0'}, head: 0, replay: 'events'}) + '\\n'));\n")
    (host / "package-lock.json").write_text(json.dumps({"packages": {"": {}}}))

    async def main():
        harness = cursor.Cursor(host=host, request_timeout=5)
        await harness.open(tmp_path / "run")
        await harness.peer()

    with pytest.raises(Unavailable, match="the Cursor host speaks wf-session/9, and wf wf-session/0"):
        asyncio.run(main())


@needs_node
def test_the_hosts_own_tests_pass():
    done = subprocess.run([shutil.which("node"), "--test", "test/"], cwd=cursor.HOST, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stdout + done.stderr


# ---------- checking a model ----------


@needs_node
def test_a_model_check_asks_the_host_and_names_what_cursor_has(tmp_path, monkeypatch, cursor_host):
    use(monkeypatch, tmp_path, cursor_host)

    async def check(model):
        harness = harnesses.create("cursor")
        await harness.open(tmp_path / "run")
        await harness.check_model(model)

    asyncio.run(check("gpt-5.6-sol-max-fast"))
    with pytest.raises(Unavailable, match="Cursor has no model `nope`: this account's models are composer-2.5, gpt-5.6-sol, default"):
        asyncio.run(check("nope"))


@needs_node
def test_an_edit_node_or_one_with_no_api_key_has_its_model_checked_on_cursor_agent(tmp_path, monkeypatch, cursor_host):
    from fakes import Fake
    acp = Fake("cursor", models={"composer-2.5"})
    use(monkeypatch, tmp_path, cursor_host, acp=acp.factory, key=None)

    async def check(model, access):
        harness = harnesses.create("cursor")
        await harness.open(tmp_path / "run")
        try:
            await harness.check_model(model, access)
        finally:
            await harness.close()

    asyncio.run(check("composer-2.5", "edit"))  # no API key is needed on cursor-agent
    with pytest.raises(Unavailable, match="cursor has no model `gpt-9`"):
        asyncio.run(check("gpt-9", "edit"))
    with pytest.raises(Unavailable, match="cursor has no model `gpt-9`"):
        asyncio.run(check("gpt-9", "read-only"))  # with no API key, read-only runs on cursor-agent too
    keyring.set_password(*cursor.KEYCHAIN, "key_revoked")
    with pytest.raises(Unavailable, match="Cursor refused the API key: Invalid User API Key. Create a Cursor API key"):
        asyncio.run(check(None, "read-only"))  # with no model: can the SDK run at all


# ---------- installing and finding what the host needs ----------


def lockfile(folder: Path, **packages) -> None:
    (folder / "package-lock.json").write_text(json.dumps({"lockfileVersion": 3, "packages": {
        "": {"name": "wf-cursor-host"}, **{f"node_modules/{name}": entry for name, entry in packages.items()}}}))


def installed(folder: Path, name: str, version: str) -> None:
    (folder / "node_modules" / name).mkdir(parents=True, exist_ok=True)
    (folder / "node_modules" / name / "package.json").write_text(json.dumps({"name": name, "version": version}))


def test_what_the_lockfile_pins_and_isnt_installed_is_missing(tmp_path):
    lockfile(tmp_path, **{"@cursor/sdk": {"version": "1.0.38"}, "zod": {"version": "4.6.7"},
                          "typescript": {"version": "7.0.2", "dev": True},
                          "@cursor/sdk-linux-x64": {"version": "1.0.38", "optional": True}})
    assert cursor.missing(tmp_path) == ["@cursor/sdk@1.0.38", "zod@4.6.7"]
    installed(tmp_path, "@cursor/sdk", "1.0.38")
    installed(tmp_path, "zod", "4.6.6")
    assert cursor.missing(tmp_path) == ["zod@4.6.7"]  # another version is missing too
    installed(tmp_path, "zod", "4.6.7")
    assert cursor.missing(tmp_path) == []


def fake_npm(folder: Path, script: str) -> Path:
    """An `npm` that logs its arguments and runs `script` (sh) in the folder it runs in."""
    bin_dir = folder / "bin"
    bin_dir.mkdir()
    npm = bin_dir / "npm"
    npm.write_text(f'#!/bin/sh\necho "$PWD $*" >> {folder / "npm.log"}\n{script}\n')
    npm.chmod(npm.stat().st_mode | stat.S_IEXEC)
    return bin_dir


def test_the_first_session_installs_the_sdk_from_the_lockfile(tmp_path, monkeypatch):
    host = tmp_path / "host"
    host.mkdir()
    lockfile(host, **{"@cursor/sdk": {"version": "1.0.38"}})
    bin_dir = fake_npm(tmp_path, 'mkdir -p node_modules/@cursor/sdk && echo \'{"version": "1.0.38"}\' > node_modules/@cursor/sdk/package.json')
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("CURSOR_API_KEY", "key")
    asyncio.run(cursor.ensure(host, sys.executable))
    asyncio.run(cursor.ensure(host, sys.executable))  # installed: npm doesn't run again
    assert (tmp_path / "npm.log").read_text() == f"{host} ci --omit=dev --ignore-scripts --no-audit --no-fund --loglevel=error\n"


def test_this_machines_package_of_the_sdks_binaries_counts_as_needed(tmp_path, monkeypatch):
    monkeypatch.setattr(cursor.sys, "platform", "darwin")
    monkeypatch.setattr(cursor.platform, "machine", lambda: "arm64")
    lockfile(tmp_path, **{"@cursor/sdk-darwin-arm64": {"version": "1.0.38", "optional": True, "os": ["darwin"], "cpu": ["arm64"]},
                          "@cursor/sdk-linux-x64": {"version": "1.0.38", "optional": True, "os": ["linux"], "cpu": ["x64"]}})
    assert cursor.missing(tmp_path) == ["@cursor/sdk-darwin-arm64@1.0.38"]
    installed(tmp_path, "@cursor/sdk-darwin-arm64", "1.0.38")
    assert cursor.missing(tmp_path) == []


def test_a_cancelled_install_stops_npm(tmp_path, monkeypatch):
    host = tmp_path / "host"
    host.mkdir()
    lockfile(host, **{"@cursor/sdk": {"version": "1.0.38"}})
    bin_dir = fake_npm(tmp_path, f"echo $$ > {tmp_path / 'npm.pid'}; sleep 60")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    async def main():
        task = asyncio.ensure_future(cursor.ensure(host, sys.executable))
        while not (tmp_path / "npm.pid").exists():
            await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    began = time.monotonic()
    asyncio.run(main())
    assert time.monotonic() - began < 10
    pid = int((tmp_path / "npm.pid").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_an_install_that_fails_makes_cursor_unavailable_and_says_where(tmp_path, monkeypatch):
    host = tmp_path / "host"
    host.mkdir()
    lockfile(host, **{"@cursor/sdk": {"version": "1.0.38"}})
    bin_dir = fake_npm(tmp_path, "echo 'npm error network request failed' >&2; exit 1")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    with pytest.raises(Unavailable, match=f"`npm ci --omit=dev` in {host} didn't install Cursor's SDK: npm error network"):
        asyncio.run(cursor.ensure(host, sys.executable))


@pytest.mark.parametrize("typescript, ok", [("strip", True), ("transform", True), ("undefined", False), ("false", False)])
def test_node_must_say_that_it_runs_typescript(tmp_path, typescript, ok):
    node = tmp_path / "node"
    node.write_text(f'#!/bin/sh\nif [ "$1" = --version ]; then echo v23.1.0; else echo {typescript}; fi\n')
    node.chmod(0o755)
    if ok:
        assert cursor.find_node(str(node)) == str(node)
    else:
        with pytest.raises(Unavailable, match=f"which runs TypeScript as it is, and {node} \\(v23.1.0\\) doesn't"):
            cursor.find_node(str(node))
    with pytest.raises(Unavailable, match="there's no `no-such-node` on PATH"):
        cursor.find_node("no-such-node")


def test_paths_node_names_the_node_that_runs_the_host(tmp_path, wf_home):
    node = tmp_path / "node-22"
    node.write_text('#!/bin/sh\nif [ "$1" = --version ]; then echo v22.18.0; else echo strip; fi\n')
    node.chmod(0o755)
    (wf_home / "config.toml").write_text(f'[paths]\nnode = "{node}"\n')
    assert cursor.find_node() == str(node)
    (wf_home / "config.toml").write_text(f'[paths]\nnode = "{tmp_path / "gone"}"\n')
    with pytest.raises(Unavailable, match=f"and {tmp_path / 'gone'} \\(paths.node in {wf_home / 'config.toml'}\\) "
                                          "isn't a program that you may run"):
        cursor.find_node()


def test_the_wheel_ships_the_host_without_its_installed_packages(tmp_path):
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv builds the wheel")
    root = Path(__file__).parent.parent
    done = subprocess.run([uv, "build", "--wheel", "--out-dir", str(tmp_path), str(root)], capture_output=True, text=True, timeout=300)
    assert done.returncode == 0, done.stderr
    (wheel,) = tmp_path.glob("*.whl")
    names = zipfile.ZipFile(wheel).namelist()
    host = "workflow/harnesses/cursor_host/"
    for shipped in ("wf-cursor-host.js", "package.json", "package-lock.json", "src/main.ts", "src/cursor.ts",
                    "protocol/wf-session-0.schema.json"):
        assert host + shipped in names
    assert not any("node_modules" in name for name in names)


# ---------- which adapter runs `cursor` ----------


def test_the_sdk_runs_cursor_unless_a_setting_or_the_environment_asks_for_acp(monkeypatch, wf_home, capsys):
    assert isinstance(cursor.create(), cursor.Cursor)
    (wf_home / "config.toml").write_text('[cursor]\nruntime = "acp"\n')
    assert isinstance(cursor.create(), CursorAcp)
    monkeypatch.setenv("WF_CURSOR_RUNTIME", "sdk")  # the environment wins
    assert isinstance(cursor.create(), cursor.Cursor)
    monkeypatch.setenv("WF_CURSOR_RUNTIME", "cli")
    assert cli.main(["validate", "no-such.yaml"]) == 1  # wf starts no command with it
    assert capsys.readouterr().err == "wf validate: WF_CURSOR_RUNTIME must be sdk or acp, not `cli`\n"


@needs_node
def test_with_acp_chosen_a_run_uses_cursor_agent(repo, monkeypatch, cursor_host):
    from fakes import Fake, submit
    log = use(monkeypatch, repo, cursor_host)
    acp = Fake("cursor", submit({"summary": "on acp"}))
    monkeypatch.setitem(harnesses.FACTORIES, "cursor", lambda: cursor.create())
    monkeypatch.setattr("workflow.harnesses.cursor_acp.CursorAcp", lambda: acp.factory())
    monkeypatch.setenv("WF_CURSOR_RUNTIME", "acp")
    Path("flow.yaml").write_text("""
version: 1
name: cu
defaults: {harness: cursor, harnesses: {cursor: {model: composer-2.5}}}
start: {input: {}}
nodes:
  ask: {needs: [start], instruction: Say hi}
end: {output: {out: "{{ ask.summary }}"}}
""")
    assert cli.main(["run", "flow.yaml"]) == 0
    assert len(acp.specs) == 1 and entries(log, "loaded") == []


# ---------- wf runtimes cursor ----------


@needs_node
def test_signin_keeps_the_key_in_the_keychain_and_checks_it(monkeypatch, cursor_host, tmp_path, capsys, fake_keyring):
    use(monkeypatch, tmp_path, cursor_host, key=None)
    monkeypatch.setattr(cursor, "HOST", cursor_host)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("getpass.getpass", lambda prompt: f"  {KEY}\n")
    assert cli.main(["runtimes", "cursor", "signin"]) == 0
    assert fake_keyring.items[cursor.KEYCHAIN] == KEY
    out = capsys.readouterr().out
    assert "Saved the Cursor API key in the macOS Keychain: the key works: wf tests (someone@example.com), 3 models" in out
    assert "api_key_file" not in out


@needs_node
def test_signin_says_when_a_key_file_wins_over_the_keychain(monkeypatch, cursor_host, tmp_path, capsys, wf_home):
    use(monkeypatch, tmp_path, cursor_host, key=None)
    monkeypatch.setattr(cursor, "HOST", cursor_host)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("getpass.getpass", lambda prompt: KEY)
    (wf_home / "config.toml").write_text(f'[cursor]\napi_key_file = "{tmp_path / "old-key"}"\n')
    assert cli.main(["runtimes", "cursor", "signin"]) == 0
    out = capsys.readouterr().out
    assert (f"cursor.api_key_file in {wf_home / 'config.toml'} names {tmp_path / 'old-key'}, and Cursor sessions take the "
            "API key from there first. Remove api_key_file from [cursor]") in out


@needs_node
def test_signin_with_a_key_that_cursor_refuses_says_so(monkeypatch, cursor_host, tmp_path, capsys):
    use(monkeypatch, tmp_path, cursor_host, key=None)
    monkeypatch.setattr(cursor, "HOST", cursor_host)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("getpass.getpass", lambda prompt: "key_typo")
    keyring.set_password(*cursor.KEYCHAIN, KEY)  # the good key it had
    assert cli.main(["runtimes", "cursor", "signin"]) == 1
    err = capsys.readouterr().err
    assert "Cursor refused the API key: Invalid User API Key" in err and "The Keychain keeps what it had" in err
    assert keyring.get_password(*cursor.KEYCHAIN) == KEY


def test_signin_needs_a_terminal(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert cli.main(["runtimes", "cursor", "signin"]) == 1
    assert "run it at a terminal" in capsys.readouterr().err


@needs_node
def test_doctor_checks_node_the_host_the_key_and_how_model_ids_map(monkeypatch, cursor_host, tmp_path, capsys):
    use(monkeypatch, tmp_path, cursor_host)
    monkeypatch.setattr(cursor, "HOST", cursor_host)
    assert cli.main(["runtimes", "cursor", "doctor", "--model", "gpt-5.6-sol-max-fast", "--model", "nope"]) == 1
    out = capsys.readouterr().out
    assert "Node.js:" in out and "Cursor's SDK: 1.0.38, installed in" in out
    assert "API key: in the macOS Keychain" in out
    assert "the key works: wf tests (someone@example.com), 3 models" in out
    assert "gpt-5.6-sol-max-fast: gpt-5.6-sol [reasoning=max, fast=true, context=1m]" in out
    assert "nope: Cursor has no model `nope`" in out
    assert "cursor runs read-only and full sessions on the SDK, and edit sessions on cursor-agent's ACP" in out


@needs_node
def test_doctor_without_a_key_says_how_to_add_one(monkeypatch, cursor_host, tmp_path, capsys):
    use(monkeypatch, tmp_path, cursor_host, key=None)
    monkeypatch.setattr(cursor, "HOST", cursor_host)
    assert cli.main(["runtimes", "cursor", "doctor"]) == 1
    out = capsys.readouterr().out
    assert "API key: none" in out and "`wf runtimes cursor signin`" in out


# ---------- a session opened directly ----------


@needs_node
def test_closing_a_session_ends_its_host(tmp_path, monkeypatch, cursor_host):
    use(monkeypatch, tmp_path, cursor_host)

    async def main():
        harness = harnesses.create("cursor")
        await harness.open(tmp_path / "run")
        tool = ResultTool(schemas.object_schema({"summary": {"type": "string"}}))
        spec = SessionSpec(cwd=tmp_path, access="read-only", web=False, model="composer-2.5", effort=None, tool=tool,
                           log=tmp_path / "attempt-1.log", emit=lambda event: None)
        session = await harness.session(spec)
        process = session.peer.process
        await session.close()
        await harness.close()
        return process.returncode

    assert asyncio.run(main()) == 0  # it stopped by itself when wf closed its end
