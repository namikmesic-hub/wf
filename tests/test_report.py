"""wf report: what runs of a workflow actually did, per node, as evidence for improving it."""

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from textwrap import dedent

import pytest

from fakes import Fake, submit, talk
from workflow import cli, harnesses, runner, runs
from workflow.report import Tally


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(runner, "interactive", lambda: False)
    subprocess.run(["git", "init", "-q"], check=True)
    return tmp_path


def write(name: str, text: str) -> None:
    Path(name).write_text(dedent(text).lstrip())


def report(capsys, *args) -> dict:
    capsys.readouterr()
    assert cli.main(["report", *args, "--json"]) == 0
    return json.loads(capsys.readouterr().out)


def run_ids() -> list[str]:
    return sorted(p.name for p in (Path(".wf") / "runs").iterdir())


LOOP = """
version: 1
name: loop
start: {input: {need: {type: integer, default: 3}}}
nodes:
  bump: {needs: [start], command: "echo x >> count"}
  check:
    needs: [bump]
    command: "test $(wc -l < count) -ge {{ start.need }}"
    check: false
    routes:
      - {when: "check.exit_code == 0", to: end}
      - {to: bump, max: 3}
      - {to: end}
end: {output: {}}
"""


def test_a_report_counts_passes_and_routes_per_node(repo, capsys):
    write("loop.yaml", LOOP)
    assert cli.main(["run", "loop.yaml"]) == 0
    (run_id,) = run_ids()
    found = report(capsys, run_id)
    (summary,) = found["runs"]
    assert (summary["run"], summary["workflow"], summary["status"], summary["dry_run"]) == (run_id, "loop", "succeeded", False)
    assert summary["duration_s"] >= 0
    assert found["nodes"]["bump"]["passes"] == 3
    assert found["nodes"]["check"]["passes"] == 3
    assert found["routes"] == [  # every route, in file order: one never taken is evidence too
        {"from": "check", "to": ["end"], "when": "check.exit_code == 0", "max": None, "taken": 1, "max_reached": 0},
        {"from": "check", "to": ["bump"], "when": None, "max": 3, "taken": 2, "max_reached": 0},
        {"from": "check", "to": ["end"], "when": None, "max": None, "taken": 0, "max_reached": 0},
    ]


def test_a_loop_that_runs_out_is_called_out(repo, capsys):
    write("loop.yaml", LOOP)
    assert cli.main(["run", "loop.yaml", "need=9"]) == 0  # never reaches 9: the loop hits max and leaves by the last route
    found = report(capsys, run_ids()[0])
    loop = next(r for r in found["routes"] if r["to"] == ["bump"])
    assert (loop["taken"], loop["max_reached"]) == (3, 1)
    assert any("check → bump reached its max (3) in 1 of 1 run" in note for note in found["notes"])


def test_attempts_failures_and_spending_per_node(repo, monkeypatch, capsys):
    fake = Fake("claude-code", talk(tokens=50, cost=0.01), talk(), talk(), submit({"summary": "ok"}, tokens=200, cost=0.04))
    monkeypatch.setitem(harnesses.FACTORIES, "claude-code", fake.factory)
    write("agent.yaml", """
        version: 1
        name: agent
        defaults: {harness: claude-code}
        start: {input: {}}
        nodes:
          think: {needs: [start], instruction: think}
          tally: {needs: [think], command: "echo done"}
        end: {output: {}}
    """)
    assert cli.main(["run", "agent.yaml"]) == 0
    think = report(capsys, run_ids()[0])["nodes"]["think"]
    assert (think["attempts"], think["failed_attempts"]) == (2, 1)
    assert think["failures"] == [{"error": "the agent never made a valid submit_result call", "count": 1}]
    assert think["tokens"] == 450 and think["cost_usd"] == pytest.approx(0.07)
    assert any("think needed a retry" in note for note in report(capsys, run_ids()[0])["notes"])


def test_a_report_over_every_run_of_a_workflow(repo, capsys):
    write("loop.yaml", LOOP)
    assert cli.main(["run", "loop.yaml"]) == 0
    Path("count").unlink()
    assert cli.main(["run", "loop.yaml", "need=1"]) == 0
    found = report(capsys, "--workflow", "loop")
    assert len(found["runs"]) == 2
    assert found["nodes"]["bump"]["passes"] == 4  # 3 + 1
    assert found["nodes"]["bump"]["runs"] == 2


