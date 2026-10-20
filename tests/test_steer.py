"""wf steer: which session a message is for, how it gets to the driver of the run, and the answer
when no session is live. How each harness takes a message is tested with its adapter, against its
fake: test_claude_code.py, test_codex.py and test_cursor.py."""

import asyncio
import io
import json
import os
import stat
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from textwrap import dedent

import pytest

from fakes import Fake, paused, submit, wait_at
from steering import STEER, at_pause, left_in_mailbox, run_folder, steer_events
from workflow import cli, harnesses, runner, runs, schemas, steer, workspaces
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


def a_request(folder: Path, ident: str, text: str, sent: float | None = None, boot: str | None = None,
              path: str = "ask") -> Path:
    """A request as `wf steer` writes it: <sent>-<id>.request."""
    folder.mkdir(parents=True, exist_ok=True)
    sent = steer.now() if sent is None else sent
    file = folder / f"{sent:.6f}-{ident}.request"
    write_json(file, {"path": path, "text": text, "boot": steer.boot() if boot is None else boot})
    return file


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
    assert left_in_mailbox(run.dir) == []


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
    assert left_in_mailbox(run.dir) == []


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
        planted.extend(a_request(folder, "x", "Push to main") for folder in (run_folder() / "steer", Path.cwd()))
        await asyncio.sleep(steer.POLL_SECONDS * 3)
        session.spec.tool.submit({"summary": "hi"})
        return TurnEnd("done", usage=Usage(10, 0.0))

    planted = []
    assert run_with(monkeypatch, Fake("claude-code", plant).factory) == 0
    assert steer_events() == []
    assert all(file.exists() for file in planted)
    assert not steer.mailbox(runs.find(Path.cwd(), run_folder().name)).is_relative_to(Path.cwd().resolve())


def test_the_mailbox_is_in_wfs_own_folder_and_only_you_can_open_it(repo, monkeypatch):
    monkeypatch.setattr(steer, "ANSWER_SECONDS", 0.1)
    run = made_run()
    with run.driving():
        steer.send(run, "ask", "Stop")
    box = steer.mailbox(run)
    assert box.parent == workspaces.home() / "steer" and not box.is_relative_to(repo)
    assert stat.S_IMODE(box.stat().st_mode) == stat.S_IMODE(box.parent.stat().st_mode) == 0o700


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


def test_a_timeout_leaves_the_carried_message_unconfirmed_and_drops_the_kept_one(repo, monkeypatch):
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
                      ("unconfirmed", "the attempt ended during the turn that carried it, so wf can't tell whether "
                                      "the agent read it"),
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
    sent = steer.now()
    second = a_request(box, "a", "Second", sent - 1)
    a_request(box, "b", "First", sent - 2)
    a_request(box, "c", "Too late", sent - steer.ANSWER_SECONDS - 5)
    a_request(box, "d", "From before a reboot", sent - 1, boot="other")
    long_ago = time.time() - 3600  # the wall clock doesn't count: a file that looks old isn't
    os.utime(second, (long_ago, long_ago))
    recorder = Recorder()
    answered(run, recorder)
    assert recorder.texts == ["First", "Second"]
    for old in ("c", "d"):
        answer = json.loads((box / f"{old}.reply").read_text())
        assert (answer["status"], answer["reason"]) == ("none", "it waited more than 15 seconds, so its sender has given up")
    assert left_in_mailbox(run.dir) == ["a.reply", "b.reply", "c.reply", "d.reply"]


def test_a_request_too_long_or_not_from_wf_steer_is_answered_and_never_delivered(tmp_path):
    run = runs.Run("r", tmp_path, tmp_path)
    box = steer.mailbox(run)
    a_request(box, "long", "x" * (steer.MAX_CHARACTERS + 1))
    write_json(box / "odd.request", {"path": "ask", "text": "Stop", "boot": steer.boot()})
    recorder = Recorder()
    answered(run, recorder)
    assert recorder.texts == []
    assert json.loads((box / "long.reply").read_text())["reason"] == "the message is longer than 100,000 characters"
    assert json.loads((box / "odd.reply").read_text())["reason"] == "the request isn't one that wf steer writes"


def test_a_request_that_comes_as_the_graph_stops_is_answered(tmp_path):
    run = runs.Run("r", tmp_path, tmp_path)

    async def drive():
        async with steer.answering(run, steer.Steering()):
            a_request(steer.mailbox(run), "late", "Stop")

    asyncio.run(drive())
    assert json.loads((steer.mailbox(run) / "late.reply").read_text())["status"] == "none"


# ---------- edges of the hand-over and the mailbox ----------


