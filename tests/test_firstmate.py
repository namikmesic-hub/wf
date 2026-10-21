"""The firstmate extension: process-event-adapter/1 over stdin/stdout, and the package wf writes.

firstmate's host sends one JSON request on stdin with a verb argument and reads
one JSON response from stdout. These tests speak that protocol to wf's adapter.
"""

import io
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from textwrap import dedent

import pytest

from workflow import cli, firstmate, runner, runs


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(runner, "interactive", lambda: False)
    monkeypatch.setattr(firstmate, "POLL_SECONDS", 1)
    subprocess.run(["git", "init", "-q"], check=True)
    return tmp_path


def call(monkeypatch, capsys, verb: str, request: dict) -> dict:
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(request)))
    assert firstmate.main(["wf-firstmate", verb]) == 0
    return json.loads(capsys.readouterr().out)


def invoke(operation: str, **data) -> dict:
    return {
        "schema": "firstmate.extension-request.v1", "request_id": "sha256:" + "a" * 64, "host_protocol": 1,
        "extension_id": firstmate.EXTENSION_ID, "extension_version": firstmate.VERSION,
        "package_digest": "sha256:" + "b" * 64, "capability": "process-event-adapter", "capability_version": 1,
        "adapter": "wf", "operation": operation, "input": data,
    }


def waiting_run(capsys, runs_dir: Path | None = None) -> Path:
    Path("ask.yaml").write_text(dedent("""
        version: 1
        name: ask
        start: {input: {}}
        nodes:
          approve: {needs: [start], human: "Go?", output: {ok: {type: boolean}}}
        end: {output: {ok: "{{ approve.ok }}"}}
    """))
    assert cli.main(["run", "ask.yaml", *(["--runs-dir", str(runs_dir)] if runs_dir else [])]) == 2
    capsys.readouterr()
    (folder,) = runs.runs_folder(Path.cwd(), runs_dir).iterdir()
    return folder.resolve()


def test_the_handshake_echoes_the_exact_identity(monkeypatch, capsys):
    request = {
        "schema": "firstmate.extension-handshake-request.v1", "request_id": "sha256:" + "c" * 64,
        "host_protocols": [1], "extension_id": firstmate.EXTENSION_ID, "extension_version": firstmate.VERSION,
        "package_digest": "sha256:" + "d" * 64,
        "capability": {"name": "process-event-adapter", "versions": [1], "adapter_names": ["wf"]},
    }
    assert call(monkeypatch, capsys, "handshake", request) == {
        "schema": "firstmate.extension-handshake-response.v1", "request_id": "sha256:" + "c" * 64,
        "extension_id": firstmate.EXTENSION_ID, "extension_version": firstmate.VERSION, "host_protocol": 1,
        "capability": "process-event-adapter", "capability_version": 1, "adapter_names": ["wf"],
    }


def test_poll_returns_a_runs_new_state_once(repo, monkeypatch, capsys):
    folder = waiting_run(capsys)
    reply = call(monkeypatch, capsys, "invoke", invoke("source.poll", source_id="ask-run", config_ref=f"wf-run:{folder}"))
    assert set(reply) == {"schema", "request_id", "ok", "result", "error"}
    assert (reply["ok"], reply["error"], reply["result"]["status"]) == (True, None, "result")
    state = json.loads(reply["result"]["output"])
    assert (state["status"], state["questions"][0]["node"]) == ("waiting", "approve")
    again = call(monkeypatch, capsys, "invoke", invoke("source.poll", source_id="ask-run", config_ref=f"wf-run:{folder}"))
    assert again["result"] == {"status": "no-result", "output": ""}  # nothing new since the last poll


def test_poll_follows_a_run_kept_in_a_runs_folder_outside_the_repo(repo, monkeypatch, capsys, tmp_path_factory):
    folder = waiting_run(capsys, tmp_path_factory.mktemp("runs"))
    assert not folder.is_relative_to(repo.resolve()) and not (repo / ".wf").exists()
    reply = call(monkeypatch, capsys, "invoke", invoke("source.poll", source_id="ask-run", config_ref=f"wf-run:{folder}"))
    state = json.loads(reply["result"]["output"])
    assert (state["status"], state["dir"], state["questions"][0]["node"]) == ("waiting", str(folder), "approve")
    assert state["answer"] == f"wf answer {folder.name} <answers.json> --runs-dir {folder.parent.parent}"
    assert runs.Run.at(folder).repo == repo.resolve()


def test_a_configuration_with_problems_refuses_the_poll_and_keeps_the_state_for_the_next_one(repo, monkeypatch, capsys,
                                                                                            wf_home):
    Path("done.yaml").write_text("version: 1\nname: done\nstart: {input: {}}\nnodes:\n"
                                 "  a: {needs: [start], command: \"true\"}\nend: {output: {}}\n")
    assert cli.main(["run", "done.yaml"]) == 0
    capsys.readouterr()
    (folder,) = (repo / ".wf" / "runs").iterdir()
    (wf_home / "config.yaml").write_text("max_agents: 2\n")
    poll = invoke("source.poll", source_id="done-run", config_ref=f"wf-run:{folder.resolve()}")
    reply = call(monkeypatch, capsys, "invoke", poll)
    assert (reply["ok"], reply["error"]["code"], reply["error"]["retryable"]) == (False, "unavailable", True)
    assert "config.yaml is still there" in reply["error"]["diagnostic"]
    (wf_home / "config.yaml").unlink()
    reply = call(monkeypatch, capsys, "invoke", poll)
    assert json.loads(reply["result"]["output"])["status"] == "succeeded"  # not lost to the refused poll