def test_items_are_counted_together_under_one_name(repo, capsys):
    write("items.yaml", """
        version: 1
        name: items
        start: {input: {}}
        nodes:
          each: {needs: [start], for_each: "[1, 2, 3]", on_item_error: continue, command: "test {{ item }} != 2"}
        end: {output: {}}
    """)
    assert cli.main(["run", "items.yaml"]) == 0
    each = report(capsys, run_ids()[0])["nodes"]["each"]
    assert (each["items"], each["failed_items"]) == (3, 1)


def test_the_text_report_is_a_table(repo, capsys):
    write("loop.yaml", LOOP)
    cli.main(["run", "loop.yaml"])
    capsys.readouterr()
    assert cli.main(["report", run_ids()[0]]) == 0
    out = capsys.readouterr().out
    assert "node" in out and "passes" in out and "bump" in out and "check → bump" in out


def test_dry_runs_and_versions_can_be_told_apart(repo, capsys):
    write("loop.yaml", LOOP)
    assert cli.main(["run", "loop.yaml"]) == 0
    write("loop.yaml", LOOP.replace("max: 3", "max: 4"))
    assert cli.main(["run", "loop.yaml"]) == 0
    found = report(capsys, "--workflow", "loop")
    first, second = (r["version"] for r in found["runs"])
    assert first != second
    assert any(note.startswith("these runs used 2 versions") for note in found["notes"])
    assert [r["version"] for r in report(capsys, "--workflow", "loop", "--version", second)["runs"]] == [second]
    assert len(report(capsys, "--workflow", "loop", "--real")["runs"]) == 2
    assert cli.main(["report", "--workflow", "loop", "--dry-runs"]) == 1  # there are none


INNER = """
version: 1
name: inner
start: {input: {need: {type: integer}}}
nodes:
  bump: {needs: [start], command: "echo x >> count-{{ start.need }}"}
  check:
    needs: [bump]
    command: "test $(wc -l < count-{{ start.need }}) -ge {{ start.need }}"
    check: false
    routes:
      - {when: "check.exit_code == 0", to: end}
      - {to: bump, max: 2}
      - {to: end}
end: {output: {}}
"""


def test_routes_inside_sub_workflows_are_reported_under_their_path(repo, capsys):
    write("inner.yaml", INNER)
    write("outer.yaml", """
        version: 1
        name: outer
        start: {input: {}}
        nodes:
          sub: {needs: [start], for_each: '[2, 9]', workflow: inner.yaml, with: {need: "{{ item }}"}}
        end: {output: {}}
    """)
    assert cli.main(["run", "outer.yaml"]) == 0
    found = report(capsys, "--workflow", "outer")
    assert [(r["from"], r["taken"], r["max_reached"]) for r in found["routes"]] == [
        ("sub[]/check", 1, 0),  # item 0 got there on its 2nd pass
        ("sub[]/check", 3, 1),  # 1 for item 0, 2 for item 1, which used up its max
        ("sub[]/check", 1, 0),
    ]


def test_failed_items_are_not_called_retries(repo, capsys):
    write("each.yaml", """
        version: 1
        name: each
        start: {input: {}}
        nodes:
          each: {needs: [start], for_each: '[0, 1, 2]', command: "test {{ item }} -ne 1", on_item_error: continue}
        end: {output: {}}
    """)
    assert cli.main(["run", "each.yaml"]) == 0
    notes = report(capsys, "--workflow", "each")["notes"]
    assert not any("retry" in n for n in notes)
    assert "each: 1 of 3 items failed" in notes


def test_no_runs_says_which_runs_it_looked_for(repo, capsys):
    write("loop.yaml", LOOP)
    assert cli.main(["run", "loop.yaml"]) == 0
    capsys.readouterr()
    assert cli.main(["report", "--workflow", "loop", "--dry-runs"]) == 1
    assert "no dry runs of loop" in capsys.readouterr().err


