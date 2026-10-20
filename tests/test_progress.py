"""Progress: the steps from start to end, the loops (shape.py), and the estimate that `wf status` shows (progress.py)."""

import json
import subprocess
import threading
from pathlib import Path
from textwrap import dedent

import pytest

from workflow import cli, progress, report, runner, spec
from workflow.runs import Run
from workflow.shape import Shape

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

PICK = """
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
  extra: {needs: [thorough], command: "true"}
end: {output: {}}
"""

INNER_MAX = """
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
"""

T0 = "2026-10-19T10:00:00+00:00"


def parse(text: str, path: Path | None = None) -> spec.Workflow:
    return spec.parse(dedent(text).lstrip(), path)


def event(kind: str, node: str | None = None, time: str = T0, **more) -> dict:
    return {"time": time, "event": kind, **({"node": node} if node else {}), **more}


def started(node: str, pass_no: int = 1, **more) -> dict:
    return event("node_started", node, **{"pass": pass_no}, **more)


def lap(*nodes: str, pass_no: int = 1) -> list[dict]:
    """Each node starts and finishes, in turn."""
    return [e for n in nodes for e in (started(n, pass_no), event("node_finished", n, **{"pass": pass_no}))]


def route(node: str, index: int, to: str, taken: int = 1) -> dict:
    return event("route", node, route=index, to=[to], taken=taken)


def percents(workflow: spec.Workflow, events: list[dict]) -> list[int]:
    """The percentage after each event, as `wf status` would show it then."""
    return [progress.estimate(workflow, events[:n], "running")["percent"] for n in range(len(events) + 1)]


def written(folder: Path, *indexes: int) -> None:
    """The output.json of each of these items, as a finished item leaves it."""
    for index in indexes:
        (folder / str(index)).mkdir(parents=True)
        (folder / str(index) / "output.json").write_text("{}")


BACK = route("review", 1, "implement")


# ---------- the shape ----------


def test_the_straight_path_counts_the_steps_and_a_route_back_makes_a_loop():
    shape = Shape(spec.load(EXAMPLES / "implement.yaml"))
    assert (shape.steps, shape.step) == (4, {"explore": 1, "implement": 2, "tests": 3, "review": 4})
    (loop,) = shape.loops
    assert (loop.last, loop.first, loop.route, loop.max) == ("review", "implement", 1, 3)
    assert loop.body == {"implement", "tests", "review"}

    feature = Shape(spec.load(EXAMPLES / "feature.yaml"))
    assert (feature.steps, feature.loops) == (5, [])


