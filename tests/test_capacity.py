"""The cap on live agent sessions across every wf process (capacity.py).

Slots are files under WF_HOME, which conftest points at a fresh folder in every
test. A test holds a slot itself with capacity.take() to make a run wait. Tests
across processes start real `wf run` processes whose agent holds its session
until the test lets it go (held_agent.py), and read from the agents' marks when
each session was live.
"""

import asyncio
import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from textwrap import dedent

import pytest

from workflow import capacity, cli, harnesses, operate, runner, ui
from workflow.harnesses.base import TurnEnd, Usage
from workflow.runs import Run
from fakes import Fake, ends, submit

HELD_AGENT = Path(__file__).parent / "held_agent.py"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    subprocess.run(["git", "init", "-q"], check=True)
    monkeypatch.setattr(runner, "interactive", lambda: False)
    return tmp_path


@pytest.fixture
def marks(tmp_path_factory) -> Path:
    return tmp_path_factory.mktemp("marks")


def agent_flow(name: str = "work", node: str = "work: {needs: [start], instruction: Work.}", top: str = "") -> str:
    Path(f"{name}.yaml").write_text(dedent("""
        version: 1
        name: {name}
        {top}
        defaults:
          harness: claude-code
          harnesses:
            claude-code: {{model: opus}}
        start: {{input: {{}}}}
        nodes:
          {node}
        end: {{output: {{summary: "{{{{ work.summary }}}}"}}}}
    """).format(name=name, node=node, top=top))
    return f"{name}.yaml"


def run_of(name: str) -> Path | None:
    """The folder of the one run of workflow `name`, once it has its run.json."""
    folders = (Path(".wf") / "runs").glob("*/run.json")
    found = [f.parent for f in folders if json.loads(f.read_text())["workflow"] == name]
    return found[0] if found else None


def kinds(name: str) -> list[str]:
    path = run_of(name) / "events.jsonl"
    return [json.loads(line)["event"] for line in path.read_text().splitlines()] if path.exists() else []


def events(name: str) -> list[dict]:
    return [json.loads(line) for line in (run_of(name) / "events.jsonl").read_text().splitlines()]


def waiting(name: str) -> bool:
    """Does the run of workflow `name` wait for an agent slot, as its status.json says?"""
    folder = run_of(name)
    return folder is not None and bool(Run.at(folder).status().get("agents_waiting"))


def contend_at(monkeypatch, attempt: int, seconds: float) -> list[Callable[[], None]]:
    """Make agent attempt `attempt` find the one slot of a cap of 1 taken: the test takes it
    the moment that attempt first asks for it, and holds it for `seconds` (hold_slot). Returns
    a list that then holds the let_go()."""
    real, held = capacity.take, []

    def take(cap: int, holder: dict):
        if holder.get("attempt") == attempt and not held:
            held.append(hold_slot(seconds))
        return real(cap, holder)

    monkeypatch.setattr(capacity, "take", take)
    return held


def failing_flow() -> str:
    """A run that fails at `check` until a file `ready` exists, then runs agent `work`."""
    return agent_flow(node='check: {needs: [start], command: "test -f ready"}\n  work: {needs: [check], instruction: Work.}')


def until(condition, seconds: float = 30) -> None:
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.05)


def hold_slot(seconds: float, waiter: str | None = None, seen: dict | None = None) -> Callable[[], None]:
    """Take the one slot of a cap of 1 now, and let it go `seconds` later, or when the returned
    let_go() is called, which waits for that. With `waiter`, the seconds count from when the run
    of that workflow waits for the slot, and `seen` gets its state then."""
    slot = capacity.take(1, {"test": True})
    assert slot is not None
    early = threading.Event()

    def later():
        try:
            if waiter:
                until(lambda: early.is_set() or waiting(waiter))
                if seen is not None and not early.is_set():
                    seen.update(operate.state(Run.at(run_of(waiter))))
                    seen["steps"] = ui.run_detail(Run.at(run_of(waiter)))["metrics"]["nodes"]  # as wf ui shows them
            early.wait(seconds)
        finally:
            slot.release()

    thread = threading.Thread(target=later)
    thread.start()

    def let_go() -> None:
        early.set()
        thread.join()

    return let_go