def test_a_pass_that_failed_and_then_finished_is_counted_once(repo, capsys, monkeypatch):
    fake = Fake("claude-code", talk(tokens=50), talk(), talk(), submit({"summary": "ok"}, tokens=200))  # attempt 1 never submits
    monkeypatch.setitem(harnesses.FACTORIES, "claude-code", fake.factory)
    write("one.yaml", """
        version: 1
        name: one
        defaults: {harness: claude-code, retry: 0}
        start: {input: {}}
        nodes:
          think: {needs: [start], instruction: x}
        end: {output: {}}
    """)
    assert cli.main(["run", "one.yaml"]) == 1
    (run_id,) = run_ids()
    assert cli.main(["retry", run_id]) == 0
    found = report(capsys, run_id)
    spent = found["nodes"]["think"]["tokens"]
    assert spent == found["runs"][0]["tokens"] and spent > 0
    events = [json.loads(l) for l in (Path(".wf/runs") / run_id / "events.jsonl").read_text().splitlines()]
    last = [e for e in events if e.get("event") in ("node_failed", "node_finished")][-1]
    assert spent == last["tokens"]  # the pass's total, not the total plus its earlier report


def test_a_sub_workflow_attempt_that_starts_over_counts_both_attempts(repo, capsys, monkeypatch):
    fake = Fake("claude-code", talk(tokens=1000), talk(), talk(), submit({"summary": "ok"}, tokens=1500))
    monkeypatch.setitem(harnesses.FACTORIES, "claude-code", fake.factory)
    write("inner.yaml", """
        version: 1
        name: inner
        defaults: {harness: claude-code, retry: 0}
        start: {input: {}}
        nodes:
          think: {needs: [start], instruction: x}
        end: {output: {}}
    """)
    write("outer.yaml", """
        version: 1
        name: outer
        start: {input: {}}
        nodes:
          sub: {needs: [start], workflow: inner.yaml, retry: 1}
        end: {output: {}}
    """)
    assert cli.main(["run", "outer.yaml"]) == 0  # sub's first attempt fails, its second starts inner over
    found = report(capsys, "--workflow", "outer")
    assert found["nodes"]["sub/think"]["tokens"] == 2700  # 1000 + 2 reminders of 100, then 1500
    assert found["runs"][0]["tokens"] == 2700


T0 = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)


def at(seconds: int, kind: str, node: str, **more) -> dict:
    """An event `seconds` after T0, of pass 1 unless it says another."""
    return {"time": (T0 + timedelta(seconds=seconds)).isoformat(), "event": kind, "node": node, "pass": 1, **more}


def after(seconds: int) -> float:
    return (T0 + timedelta(seconds=seconds)).timestamp()


def nested_run() -> runs.Run:
    """A run of outer.yaml: sub runs inner.yaml, build runs it for each item, and each is an agent for each item."""
    write("inner.yaml", """
        version: 1
        name: inner
        defaults: {harness: claude-code}
        start: {input: {}}
        nodes:
          think: {needs: [start], instruction: x}
        end: {output: {}}
    """)
    write("outer.yaml", """
        version: 1
        name: outer
        defaults: {harness: claude-code}
        start: {input: {}}
        nodes:
          sub: {needs: [start], workflow: inner.yaml, retry: 1}
          build: {needs: [start], for_each: "[1, 2, 3]", workflow: inner.yaml}
          each: {needs: [start], for_each: "[1, 2]", instruction: x}
        end: {output: {}}
    """)
    return runs.create(Path.cwd(), "outer", [Path("outer.yaml"), Path("inner.yaml")], Path("outer.yaml"), {})


def tally_of(run: runs.Run, events: list[dict]) -> Tally:
    tally = Tally(run)
    for event in events:
        tally.add(event)
    return tally


def test_a_pass_that_runs_adds_what_it_has_done_so_far_also_when_its_sub_workflow_starts_over(repo):
    run = nested_run()
    tally = tally_of(run, [at(0, "node_started", "sub"), at(0, "node_started", "sub/think"),
                           at(10, "agent_usage", "sub/think", attempt=1, tokens=100, cost_usd=0.1),
                           at(20, "agent_usage", "sub/think", attempt=1, tokens=300, cost_usd=0.3),
                           at(30, "attempt_failed", "sub", attempt=1, error="timed out after 30s"),
                           at(31, "attempt_started", "sub", attempt=2), at(31, "node_started", "sub/think"),
                           at(40, "agent_usage", "sub/think", attempt=1, tokens=50, cost_usd=0.05)])  # the same names again
    found = tally.report(now=after(60))
    sub, think = found["nodes"]["sub"], found["nodes"]["sub/think"]
    assert (sub["attempts"], sub["tokens"], sub["cost_usd"], sub["duration_s"]) == (2, 350, 0.35, 60)  # 1 failed, 1 runs
    assert (think["passes"], think["attempts"], think["tokens"], think["duration_s"]) == (2, 1, 50, 29)  # the cut one ran out
    assert (found["runs"][0]["tokens"], found["runs"][0]["duration_s"]) == (350, 60)
    assert Tally(run).report()["nodes"] == {} and tally.report()["nodes"]["sub"]["attempts"] == 1  # without now: ended only

    tally.add(at(50, "attempt_failed", "sub", attempt=2, error="timed out after 30s"))
    tally.add(at(50, "node_failed", "sub", error="timed out after 30s", tokens=350, cost_usd=0.35))
    ended = tally.report(now=after(600))  # think's attempt was cut: it runs no more
    assert {k: ended["nodes"]["sub/think"][k] for k in ("attempts", "tokens", "duration_s")} == {"attempts": 0, "tokens": 0, "duration_s": 0}
    assert {k: ended["nodes"]["sub"][k] for k in ("attempts", "tokens", "duration_s")} == {"attempts": 2, "tokens": 350, "duration_s": 50}


