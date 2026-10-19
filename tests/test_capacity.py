"""The cap on live agent sessions across every wf process (capacity.py).

Slots are files under WF_HOME, which conftest points at a fresh folder in every
test. A test holds a slot itself with capacity.take() to make a run wait. Tests
across processes start real `wf run` processes whose agent holds its session
until the test lets it go (held_agent.py), and read from the agents' marks when
each session was live.
"""

import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from textwrap import dedent

import pytest

from workflow import capacity, cli, harnesses, operate, runner, settings
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


def until(condition, seconds: float = 30) -> None:
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.05)


def hold_slot(seconds: float, waiter: str | None = None, seen: dict | None = None) -> threading.Thread:
    """Take the one slot of a cap of 1 now, and let it go `seconds` later. With `waiter`, the
    seconds count from when the run of that workflow waits for the slot, and `seen` gets its state then."""
    slot = capacity.take(1, {"test": True})
    assert slot is not None

    def later():
        try:
            if waiter:
                until(lambda: waiting(waiter))
                if seen is not None:
                    seen.update(operate.state(Run.at(run_of(waiter))))
            time.sleep(seconds)
        finally:
            slot.release()

    thread = threading.Thread(target=later)
    thread.start()
    return thread


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
    (wf_home / "config.yaml").write_text("max_agents: 3\n")
    assert capacity.configured() == 3
    monkeypatch.setenv("WF_MAX_AGENTS", "5")  # the environment wins
    assert capacity.configured() == 5
    monkeypatch.setenv("WF_MAX_AGENTS", "")
    assert capacity.configured() == 3


def test_another_setting_in_the_config_leaves_the_cap_alone(monkeypatch, wf_home):
    monkeypatch.setattr(settings, "KNOWN", ("max_agents", "tools"))
    (wf_home / "config.yaml").write_text("tools: [git]\n")
    assert capacity.configured() is None
    (wf_home / "config.yaml").write_text("tools: [git]\nmax_agents: 2\n")
    assert capacity.configured() == 2


@pytest.mark.parametrize("env, config, problem", [
    ("0", None, "WF_MAX_AGENTS must be a whole number of 1 or more, not `0`"),
    ("four", None, "not `four`"),
    (None, "max_agents: 2.5\n", "must be a whole number of 1 or more, not `2.5`"),
    (None, "max_agent: 4\n", "unknown setting max_agent (known: max_agents)"),
    (None, "- 4\n", "must hold settings by name"),
])
def test_a_bad_cap_stops_wf_run_before_the_run_exists(repo, monkeypatch, capsys, wf_home, env, config, problem):
    if env is not None:
        monkeypatch.setenv("WF_MAX_AGENTS", env)
    if config is not None:
        (wf_home / "config.yaml").write_text(config)
    assert cli.main(["run", agent_flow()]) == 1
    assert problem in capsys.readouterr().err
    assert not Path(".wf").exists()


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


def test_without_a_cap_nothing_waits_and_no_slot_is_made(repo, monkeypatch, wf_home):
    monkeypatch.setitem(harnesses.FACTORIES, "claude-code", Fake("claude-code", submit({"summary": "done"})).factory)
    assert cli.main(["run", agent_flow()]) == 0
    assert not (wf_home / "agent-slots").exists()
    assert not {"slot_waiting", "slot_taken"} & set(kinds("work"))


def test_a_waiting_attempt_keeps_its_retries_and_its_timeout_starts_with_its_slot(repo, monkeypatch, capsys):
    # The wait (1.5s) is longer than the timeout (1s). Counted in attempt 1, it would time
    # that attempt out, and attempt 2, the one retry, would then fail on the fake's "boom"
    monkeypatch.setenv("WF_MAX_AGENTS", "1")
    fake = Fake("claude-code", ends("failed", "boom"), submit({"summary": "done"}))
    monkeypatch.setitem(harnesses.FACTORIES, "claude-code", fake.factory)
    seen = {}
    held = hold_slot(1.5, waiter="work", seen=seen)
    assert cli.main(["run", agent_flow(node="work: {needs: [start], instruction: Work., timeout: 1s, retry: 1}")]) == 0
    held.join()
    assert json.loads((run_of("work") / "output.json").read_text()) == {"summary": "done"}
    assert "work: attempt 1 waits for an agent slot (1 of 1 in use)" in capsys.readouterr().out
    said = [(e["event"], e["attempt"], e.get("error")) for e in events("work") if e["event"].startswith(("slot", "attempt"))]
    assert said == [("slot_waiting", 1, None), ("slot_taken", 1, None), ("attempt_failed", 1, "boom"),
                    ("attempt_started", 2, None)]
    assert [e["waited_s"] for e in events("work") if e["event"] == "slot_taken"][0] >= 1.4
    assert seen["status"] == "running"
    assert [(w["node"], w["attempt"]) for w in seen["agents_waiting"]] == [("work", 1)]
    assert "agents_waiting" not in json.loads((run_of("work") / "status.json").read_text())


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
    held = hold_slot(5)  # a node that took a slot would wait this long, and say so in events.jsonl
    started = time.monotonic()
    assert cli.main(["run", "ask.yaml"]) == 2  # waiting for a person
    assert time.monotonic() - started < 4
    held.join()
    assert not {"slot_waiting", "slot_taken"} & set(kinds("ask"))


def test_a_dry_run_takes_no_slot(repo, monkeypatch):
    monkeypatch.setenv("WF_MAX_AGENTS", "1")
    Path("script.yaml").write_text("agents:\n  work:\n    - {summary: scripted}\n")
    held = hold_slot(5)
    started = time.monotonic()
    assert cli.main(["run", agent_flow(), "--dry-run", "script.yaml"]) == 0
    assert time.monotonic() - started < 4
    held.join()
    assert not {"slot_waiting", "slot_taken"} & set(kinds("work"))


def test_a_limit_reached_while_waiting_keeps_the_attempt_from_starting(repo, monkeypatch):
    monkeypatch.setenv("WF_MAX_AGENTS", "1")
    fake = Fake("claude-code", submit({"summary": "done"}))
    monkeypatch.setitem(harnesses.FACTORIES, "claude-code", fake.factory)
    held = hold_slot(1.5, waiter="work")
    assert cli.main(["run", agent_flow(top="limits: {duration: 1s}")]) == 1
    held.join()
    folder = run_of("work")
    assert json.loads((folder / "status.json").read_text())["status"] == "limit"
    assert fake.sent == []  # it never opened a session
    assert cli.main(["resume", folder.name, "--limit", "duration=1h"]) == 0
    assert json.loads((folder / "output.json").read_text()) == {"summary": "done"}
    assert [e["attempt"] for e in events("work") if e["event"] == "slot_waiting"] == [1]
    assert "attempt_started" not in kinds("work")  # the attempt that ran is still attempt 1


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
