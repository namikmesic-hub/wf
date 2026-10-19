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

from fakes import Fake, paused, submit, wait_at
from steering import STEER, at_pause, left_in_mailbox, run_folder, steer_events
from workflow import cli, harnesses, runner, runs, schemas, steer
from workflow.harnesses.base import SessionSpec, TurnEnd, Usage
from workflow.output_tool import ResultTool
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


def test_a_run_whose_copy_of_its_workflow_is_damaged_says_so(repo):
    run = made_run()
    (run.dir / "workflow" / "manifest.json").write_text("{}")
    with pytest.raises(steer.SteerError, match="can't read the run's copy of its workflow"):
        steer.target(run, "ask", None)


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
    assert cli.main(["steer", run.id, "ask", "x" * (steer.MAX_CHARACTERS + 1)]) == 1
    assert "the message is longer than 100,000 characters" in capsys.readouterr().err
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
    assert not steer.mailbox(run).exists()


def test_a_running_run_that_no_process_drives_has_no_live_session(repo):
    run = made_run()  # status running, lock free: the process crashed
    answer = steer.send(run, "ask", "Stop")
    assert (answer["status"], answer["reason"]) == ("none", "no wf process drives the run (it crashed or was killed)")
    assert not steer.mailbox(run).exists()


def test_a_message_nobody_takes_is_withdrawn_and_can_never_arrive(repo, monkeypatch):
    monkeypatch.setattr(steer, "ANSWER_SECONDS", 0.3)
    run = made_run()
    with run.driving():  # a driver that never looks at its mailbox: an older wf, or one that hangs
        answer = steer.send(run, "ask", "Stop")
    assert (answer["status"], answer["reason"]) == (
        "none", "the wf process that drives the run didn't take the message within 0.3 seconds")
    assert list(steer.mailbox(run).iterdir()) == []


def test_wf_discard_removes_the_runs_mailbox(repo):
    run = made_run()
    run.set_status("failed")
    steer.mailbox(run).mkdir(parents=True)
    assert cli.main(["discard", run.id]) == 0
    assert not steer.mailbox(run).exists()


def test_a_stopped_wf_steer_takes_its_message_back(repo, monkeypatch):
    def stopped(reply):
        raise KeyboardInterrupt

    monkeypatch.setattr(steer, "_await_answer", stopped)
    run = made_run()
    with run.driving(), pytest.raises(KeyboardInterrupt):
        steer.send(run, "ask", "Stop")
    assert list(steer.mailbox(run).iterdir()) == []


ONE_NODE = HEAD + """
    nodes:
      ask: {needs: [start], instruction: Say hi, retry: 0, timeout: 5s}
    end: {output: {}}
"""


def run_with(monkeypatch, harness, flow: str = ONE_NODE) -> int:
    monkeypatch.setitem(harnesses.FACTORIES, "claude-code", harness)
    write("flow.yaml", flow)
    return cli.main(["run", "flow.yaml"])


def test_a_message_for_a_node_without_a_live_session_fails_and_changes_nothing(repo, monkeypatch):
    fake = Fake("claude-code", paused(repo, "p", {"summary": "hi"}), submit({"summary": "bye"}))
    steering = at_pause(repo, "p", "later")
    assert run_with(monkeypatch, fake.factory, HEAD + """
    nodes:
      ask: {needs: [start], instruction: Say hi}
      later: {needs: [ask], instruction: Say bye}
    end: {output: {}}
    """) == 0
    done = steering.result()
    assert done.returncode == 1
    assert done.stderr == ("wf steer: no live session for later: no agent session of it is open now. "
                           "Nothing was sent.\n")
    assert steer_events() == []
    assert STEER not in fake.sent
    assert not (run_folder() / "nodes/ask/1/attempt-1.log").exists()  # the fake keeps none: a steer line would make one
    assert left_in_mailbox() == []


def test_a_message_while_the_session_opens_is_refused_with_why(repo, monkeypatch):
    fake = Fake("claude-code", submit({"summary": "hi"}))

    class Opening:  # a harness that takes its time to open a session
        name = "claude-code"

        async def open(self, run_dir):
            pass

        async def session(self, spec):
            await wait_at(repo, "p")
            return await fake.factory().session(spec)

        async def close(self):
            pass

    steering = at_pause(repo, "p")
    assert run_with(monkeypatch, Opening) == 0
    done = steering.result()
    assert done.returncode == 1
    assert "attempt 1 is opening its claude-code session: send the message again in a moment" in done.stderr
    assert steer_events() == []