def test_a_sub_workflow_attempt_that_goes_on_keeps_the_time_and_spending_of_its_steps(repo):
    second = [at(0, "node_started", "sub"), at(5, "attempt_failed", "sub", attempt=1, error="timed out after 5s"),
              at(6, "attempt_started", "sub", attempt=2), at(6, "node_started", "sub/think"),
              at(8, "agent_usage", "sub/think", attempt=1, tokens=100, cost_usd=0.1)]
    # A limit paused attempt 2, and `wf retry` goes on with it: the same thread of the sub-workflow
    goes_on = second + [at(9, "attempt_started", "sub", attempt=2), at(12, "agent_usage", "sub/think", attempt=1, tokens=300, cost_usd=0.3)]
    tally = tally_of(nested_run(), goes_on)
    assert tally.report(now=after(13))["nodes"]["sub"]["tokens"] == 300  # what the session said last, not 100 + 300
    tally.add(at(14, "node_finished", "sub/think", tokens=300, cost_usd=0.3))
    assert tally.report()["nodes"]["sub/think"]["duration_s"] == 8  # from its start in attempt 2, at 6 s


def test_a_node_that_failed_before_it_started_adds_no_time_once_its_pass_ran(repo):
    tally = tally_of(nested_run(), [at(0, "node_failed", "each", error="for_each: no such list", **{"pass": None}),
                                    at(10, "node_started", "each", items=2), at(11, "node_finished", "each"),
                                    {"time": (T0 + timedelta(seconds=20)).isoformat(), "event": "run_finished"}])
    assert tally.report(now=after(20))["nodes"]["each"]["duration_s"] == tally.report()["nodes"]["each"]["duration_s"] == 1


def test_the_spending_so_far_of_items_and_of_agents_inside_workflow_items_adds_up(repo):
    tally = tally_of(nested_run(), [
        at(0, "node_started", "each", items=2), at(0, "node_started", "build", items=3),
        at(1, "agent_usage", "each", index=0, attempt=1, tokens=50, cost_usd=0.05),
        at(2, "agent_usage", "each", index=1, attempt=1, tokens=70, cost_usd=0.07),
        at(1, "node_started", "build[0]/think"), at(1, "node_started", "build[2]/think"),
        at(3, "agent_usage", "build[0]/think", attempt=1, tokens=100, cost_usd=0.1),
        at(4, "agent_usage", "build[2]/think", attempt=1, tokens=200, cost_usd=0.2)])
    found = tally.report(now=after(10))
    assert {name: found["nodes"][name]["tokens"] for name in ("each", "build", "build[]/think")} == {
        "each": 120, "build": 300, "build[]/think": 300}
    assert (found["runs"][0]["tokens"], found["runs"][0]["cost_usd"]) == (420, 0.42)  # build's items, counted once


def test_an_agent_attempt_that_waits_for_its_slot_has_not_started(repo):
    waiting = [at(0, "node_started", "sub"), at(0, "slot_waiting", "sub", attempt=1, harness="claude-code", max_agents=1)]
    tally = tally_of(nested_run(), waiting)
    assert tally.report(now=after(5))["nodes"]["sub"]["attempts"] == 0
    tally.add(at(6, "slot_taken", "sub", attempt=1, harness="claude-code", waited_s=6))
    assert tally.report(now=after(7))["nodes"]["sub"]["attempts"] == 1