def wf_process(marks: Path, release: Path, *args: str) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, str(HELD_AGENT), str(marks), str(release), *args],
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def succeeds(process: subprocess.Popen) -> None:
    try:
        out, _ = process.communicate(timeout=60)
    except subprocess.TimeoutExpired:
        process.kill()
        raise
    assert process.returncode == 0, out


def mark(marks: Path, process: subprocess.Popen, which: str) -> Path:
    return marks / f"{process.pid}.{which}"


# ---------- setting the cap ----------


def test_the_cap_comes_from_wf_max_agents_or_the_config_in_wf_home(monkeypatch, wf_home):
    assert capacity.configured() is None
    (wf_home / "config.toml").write_text("max_agents = 3\n")
    assert capacity.configured() == 3
    monkeypatch.setenv("WF_MAX_AGENTS", "5")  # the environment wins
    assert capacity.configured() == 5
    monkeypatch.setenv("WF_MAX_AGENTS", "")  # a blank variable counts as no variable
    assert capacity.configured() == 3


def test_another_key_in_the_config_leaves_the_cap_alone(wf_home):
    (wf_home / "config.toml").write_text('[cursor]\nruntime = "acp"\n')
    assert capacity.configured() is None
    (wf_home / "config.toml").write_text('max_agents = 2\n[cursor]\nruntime = "acp"\n')
    assert capacity.configured() == 2


UNKNOWN = "unknown key max_agent. The top of the file takes runs_dir and max_agents"


@pytest.mark.parametrize("env, config, problem", [
    ("0", None, "WF_MAX_AGENTS must be a whole number of 1 or more, not `0`"),
    ("four", None, "not `four`"),
    ("²", None, "WF_MAX_AGENTS must be a whole number of 1 or more, not `²`"),
    ("9" * 5000, None, "WF_MAX_AGENTS must be a whole number of 1 or more"),
    (None, "max_agents =\n", "isn't valid TOML: Invalid value (at line 1, column 13)"),
    (None, "[max_agents]\n", "max_agents must be a whole number of 1 or more, not a table"),
    (None, 'max_agents = "4"\n', 'max_agents must be a whole number of 1 or more, not `"4"`'),
    ("2", "max_agent = 4\n", UNKNOWN),  # checked though the variable wins
    ("2", "max_agents = 0\n", "config.toml: max_agents must be a whole number of 1 or more, not `0`"),
    (None, "max_agents = 2.5\n", "must be a whole number of 1 or more, not `2.5`"),
    (None, "max_agents = true\n", "must be a whole number of 1 or more, not `true`"),
    (None, "max_agent = 4\n", UNKNOWN),
])
def test_a_bad_cap_stops_wf_run_before_the_run_exists(repo, monkeypatch, capsys, wf_home, env, config, problem):
    if env is not None:
        monkeypatch.setenv("WF_MAX_AGENTS", env)
    if config is not None:
        (wf_home / "config.toml").write_text(config)
    assert cli.main(["run", agent_flow()]) == 1
    assert problem in capsys.readouterr().err
    assert not Path(".wf").exists()


@pytest.mark.parametrize("env, config, problem", [
    ("0", None, "wf retry: WF_MAX_AGENTS must be a whole number of 1 or more, not `0`"),
    (None, "max_agent = 4\n", UNKNOWN),
])
def test_a_bad_cap_stops_wf_retry(repo, monkeypatch, capsys, wf_home, env, config, problem):
    assert cli.main(["run", failing_flow()]) == 1
    folder = run_of("work")
    if env is not None:
        monkeypatch.setenv("WF_MAX_AGENTS", env)
    if config is not None:
        (wf_home / "config.toml").write_text(config)
    capsys.readouterr()
    Path("ready").touch()
    assert cli.main(["retry", folder.name]) == 1
    assert problem in capsys.readouterr().err
    assert Run.at(folder).status()["status"] == "failed"  # it didn't continue


