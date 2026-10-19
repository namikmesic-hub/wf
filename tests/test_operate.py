"""Operating runs from outside: detached runs, wait, answer, status, list, the run lock.

These are the commands an autonomous operator (firstmate's first mate, a
script, a person on another machine) uses instead of a terminal. Detached runs
are real background processes, so each test waits for them with `wf wait`.
"""

import json
import os
import signal
import subprocess
import time
from pathlib import Path
from textwrap import dedent

import pytest

from workflow import cli, operate, runner


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(runner, "interactive", lambda: False)
    subprocess.run(["git", "init", "-q"], check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "first"], check=True)
    return tmp_path


def write(name: str, text: str) -> None:
    Path(name).write_text(dedent(text).lstrip())


def json_out(capsys) -> dict:
    return json.loads(capsys.readouterr().out)


ASK = """
version: 1
name: ask
start: {input: {}}
nodes:
  approve:
    needs: [start]
    human: Ship it?
    output: {ok: {type: boolean}, note: {type: string, optional: true}}
  after: {needs: [approve], command: "sleep 0.2; echo {{ 'shipped' if approve.ok else 'held' }}"}
end: {output: {said: "{{ after.stdout }}"}}
"""


def detached(capsys, *args) -> str:
    assert cli.main(["run", *args, "--detach", "--json"]) == 0
    return json_out(capsys)["run"]


def test_a_detached_run_returns_at_once_and_wait_reports_its_question(repo, capsys):
    write("ask.yaml", ASK)
    started = time.monotonic()
    run_id = detached(capsys, "ask.yaml")
    assert time.monotonic() - started < 5
    assert cli.main(["wait", run_id, "--timeout", "30"]) == 0
    state = json_out(capsys)
    assert (state["run"], state["status"]) == (run_id, "waiting")
    (question,) = state["questions"]
    assert (question["node"], question["message"]) == ("approve", "Ship it?")
    assert question["schema"]["required"] == ["ok"]
    assert state["answer"] == f"wf answer {run_id} <answers.json>"


def test_answer_continues_the_run_without_a_terminal(repo, capsys):
    write("ask.yaml", ASK)
    run_id = detached(capsys, "ask.yaml")
    cli.main(["wait", run_id, "--timeout", "30"])
    key = json_out(capsys)["questions"][0]["key"]
    Path("answers.json").write_text(json.dumps({key: {"ok": True}}))
    assert cli.main(["answer", run_id, "answers.json"]) == 0
    capsys.readouterr()
    assert cli.main(["status", run_id, "--json"]) == 0
    assert json_out(capsys)["status"] == "succeeded"
    output = json.loads((Path(".wf/runs") / run_id / "output.json").read_text())
    assert output == {"said": "shipped\n"}


def test_answers_are_checked_before_the_run_continues(repo, capsys):
    write("ask.yaml", ASK)
    run_id = detached(capsys, "ask.yaml")
    cli.main(["wait", run_id, "--timeout", "30"])
    key = json_out(capsys)["questions"][0]["key"]
    Path("bad.json").write_text(json.dumps({key: {"ok": "sure"}}))
    assert cli.main(["answer", run_id, "bad.json"]) == 1
    assert "'sure' is not of type 'boolean'" in capsys.readouterr().err
    Path("none.json").write_text("{}")
    assert cli.main(["answer", run_id, "none.json"]) == 1
    assert f"no answer for {key}" in capsys.readouterr().err
    cli.main(["status", run_id, "--json"])
    assert json_out(capsys)["status"] == "waiting"  # nothing moved


