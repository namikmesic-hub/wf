"""session: continue, on a fake harness: forks, fallbacks to fresh sessions, and session.continued."""

import json
import subprocess
from pathlib import Path

import pytest

from fakes import Fake, submit, talk
from workflow import cli, harnesses, runner
from workflow.output_tool import FINISH, NEW_RESULT


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(runner, "interactive", lambda: False)
    subprocess.run(["git", "init", "-q"], check=True)
    return tmp_path


LOOP = """
version: 1
name: loop
defaults: {harness: claude-code, harnesses: {claude-code: {model: opus}}}
start: {input: {}}
nodes:
  work:
    needs: [start]
    session: continue
    instruction: "{% if session.continued %}Fix: {{ check.stdout | trim }}{% else %}The whole task{% endif %}"
    EXTRA
  check:
    needs: [work]
    command: "echo try >> tries; test $(wc -l < tries) -ge TRIES"
    check: false
    routes: [{when: "check.exit_code != 0", to: work, max: 5}, {to: end}]
end: {output: {}}
"""


def loop(extra: str, tries: int) -> str:
    return LOOP.replace("EXTRA", extra).replace("TRIES", str(tries))


def run(text: str) -> int:
    Path("flow.yaml").write_text(text)
    return cli.main(["run", "flow.yaml"])


def events() -> list[dict]:
    (folder,) = (Path(".wf") / "runs").iterdir()
    return [json.loads(line) for line in (folder / "events.jsonl").read_text().splitlines()]


def test_each_pass_forks_the_last_one_with_only_the_new_feedback(repo, monkeypatch):
    fake = Fake("claude-code", *[submit({"summary": f"pass {n}"}) for n in range(1, 4)])
    monkeypatch.setitem(harnesses.FACTORIES, "claude-code", fake.factory)
    assert run(loop("", 3)) == 0
    assert [s.fork_from for s in fake.specs] == [None, "session-1", "session-2"]
    assert all(s.continuable for s in fake.specs)  # each one may be continued next time
    assert fake.sent[0] == f"The whole task\n\n{FINISH}"
    assert fake.sent[1] == f"Fix:\n\n{NEW_RESULT}\n\n{FINISH}"
    (folder,) = (Path(".wf") / "runs").iterdir()
    assert json.loads((folder / "nodes" / "work" / "2" / "session.json").read_text())["id"] == "session-2"


def test_after_two_continued_passes_the_next_one_starts_fresh(repo, monkeypatch, capsys):
    fake = Fake("claude-code", *[submit({"summary": f"pass {n}"}) for n in range(1, 5)])
    monkeypatch.setitem(harnesses.FACTORIES, "claude-code", fake.factory)
    assert run(loop("", 4)) == 0
    assert [s.fork_from for s in fake.specs] == [None, "session-1", "session-2", None]
    assert fake.sent[3] == f"The whole task\n\n{FINISH}"
    assert "starting a fresh session: the two passes before it both continued" in capsys.readouterr().out


def test_a_pass_on_a_different_harness_starts_fresh(repo, monkeypatch, capsys):
    from fakes import ends
    claude = Fake("claude-code", submit({"summary": "one"}), ends("unavailable", "Claude usage limit reached"))
    codex = Fake("codex", submit({"summary": "two"}))
    monkeypatch.setitem(harnesses.FACTORIES, "claude-code", claude.factory)
    monkeypatch.setitem(harnesses.FACTORIES, "codex", codex.factory)
    node = "harness: [claude-code, codex]\n    model: {claude-code: opus, codex: gpt-6}"
    assert run(loop(node, 2)) == 0
    assert [s.fork_from for s in claude.specs] == [None, "session-1"]
    assert [s.fork_from for s in codex.specs] == [None]
    assert codex.sent[0] == f"The whole task\n\n{FINISH}"
    assert "starting a fresh session: the last pass ran on claude-code" in capsys.readouterr().out


def test_a_session_that_cannot_be_forked_falls_back_to_fresh(repo, monkeypatch, capsys):
    fake = Fake("claude-code", submit({"summary": "one"}), submit({"summary": "two"}), fork_fails=True)
    monkeypatch.setitem(harnesses.FACTORIES, "claude-code", fake.factory)
    assert run(loop("", 2)) == 0
    assert [s.fork_from for s in fake.specs] == [None, "session-1", None]
    assert fake.sent[1] == f"The whole task\n\n{FINISH}"
    assert "starting a fresh session: no session session-1" in capsys.readouterr().out


def test_a_retry_forks_from_the_same_session_again(repo, monkeypatch):
    fake = Fake("claude-code", submit({"summary": "one"}), talk(), talk(), talk(), submit({"summary": "two"}))
    monkeypatch.setitem(harnesses.FACTORIES, "claude-code", fake.factory)
    assert run(loop("", 2)) == 0
    assert [s.fork_from for s in fake.specs] == [None, "session-1", "session-1"]


def test_for_each_items_never_keep_a_session_to_continue(repo, monkeypatch):
    fake = Fake("claude-code", submit({"summary": "a"}), submit({"summary": "b"}))
    monkeypatch.setitem(harnesses.FACTORIES, "claude-code", fake.factory)
    Path("flow.yaml").write_text("""
version: 1
name: items
defaults: {harness: claude-code}
start: {input: {}}
nodes:
  each: {needs: [start], for_each: '[1, 2]', session: continue, instruction: "{{ item }}"}
end: {output: {}}
""")
    assert cli.main(["run", "flow.yaml"]) == 0
    assert [s.continuable for s in fake.specs] == [False, False]


@pytest.mark.parametrize("items, forks", [
    ("", [None, "session-1"]),
    ("for_each: '[1]', ", [None, None]),  # an item starts its sub-workflow without sessions, as an agent's item does
])
def test_a_workflow_node_passes_its_sub_workflows_sessions_to_its_next_pass(repo, monkeypatch, items, forks):
    fake = Fake("claude-code", submit({"summary": "one"}), submit({"summary": "two"}))
    monkeypatch.setitem(harnesses.FACTORIES, "claude-code", fake.factory)
    Path("child.yaml").write_text("""
version: 1
name: child
defaults: {harness: claude-code, harnesses: {claude-code: {model: opus}}}
start: {input: {}}
nodes:
  work: {needs: [start], session: continue, instruction: "{% if session.continued %}Again{% else %}The task{% endif %}"}
end: {output: {}}
""")
    Path("flow.yaml").write_text("""
version: 1
name: outer
start: {input: {}}
nodes:
  build: {needs: [start], ITEMSworkflow: child.yaml}
  check:
    needs: [build]
    command: "echo try >> tries; test $(wc -l < tries) -ge 2"
    check: false
    routes: [{when: "check.exit_code != 0", to: build, max: 1}, {to: end}]
end: {output: {}}
""".replace("ITEMS", items))
    assert cli.main(["run", "flow.yaml"]) == 0
    assert [s.fork_from for s in fake.specs] == forks
    assert fake.sent[1].splitlines()[0] == ("Again" if forks[1] else "The task")