def test_a_smaller_cap_uses_only_the_first_slots(wf_home):
    small = capacity.take(1, {"cap": 1})
    big = capacity.take(2, {"cap": 2})
    assert capacity.take(1, {"cap": 1}) is None  # a cap of 1 has slot 0 only
    assert json.loads((wf_home / "agent-slots" / "1").read_text()) == {"cap": 2}
    small.release()
    big.release()


def test_attempts_that_wait_for_one_slot_take_it_one_at_a_time(wf_home):
    async def main():
        taken = capacity.take(1, {"n": 0})
        got, live, most = [], [0], [0]

        async def waiter(n: int):
            slot = await capacity.wait(1, {"n": n}, stop=lambda: False)
            live[0] += 1
            most[0] = max(most[0], live[0])
            got.append(n)
            await asyncio.sleep(3 * capacity.POLL_SECONDS)  # long enough for the other to try again
            live[0] -= 1
            slot.release()

        waiters = [asyncio.create_task(waiter(n)) for n in (1, 2)]
        await asyncio.sleep(3 * capacity.POLL_SECONDS)
        assert got == []  # neither while the slot is taken
        taken.release()
        await asyncio.wait_for(asyncio.gather(*waiters), 10)
        assert sorted(got) == [1, 2] and most[0] == 1  # each in the end, one at a time, in either order

    asyncio.run(main())


def test_each_wf_home_has_its_own_slots(monkeypatch, tmp_path):
    held = capacity.take(1, {"home": "first"})
    monkeypatch.setenv("WF_HOME", str(tmp_path / "other-home"))
    other = capacity.take(1, {"home": "other"})
    assert other is not None
    other.release()
    held.release()


def test_a_wait_tries_again_every_0_2_seconds(monkeypatch, wf_home):
    taken = capacity.take(1, {"n": 0})
    pauses, real_sleep = [], asyncio.sleep

    async def sleep(seconds):
        pauses.append(seconds)
        if len(pauses) == 3:
            taken.release()
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", sleep)
    slot = asyncio.run(capacity.wait(1, {"n": 1}, stop=lambda: False))
    assert pauses == [0.2, 0.2, 0.2]  # three tries found the slot taken, the fourth got it
    slot.release()


def test_a_slot_has_one_holder_at_a_time(wf_home):
    first = capacity.take(2, {"node": "a"})
    second = capacity.take(2, {"node": "b"})
    assert capacity.take(2, {"node": "c"}) is None
    assert json.loads((wf_home / "agent-slots" / "1").read_text()) == {"node": "b"}
    second.release()
    third = capacity.take(2, {"node": "c"})
    assert third is not None
    first.release()
    third.release()


# ---------- runs ----------


def test_a_slot_file_names_its_holder(repo, monkeypatch, wf_home):
    monkeypatch.setenv("WF_MAX_AGENTS", "1")
    holder = {}

    async def look(session):  # while the session is live
        holder.update(json.loads((wf_home / "agent-slots" / "0").read_text()))
        session.spec.tool.submit({"summary": "done"})
        return TurnEnd("done", usage=Usage())

    monkeypatch.setitem(harnesses.FACTORIES, "claude-code", Fake("claude-code", look).factory)
    assert cli.main(["run", agent_flow()]) == 0
    assert holder == {"pid": os.getpid(), "run": run_of("work").name, "node": "work", "attempt": 1}


def test_wf_run_and_wf_retry_read_the_cap_once_when_they_start(repo, monkeypatch):
    monkeypatch.setenv("WF_MAX_AGENTS", "2")
    reads, real = [], capacity.configured
    monkeypatch.setattr(capacity, "configured", lambda: reads.append(1) or real())
    fake = Fake("claude-code", submit({"summary": "first"}), submit({"summary": "done"}))
    monkeypatch.setitem(harnesses.FACTORIES, "claude-code", fake.factory)
    two = 'first: {needs: [start], instruction: First.}\n  work: {needs: [first], instruction: Work.}'
    assert cli.main(["run", agent_flow(node=two)]) == 0
    assert len(fake.sent) == 2 and len(reads) == 1  # two agent attempts, one read
    assert cli.main(["retry", run_of("work").name]) == 0  # already succeeded
    assert len(reads) == 2