def test_poll_refuses_a_runs_folder_run_that_does_not_record_its_repo(repo, monkeypatch, capsys, tmp_path_factory):
    folder = waiting_run(capsys, tmp_path_factory.mktemp("runs"))
    (folder / "run.json").write_text("{}")
    reply = call(monkeypatch, capsys, "invoke", invoke("source.poll", source_id="ask-run", config_ref=f"wf-run:{folder}"))
    assert (reply["ok"], reply["error"]["code"]) == (False, "unavailable")
    assert "doesn't say which repo the run is in" in reply["error"]["diagnostic"]


@pytest.mark.parametrize("status, kind, terminal", [
    ("waiting", "wf-waiting", False), ("failed", "wf-failed", False), ("stopped", "wf-stopped", False),
    ("succeeded", "wf-succeeded", True), ("limit", "wf-limit", False),
])
def test_results_are_classified_and_finished_runs_end_the_source(monkeypatch, capsys, status, kind, terminal):
    content = json.dumps({"run": "r", "status": status})
    args = {"source_id": "s", "sequence": 1, "content": content}
    assert call(monkeypatch, capsys, "invoke", invoke("result.classify", **args))["result"] == {"classification": kind}
    assert call(monkeypatch, capsys, "invoke", invoke("result.terminal", **args))["result"] == {"value": terminal}
    assert call(monkeypatch, capsys, "invoke", invoke("result.silent", **args))["result"] == {"value": False}


def test_a_result_too_big_to_slim_keeps_the_command_that_continues_the_run():
    state = {"run": "r", "status": "failed", "seq": 3, "dir": "/runs/r", "workflow": "w", "node": "n",
             "error": "x" * (firstmate.MAX_OUTPUT + 1), "retry": "wf retry r"}
    assert json.loads(firstmate._bounded(state, "wf status r --json")) == {
        "run": "r", "status": "failed", "seq": 3, "dir": "/runs/r", "workflow": "w", "retry": "wf retry r",
        "more": "wf status r --json"}


def test_bad_requests_fail_with_a_protocol_error(repo, monkeypatch, capsys):
    reply = call(monkeypatch, capsys, "invoke", invoke("source.poll", source_id="s", config_ref="/not/a/wf-run/ref"))
    assert (reply["ok"], reply["result"]) == (False, None)
    assert reply["error"]["code"] == "invalid-request" and reply["error"]["retryable"] is False
    reply = call(monkeypatch, capsys, "invoke", invoke("source.poll", source_id="s", config_ref=f"wf-run:{repo}/nope"))
    assert reply["error"]["code"] == "unavailable"
    reply = call(monkeypatch, capsys, "invoke", invoke("result.explode", source_id="s"))
    assert reply["error"]["code"] == "invalid-request"


def test_the_package_is_a_bindable_shim_around_this_wf(tmp_path, capsys):
    os.umask(0o002)  # many accounts have it, so write_package must set each mode itself
    package = tmp_path / "packages" / "wf"
    assert cli.main(["integrate", "firstmate", str(package)]) == 0
    out = capsys.readouterr().out
    assert "fm-extension.sh bind" in out and "register-extension wf" in out
    manifest = json.loads((package / "firstmate-extension.json").read_text())
    assert manifest["schema"] == "firstmate.extension-manifest.v1"
    assert manifest["capabilities"] == [{"name": "process-event-adapter", "versions": [1], "adapter_names": ["wf"]}]
    entrypoint = package / manifest["entrypoint"]
    assert entrypoint.read_text().splitlines()[0] == f"#!{sys.executable}"
    for path in [package, *package.rglob("*")]:  # firstmate refuses group- or world-writable packages
        assert not path.stat().st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    assert entrypoint.stat().st_mode & stat.S_IXUSR
    request = {
        "schema": "firstmate.extension-handshake-request.v1", "request_id": "sha256:" + "e" * 64,
        "host_protocols": [1], "extension_id": manifest["id"], "extension_version": manifest["version"],
        "package_digest": "sha256:" + "f" * 64,
        "capability": {"name": "process-event-adapter", "versions": [1], "adapter_names": ["wf"]},
    }
    # firstmate runs it with a minimal environment, from the package folder
    done = subprocess.run([str(entrypoint), "handshake"], input=json.dumps(request), capture_output=True, text=True,
                          cwd=package, env={"PATH": "/usr/bin:/bin"})
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout)["request_id"] == "sha256:" + "e" * 64


def test_the_package_folder_must_be_new(tmp_path, capsys):
    (tmp_path / "taken").mkdir()
    (tmp_path / "taken" / "x").write_text("x")
    assert cli.main(["integrate", "firstmate", str(tmp_path / "taken")]) == 1
    assert "isn't empty" in capsys.readouterr().err


def test_integrate_expands_a_quoted_tilde(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert cli.main(["integrate", "firstmate", "~/pkgs/wf"]) == 0
    assert (tmp_path / "pkgs" / "wf" / "firstmate-extension.json").exists()
    assert f"bind {tmp_path}/pkgs/wf" in capsys.readouterr().out
