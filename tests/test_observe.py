"""Watching runs: the agent sessions that run now (wf sessions), and the event stream (wf watch)."""

import asyncio
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from textwrap import dedent

import pytest

from fakes import Fake, hang, submit, talk
from workflow import cli, harnesses, nodes, observe, runner, runs
from workflow.harnesses.base import TurnEnd, Usage
from workflow.runs import Run

HELD_AGENT = Path(__file__).parent / "held_agent.py"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(runner, "interactive", lambda: False)
    subprocess.run(["git", "init", "-q"], check=True)
    return tmp_path


FLOW = """
version: 1
name: work
defaults:
  harness: claude-code
  harnesses:
    claude-code: {model: opus, effort: high}
start: {input: {}}
nodes:
  work: {needs: [start], instruction: Work.}
end: {output: {summary: "{{ work.summary }}"}}
"""


def only_run() -> Run:
    (folder,) = (Path(".wf") / "runs").iterdir()
    return Run.at(folder)


def until(condition, seconds: float = 30) -> None:
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.05)


def held_run(marks: Path, release: Path) -> subprocess.Popen:
    """A `wf run` process whose agent holds its session until `release` exists (held_agent.py)."""
    return subprocess.Popen([sys.executable, str(HELD_AGENT), str(marks), str(release), "run", "flow.yaml"],
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def test_each_session_starts_and_ends_with_an_event(repo, monkeypatch):
    fake = Fake("claude-code", submit({"summary": "done"}))
    monkeypatch.setitem(harnesses.FACTORIES, "claude-code", fake.factory)
    Path("flow.yaml").write_text(FLOW.lstrip())
    assert cli.main(["run", "flow.yaml"]) == 0
    events = [json.loads(line) for line in (only_run().dir / "events.jsonl").read_text().splitlines()]
    started = next(e for e in events if e["event"] == "session_started")
    assert {k: started[k] for k in ("node", "pass", "attempt", "harness", "model", "effort", "log", "pid")} == {
        "node": "work", "pass": 1, "attempt": 1, "harness": "claude-code", "model": "opus", "effort": "high",
        "log": "nodes/work/1/attempt-1.log", "pid": os.getpid()}
    kinds = [e["event"] for e in events]
    assert kinds.index("session_started") < kinds.index("agent_tool") < kinds.index("session_ended") < kinds.index("node_finished")


def test_a_session_tells_what_its_attempt_has_used_so_far_while_and_after_each_turn_runs(repo, monkeypatch):
    async def long_turn(session):
        session.pending = Usage(300, 0.03)  # what the harness has reported of the turn so far
        await asyncio.sleep(0.3)
        session.pending = Usage()
        session.spec.tool.submit({"summary": "done"})
        return TurnEnd("done", usage=Usage(500, 0.05))

    fake = Fake("claude-code", talk(tokens=100, cost=0.01), long_turn)
    monkeypatch.setitem(harnesses.FACTORIES, "claude-code", fake.factory)
    monkeypatch.setattr(nodes, "USAGE_SECONDS", 0.05)
    Path("flow.yaml").write_text(FLOW.lstrip())
    assert cli.main(["run", "flow.yaml"]) == 0
    events = [json.loads(line) for line in (only_run().dir / "events.jsonl").read_text().splitlines()]
    told = [e for e in events if e["event"] == "agent_usage"]
    assert [(e["tokens"], e["cost_usd"]) for e in told] == [(100, 0.01), (400, 0.04), (600, 0.06)]
    assert {(e["node"], e["pass"], e["attempt"], e["harness"]) for e in told} == {("work", 1, 1, "claude-code")}
    kinds = [e["event"] for e in events]
    assert kinds.index("session_started") < kinds.index("agent_usage")
    assert max(n for n, kind in enumerate(kinds) if kind == "agent_usage") < kinds.index("session_ended")


def test_a_session_tells_its_usage_again_only_when_the_tokens_or_the_cost_change(repo, monkeypatch):
    reported = Usage(1200, 0.0, input_tokens=400, output_tokens=200, cache_read_tokens=600)

    async def reported_while_it_runs(session):
        session.pending = reported  # the harness reports all of the turn before it ends
        await asyncio.sleep(0.3)
        session.pending = Usage()
        session.spec.tool.submit({"summary": "done"})
        return TurnEnd("done", usage=reported)

    monkeypatch.setitem(harnesses.FACTORIES, "claude-code", Fake("claude-code", reported_while_it_runs).factory)
    monkeypatch.setattr(nodes, "USAGE_SECONDS", 0.05)
    Path("flow.yaml").write_text(FLOW.lstrip())
    assert cli.main(["run", "flow.yaml"]) == 0
    events = [json.loads(line) for line in (only_run().dir / "events.jsonl").read_text().splitlines()]
    assert [(e["tokens"], e["cost_usd"]) for e in events if e["event"] == "agent_usage"] == [(1200, 0.0)]


def test_a_session_inside_a_sub_workflow_names_its_transcript_from_the_run_folder(repo, monkeypatch):
    fake = Fake("claude-code", submit({"summary": "done"}))
    monkeypatch.setitem(harnesses.FACTORIES, "claude-code", fake.factory)
    Path("flow.yaml").write_text(FLOW.lstrip())
    Path("outer.yaml").write_text(dedent("""
        version: 1
        name: outer
        start: {input: {}}
        nodes:
          sub: {needs: [start], workflow: flow.yaml}
        end: {output: {}}
    """).lstrip())
    assert cli.main(["run", "outer.yaml"]) == 0
    run = only_run()
    started = next(json.loads(l) for l in (run.dir / "events.jsonl").read_text().splitlines() if '"session_started"' in l)
    assert started["node"] == "sub/work"
    assert started["log"] == "nodes/sub/1/attempt-1/nodes/work/1/attempt-1.log"
    assert (run.dir / started["log"]).with_name("instruction.md").is_file()  # the fake agent writes no transcript


def test_wf_sessions_lists_the_agent_sessions_that_run_now(repo, tmp_path_factory, capsys):
    marks, release = tmp_path_factory.mktemp("marks"), tmp_path_factory.mktemp("release") / "go"
    Path("flow.yaml").write_text(FLOW.lstrip())
    process = held_run(marks, release)
    try:
        until(lambda: (marks / f"{process.pid}.started").exists())
        capsys.readouterr()
        assert cli.main(["sessions", "--json"]) == 0
        (session,) = json.loads(capsys.readouterr().out)
        run = only_run()
        assert {k: session[k] for k in ("run", "node", "pass", "index", "attempt", "harness", "model")} == {
            "run": run.id, "node": "work", "pass": 1, "index": None, "attempt": 1, "harness": "claude-code", "model": "opus"}
        assert session["last_tool"]["tool"] == "Read" and session["last_tool"]["detail"] == "notes.txt"
        assert session["transcript"] == str(run.dir / "nodes" / "work" / "1" / "attempt-1.log")
        assert session["instruction"] == str(run.dir / "nodes" / "work" / "1" / "instruction.md")
        assert session["started"] and 0 <= session["quiet_s"] < 30

        assert cli.main(["sessions", run.id]) == 0
        lines = capsys.readouterr().out.splitlines()
        assert lines[0] == f"{run.id}  work  pass 1, attempt 1  claude-code opus"
        assert lines[1].startswith("  started ") and lines[1].endswith("last tool call: Read notes.txt")
    finally:
        release.touch()
        out, _ = process.communicate(timeout=60)
    assert process.returncode == 0, out
    capsys.readouterr()
    assert cli.main(["sessions"]) == 0
    assert capsys.readouterr().out == "No agent session runs now.\n"


def test_a_session_left_open_by_a_process_that_no_longer_drives_the_run_is_not_listed(repo):
    Path("flow.yaml").write_text(FLOW.lstrip())
    run = runs.create(Path.cwd(), "work", [Path("flow.yaml")], Path("flow.yaml"), {})  # a run folder that nobody drives
    log = run.dir / "nodes" / "work" / "1" / "attempt-1.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("")
    lines = [{"time": "2026-10-19T10:00:00+00:00", "event": "session_started", "node": "work", "pass": 1, "attempt": attempt,
              "harness": "claude-code", "model": "opus", "pid": pid, "log": "nodes/work/1/attempt-1.log"}
             for attempt, pid in ((1, 999_999_999), (2, os.getpid()))]
    (run.dir / "events.jsonl").write_text("".join(json.dumps(line) + "\n" for line in lines))
    assert observe.sessions(run) == []  # nobody drives the run
    with run.driving():  # now this process does: its own session is live, the other one's isn't
        (session,) = observe.sessions(run)
    assert session["attempt"] == 2 and session["last_tool"] is None


def test_a_session_ends_with_its_attempt_its_item_or_the_workflow_node_around_it(repo):
    Path("flow.yaml").write_text(FLOW.lstrip())
    run = runs.create(Path.cwd(), "work", [Path("flow.yaml")], Path("flow.yaml"), {})
    opened = [("build[0]/work", None), ("build[1]/work", None), ("review", 1), ("review", 2), ("think", None), ("solo", None)]
    lines = [{"event": "session_started", "node": node, "pass": 1, "attempt": 1, "harness": "claude-code", "pid": os.getpid(),
              **({"index": index} if index is not None else {})} for node, index in opened]
    lines += [{"event": "attempt_failed", "node": "build", "index": 0, "pass": 1, "attempt": 1, "error": "timed out after 1s"},
              {"event": "item_failed", "node": "review", "index": 1, "error": "no result"},
              {"event": "attempt_failed", "node": "think", "pass": 1, "attempt": 1, "error": "crashed"},
              {"event": "node_finished", "node": "solo", "pass": 1}]
    (run.dir / "events.jsonl").write_text("".join(json.dumps({"time": "2026-10-19T10:00:00+00:00", **line}) + "\n" for line in lines))
    with run.driving():
        assert [s["node"] for s in observe.sessions(run)] == ["build[1]/work", "review[2]"]


def test_a_session_inside_a_workflow_node_that_timed_out_is_not_listed(repo, monkeypatch):
    monkeypatch.setitem(harnesses.FACTORIES, "claude-code", Fake("claude-code", hang()).factory)
    Path("flow.yaml").write_text(FLOW.lstrip())
    Path("outer.yaml").write_text(dedent("""
        version: 1
        name: outer
        start: {input: {}}
        nodes:
          sub: {needs: [start], workflow: flow.yaml, timeout: 1s}
        end: {output: {}}
    """).lstrip())
    assert cli.main(["run", "outer.yaml"]) == 1  # the agent inside never answers
    run = only_run()
    kinds = [(e["event"], e.get("node")) for e in map(json.loads, (run.dir / "events.jsonl").read_text().splitlines())]
    assert ("session_started", "sub/work") in kinds and ("attempt_failed", "sub") in kinds
    assert ("session_ended", "sub/work") not in kinds  # the stopped sub-workflow couldn't pass it up
    with run.driving():  # as if its process went on with other work
        assert observe.sessions(run) == []


def test_wf_watch_prints_the_steps_and_tool_calls_as_they_come_until_the_run_ends(repo, tmp_path_factory):
    marks, release = tmp_path_factory.mktemp("marks"), tmp_path_factory.mktemp("release") / "go"
    Path("flow.yaml").write_text(FLOW.lstrip())
    process = held_run(marks, release)
    lines, ended = [], {}
    try:
        until(lambda: (Path(".wf") / "runs").is_dir() and any((Path(".wf") / "runs").iterdir()))
        run = only_run()
        watcher = threading.Thread(target=lambda: ended.update(observe.watch(run, say=lines.append, poll=0.05)))
        watcher.start()
        until(lambda: any("Read notes.txt" in line for line in lines))
        assert not ended  # still watching: the run goes on
    finally:
        release.touch()
        out, _ = process.communicate(timeout=60)
    assert process.returncode == 0, out
    watcher.join(30)
    assert ended["status"] == "succeeded"
    texts = [line[9:] for line in lines]  # after the time of day
    assert texts == ["▶ work", "  work: attempt 1 on claude-code (opus)", "  work: Read notes.txt", "✓ work"]
    assert all(line[2] == ":" and line[5] == ":" for line in lines)


def test_wf_watch_on_a_run_that_ended_prints_the_last_events_and_how_it_ended(repo, monkeypatch, capsys):
    fake = Fake("claude-code", submit({"summary": "done"}))
    monkeypatch.setitem(harnesses.FACTORIES, "claude-code", fake.factory)
    Path("flow.yaml").write_text(FLOW.lstrip())
    assert cli.main(["run", "flow.yaml"]) == 0
    capsys.readouterr()
    assert cli.main(["watch", only_run().id]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].endswith("▶ work") and out[-1] == f"{only_run().id} [work]: succeeded"

    for number in range(30):
        only_run().add_event({"event": "warning", "message": f"note {number}"})
    assert cli.main(["watch", only_run().id]) == 0
    out = capsys.readouterr().out.splitlines()
    assert len(out) == observe.WATCH_BACK + 1 and out[-2].endswith("⚠ note 29")
    assert cli.main(["watch", only_run().id, "--from-start"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].endswith("▶ work") and len(out) == 4 + 30 + 1  # every event line, then the state


ASK = """
version: 1
name: ask
start: {input: {}}
nodes:
  approve: {needs: [start], human: "Ship it?", output: {ok: {type: boolean}}}
  after: {needs: [approve], command: "echo shipped"}
end: {output: {}}
"""


def test_wf_watch_goes_on_through_a_question_until_the_run_succeeds(repo):
    Path("ask.yaml").write_text(ASK.lstrip())
    assert cli.main(["run", "ask.yaml"]) == 2  # it waits for a person
    run = only_run()
    lines, ended = [], {}
    tell = lambda state: f"now {state['status']}"  # noqa: E731
    watcher = threading.Thread(target=lambda: ended.update(observe.watch(run, say=lines.append, poll=0.05, tell=tell)))
    watcher.start()
    until(lambda: "now waiting" in lines)
    key = run.status()["questions"][0]["key"]
    Path("answers.json").write_text(json.dumps({key: {"ok": True}}))
    assert cli.main(["answer", run.id, "answers.json"]) == 0
    watcher.join(30)
    assert ended["status"] == "succeeded"
    assert lines.count("now waiting") == 1 and lines[-1].endswith("✓ after")


def test_wf_watch_stops_when_the_run_is_discarded(repo):
    Path("flow.yaml").write_text(FLOW.lstrip())
    run = runs.create(Path.cwd(), "work", [Path("flow.yaml")], Path("flow.yaml"), {})
    ended = {}
    watcher = threading.Thread(target=lambda: ended.update(observe.watch(run, say=lambda _: None, poll=0.05)))
    watcher.start()
    time.sleep(0.2)
    runs.discard(run, branches=False)
    watcher.join(10)
    assert ended == {"run": run.id, "status": "discarded", "workflow": "work"}


def test_reading_events_leaves_a_line_that_is_still_being_written(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text('{"event": "node_started", "node": "a"}\n{"event": "node_fin')
    found, offset = observe.read_events(path)
    assert [e["event"] for e in found] == ["node_started"]
    with path.open("a") as file:
        file.write('ished", "node": "a"}\n')
    found, offset = observe.read_events(path, offset)
    assert [e["event"] for e in found] == ["node_finished"]
    assert observe.read_events(path, offset) == ([], offset)
    assert observe.read_events(tmp_path / "none.jsonl") == ([], 0)
