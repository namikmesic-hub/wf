"""Progress: the steps from start to end, the loops, and the estimate that `wf status` shows (progress.py)."""

import json
import subprocess
from pathlib import Path
from textwrap import dedent

import pytest

from workflow import cli, progress, runner, spec
from workflow.runs import Run

EXAMPLES = Path(__file__).parent.parent / "workflow" / "examples"

LOOP = """
version: 1
name: loop
start: {input: {}}
nodes:
  explore: {needs: [start], command: "true"}
  implement: {needs: [explore], command: "true"}
  tests: {needs: [implement], command: "true"}
  review:
    needs: [tests]
    command: "true"
    routes:
      - {when: "review.exit_code == 0", to: end}
      - {to: implement, max: 3}
      - {to: end}
end: {output: {}}
"""

T0 = "2026-10-19T10:00:00+00:00"


def parse(text: str, path: Path | None = None) -> spec.Workflow:
    return spec.parse(dedent(text).lstrip(), path)


def event(kind: str, node: str | None = None, time: str = T0, **more) -> dict:
    return {"time": time, "event": kind, **({"node": node} if node else {}), **more}


def lap(*nodes: str, pass_no: int = 1) -> list[dict]:
    """Each node starts and finishes, in turn."""
    return [e for n in nodes for e in (event("node_started", n, **{"pass": pass_no}), event("node_finished", n, **{"pass": pass_no}))]


BACK = event("route", "review", route=1, to=["implement"], taken=1, max=3)


# ---------- the shape ----------


def test_the_straight_path_counts_the_steps_and_a_route_back_makes_a_loop():
    shape = progress.Shape(spec.load(EXAMPLES / "implement.yaml"))
    assert (shape.steps, shape.step) == (4, {"explore": 1, "implement": 2, "tests": 3, "review": 4})
    (loop,) = shape.loops
    assert (loop.last, loop.first, loop.node, loop.route, loop.max) == ("review", "implement", "review", 1, 3)
    assert loop.body == {"implement", "tests", "review"}

    feature = progress.Shape(spec.load(EXAMPLES / "feature.yaml"))
    assert (feature.steps, feature.loops) == (5, [])


def test_parallel_steps_share_a_step_number():
    shape = progress.Shape(parse("""
        version: 1
        name: fan
        start: {input: {}}
        nodes:
          a: {needs: [start], command: "true"}
          b: {needs: [a], command: "true"}
          c: {needs: [a], command: "true"}
          slow: {needs: [c], command: "true"}
          d: {needs: [b, slow], command: "true"}
        end: {output: {}}
    """))
    assert shape.step == {"a": 1, "b": 2, "c": 2, "slow": 3, "d": 4}
    assert shape.steps == 4


def test_a_loop_whose_way_back_has_no_max_is_counted_by_the_route_that_has_one():
    shape = progress.Shape(parse("""
        version: 1
        name: inner-max
        start: {input: {}}
        nodes:
          a:
            needs: [start]
            command: "true"
            routes: [{to: b, max: 2}, {to: end}]
          b:
            command: "true"
            routes: [{when: "b.exit_code != 0", to: a}, {to: end}]
        end: {output: {}}
    """))
    (loop,) = shape.loops
    assert (loop.last, loop.first, loop.node, loop.route, loop.max) == ("b", "a", "a", 0, 2)


# ---------- the estimate, step by step ----------


