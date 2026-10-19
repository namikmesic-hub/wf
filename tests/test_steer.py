"""wf steer: which session a message is for, how it gets to the driver of the run, and the answer
when no session is live. How each harness takes a message is tested with its adapter, against its
fake: test_claude_code.py, test_codex.py and test_cursor.py."""

import asyncio
import io
import json
import os
import subprocess
import time
from pathlib import Path
from textwrap import dedent

import pytest

from fakes import Fake, paused, submit
from steering import STEER, at_pause, left_in_mailbox, run_folder, steer_events
from workflow import cli, harnesses, runner, runs, steer
from workflow.runs import write_json


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / "repo").mkdir()
    monkeypatch.chdir(tmp_path / "repo")
    monkeypatch.setattr(runner, "interactive", lambda: False)
    subprocess.run(["git", "init", "-q"], check=True)
    return tmp_path


def write(name: str, text: str) -> None:
    Path(name).write_text(dedent(text).lstrip())


HEAD = """
    version: 1
    name: st
    defaults: {harness: claude-code, harnesses: {claude-code: {model: opus}}}
    start: {input: {}}
"""


def made_run() -> runs.Run:
    """A run of a workflow with every kind of node, and a sub-workflow, that nothing drives."""
    write("main.yaml", HEAD + """
    nodes:
      ask: {needs: [start], instruction: Say hi}
      tests: {needs: [ask], command: "true"}
      each: {needs: [ask], for_each: "[1, 2, 3]", instruction: "Look at {{ item }}"}
      build: {needs: [ask], workflow: inner.yaml}
      builds: {needs: [ask], for_each: "[1, 2]", workflow: inner.yaml}
    end: {output: {}}
    """)
    write("inner.yaml", HEAD.replace("name: st", "name: inner") + """
    nodes:
      implement: {needs: [start], instruction: Build it}
    end: {output: {}}
    """)
    return runs.create(Path.cwd(), "st", [Path("main.yaml"), Path("inner.yaml")], Path("main.yaml"), {})


# ---------- which session ----------


def test_a_message_names_its_session_as_events_name_the_node(repo):
    run = made_run()
    assert steer.target(run, "ask", None) == "ask"
    assert steer.target(run, "each", 2) == steer.target(run, "each[2]", None) == steer.target(run, "each[02]", None) == "each[2]"
    assert steer.target(run, "build/implement", None) == "build/implement"
    assert steer.target(run, "builds[1]/implement", None) == "builds[1]/implement"


@pytest.mark.parametrize("node, item, problem", [
    ("nope", None, "workflow st has no node `nope`"),
    ("tests", None, "`tests` is a command node: only agent nodes have sessions"),
    ("build", None, "`build` is a workflow node: name an agent node inside it, as build/<node>"),
    ("builds", 1, "`builds` is a workflow node: name an agent node inside it, as builds[<index>]/<node>"),
    ("each", None, "`each` runs a session for each item: name the item with --item <index>"),
    ("ask", 1, "`ask` has no items: it has no for_each"),
    ("each[1]", 2, "give the item's index once: in the node, or with --item"),
    ("each", -1, "--item is an item's index: 0 or more"),
    ("builds/implement", None, "`builds` runs its sub-workflow for each item: name the item, as builds[<index>]/<node>"),
    ("ask/implement", None, "`ask` isn't a workflow node, so it has no nodes inside"),
    ("build/nope", None, "workflow inner has no node `nope`"),
    ("Ask!", None, "`Ask!` isn't a node id, or a node id with an item's index such as review[2]"),
])
def test_a_message_for_what_isnt_an_agent_node_is_refused(repo, node, item, problem):
    with pytest.raises(steer.SteerError, match=problem.replace("[", r"\[").replace("]", r"\]")):
        steer.target(made_run(), node, item)