def test_without_a_cap_nothing_waits_and_no_slot_is_made(repo, monkeypatch, wf_home):
    monkeypatch.setitem(harnesses.FACTORIES, "claude-code", Fake("claude-code", submit({"summary": "done"})).factory)
    assert cli.main(["run", agent_flow()]) == 0
    assert not (wf_home / "agent-slots").exists()
    assert not {"slot_waiting", "slot_taken"} & set(kinds("work"))


def test_a_waiting_attempt_keeps_its_retries_and_its_timeout_starts_with_its_slot(repo, monkeypatch, capsys):
    # The wait (1.5s) is longer than the timeout (1s). Counted in attempt 1, it would time
    # that attempt out, and attempt 2, the one retry, would then fail on the fake's "boom"
    monkeypatch.setenv("WF_MAX_AGENTS", "1")
    live = {}

    async def look_then_fail(session):  # while the session is live, the wait is no longer in status.json
        live.update(Run.at(run_of("work")).status())
        return TurnEnd("failed", "boom", Usage())

    fake = Fake("claude-code", look_then_fail, submit({"summary": "done"}))
    monkeypatch.setitem(harnesses.FACTORIES, "claude-code", fake.factory)
    seen = {}
    let_go = hold_slot(1.5, waiter="work", seen=seen)
    assert cli.main(["run", agent_flow(node="work: {needs: [start], instruction: Work., timeout: 1s, retry: 1}")]) == 0
    let_go()
    assert json.loads((run_of("work") / "output.json").read_text()) == {"summary": "done"}
    assert "work: attempt 1 waits for an agent slot (1 of 1 in use)" in capsys.readouterr().out
    said = [(e["event"], e["attempt"], e.get("error")) for e in events("work") if e["event"].startswith(("slot", "attempt"))]
    assert said == [("slot_waiting", 1, None), ("slot_taken", 1, None), ("attempt_failed", 1, "boom"),
                    ("attempt_started", 2, None)]
    assert [e["waited_s"] for e in events("work") if e["event"] == "slot_taken"][0] >= 1.4
    assert seen["status"] == "running"
    assert [(w["node"], w["attempt"]) for w in seen["agents_waiting"]] == [("work", 1)]
    assert (seen["steps"]["work"]["passes"], seen["steps"]["work"]["attempts"]) == (1, 0)  # no attempt has started yet
    assert datetime.fromisoformat(seen["agents_waiting"][0]["since"]) <= datetime.now().astimezone()
    assert live["status"] == "running" and "agents_waiting" not in live


def test_command_and_human_nodes_take_no_slot(repo, monkeypatch):
    monkeypatch.setenv("WF_MAX_AGENTS", "1")
    Path("ask.yaml").write_text(dedent("""
        version: 1
        name: ask
        start: {input: {}}
        nodes:
          build: {needs: [start], command: "echo built"}
          approve: {needs: [build], human: "Ship it?", output: {ok: {type: boolean}}}
        end: {output: {ok: "{{ approve.ok }}"}}
    """))
    let_go = hold_slot(3)  # a node that took a slot would wait for it, and say so in events.jsonl
    assert cli.main(["run", "ask.yaml"]) == 2  # waiting for a person
    let_go()
    assert not {"slot_waiting", "slot_taken"} & set(kinds("ask"))


def test_a_dry_run_takes_no_slot(repo, monkeypatch):
    monkeypatch.setenv("WF_MAX_AGENTS", "1")
    Path("script.yaml").write_text("agents:\n  work:\n    - {summary: scripted}\n")
    let_go = hold_slot(3)
    assert cli.main(["run", agent_flow(), "--dry-run", "script.yaml"]) == 0
    let_go()
    assert not {"slot_waiting", "slot_taken"} & set(kinds("work"))


