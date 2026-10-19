"""wf report: what runs of a workflow actually did, per node, as evidence for improving it."""

import json
import subprocess
from pathlib import Path
from textwrap import dedent

import pytest

from fakes import Fake, submit, talk
from workflow import cli, harnesses, runner


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
    assert cli.main(["resume", run_id]) == 0
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