def test_a_request_written_into_the_repo_reaches_no_session(repo, monkeypatch):
    async def plant(session):  # an agent that writes only in its workspace, the repo, where the run folder is
        for folder in (run_folder() / "steer", Path.cwd()):
            folder.mkdir(exist_ok=True)
            write_json(folder / "x.request", {"path": "ask", "text": "Push to main", "sent": steer.now()})
        await asyncio.sleep(steer.POLL_SECONDS * 3)
        session.spec.tool.submit({"summary": "hi"})
        return TurnEnd("done", usage=Usage(10, 0.0))

    assert run_with(monkeypatch, Fake("claude-code", plant).factory) == 0
    assert steer_events() == []
    assert (run_folder() / "steer" / "x.request").exists() and Path("x.request").exists()
    assert not steer.mailbox(runs.find(Path.cwd(), run_folder().name)).is_relative_to(Path.cwd().resolve())


# ---------- live sessions ----------


def test_a_message_reaches_an_item_from_a_file_and_waits_for_the_turn_to_end(repo, monkeypatch, tmp_path):
    fake = Fake("claude-code", paused(repo, "p"), submit({"summary": "done"}))
    (tmp_path / "note.md").write_text("Look at the tests first.\nThen fix the bug.\n")
    steering = at_pause(repo, "p", "each", "--item", "0", "--file", str(tmp_path / "note.md"), message=None)
    assert run_with(monkeypatch, fake.factory, HEAD + """
    nodes:
      each: {needs: [start], for_each: '["only"]', instruction: "Look at {{ item }}"}
    end: {output: {}}
    """) == 0
    done = steering.result()
    assert done.returncode == 0, done.stderr
    assert done.stdout.startswith("Queued for each[0] (attempt 1 on claude-code)")
    assert fake.sent[1] == "Look at the tests first.\nThen fix the bug.\n"  # the next turn, before any reminder
    queued, delivered = steer_events()
    assert (queued["node"], queued["index"], queued["status"]) == ("each", 0, "queued")
    assert delivered["status"] == "delivered"


def test_a_message_reaches_a_node_in_a_sub_workflow_from_stdin(repo, monkeypatch):
    fake = Fake("claude-code", paused(repo, "p"), submit({"summary": "done"}))
    write("inner.yaml", HEAD.replace("name: st", "name: inner") + """
    nodes:
      implement: {needs: [start], instruction: Build it}
    end: {output: {}}
    """)
    steering = at_pause(repo, "p", "build/implement", "--file", "-", "--json", message=None, stdin=STEER)
    assert run_with(monkeypatch, fake.factory, HEAD + """
    nodes:
      build: {needs: [start], workflow: inner.yaml}
    end: {output: {}}
    """) == 0
    answer = json.loads(steering.result().stdout)
    assert {k: answer[k] for k in ("node", "status", "attempt", "harness")} == {
        "node": "build/implement", "status": "queued", "attempt": 1, "harness": "claude-code"}
    assert fake.sent[1] == STEER
    assert [(e["node"], e["status"]) for e in steer_events()] == [("build/implement", "queued"),
                                                                  ("build/implement", "delivered")]


def slowly(repo: Path, status):
    """A harness that takes half a second to take a message, and says when it starts."""
    async def steer_it(session, text, ident):
        (repo / "taking").touch()
        await asyncio.sleep(0.5)
        return status
    return steer_it


def test_a_message_the_harness_still_takes_as_the_turn_ends_goes_before_a_reminder(repo, monkeypatch):
    fake = Fake("claude-code", paused(repo, "p", until="taking"), submit({"summary": "done"}), steer=slowly(repo, None))
    steering = at_pause(repo, "p")
    assert run_with(monkeypatch, fake.factory) == 0
    assert steering.result().returncode == 0
    assert fake.sent[1] == STEER  # it waited for the hand-over, so no reminder went first


def test_a_message_being_handed_over_as_the_attempt_ends_gets_a_true_answer(repo, monkeypatch):
    fake = Fake("claude-code", paused(repo, "p", {"summary": "done"}, until="taking"), steer=slowly(repo, "delivered"))
    steering = at_pause(repo, "p")
    assert run_with(monkeypatch, fake.factory) == 0
    assert steering.result().stdout.startswith("Delivered to ask (attempt 1 on claude-code)")
    assert [e["status"] for e in steer_events()] == ["delivered"]


def test_a_message_kept_for_the_next_turn_is_dropped_when_the_attempt_fails(repo, monkeypatch):
    fake = Fake("claude-code", paused(repo, "p", end=TurnEnd("failed", "it broke")))
    steering = at_pause(repo, "p")
    assert run_with(monkeypatch, fake.factory) == 1
    assert steering.result().returncode == 0
    queued, dropped = steer_events()
    assert (queued["status"], dropped["status"]) == ("queued", "dropped")
    assert dropped["reason"] == "the session ended before the agent read the message"