def test_without_earlier_runs_each_step_weighs_one_and_each_loop_counts_to_its_max():
    workflow = parse(LOOP)
    started = lambda n, p=1: [event("node_started", n, **{"pass": p})]  # noqa: E731
    moments = [
        started("explore"),
        lap("explore") + started("implement"),
        lap("explore", "implement") + started("tests"),
        lap("explore", "implement", "tests") + started("review"),
        lap("explore", "implement", "tests", "review") + [BACK] + started("implement", 2),
        lap("explore", "implement", "tests", "review") + [BACK] + lap("implement", "tests", pass_no=2) + started("review", 2),
    ]
    found = [progress.estimate(workflow, events, "running") for events in moments]
    # 13 steps at most: 4 on the straight path, then the loop's 3 up to 3 more times
    assert [f["percent"] for f in found] == [0, 8, 15, 23, 31, 46]
    assert [f["step"] for f in found] == [1, 2, 3, 4, 2, 4]
    assert all(f["estimate"] == "steps" and f["left_s"] is None for f in found)
    assert progress.describe(found[4]) == "step 2 of 4 (implement), review → implement 1 of at most 3, about 31%"
    assert {n: i["state"] for n, i in found[4]["nodes"].items()} == {
        "explore": "done", "implement": "running", "tests": "waiting", "review": "waiting"}
    assert found[4]["nodes"]["implement"]["passes"] == 2
    assert found[4]["loops"] == [{"from": "review", "to": "implement", "node": "review", "route": 1, "taken": 1, "max": 3}]

    done = lap("explore", "implement", "tests", "review") + [event("route", "review", route=0, to=["end"], taken=1)]
    finished = progress.estimate(workflow, done, "succeeded")
    assert (finished["percent"], finished["step"]) == (100, 4)
    assert {i["state"] for i in finished["nodes"].values()} == {"done"}


def test_a_loop_the_run_has_left_counts_no_more():
    workflow = parse(LOOP.replace("to: end}", "to: publish}").replace("end: {output: {}}", """  publish: {command: "true"}
end: {output: {}}"""))
    events = lap("explore", "implement", "tests", "review") + [event("route", "review", route=0, to=["publish"], taken=1),
                                                                 event("node_started", "publish", **{"pass": 1})]
    found = progress.estimate(workflow, events, "running")
    assert (found["step"], found["steps"], found["percent"]) == (5, 5, 80)  # 4 of 5 steps: the loop can't come back

    ended = lap("explore", "implement", "tests", "review", "publish")
    assert progress.estimate(workflow, ended, "running")["percent"] == 99  # all done, but the run hasn't said so yet


def test_with_earlier_runs_steps_weigh_their_time_and_loops_their_mean_count():
    workflow = parse(LOOP)
    learnt = progress.Earlier(runs=2, weight={"explore": 10.0, "implement": 100.0, "tests": 20.0, "review": 30.0},
                              mean={"explore": 10.0, "implement": 100.0, "tests": 20.0, "review": 30.0},
                              taken={("review", 1): 1.0})
    events = lap("explore", "implement", "tests") + [event("node_started", "review", **{"pass": 1})]
    now = progress._seconds(T0) + 15  # half of review's mean time
    found = progress.estimate(workflow, events, "running", learnt, now=now)
    # 145 s done: 10 + 100 + 20, and 15 of review's 30
    # 165 s left: review's other 15, then the loop once more: 100 + 20 + 30
    assert (found["estimate"], found["earlier_runs"], found["left_s"], found["percent"]) == ("earlier runs", 2, 165, 47)
    assert found["loops"][0]["expected"] == 1.0
    assert progress.describe(found).endswith("about 47%, about 3 min left")


def test_a_branch_a_route_left_out_is_skipped_and_a_failed_step_is_failed():
    workflow = parse("""
        version: 1
        name: pick
        start: {input: {small: {type: boolean, default: true}}}
        nodes:
          pick:
            needs: [start]
            command: "true"
            routes: [{when: start.small, to: quick}, {to: thorough}]
          quick: {command: "true"}
          thorough: {command: "true"}
        end: {output: {}}
    """)
    events = lap("pick") + [event("route", "pick", route=0, to=["quick"], taken=1), event("node_started", "quick", **{"pass": 1})]
    states = {n: i["state"] for n, i in progress.estimate(workflow, events, "running")["nodes"].items()}
    assert states == {"pick": "done", "quick": "running", "thorough": "skipped"}

    failed = events + [event("node_failed", "quick", error="exited with code 1", **{"pass": 1})]
    found = progress.estimate(workflow, failed, "failed")
    assert found["nodes"]["quick"]["state"] == "failed" and found["at"] == ["quick"]

    retried = failed + [event("attempt_started", "quick", attempt=2, **{"pass": 1})]
    assert progress.estimate(workflow, retried, "running")["nodes"]["quick"]["state"] == "running"