def test_a_detached_answer_and_a_consumer_cursor(repo, capsys):
    write("ask.yaml", ASK)
    run_id = detached(capsys, "ask.yaml")
    assert cli.main(["wait", run_id, "--timeout", "30", "--consumer", "fm"]) == 0
    key = json_out(capsys)["questions"][0]["key"]
    assert cli.main(["wait", run_id, "--timeout", "1", "--consumer", "fm"]) == 3  # nothing new for fm
    capsys.readouterr()
    Path("answers.json").write_text(json.dumps({key: {"ok": False, "note": "not yet"}}))
    assert cli.main(["answer", run_id, "answers.json", "--detach"]) == 0
    capsys.readouterr()
    assert cli.main(["wait", run_id, "--timeout", "30", "--consumer", "fm"]) == 0
    state = json_out(capsys)
    assert (state["status"], state["output"]) == ("succeeded", {"said": "held\n"})


def test_status_and_list(repo, capsys):
    write("ask.yaml", ASK)
    run_id = detached(capsys, "ask.yaml")
    cli.main(["wait", run_id, "--timeout", "30"])
    capsys.readouterr()
    assert cli.main(["list", "--json"]) == 0
    (listed,) = json_out(capsys)
    assert (listed["run"], listed["status"], listed["workflow"]) == (run_id, "waiting", "ask")
    assert cli.main(["status", run_id]) == 0
    assert "waiting for a person: approve" in capsys.readouterr().out


SLOW = """
version: 1
name: slow
start: {input: {}}
nodes:
  wait: {needs: [start], command: "sleep 3; echo done"}
end: {output: {said: "{{ wait.stdout }}"}}
"""


def test_only_one_process_drives_a_run(repo, capsys):
    write("slow.yaml", SLOW)
    run_id = detached(capsys, "slow.yaml")
    folder = Path(".wf/runs") / run_id
    deadline = time.monotonic() + 10
    while not operate.driven(folder) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert cli.main(["resume", run_id]) == 1
    assert "already running" in capsys.readouterr().err
    assert cli.main(["wait", run_id, "--timeout", "30"]) == 0
    assert json_out(capsys)["status"] == "succeeded"


def test_a_run_whose_process_died_is_reported_stopped_and_can_be_resumed(repo, capsys, monkeypatch):
    monkeypatch.setattr(operate, "GRACE_SECONDS", 0)
    write("slow.yaml", SLOW)
    run_id = detached(capsys, "slow.yaml")
    folder = Path(".wf/runs") / run_id
    deadline = time.monotonic() + 10
    while not operate.driven(folder) and time.monotonic() < deadline:
        time.sleep(0.05)
    os.killpg(int((folder / "lock").read_text()), signal.SIGKILL)  # the driver and its command
    assert cli.main(["wait", run_id, "--timeout", "10"]) == 0
    state = json_out(capsys)
    assert state["status"] == "stopped"
    assert state["resume"] == f"wf resume {run_id}"
    assert cli.main(["resume", run_id]) == 0
    assert json.loads((folder / "output.json").read_text()) == {"said": "done\n"}


def test_a_failed_run_reports_why_and_resumes_in_the_background(repo, capsys):
    write("flaky.yaml", """
        version: 1
        name: flaky
        start: {input: {}}
        nodes:
          check: {needs: [start], command: "test -e ready && echo ok"}
        end: {output: {said: "{{ check.stdout }}"}}
    """)
    run_id = detached(capsys, "flaky.yaml")
    assert cli.main(["wait", run_id, "--timeout", "30"]) == 0
    state = json_out(capsys)
    assert (state["status"], state["node"]) == ("failed", "check")
    assert "exited with code 1" in state["error"]
    Path("ready").touch()
    assert cli.main(["resume", run_id, "--detach"]) == 0
    capsys.readouterr()
    assert cli.main(["wait", run_id, "--timeout", "30"]) == 0
    assert json_out(capsys)["status"] == "succeeded"