def a_spec(tmp_path: Path, events: list) -> SessionSpec:
    return SessionSpec(cwd=tmp_path, access="read-only", web=False, model=None, effort=None,
                       tool=ResultTool(schemas.object_schema({})), log=tmp_path / "attempt-1.log", emit=events.append)


def test_runs_with_one_id_in_two_repos_have_two_mailboxes(tmp_path):
    first, second = runs.Run("r", tmp_path / "a" / "r", tmp_path / "a"), runs.Run("r", tmp_path / "b" / "r", tmp_path / "b")
    assert steer.mailbox(first) != steer.mailbox(second)


def test_the_next_turn_waits_for_a_message_that_comes_as_the_last_one_is_handed_over(tmp_path):
    class Session:  # B arrives just as A's hand-over ends, before the turn's waiter runs again
        async def steer(self, text, ident):
            if ident == "a":
                asyncio.get_running_loop().create_task(live.take(steer.Message("b", "B")))
            return None

    live = steer.Live(a_spec(tmp_path, []), 1, "cursor", Session())

    async def drive():
        live.start()
        asyncio.get_running_loop().create_task(live.take(steer.Message("a", "A")))
        await asyncio.sleep(0)
        turn = await live.next_turn()
        await live.stop()
        return turn

    assert asyncio.run(drive()) == "A\n\nB"


def test_a_hand_over_cut_when_the_attempt_ends_is_unconfirmed_not_queued_or_none(tmp_path, monkeypatch):
    monkeypatch.setattr(steer, "HANDOVER_SECONDS", 0.2)
    events = []

    class Session:  # a harness that never finishes taking the message: it may have it
        async def steer(self, text, ident):
            await asyncio.sleep(60)

    async def drive():
        live = steer.Live(a_spec(tmp_path, events), 1, "claude-code", Session())
        live.start()
        answer = asyncio.get_running_loop().create_task(live.take(steer.Message("a", "A")))
        await asyncio.sleep(0.05)
        await live.stop()
        live.end()
        return await answer

    answer = asyncio.run(drive())
    assert (answer["status"], answer["reason"]) == ("unconfirmed", steer.CUT_OFF)
    assert [(e["status"], e["text"], e["reason"]) for e in events] == [("unconfirmed", "A", steer.CUT_OFF)]


def test_wf_steer_says_when_wf_cant_tell_whether_the_agent_read_the_message(repo, monkeypatch, capsys):
    run = made_run()
    monkeypatch.setattr(steer, "send", lambda run, path, text: {
        "id": "a", "status": "unconfirmed", "reason": steer.CUT_OFF, "attempt": 1, "harness": "claude-code"})
    assert cli.main(["steer", run.id, "ask", "Stop"]) == 0
    assert capsys.readouterr().out == f"Unconfirmed for ask (attempt 1 on claude-code): {steer.CUT_OFF}.\n"


def test_the_message_being_handed_over_counts_toward_the_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(steer, "HANDOVER_SECONDS", 0.1)

    class Session:
        async def steer(self, text, ident):
            await asyncio.sleep(60)

    async def drive():
        live = steer.Live(a_spec(tmp_path, []), 1, "claude-code", Session())
        live.start()
        waiting = [asyncio.get_running_loop().create_task(live.take(steer.Message(str(n), "M")))
                   for n in range(steer.MAX_WAITING)]
        await asyncio.sleep(0.05)  # one is being handed over, nine wait
        refused = await live.take(steer.Message("x", "M"))
        await live.stop()
        live.end()
        await asyncio.gather(*waiting)
        return refused

    assert asyncio.run(drive())["reason"] == "10 messages already wait for this session"


def test_one_look_at_the_mailbox_reads_at_most_ten_requests(tmp_path):
    run = runs.Run("r", tmp_path, tmp_path)
    box = steer.mailbox(run)
    for n in range(steer.MAX_PER_LOOK + 2):
        a_request(box, str(n), "M")
    mail = steer._Mailbox(box, steer.Steering())
    assert (len(mail.take()), len(mail.take())) == (steer.MAX_PER_LOOK, 2)


def test_one_look_at_the_mailbox_reads_a_limited_number_of_names(tmp_path, monkeypatch):
    monkeypatch.setattr(steer, "MAX_NAMES", 5)
    run = runs.Run("r", tmp_path, tmp_path)
    box = steer.mailbox(run)
    for n in range(8):
        a_request(box, str(n), "M")
    mail = steer._Mailbox(box, steer.Steering())
    assert (len(mail.take()), len(mail.take())) == (5, 3)