def test_a_limit_reached_while_waiting_ends_the_wait_and_the_attempt_never_starts(repo, monkeypatch):
    monkeypatch.setenv("WF_MAX_AGENTS", "1")
    fake = Fake("claude-code", submit({"summary": "done"}))
    monkeypatch.setitem(harnesses.FACTORIES, "claude-code", fake.factory)
    let_go = hold_slot(60)  # only the limit can end the wait
    started = time.monotonic()
    assert cli.main(["run", agent_flow(top="limits: {duration: 1s}")]) == 1
    assert time.monotonic() - started < 10
    let_go()
    folder = run_of("work")
    assert json.loads((folder / "status.json").read_text())["status"] == "limit"
    assert fake.sent == []  # it never opened a session
    assert "slot_taken" not in kinds("work")
    assert cli.main(["retry", folder.name, "--limit", "duration=1h"]) == 0
    assert json.loads((folder / "output.json").read_text()) == {"summary": "done"}
    assert [e["attempt"] for e in events("work") if e["event"] == "slot_waiting"] == [1]
    assert "attempt_started" not in kinds("work")  # the attempt that ran is still attempt 1


def test_a_slot_that_comes_free_as_a_limit_is_reached_goes_back_unused(repo, monkeypatch):
    monkeypatch.setenv("WF_MAX_AGENTS", "1")
    fake = Fake("claude-code", submit({"summary": "done"}))
    monkeypatch.setitem(harnesses.FACTORIES, "claude-code", fake.factory)
    let_go = hold_slot(60)

    async def wait(cap, holder, stop):  # the slot comes free after the limit, before the wait asks stop()
        await asyncio.sleep(1.2)
        let_go()
        return capacity.take(cap, holder)

    monkeypatch.setattr(capacity, "wait", wait)
    assert cli.main(["run", agent_flow(top="limits: {duration: 1s}")]) == 1
    assert Run.at(run_of("work")).status()["status"] == "limit"
    assert fake.sent == []
    assert "slot_taken" not in kinds("work")
    slot = capacity.take(1, {"test": True})  # given back
    assert slot is not None
    slot.release()


@pytest.mark.parametrize("node, second", [
    ("work: {needs: [start], instruction: Work., retry: 1}", "claude-code"),
    ("work: {needs: [start], instruction: Work., harness: [claude-code, codex]}", "codex"),
])
def test_a_retry_and_a_fallback_take_a_slot_again(repo, monkeypatch, node, second):
    monkeypatch.setenv("WF_MAX_AGENTS", "1")
    first_turn = ends("failed", "boom") if second == "claude-code" else ends("unavailable", "usage limit")
    monkeypatch.setitem(harnesses.FACTORIES, "claude-code",
                        Fake("claude-code", first_turn, submit({"summary": "done"})).factory)
    monkeypatch.setitem(harnesses.FACTORIES, "codex", Fake("codex", submit({"summary": "done"})).factory)
    held = contend_at(monkeypatch, attempt=2, seconds=1)
    assert cli.main(["run", agent_flow(node=node)]) == 0
    held[0]()
    said = [(e["event"], e["attempt"], e.get("harness")) for e in events("work")
            if e["event"] in ("slot_waiting", "slot_taken", "attempt_started")]
    assert said == [("slot_waiting", 2, second), ("slot_taken", 2, second), ("attempt_started", 2, second)]
    assert [e["waited_s"] for e in events("work") if e["event"] == "slot_taken"][0] >= 0.9


def test_a_retry_that_a_limit_stops_while_it_waits_starts_once_after_wf_retry(repo, monkeypatch):
    monkeypatch.setenv("WF_MAX_AGENTS", "1")
    fake = Fake("claude-code", ends("failed", "boom"), submit({"summary": "done"}))
    monkeypatch.setitem(harnesses.FACTORIES, "claude-code", fake.factory)
    held = contend_at(monkeypatch, attempt=2, seconds=60)
    node = "work: {needs: [start], instruction: Work., retry: 1}"
    assert cli.main(["run", agent_flow(node=node, top="limits: {duration: 1s}")]) == 1
    held[0]()
    folder = run_of("work")
    assert Run.at(folder).status()["status"] == "limit"
    assert "attempt_started" not in kinds("work")
    assert cli.main(["retry", folder.name, "--limit", "duration=1h"]) == 0
    assert [e for e in kinds("work") if e.startswith(("slot", "attempt"))] == [
        "attempt_failed", "slot_waiting", "attempt_started"]