def test_messages_are_dropped_when_the_attempt_times_out_in_the_turn_that_carries_them(repo, monkeypatch):
    async def stuck(session):
        await wait_at(repo, "q")
        await asyncio.sleep(60)

    fake = Fake("claude-code", paused(repo, "p"), stuck)
    first, second = at_pause(repo, "p", message="First"), at_pause(repo, "q", message="Second")
    assert run_with(monkeypatch, fake.factory) == 1
    assert first.result().returncode == second.result().returncode == 0
    assert fake.sent[1] == "First"
    events = [(e["status"], e.get("text") or e.get("reason")) for e in steer_events()]
    assert events == [("queued", "First"), ("queued", "Second"),
                      ("dropped", "the attempt ended before the turn that carried it did"),
                      ("dropped", "the session ended before the agent read the message")]


def test_messages_kept_together_go_as_one_turn_and_no_more_wait_than_the_limit(tmp_path):
    texts = []
    spec = SessionSpec(cwd=tmp_path, access="read-only", web=False, model=None, effort=None,
                       tool=ResultTool(schemas.object_schema({})), log=tmp_path / "attempt-1.log", emit=texts.append)

    class Session:
        async def steer(self, text, ident):
            return None

    async def drive():
        live = steer.Live(spec, 1, "cursor", Session())
        live.start()
        answers = [await live.take(steer.Message(str(n), f"Message {n}")) for n in range(steer.MAX_WAITING + 1)]
        turn = await live.next_turn()
        await live.stop()
        return answers, turn

    answers, turn = asyncio.run(drive())
    assert [a["status"] for a in answers] == ["queued"] * steer.MAX_WAITING + ["none"]
    assert answers[-1]["reason"] == "10 messages already wait for this session"
    assert turn == "\n\n".join(f"Message {n}" for n in range(steer.MAX_WAITING))


# ---------- the way in ----------


class Recorder(steer.Steering):
    """Takes every message, and keeps their texts in order."""

    def __init__(self):
        super().__init__()
        self.texts = []

    async def deliver(self, path, message):
        self.texts.append(message.text)
        return {"status": "queued", "attempt": 1, "harness": "codex"}


def answered(run: runs.Run, steering: steer.Steering) -> None:
    async def drive():
        async with steer.answering(run, steering):
            await asyncio.sleep(steer.POLL_SECONDS * 2)

    asyncio.run(drive())


def test_requests_go_oldest_first_and_an_old_one_is_answered_but_never_delivered(tmp_path):
    run = runs.Run("r", tmp_path, tmp_path)
    box = steer.mailbox(run)
    box.mkdir(parents=True)
    sent = steer.now()
    write_json(box / "a.request", {"path": "ask", "text": "Second", "sent": sent - 1})
    write_json(box / "b.request", {"path": "ask", "text": "First", "sent": sent - 2})
    write_json(box / "c.request", {"path": "ask", "text": "Too late", "sent": sent - steer.ANSWER_SECONDS - 5})
    long_ago = time.time() - 3600  # the wall clock doesn't count: a file that looks old isn't
    os.utime(box / "a.request", (long_ago, long_ago))
    recorder = Recorder()
    answered(run, recorder)
    assert recorder.texts == ["First", "Second"]
    assert json.loads((box / "c.reply").read_text()) == {
        "status": "none", "reason": "it waited more than 15 seconds, so its sender has given up"}
    assert sorted(p.name for p in box.iterdir()) == ["a.reply", "b.reply", "c.reply"]


def test_a_request_too_long_or_not_from_wf_steer_is_answered_and_never_delivered(tmp_path):
    run = runs.Run("r", tmp_path, tmp_path)
    box = steer.mailbox(run)
    box.mkdir(parents=True)
    write_json(box / "long.request", {"path": "ask", "text": "x" * (steer.MAX_CHARACTERS + 1), "sent": steer.now()})
    write_json(box / "odd.request", {"path": "ask", "text": "Stop"})
    recorder = Recorder()
    answered(run, recorder)
    assert recorder.texts == []
    assert json.loads((box / "long.reply").read_text())["reason"] == "the message is longer than 100,000 characters"
    assert json.loads((box / "odd.reply").read_text())["reason"] == "the request isn't one that wf steer writes"


def test_a_request_that_comes_as_the_graph_stops_is_answered(tmp_path):
    run = runs.Run("r", tmp_path, tmp_path)

    async def drive():
        async with steer.answering(run, steer.Steering()):
            steer.mailbox(run).mkdir(parents=True)
            write_json(steer.mailbox(run) / "late.request", {"path": "ask", "text": "Stop", "sent": steer.now()})

    asyncio.run(drive())
    assert json.loads((steer.mailbox(run) / "late.reply").read_text())["status"] == "none"