def test_questions_from_sub_workflows_are_answered_by_their_full_key(repo, capsys):
    write("inner.yaml", ASK.replace("name: ask", "name: inner"))
    write("outer.yaml", """
        version: 1
        name: outer
        start: {input: {}}
        nodes:
          sub: {needs: [start], for_each: '["a", "b"]', workflow: inner.yaml}
        end: {output: {said: {type: array, value: "{{ sub | map(attribute='said') | list }}"}}}
    """)
    run_id = detached(capsys, "outer.yaml")
    cli.main(["wait", run_id, "--timeout", "30"])
    questions = json_out(capsys)["questions"]
    assert [q["node"] for q in questions] == ["sub[0]/approve", "sub[1]/approve"]
    assert all("/" in q["key"] for q in questions)
    Path("answers.json").write_text(json.dumps({questions[0]["key"]: {"ok": True}, questions[1]["key"]: {"ok": False}}))
    assert cli.main(["answer", run_id, "answers.json"]) == 0
    output = json.loads((Path(".wf/runs") / run_id / "output.json").read_text())
    assert output == {"said": ["shipped\n", "held\n"]}


def test_a_detached_run_is_locked_from_the_moment_it_is_started(repo, capsys):
    write("slow.yaml", SLOW)
    run_id = detached(capsys, "slow.yaml")  # the lock passes straight from this process to the driver
    assert cli.main(["resume", run_id]) == 1
    assert "already running" in capsys.readouterr().err
    assert cli.main(["wait", run_id, "--timeout", "30"]) == 0


def test_a_repo_with_its_own_workflow_package_cannot_replace_wf(repo, capsys):
    Path("workflow").mkdir()
    Path("workflow/__init__.py").write_text("")
    Path("workflow/cli.py").write_text("import sys\nprint('not wf')\nsys.exit(7)\n")
    write("slow.yaml", SLOW.replace("sleep 3; ", ""))
    run_id = detached(capsys, "slow.yaml")
    assert cli.main(["wait", run_id, "--timeout", "30"]) == 0
    assert json_out(capsys)["status"] == "succeeded"


def test_a_consumer_name_must_be_path_safe(repo, capsys):
    write("ask.yaml", ASK)
    run_id = detached(capsys, "ask.yaml")
    assert cli.main(["wait", run_id, "--timeout", "1", "--consumer", "../../escape"]) == 1
    assert "path-safe" in capsys.readouterr().err
    assert not Path(".wf/escape.json").exists()


def test_list_shows_the_newest_run_first(repo, capsys):
    write("quick.yaml", SLOW.replace("sleep 3; ", ""))
    for _ in range(2):
        assert cli.main(["run", "quick.yaml"]) == 0
    capsys.readouterr()
    cli.main(["list", "--json"])
    listed = [r["run"] for r in json_out(capsys)]
    made = sorted(Path(".wf/runs").iterdir(), key=lambda p: (p / "workflow" / "manifest.json").stat().st_mtime)
    assert listed == [p.name for p in reversed(made)]


def test_discard_refuses_a_run_that_is_being_driven_and_keeps_real_branches(repo, capsys):
    write("slow.yaml", SLOW)
    run_id = detached(capsys, "slow.yaml")
    assert cli.main(["discard", run_id]) == 1
    assert "already running" in capsys.readouterr().err
    cli.main(["wait", run_id, "--timeout", "30"])
    subprocess.run(["git", "branch", f"wf/{run_id}/x"], check=True)  # as a `workspace: new` node would leave
    capsys.readouterr()
    assert cli.main(["discard", run_id]) == 0
    assert f"kept its branches wf/{run_id}/x" in capsys.readouterr().out
    assert cli.main(["status", run_id]) == 1


def test_a_damaged_output_file_does_not_break_list(repo, capsys):
    write("quick.yaml", SLOW.replace("sleep 3; ", ""))
    assert cli.main(["run", "quick.yaml"]) == 0
    (next(Path(".wf/runs").iterdir()) / "output.json").write_text("{not json")
    capsys.readouterr()
    assert cli.main(["list", "--json"]) == 0
    assert json_out(capsys)[0]["status"] == "succeeded"