def test_a_step_in_progress_of_a_run_that_does_not_move_is_paused():
    events = lap("explore") + [event("node_started", "implement", **{"pass": 1})]
    for status in ("limit", "stopped", "waiting"):
        assert progress.estimate(parse(LOOP), events, status)["nodes"]["implement"]["state"] == "paused"


def test_items_that_finished_count_for_their_step(tmp_path):
    workflow = parse("""
        version: 1
        name: items
        start: {input: {}}
        nodes:
          each: {needs: [start], for_each: "[1, 2, 3, 4]", command: "true"}
          after: {needs: [each], command: "true"}
        end: {output: {}}
    """)
    for index in (0, 2):
        folder = tmp_path / "nodes" / "each" / "1" / str(index)
        folder.mkdir(parents=True)
        (folder / "output.json").write_text("{}")
    found = progress.estimate(workflow, [event("node_started", "each", items=4, **{"pass": 1})], "running", folder=tmp_path)
    assert (found["nodes"]["each"]["items"], found["nodes"]["each"]["items_done"]) == (4, 2)
    assert progress.describe(found) == "step 1 of 2 (each: 2 of 4 items done), about 25%"


def test_a_sub_workflow_in_progress_counts_its_own_steps(tmp_path):
    (tmp_path / "inner.yaml").write_text(dedent("""
        version: 1
        name: inner
        start: {input: {}}
        nodes:
          one: {needs: [start], command: "true"}
          two: {needs: [one], command: "true"}
        end: {output: {}}
    """).lstrip())
    outer = tmp_path / "outer.yaml"
    outer.write_text(dedent("""
        version: 1
        name: outer
        start: {input: {}}
        nodes:
          sub: {needs: [start], workflow: inner.yaml}
          after: {needs: [sub], command: "true"}
        end: {output: {}}
    """).lstrip())
    events = [event("node_started", "sub", **{"pass": 1})] + lap("sub/one") + [event("node_started", "sub/two", **{"pass": 1})]
    found = progress.estimate(spec.load(outer), events, "running")
    assert found["nodes"]["sub"]["inner"] == {"step": 2, "steps": 2, "at": ["two"], "percent": 50}
    assert progress.describe(found) == "step 1 of 2 (sub: step 2 of 2), about 25%"


def test_durations_read_as_people_say_them():
    assert [progress.duration(s) for s in (45, 720, 3900, 7200)] == ["45 s", "12 min", "1 h 5 min", "2 h"]


# ---------- wf status ----------


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(runner, "interactive", lambda: False)
    subprocess.run(["git", "init", "-q"], check=True)
    return tmp_path


STEPS = """
version: 1
name: steps
start: {input: {fail: {type: boolean, default: false}}}
nodes:
  one: {needs: [start], command: "sleep 1"}
  two: {needs: [one], command: "exit {{ 1 if start.fail else 0 }}"}
end: {output: {}}
"""


def test_wf_status_says_how_far_a_run_is_and_learns_from_earlier_real_runs(repo, capsys):
    Path("steps.yaml").write_text(STEPS.lstrip())
    Path("script.yaml").write_text("inputs: {}\n")
    assert cli.main(["run", "steps.yaml"]) == 0
    assert cli.main(["run", "steps.yaml"]) == 0
    assert cli.main(["run", "steps.yaml", "--dry-run", "script.yaml"]) == 0  # a dry run: not counted for a real one
    assert cli.main(["run", "steps.yaml", "fail=true"]) == 1
    failed = max((Path(".wf") / "runs").iterdir(), key=lambda p: (p / "workflow" / "manifest.json").stat().st_mtime)

    capsys.readouterr()
    assert cli.main(["status", failed.name]) == 0
    line = capsys.readouterr().out.splitlines()[1]
    assert line.startswith("  step 2 of 2 (two), about ")

    assert cli.main(["status", failed.name, "--json"]) == 0
    found = json.loads(capsys.readouterr().out)["progress"]
    assert (found["estimate"], found["earlier_runs"], found["at"]) == ("earlier runs", 2, ["two"])
    assert found["nodes"]["two"]["state"] == "failed" and found["left_s"] is not None
    assert failed.name not in [r.id for r in progress.earlier(Run.at(failed), "steps")]