def test_wf_steer_says_what_is_wrong_with_the_node_or_the_message(repo, capsys, tmp_path):
    run = made_run()
    assert cli.main(["steer", run.id, "tests", "Stop"]) == 1
    assert "wf steer: `tests` is a command node" in capsys.readouterr().err
    assert cli.main(["steer", run.id, "ask"]) == 1
    assert "give the message, or --file, but not both" in capsys.readouterr().err
    (tmp_path / "note.md").write_text("Stop")
    assert cli.main(["steer", run.id, "ask", "Stop", "--file", str(tmp_path / "note.md")]) == 1
    assert "give the message, or --file, but not both" in capsys.readouterr().err
    assert cli.main(["steer", run.id, "ask", "--file", str(tmp_path / "missing.md")]) == 1
    assert "can't read" in capsys.readouterr().err
    assert cli.main(["steer", run.id, "ask", " \n"]) == 1
    assert "the message is empty" in capsys.readouterr().err
    assert cli.main(["steer", "no-such-run", "ask", "Stop"]) == 1
    assert "no run `no-such-run`" in capsys.readouterr().err
    assert left_in_mailbox() == []


# ---------- no live session ----------


def test_a_run_that_isnt_running_has_no_live_session(repo, capsys):
    run = made_run()
    run.set_status("waiting", nodes=["approve"])
    assert cli.main(["steer", run.id, "ask", "Stop"]) == 1
    assert capsys.readouterr().err == ("wf steer: no live session for ask: the run is waiting for a person, so no agent "
                                       "works in it. Nothing was sent.\n")
    run.set_status("succeeded")
    assert cli.main(["steer", run.id, "ask", "Stop", "--json"]) == 1
    answer = json.loads(capsys.readouterr().out)
    assert answer == {"run": run.id, "node": "ask", "id": answer["id"], "status": "none",
                      "reason": "the run succeeded, so no agent works in it"}
    assert not (run.dir / "steer").exists()


def test_a_running_run_that_no_process_drives_has_no_live_session(repo):
    run = made_run()  # status running, lock free: the process crashed
    answer = steer.send(run, "ask", "Stop")
    assert (answer["status"], answer["reason"]) == ("none", "no wf process drives the run (it crashed or was killed)")
    assert not (run.dir / "steer").exists()


def test_a_message_nobody_takes_is_withdrawn_and_can_never_arrive(repo, monkeypatch):
    monkeypatch.setattr(steer, "ANSWER_SECONDS", 0.3)
    run = made_run()
    with run.driving():  # a driver that never looks at its mailbox: an older wf, or one that hangs
        answer = steer.send(run, "ask", "Stop")
    assert (answer["status"], answer["reason"]) == (
        "none", "the wf process that drives the run didn't take the message within 0.3 seconds")
    assert list((run.dir / "steer").iterdir()) == []


def test_a_message_for_a_node_without_a_live_session_fails_and_changes_nothing(repo, monkeypatch):
    fake = Fake("claude-code", paused(repo, "p", {"summary": "hi"}), submit({"summary": "bye"}))
    monkeypatch.setitem(harnesses.FACTORIES, "claude-code", fake.factory)
    write("flow.yaml", HEAD + """
    nodes:
      ask: {needs: [start], instruction: Say hi}
      later: {needs: [ask], instruction: Say bye}
    end: {output: {}}
    """)
    steering = at_pause(repo, "p", "later")
    assert cli.main(["run", "flow.yaml"]) == 0
    done = steering.result()
    assert done.returncode == 1
    assert done.stderr == ("wf steer: no live session for later: no agent session of it is open now. "
                           "Nothing was sent.\n")
    assert steer_events() == []
    assert STEER not in fake.sent
    assert not (run_folder() / "nodes/ask/1/attempt-1.log").exists()  # the fake keeps none: a steer line would make one
    assert left_in_mailbox() == []


# ---------- live sessions ----------