def test_an_agent_in_a_sub_workflow_waits_for_a_slot(repo, monkeypatch):
    monkeypatch.setenv("WF_MAX_AGENTS", "1")
    monkeypatch.setitem(harnesses.FACTORIES, "claude-code", Fake("claude-code", submit({"summary": "done"})).factory)
    agent_flow("inner")
    Path("outer.yaml").write_text(dedent("""
        version: 1
        name: outer
        start: {input: {}}
        nodes:
          sub: {needs: [start], workflow: inner.yaml}
        end: {output: {summary: "{{ sub.summary }}"}}
    """))
    seen = {}
    let_go = hold_slot(0.5, waiter="outer", seen=seen)
    assert cli.main(["run", "outer.yaml"]) == 0
    let_go()
    assert [w["node"] for w in seen["agents_waiting"]] == ["sub/work"]
    assert [e["node"] for e in events("outer") if e["event"].startswith("slot")] == ["sub/work", "sub/work"]


def test_a_retried_run_waits_for_a_slot(repo, monkeypatch):
    monkeypatch.setenv("WF_MAX_AGENTS", "1")
    monkeypatch.setitem(harnesses.FACTORIES, "claude-code", Fake("claude-code", submit({"summary": "done"})).factory)
    assert cli.main(["run", failing_flow()]) == 1
    Path("ready").touch()
    let_go = hold_slot(0.5, waiter="work")
    assert cli.main(["retry", run_of("work").name]) == 0
    let_go()
    assert [e for e in kinds("work") if e.startswith("slot")] == ["slot_waiting", "slot_taken"]


# ---------- across processes ----------


def test_two_runs_in_separate_processes_share_the_cap(repo, monkeypatch, marks):
    monkeypatch.setenv("WF_MAX_AGENTS", "1")
    (marks / "go").touch()  # the second agent lets go at once
    first = wf_process(marks, marks / "let-go", "run", agent_flow("one"))
    try:
        until(lambda: mark(marks, first, "started").exists())
        second = wf_process(marks, marks / "go", "run", agent_flow("two"))
        until(lambda: waiting("two"))
        assert not mark(marks, second, "started").exists()
        (marks / "let-go").touch()
        succeeds(first)
        succeeds(second)
    finally:
        first.kill()
    assert float(mark(marks, second, "started").read_text()) >= float(mark(marks, first, "ended").read_text())
    assert "slot_waiting" not in kinds("one")
    assert kinds("two").count("slot_taken") == 1


def test_a_killed_process_frees_its_slot(repo, monkeypatch, marks, capsys):
    monkeypatch.setenv("WF_MAX_AGENTS", "1")
    (marks / "go").touch()
    holder = wf_process(marks, marks / "never", "run", agent_flow("one"))
    try:
        until(lambda: mark(marks, holder, "started").exists())
        waiter = wf_process(marks, marks / "go", "run", agent_flow("two"))
        until(lambda: waiting("two"))
        assert cli.main(["status", run_of("two").name]) == 0
        assert "running, waiting for an agent slot: work" in capsys.readouterr().out
        holder.kill()  # SIGKILL: it can't let go of anything itself
        holder.wait()
        succeeds(waiter)
    finally:
        holder.kill()
    assert not mark(marks, holder, "ended").exists()
    assert mark(marks, waiter, "ended").exists()
    assert kinds("two").count("slot_taken") == 1


def test_a_killed_waiter_is_stopped_and_no_longer_waiting(repo, monkeypatch, marks):
    monkeypatch.setenv("WF_MAX_AGENTS", "1")
    monkeypatch.setattr(operate, "GRACE_SECONDS", 0)
    let_go = hold_slot(60)
    (marks / "go").touch()
    waiter = wf_process(marks, marks / "go", "run", agent_flow())
    try:
        until(lambda: waiting("work"))
        waiter.kill()
        waiter.wait()
    finally:
        waiter.kill()
        let_go()
    folder = run_of("work")
    assert "agents_waiting" in Run.at(folder).status()  # what the killed process left behind
    state = operate.state(Run.at(folder))
    assert state["status"] == "stopped"
    assert "agents_waiting" not in state