def test_parallel_steps_share_a_step_number():
    shape = Shape(parse("""
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
    assert shape.steps_after == {"a": 3, "b": 1, "c": 2, "slow": 1, "d": 0}


def test_a_lap_is_the_loop_s_own_path_not_a_heavier_one_into_it():
    workflow = parse("""
        version: 1
        name: heavy
        start: {input: {}}
        nodes:
          a: {needs: [start], command: "true"}
          heavy1: {needs: [start], command: "true"}
          heavy2: {needs: [heavy1], command: "true"}
          b:
            needs: [a, heavy2]
            command: "true"
            routes: [{when: "b.exit_code != 0", to: a, max: 3}, {to: end}]
        end: {output: {}}
    """)
    shape = Shape(workflow)
    (loop,) = shape.loops
    assert (loop.body, shape.span(loop, {n: 1.0 for n in shape.order})) == ({"a", "b"}, 2.0)  # b's step is 3: not a lap
    events = lap("a", "heavy1", "heavy2", "b") + [route("b", 0, "a"), started("a", 2)]
    assert progress.estimate(workflow, events, "running")["percent"] == 25  # 2 done, then 2 now and 2 x 2 to come


def test_a_loop_whose_way_back_has_no_max_is_bounded_by_the_route_that_has_one_and_counted_by_its_own():
    workflow = parse(INNER_MAX)
    (loop,) = Shape(workflow).loops
    assert (loop.last, loop.first, loop.route, loop.max) == ("b", "a", 0, 2)
    events = [started("a"), event("node_finished", "a", **{"pass": 1}), route("a", 0, "b"), started("b"),
              event("node_finished", "b", **{"pass": 1}), route("b", 0, "a"), started("a", 2),
              event("node_finished", "a", **{"pass": 2}), route("a", 0, "b", taken=2), started("b", 2)]
    taken = [progress.estimate(workflow, events[:n], "running")["loops"][0]["taken"] for n in range(len(events) + 1)]
    assert taken == [0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1]  # only b going back to a counts
    found = percents(workflow, events)
    assert found == sorted(found)


# ---------- the estimate, step by step ----------


def test_without_earlier_runs_each_step_weighs_one_and_each_loop_counts_to_its_max():
    workflow = parse(LOOP)
    moments = [
        [started("explore")],
        lap("explore") + [started("implement")],
        lap("explore", "implement") + [started("tests")],
        lap("explore", "implement", "tests") + [started("review")],
        lap("explore", "implement", "tests", "review") + [BACK, started("implement", 2)],
        lap("explore", "implement", "tests", "review") + [BACK] + lap("implement", "tests", pass_no=2) + [started("review", 2)],
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
    assert found[4]["loops"] == [{"from": "review", "to": "implement", "route": 1, "taken": 1, "max": 3}]

    done = lap("explore", "implement", "tests", "review") + [route("review", 0, "end")]
    finished = progress.estimate(workflow, done, "succeeded")
    assert (finished["percent"], finished["step"]) == (100, 4)
    assert {i["state"] for i in finished["nodes"].values()} == {"done"}


def test_the_percentage_never_goes_down_event_by_event_even_before_a_route_is_chosen():
    workflow = parse(LOOP)
    events = (lap("explore", "implement", "tests", "review") + [BACK] + lap("implement", "tests", "review", pass_no=2)
              + [route("review", 0, "end")])
    found = percents(workflow, events)
    assert found == sorted(found)
    finished_review = len(lap("explore", "implement", "tests", "review"))
    assert found[finished_review] == found[finished_review + 1] == 31  # review finished: its route may go back


def test_a_loop_the_run_has_left_counts_no_more():
    workflow = parse(LOOP.replace("to: end}", "to: publish}").replace("end: {output: {}}", """  publish: {command: "true"}
end: {output: {}}"""))
    events = lap("explore", "implement", "tests", "review") + [route("review", 0, "publish"), started("publish")]
    found = progress.estimate(workflow, events, "running")
    assert (found["step"], found["steps"], found["percent"]) == (5, 5, 80)  # 4 of 5 steps: the loop can't come back

    ended = lap("explore", "implement", "tests", "review") + [route("review", 0, "publish")] + lap("publish")
    assert progress.estimate(workflow, ended, "running")["percent"] == 99  # all done, but the run hasn't said so yet


def test_a_branch_a_route_left_out_is_skipped_and_counts_no_more():
    workflow = parse(PICK)
    events = lap("pick") + [route("pick", 0, "quick"), started("quick")]
    found = progress.estimate(workflow, events, "running")
    assert {n: i["state"] for n, i in found["nodes"].items()} == {
        "pick": "done", "quick": "running", "thorough": "skipped", "extra": "skipped"}
    assert progress.describe(found) == "step 2 of 2 (quick), about 50%"

    learnt = progress.Earlier(runs=1, mean={"pick": 10.0, "quick": 10.0, "thorough": 100.0, "extra": 100.0}, taken={})
    timed = progress.estimate(workflow, events, "running", learnt, now=progress._seconds(T0))
    assert (timed["left_s"], timed["percent"]) == (10, 50)  # quick's 10 s, not the 200 s of the branch left out


def test_a_failed_step_is_failed_until_wf_retry_reruns_it():
    workflow = parse(PICK)
    events = lap("pick") + [route("pick", 0, "quick"), started("quick"),
                            event("node_failed", "quick", error="exited with code 1", **{"pass": 1})]
    found = progress.estimate(workflow, events, "failed")
    assert found["nodes"]["quick"]["state"] == "failed" and found["at"] == ["quick"]
    retried = events + [event("attempt_started", "quick", attempt=2, **{"pass": 1})]
    assert progress.estimate(workflow, retried, "running")["nodes"]["quick"]["state"] == "running"


def test_a_branch_left_out_that_leads_back_into_the_chosen_one_counts_no_more():
    workflow = parse("""
        version: 1
        name: reconverge
        start: {input: {small: {type: boolean, default: true}}}
        nodes:
          pick:
            needs: [start]
            command: "true"
            routes: [{when: start.small, to: quick}, {to: heavy}]
          heavy: {command: "true"}
          quick: {needs: [heavy], command: "true"}
        end: {output: {}}
    """)
    assert Shape(workflow).step["quick"] == 3  # through heavy, in the graph
    events = lap("pick") + [route("pick", 0, "quick"), started("quick")]
    found = progress.estimate(workflow, events, "running")
    assert found["nodes"]["heavy"]["state"] == "skipped"
    assert progress.describe(found) == "step 2 of 2 (quick), about 50%"  # the run's own path: pick, quick


def test_a_loop_that_a_needs_edge_closes_counts_its_lap_when_it_goes_back():
    workflow = parse("""
        version: 1
        name: needs-loop
        start: {input: {}}
        nodes:
          a:
            needs: [start, b]
            command: "true"
            routes: [{when: "a.exit_code == 0", to: b, max: 2}, {to: end}]
          b: {command: "true"}
        end: {output: {}}
    """)
    (loop,) = Shape(workflow).loops
    assert (loop.last, loop.first, loop.route, loop.max) == ("b", "a", None, 2)
    events = lap("a") + [route("a", 0, "b")] + lap("b") + [started("a", 2)]
    taken = [progress.estimate(workflow, events[:n], "running")["loops"][0]["taken"] for n in range(len(events) + 1)]
    assert taken == [0, 0, 0, 0, 0, 1, 1]  # b's end takes the needs edge back to a
    assert percents(workflow, events) == [0, 0, 17, 17, 17, 33, 33]


def test_a_needs_edge_loop_counts_no_lap_before_its_first_step_ran(repo):
    Path("entry.yaml").write_text(dedent("""
        version: 1
        name: entry
        start: {input: {}}
        nodes:
          a:
            needs: [start, b]
            command: "true"
            routes: [{when: "a.exit_code == 0", to: b, max: 2}, {to: end}]
          b: {needs: [start], command: "true"}
        end: {output: {}}
    """).lstrip())
    assert cli.main(["run", "entry.yaml"]) == 0
    workflow, events = spec.load(Path("entry.yaml")), report.events(Run.at(newest()))
    (loop,) = Shape(workflow).loops
    assert (loop.last, loop.first, loop.route, loop.max) == ("b", "a", None, 2)
    ends = [n for n, e in enumerate(events) if e.get("event") == "node_finished"]
    assert [events[n]["node"] for n in ends] == ["b", "a", "b", "a", "b", "a"]  # start leads into b: b runs first
    laps = [progress.estimate(workflow, events[:n + 1], "running")["loops"][0]["taken"] for n in ends if events[n]["node"] == "b"]
    assert laps == [0, 1, 2]  # never more than the loop's max
    found = percents(workflow, events)
    assert found == sorted(found)


def test_a_needs_edge_loop_counts_no_lap_when_a_route_made_its_first_step_pending_already(repo):
    Path("both.yaml").write_text(dedent("""
        version: 1
        name: both
        start: {input: {}}
        nodes:
          a:
            needs: [start, b]
            command: "true"
            routes: [{to: b, max: 1}, {to: c}]
          b: {needs: [start], command: "true"}
          c:
            command: "true"
            routes: [{to: [a, b], max: 1}, {to: end}]
        end: {output: {}}
    """).lstrip())
    assert cli.main(["run", "both.yaml"]) == 0
    workflow, events = spec.load(Path("both.yaml")), report.events(Run.at(newest()))
    needs_loop = next(n for n, loop in enumerate(Shape(workflow).loops) if loop.route is None)
    ends = [n for n, e in enumerate(events) if e.get("event") == "node_finished"]
    assert [events[n]["node"] for n in ends] == ["b", "a", "b", "a", "c", "b", "a", "c"]
    laps = [progress.estimate(workflow, events[:n + 1], "running")["loops"][needs_loop]["taken"]
            for n in ends if events[n]["node"] == "b"]
    assert laps == [0, 1, 1]  # c's route made a pending, so b's last end doesn't send the run back to it


@pytest.mark.parametrize("first, second", [("a", "b"), ("b", "a")])
def test_open_steps_with_as_much_left_are_told_apart_by_the_least_done_not_by_chance(tmp_path, first, second):
    steps = {"a": '{needs: [start], for_each: "[1, 2, 3, 4]", command: "true"}',
             "b": '{needs: [start], for_each: "[1, 2, 3, 4]", command: "true"}'}
    workflow = parse(f"""
        version: 1
        name: tie
        start: {{input: {{}}}}
        nodes:
          {first}: {steps[first]}
          {second}: {steps[second]}
          c: {{needs: [b], command: "true"}}
        end: {{output: {{}}}}
    """)
    written(tmp_path / "nodes" / "b" / "1", 0, 1, 2, 3)  # b has all its items, a none: each has 1 step left
    found = progress.estimate(workflow, [started("a", items=4), started("b", items=4)], "running", folder=tmp_path)
    assert found["percent"] == 0  # a decides: nothing of its way is done


def test_a_slow_parallel_step_holds_the_estimate_back(tmp_path):
    workflow = parse("""
        version: 1
        name: pair
        start: {input: {}}
        nodes:
          a: {needs: [start], for_each: "range(10) | list", command: "true"}
          b: {needs: [start], for_each: "range(10) | list", command: "true"}
        end: {output: {}}
    """)
    written(tmp_path / "nodes" / "a" / "1", *range(10))
    written(tmp_path / "nodes" / "b" / "1", 0)
    events = [started("a", items=10), started("b", items=10)]
    found = progress.estimate(workflow, events, "running", folder=tmp_path)
    assert found["percent"] == 10  # b has 9 of its 10 items to go, whatever a did


def test_a_step_in_progress_of_a_run_that_does_not_move_is_paused():
    events = lap("explore") + [started("implement")]
    for status in ("limit", "stopped", "waiting"):
        assert progress.estimate(parse(LOOP), events, status)["nodes"]["implement"]["state"] == "paused"


# ---------- with earlier runs ----------


MEANS = {"explore": 10.0, "implement": 100.0, "tests": 20.0, "review": 30.0}


def test_with_earlier_runs_steps_weigh_their_time_and_loops_their_mean_count():
    learnt = progress.Earlier(runs=2, mean=MEANS, taken={("review", 1): 1.0})
    events = lap("explore", "implement", "tests") + [started("review")]
    found = progress.estimate(parse(LOOP), events, "running", learnt, now=progress._seconds(T0) + 15)
    # 145 s done: 10 + 100 + 20, and 15 of review's 30
    # 165 s left: review's other 15, then the loop once more: 100 + 20 + 30
    assert (found["estimate"], found["earlier_runs"], found["left_s"], found["percent"]) == ("earlier runs", 2, 165, 47)
    assert found["loops"][0]["expected"] == 1.0
    assert progress.describe(found).endswith("about 47%, about 3 min left")


def test_the_items_that_finished_beat_the_time_a_step_took(tmp_path):
    workflow = parse("""
        version: 1
        name: items
        start: {input: {}}
        nodes:
          each: {needs: [start], for_each: "[1, 2, 3, 4]", command: "true"}
          after: {needs: [each], command: "true"}
        end: {output: {}}
    """)
    written(tmp_path / "nodes" / "each" / "1", 0)
    events = [started("each", items=4)]
    untimed = progress.estimate(workflow, events, "running", folder=tmp_path)
    assert (untimed["nodes"]["each"]["items_done"], untimed["percent"]) == (1, 12)
    assert progress.describe(untimed) == "step 1 of 2 (each: 1 of 4 items done), about 12%"

    learnt = progress.Earlier(runs=1, mean={"each": 100.0, "after": 10.0}, taken={})
    timed = progress.estimate(workflow, events, "running", learnt, folder=tmp_path, now=progress._seconds(T0) + 100)
    assert (timed["left_s"], timed["percent"]) == (85, 23)  # 3 items of 4 to go, though the mean time has passed


def test_a_run_that_does_not_move_stops_its_clock_at_its_last_event():
    learnt = progress.Earlier(runs=1, mean=MEANS, taken={("review", 1): 0.0})
    events = lap("explore") + [started("implement")]
    found = progress.estimate(parse(LOOP), events, "stopped", learnt)  # long after T0: the clock is now
    assert found["left_s"] == 150  # implement's 100 s, tests and review: none of it done while it stood still


def test_a_step_that_no_earlier_run_ran_weighs_the_mean_of_the_others():
    workflow = parse(LOOP.replace("end: {output: {}}", """  publish: {needs: [], command: "true"}
end: {output: {}}""").replace("to: end}\n      - {to: implement", "to: publish}\n      - {to: implement"))
    learnt = progress.Earlier(runs=1, mean=MEANS, taken={("review", 1): 0.0})
    events = lap("explore", "implement", "tests", "review") + [route("review", 0, "publish"), started("publish")]
    found = progress.estimate(workflow, events, "running", learnt, now=progress._seconds(T0))
    assert found["left_s"] == 40 and found["percent"] == 80  # publish weighs (10 + 100 + 20 + 30) / 4
    later = progress.estimate(workflow, events, "running", learnt, now=progress._seconds(T0) + 20)
    assert later["left_s"] == 20 and later["percent"] == 90  # half of that mean has passed


def test_a_sub_workflow_in_progress_counts_its_own_steps_and_items(tmp_path):
    (tmp_path / "inner.yaml").write_text(dedent("""
        version: 1
        name: inner
        start: {input: {}}
        nodes:
          one: {needs: [start], command: "true"}
          each: {needs: [one], for_each: "[1, 2, 3, 4]", command: "true"}
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
    run_dir = tmp_path / "run"
    written(run_dir / "nodes" / "sub" / "1" / "attempt-2" / "nodes" / "each" / "1", 0, 1)
    events = [started("sub"), event("attempt_started", "sub", attempt=2, **{"pass": 1})]
    events += lap("sub/one") + [started("sub/each", items=4)]
    found = progress.estimate(spec.load(outer), events, "running", folder=run_dir)
    assert found["nodes"]["sub"]["inner"] == {"step": 2, "steps": 2, "at": ["each"], "percent": 75}
    assert progress.describe(found) == "step 1 of 2 (sub: step 2 of 2), about 38%"


def test_each_step_of_a_sub_workflow_has_a_state_from_its_own_events(tmp_path):
    (tmp_path / "inner.yaml").write_text(dedent("""
        version: 1
        name: inner
        start: {input: {}}
        nodes:
          print: {needs: [start], command: "true"}
          check: {needs: [print], command: "true"}
        end: {output: {}}
    """).lstrip())
    outer = tmp_path / "outer.yaml"
    outer.write_text(dedent("""
        version: 1
        name: outer
        start: {input: {}}
        nodes:
          rules: {needs: [start], workflow: inner.yaml}
          build: {needs: [rules], for_each: "[1, 2]", workflow: inner.yaml}
          review:
            needs: [build]
            command: "true"
            routes: [{when: "review.exit_code != 0", to: rules, max: 2}, {to: end}]
        end: {output: {}}
    """).lstrip())
    workflow = spec.load(outer)

    def states(events: list[dict], status: str = "running") -> dict:
        tracker = progress.Tracker(workflow)
        for one in events:
            tracker.add(one)
        return tracker.states(status)

    rules = [started("rules"), *lap("rules/print"), started("rules/check")]
    assert states(rules) == {"rules": "running", "build": "waiting", "review": "waiting",
                             "rules/print": "done", "rules/check": "running"}
    assert states(rules, "waiting")["rules/check"] == "paused"

    items = rules + lap("rules/check") + [event("node_finished", "rules", **{"pass": 1}), started("build", items=2)]
    items += lap("build[0]/print", "build[0]/check") + [started("build[1]/print")]
    found = states(items)
    assert (found["rules/check"], found["build"], found["build[]/print"], found["build[]/check"]) == (
        "done", "running", "running", "waiting")  # item 1 still prints, and checks after

    cut = items + [event("attempt_failed", "build", index=1, attempt=1, error="timed out after 10m", **{"pass": 1})]
    assert states(cut)["build[]/print"] == "failed"  # its item's attempt ended under it
    again = cut + [event("attempt_started", "build", index=1, attempt=2, **{"pass": 1})]
    assert states(again)["build[]/print"] == "waiting"  # the next attempt starts its sub-workflow over

    back = items + [event("node_finished", "build[1]/print", **{"pass": 1})] + lap("build[1]/check")
    back += [event("node_finished", "build", **{"pass": 1})] + lap("review") + [route("review", 0, "rules")]
    found = states(back)
    assert (found["rules"], found["rules/print"], found["build[]/check"]) == ("waiting", "waiting", "waiting")
    done = back[:-1] + [route("review", 1, "end")]
    assert set(states(done, "succeeded").values()) == {"done"}


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


def newest() -> Path:
    return max((Path(".wf") / "runs").iterdir(), key=lambda p: (p / "workflow" / "manifest.json").stat().st_mtime)


def test_wf_status_says_how_far_a_run_is_and_learns_from_earlier_real_runs(repo, capsys):
    Path("steps.yaml").write_text(STEPS.lstrip())
    Path("script.yaml").write_text("inputs: {}\n")
    assert cli.main(["run", "steps.yaml"]) == 0
    assert cli.main(["run", "steps.yaml"]) == 0
    assert cli.main(["run", "steps.yaml", "--dry-run", "script.yaml"]) == 0  # a dry run: not counted for a real one
    assert cli.main(["run", "steps.yaml", "fail=true"]) == 1
    failed = newest()

    capsys.readouterr()
    assert cli.main(["status", failed.name]) == 0
    line = capsys.readouterr().out.splitlines()[1]
    assert line.startswith("  step 2 of 2 (two), about ")

    assert cli.main(["status", failed.name, "--json"]) == 0
    found = json.loads(capsys.readouterr().out)["progress"]
    assert (found["estimate"], found["earlier_runs"], found["at"]) == ("earlier runs", 2, ["two"])
    assert found["nodes"]["two"]["state"] == "failed" and found["left_s"] is not None
    history = progress.earlier(Run.at(failed), "steps")
    assert failed.name not in [r.id for r in history]

    # what earlier runs say belongs to a version of the workflow: here `two` has another name
    renamed = parse(STEPS.replace("  two:", "  second:"))
    assert set(progress.learn(parse(STEPS), history).mean) == {"one", "two"}
    assert set(progress.learn(renamed, history).mean) == {"one"}


def test_learning_from_several_threads_keeps_the_numbers_whole(repo, monkeypatch):
    Path("steps.yaml").write_text(STEPS.lstrip().replace("sleep 1", "true"))
    assert cli.main(["run", "steps.yaml"]) == 0
    history = [Run.at(newest())]
    monkeypatch.setattr(progress, "_LEARNT", {})
    monkeypatch.setattr(progress, "_LEARNT_MOST", 2)
    versions = [parse(STEPS.replace("  two:", f"  two{n}:")) for n in range(8)]
    problems = []

    def learn_all() -> None:
        try:
            for workflow in versions * 5:
                progress.learn(workflow, history)
        except Exception as error:  # noqa: BLE001 - the test reports any of them
            problems.append(error)

    threads = [threading.Thread(target=learn_all) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert problems == [] and len(progress._LEARNT) <= 2


def test_wf_status_still_says_where_a_run_stands_when_its_files_change_meanwhile(repo, monkeypatch, capsys):
    Path("steps.yaml").write_text(STEPS.lstrip().replace("sleep 1", "true"))
    assert cli.main(["run", "steps.yaml"]) == 0
    assert cli.main(["run", "steps.yaml", "fail=true"]) == 1
    failed = newest()

    def gone(run):
        raise FileNotFoundError(f"{run.dir} was discarded")

    monkeypatch.setattr(report, "version", gone)  # an earlier run discarded while it is read
    capsys.readouterr()
    assert cli.main(["status", failed.name, "--json"]) == 0
    state = json.loads(capsys.readouterr().out)
    assert (state["status"], state["progress"]) == ("failed", None)
    (failed / "events.jsonl").unlink()
    assert report.events(Run.at(failed)) == []