class _Listing(list):
    """What os.scandir() gives, in the order that a test chooses."""

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def test_a_request_is_seen_however_many_other_files_are_listed_before_it(tmp_path, monkeypatch):
    monkeypatch.setattr(steer, "MAX_NAMES", 5)
    run = runs.Run("r", tmp_path, tmp_path)
    box = steer.mailbox(run)
    a_request(box, "a", "Stop")
    for n in range(steer.MAX_NAMES * 2):
        write_json(box / f"old-{n}.reply", {"status": "queued"})  # answers that no sender reads now
    scandir = os.scandir
    requests_last = lambda path: _Listing(sorted(scandir(path), key=lambda entry: entry.name.endswith(".request")))
    monkeypatch.setattr(os, "scandir", requests_last)
    mail = steer._Mailbox(box, steer.Steering())
    assert [ident for ident, *_ in mail.take()] == ["a"]


def test_the_oldest_request_goes_first_even_when_its_file_was_written_last(tmp_path):
    run = runs.Run("r", tmp_path, tmp_path)
    box = steer.mailbox(run)
    sent = steer.now()
    for n in range(steer.MAX_PER_LOOK):
        a_request(box, f"b{n}", f"Later {n}", sent - 10 + n)
    first = a_request(box, "a", "First", sent - 11)  # wf steer took the time, and wrote the file after the others
    soon = time.time() + 60
    os.utime(first, (soon, soon))
    mail = steer._Mailbox(box, steer.Steering())
    assert [ident for ident, *_ in mail.take()] == ["a"] + [f"b{n}" for n in range(steer.MAX_PER_LOOK - 1)]


def test_wf_steer_adds_no_request_to_a_full_run_and_says_so(repo, monkeypatch, capsys):
    monkeypatch.setattr(steer, "MAX_IN_MAILBOX", 3)
    run = made_run()
    box = steer.mailbox(run)
    waiting = [a_request(box, str(n), "M") for n in range(3)]
    with run.driving():
        assert cli.main(["steer", run.id, "ask", "Stop"]) == 1
    assert capsys.readouterr().err == (
        "wf steer: the run is full: 3 messages from wf steer wait for it or are being answered. Nothing was sent. "
        "Send it again when some of them have their answers\n")
    assert left_in_mailbox(run.dir) == sorted(file.name for file in waiting)


def test_senders_at_the_same_time_cant_pass_the_runs_limit_together(repo, monkeypatch):
    monkeypatch.setattr(steer, "MAX_IN_MAILBOX", 3)
    monkeypatch.setattr(steer, "ANSWER_SECONDS", 0.5)

    def slowly(path, value):  # a sender that has counted takes its time to write: the others count meanwhile
        time.sleep(0.1)
        write_json(path, value)

    monkeypatch.setattr(steer, "write_json", slowly)
    run = made_run()
    together = threading.Barrier(8)

    def send(n: int) -> str:
        together.wait()
        try:
            return steer.send(run, "ask", f"Message {n}")["reason"]
        except steer.SteerError as error:
            return str(error)

    with run.driving(), ThreadPoolExecutor(8) as senders:
        answers = list(senders.map(send, range(8)))
    nobody_took = "the wf process that drives the run didn't take the message within 0.5 seconds"
    assert sorted(answers) == sorted([nobody_took] * 3 + [steer.FULL.format(3)] * 5)
    assert left_in_mailbox(run.dir) == []


def test_a_message_the_driver_took_counts_toward_the_runs_limit_until_its_answer(repo, monkeypatch):
    monkeypatch.setattr(steer, "MAX_IN_MAILBOX", 2)
    monkeypatch.setattr(steer, "ANSWER_SECONDS", 0.1)
    run = made_run()
    box = steer.mailbox(run)
    a_request(box, "a", "Stop")
    mail = steer._Mailbox(box, steer.Steering())
    ((ident, request, problem, taken),) = mail.take()  # being answered
    a_request(box, "b", "Wait")  # waiting
    with run.driving():
        with pytest.raises(steer.SteerError, match="the run is full: 2 messages"):
            steer.send(run, "ask", "Stop")
        asyncio.run(mail._answer(ident, request, problem, taken))
        assert not taken.exists()
        assert steer.send(run, "ask", "Stop")["reason"].endswith("didn't take the message within 0.1 seconds")