def test_a_message_reaches_an_item_from_a_file_and_waits_for_the_turn_to_end(repo, monkeypatch, tmp_path):
    fake = Fake("claude-code", paused(repo, "p"), submit({"summary": "done"}))
    monkeypatch.setitem(harnesses.FACTORIES, "claude-code", fake.factory)
    write("flow.yaml", HEAD + """
    nodes:
      each: {needs: [start], for_each: '["only"]', instruction: "Look at {{ item }}"}
    end: {output: {}}
    """)
    (tmp_path / "note.md").write_text("Look at the tests first.\nThen fix the bug.\n")
    steering = at_pause(repo, "p", "each", "--item", "0", "--file", str(tmp_path / "note.md"), message=None)
    assert cli.main(["run", "flow.yaml"]) == 0
    done = steering.result()
    assert done.returncode == 0, done.stderr
    assert done.stdout.startswith("Queued for each[0] (attempt 1 on claude-code)")
    assert fake.sent[1] == "Look at the tests first.\nThen fix the bug.\n"  # the next turn, before any reminder
    queued, delivered = steer_events()
    assert (queued["node"], queued["index"], queued["status"]) == ("each", 0, "queued")
    assert delivered["status"] == "delivered"


def test_a_message_reaches_a_node_in_a_sub_workflow_from_stdin(repo, monkeypatch):
    fake = Fake("claude-code", paused(repo, "p"), submit({"summary": "done"}))
    monkeypatch.setitem(harnesses.FACTORIES, "claude-code", fake.factory)
    write("flow.yaml", HEAD + """
    nodes:
      build: {needs: [start], workflow: inner.yaml}
    end: {output: {}}
    """)
    write("inner.yaml", HEAD.replace("name: st", "name: inner") + """
    nodes:
      implement: {needs: [start], instruction: Build it}
    end: {output: {}}
    """)
    steering = at_pause(repo, "p", "build/implement", "--file", "-", "--json", message=None, stdin=STEER)
    assert cli.main(["run", "flow.yaml"]) == 0
    answer = json.loads(steering.result().stdout)
    assert {k: answer[k] for k in ("node", "status", "attempt", "harness")} == {
        "node": "build/implement", "status": "queued", "attempt": 1, "harness": "claude-code"}
    assert fake.sent[1] == STEER
    assert [(e["node"], e["status"]) for e in steer_events()] == [("build/implement", "queued"),
                                                                  ("build/implement", "delivered")]


# ---------- the way in ----------


def test_a_request_older_than_the_wait_is_answered_and_never_delivered(tmp_path):
    class Nobody(steer.Steering):
        async def deliver(self, path, message):
            raise AssertionError("a stale request was delivered")

    run = runs.Run("r", tmp_path, tmp_path)
    (tmp_path / "steer").mkdir()
    write_json(tmp_path / "steer" / "old.request", {"path": "ask", "text": "Too late"})
    long_ago = time.time() - steer.ANSWER_SECONDS - 5
    os.utime(tmp_path / "steer" / "old.request", (long_ago, long_ago))

    async def drive():
        async with steer.answering(run, Nobody()):
            await asyncio.sleep(steer.POLL_SECONDS * 2)

    asyncio.run(drive())
    answer = json.loads((tmp_path / "steer" / "old.reply").read_text())
    assert answer == {"status": "none", "reason": "it waited more than 15 seconds, so its sender has given up"}
    assert sorted(p.name for p in (tmp_path / "steer").iterdir()) == ["old.reply"]


def test_a_request_that_comes_as_the_graph_stops_is_answered(tmp_path):
    run = runs.Run("r", tmp_path, tmp_path)

    async def drive():
        async with steer.answering(run, steer.Steering()):
            (tmp_path / "steer").mkdir()
            write_json(tmp_path / "steer" / "late.request", {"path": "ask", "text": "Stop"})

    asyncio.run(drive())
    assert json.loads((tmp_path / "steer" / "late.reply").read_text())["status"] == "none"