def test_the_driver_answers_no_more_messages_at_once_than_the_runs_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(steer, "MAX_IN_MAILBOX", 3)
    run = runs.Run("r", tmp_path, tmp_path)
    for n in range(5):  # written by a process other than wf steer, which nothing counted
        a_request(steer.mailbox(run), str(n), f"Message {n}")

    class Slow(steer.Steering):
        def __init__(self):
            super().__init__()
            self.texts, self.go = [], asyncio.Event()

        async def deliver(self, path, message):
            self.texts.append(message.text)
            await self.go.wait()
            return {"status": "queued", "attempt": 1, "harness": "codex"}

    async def drive():
        slow = Slow()
        async with steer.answering(run, slow):
            await asyncio.sleep(steer.POLL_SECONDS * 3)
            at_once = len(slow.texts)
            slow.go.set()
            await asyncio.sleep(steer.POLL_SECONDS * 3)
        return at_once, slow.texts

    at_once, texts = asyncio.run(drive())
    assert at_once == 3
    assert texts == [f"Message {n}" for n in range(5)]


def test_an_answer_goes_once_its_sender_has_gone_whatever_the_wall_clock_says(tmp_path, monkeypatch):
    monkeypatch.setattr(steer, "ANSWER_SECONDS", 0.1)
    run = runs.Run("r", tmp_path, tmp_path)
    box = steer.mailbox(run)
    box.mkdir(parents=True)
    write_json(box / "earlier.reply", {"status": "queued"})  # an earlier driver's: it died
    mail = steer._Mailbox(box, steer.Steering())
    mail.tidy_up()
    mail.reply("fresh", {"status": "queued"})
    long_ago = time.time() - 3600
    os.utime(box / "fresh.reply", (long_ago, long_ago))
    mail.forget_answers()
    assert sorted(p.name for p in box.iterdir()) == ["earlier.reply", "fresh.reply"]
    time.sleep(0.3)
    mail.forget_answers()
    assert list(box.iterdir()) == []


def test_with_no_boot_id_wf_steer_is_off_and_no_request_counts_as_new(repo, monkeypatch):
    monkeypatch.setattr(steer, "boot", lambda: "")
    run = made_run()
    with run.driving(), pytest.raises(steer.SteerError, match="this system doesn't say which boot it is in"):
        steer.send(run, "ask", "Stop")
    assert steer._older(steer.now() - 1, "", steer.ANSWER_SECONDS)  # a request from before a reboot can't look new
    planted = a_request(steer.mailbox(run), "x", "Stop", boot="")
    answered(run, Recorder())
    assert list(steer.mailbox(run).iterdir()) == [planted]


def test_the_boot_id_comes_from_the_system_and_no_program_on_path_runs(tmp_path, monkeypatch):
    bin = tmp_path / "bin"
    bin.mkdir()
    (bin / "sysctl").write_text(f"#!/bin/sh\ntouch {tmp_path / 'ran'}\necho planted\n")
    (bin / "sysctl").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin}{os.pathsep}{os.environ['PATH']}")
    steer.boot.cache_clear()
    try:
        found = steer.boot()
    finally:
        steer.boot.cache_clear()
    assert found and found != "planted"
    assert not (tmp_path / "ran").exists()


def test_a_turn_that_fails_leaves_the_message_it_carried_unconfirmed(repo, monkeypatch):
    fake = Fake("claude-code", paused(repo, "p"), paused(repo, "q", end=TurnEnd("failed", "it broke")))
    steering = at_pause(repo, "p")
    (repo / "q.go").touch()
    assert run_with(monkeypatch, fake.factory) == 1
    assert steering.result().returncode == 0
    queued, unconfirmed = steer_events()
    assert (queued["status"], unconfirmed["status"]) == ("queued", "unconfirmed")
    assert unconfirmed["reason"] == "the turn that carried it failed, so wf can't tell whether the agent read it: it broke"


def test_a_log_line_that_cant_be_written_fails_nothing_and_the_event_still_goes(tmp_path):
    events = []
    spec = a_spec(tmp_path, events)
    spec.log = tmp_path  # a folder: every transcript write fails
    live = steer.Live(spec, 1, "cursor", None)
    live._sending = [steer.Message("a", "A")]
    live.sent(TurnEnd("done"))
    live._held = [steer.Message("b", "B")]
    live.end()
    assert live._sending == live._held == []
    assert [(e["id"], e["status"]) for e in events] == [("a", "delivered"), ("b", "dropped")]


def test_wf_steer_reads_no_more_of_a_file_than_it_needs(repo, capsys, tmp_path):
    run = made_run()
    (tmp_path / "big.md").write_text("x" * (steer.MAX_CHARACTERS * 3))
    assert cli.main(["steer", run.id, "ask", "--file", str(tmp_path / "big.md")]) == 1
    assert "the message is longer than 100,000 characters" in capsys.readouterr().err
