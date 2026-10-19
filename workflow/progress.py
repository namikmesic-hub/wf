"""Progress: where a run is on its way from start to end, and how far that is, as an estimate.

    wf status <run-id>          step 2 of 4 (implement), review → implement 1 of at most 3, about 31%
    wf status <run-id> --json   the same under "progress", with the state of each node

It reads what `wf report` reads: the copied workflow files and events.jsonl.
So it works for each run, finished or not, and costs nothing. The steps and
the loops come from the graph (shape.py).

The run's events give its open nodes: the nodes that run, wait to start,
failed, or finished and wait for their route to be chosen. The rest of this
lap is the heaviest path from an open node to end, with what is left of that
node. The open node with the most left decides how far the lap is. Of open
nodes with as much left, the one with the least done decides, then file order.
So a slow parallel step holds the estimate back. A step that a route left out
weighs nothing, also on a path that leads back into the branch that the route
chose. So it counts no more, in the steps or in the percentage.

    done    = each loop's lap, times the times the run went back,
              plus the part of this lap before the open node that decides it
    left    = the rest of this lap from that node,
              plus each loop's lap, times the times it can still go back
    percent = done / (done + left)

Each step weighs 1, and each loop counts as if the run goes back up to its
`max`. So the percentage is low rather than high. When the run has no
parallel steps, it does not go down.

When the runs folder holds earlier runs of the workflow that succeeded, the
estimate uses the newest HISTORY of them. A dry run uses only dry runs. Each
step then weighs its mean time per pass in them. A step that none of them ran
weighs the mean of the other steps. Each loop counts as many times as they
went back on average. `left` is then in seconds: the time left.

A step in progress counts in part: the share of its items that finished, or
the share of its sub-workflow that is done. With earlier runs, a step with no
items and no sub-workflow counts its time so far against its weight. While the
run does not move, that time stops at its last event.
"""

import hashlib
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import report, spec
from .runs import Run
from .scheduler import START, Topology, triggered
from .shape import Shape

HISTORY = 10
_RETRIED = {"attempt_started", "attempt_failed", "agent_tool", "harness_unavailable", "session_started",
            "session_fresh", "session_continued", "slot_waiting", "slot_taken"}  # a failed pass that `wf retry` reruns


# ---------- what the run did ----------


@dataclass
class _Trace:
    pending: set[str]
    running: dict[str, str]  # node -> when its pass started (or `wf retry` reran it)
    since: dict[str, int]  # node -> the index of the event that started its current attempt
    attempt: dict[str, int]  # node -> the number of its current attempt
    failed: set[str]
    routing: set[str]  # finished, and its route not chosen yet
    done: set[str]  # finished in this lap
    ran: set[str]  # finished at least once
    passes: dict[str, int]
    items: dict[str, int | None]
    taken: dict[tuple[str, int], int]  # (node, route index) -> times taken
    back: dict[tuple[str, str], int]  # (node, the node that needs it) -> times a needs edge went back


def _trace(shape: Shape, events: list[dict]) -> _Trace:
    """Play the run's events for this workflow's own nodes: what is open, and what is done."""
    topo = shape.topo
    needs_back = {(loop.last, loop.first) for loop in shape.loops if loop.route is None}
    found = _Trace(set(triggered(topo, START, None)), {}, {}, {}, set(), set(), set(), set(), {}, {}, {}, {})
    for number, event in enumerate(events):
        kind, node = event.get("event"), event.get("node")
        if not isinstance(node, str) or node not in topo.needs:  # run-level events, and nodes in sub-workflows (build[2]/x)
            continue
        if kind == "node_started":
            found.pending.discard(node)
            found.failed.discard(node)
            found.done.discard(node)
            found.running[node], found.since[node], found.attempt[node] = event.get("time", ""), number, 1
            found.passes[node] = event.get("pass") or found.passes.get(node, 0) + 1
            found.items[node] = event.get("items")
        elif kind == "node_finished":
            found.running.pop(node, None)
            found.done.add(node)
            found.ran.add(node)
            if topo.routes.get(node):
                found.routing.add(node)  # the route event follows
            else:
                found.pending.update(topo.dependents.get(node, ()))
                for target in (t for t in topo.dependents.get(node, ()) if (node, t) in needs_back):
                    found.back[(node, target)] = found.back.get((node, target), 0) + 1
                    found.done -= shape.after(target)  # a loop: its steps are ahead again
        elif kind == "node_failed":
            found.running.pop(node, None)
            found.failed.add(node)
        elif kind == "route":
            found.routing.discard(node)
            index = _route_index(topo, node, event)
            if index is not None:
                found.taken[(node, index)] = event.get("taken") or found.taken.get((node, index), 0) + 1
            for target in (t for t in event.get("to") or [] if t in topo.needs):
                found.pending.add(target)
                if target in found.ran:  # a loop: its steps are ahead again
                    found.done -= shape.after(target)
        elif kind in _RETRIED:
            if node in found.failed:
                found.failed.discard(node)
                found.running[node] = event.get("time", "")
            if kind in ("attempt_started", "attempt_failed"):  # a sub-workflow's next attempt starts it over
                found.since[node] = number
            if kind == "attempt_started" and isinstance(event.get("attempt"), int):
                found.attempt[node] = event["attempt"]
    return found


def _route_index(topo: Topology, node: str, event: dict) -> int | None:
    if isinstance(event.get("route"), int):
        return event["route"]
    for index, route in enumerate(topo.routes.get(node, ())):  # older runs: no route index in the event
        if (list(route.targets), route.when, route.max) == (event.get("to"), event.get("when"), event.get("max")):
            return index
    return None


# ---------- earlier runs ----------


@dataclass(frozen=True)
class Earlier:
    runs: int
    mean: dict[str, float]  # node -> seconds per pass
    taken: dict[tuple[str, int], float]  # (node, route index) -> times taken per run


def earlier(run: Run, name: str) -> list[Run]:
    """The newest HISTORY other runs of workflow `name` that succeeded, in the run's runs folder.
    A dry run gets dry runs, and a real run real runs."""
    dry = bool(run.info().get("dry_run"))
    try:
        folders = sorted((p for p in run.dir.parent.iterdir() if f"-{name}-" in p.name and p != run.dir
                          and not p.name.startswith(".")), key=lambda p: p.name, reverse=True)  # ids start with the time
    except OSError:
        return []
    found = []
    for folder in folders:
        other = Run(folder.name, folder, run.repo)
        info = other.info()
        if (info.get("workflow") == name and bool(info.get("dry_run")) == dry and other.status().get("status") == "succeeded"
                and (folder / "workflow" / "manifest.json").is_file()):
            found.append(other)
            if len(found) == HISTORY:
                break
    return found


_LEARNT: dict[tuple, Earlier] = {}  # finished runs don't change, so their numbers are kept
_LEARNT_MOST = 32
_LEARNING = threading.Lock()  # wf ui asks from several threads


def learn(workflow: spec.Workflow, runs: list[Run]) -> Earlier:
    """What earlier runs say about each step's time and each route's use, from `wf report`. The
    numbers belong to this version of the workflow, with its nodes and routes."""
    key = (hashlib.sha256(workflow.model_dump_json().encode()).hexdigest(), tuple(str(r.dir) for r in runs))
    with _LEARNING:
        if key in _LEARNT:
            return _LEARNT[key]
    found, count = report.build(runs), len(runs)
    mean = {name: node["duration_s"] / node["passes"] for name, node in found["nodes"].items()
            if name in workflow.nodes and node["passes"]}
    taken = {}
    for name, node in workflow.nodes.items():
        listed = [r for r in found["routes"] if r["from"] == name]
        for index, route in enumerate(node.routes):
            same = [r for r in listed if (r["to"], r["when"], r["max"]) == (list(route.targets), route.when, route.max)]
            at = listed[index] if index < len(listed) and listed[index] in same else (same or [None])[0]
            if at is not None:
                taken[(name, index)] = at["taken"] / count
    learnt = Earlier(count, mean, taken)
    with _LEARNING:
        while len(_LEARNT) >= _LEARNT_MOST:
            del _LEARNT[next(iter(_LEARNT))]
        _LEARNT[key] = learnt
    return learnt


# ---------- the estimate ----------


def of(run: Run, status: str | None = None, events: list[dict] | None = None) -> dict | None:
    """The run's progress, as the module's docstring says. None if the run's files can't be read,
    for example while it is discarded. The status comes from operate.state(), which knows a
    stopped run. With `events`, it doesn't read events.jsonl again."""
    try:
        workflow = spec.load(run.workflow_file)
        status = status or run.status().get("status")
        history = earlier(run, workflow.name) if status != "succeeded" else []
        learnt = learn(workflow, history) if history else None
        return estimate(workflow, report.events(run) if events is None else events, status, learnt, run.dir)
    except (spec.SpecError, OSError, KeyError, ValueError):
        return None


def estimate(workflow: spec.Workflow, events: list[dict], status: str | None, learnt: Earlier | None = None,
             folder: Path | None = None, now: float | None = None, depth: int = 0) -> dict:
    """The progress of a run of `workflow`, from its events. It counts the items that finished in
    `folder`, the run folder. Without `now`, the time is now, or the last event's while the run
    does not move. The depth is how deep in sub-workflows this one is."""
    moving = status in ("running", None)
    if now is None:
        now = _seconds(events[-1].get("time")) if events and not moving else time.time()
    shape = Shape(workflow)
    trace = _trace(shape, events)
    open_nodes = trace.pending | set(trace.running) | trace.failed | trace.routing if status != "succeeded" else set()
    ahead = shape.topo.reachable(open_nodes)
    states = {node: _state(node, trace, ahead, moving) for node in shape.order}
    known = [learnt.mean[n] for n in shape.order if learnt and n in learnt.mean]
    timed = any(known)
    typical = sum(known) / len(known) if timed else 1.0
    # A step that a route left out weighs nothing, also on a path that comes back into the chosen branch
    weight = {n: 0.0 if states[n] == "skipped" else learnt.mean.get(n, typical) if timed else 1.0 for n in shape.order}
    unit = {n: 0.0 if states[n] == "skipped" else 1.0 for n in shape.order}
    depths, tails = shape.depth(weight), shape.tail(weight)
    step_of, steps_after = shape.depth(unit), shape.tail(unit)

    nodes = {}
    for node in shape.order:
        info = {"state": states[node], "step": shape.step[node], "passes": trace.passes.get(node, 0)}
        if trace.items.get(node) is not None:
            info["items"] = trace.items[node]
            if folder is not None and node in trace.running:
                info["items_done"] = _items_done(folder, node, trace.passes[node], trace.items[node])
        if node in trace.running and workflow.nodes[node].kind == "workflow" and info.get("items") is None:
            attempt_dir = (folder / "nodes" / node / str(trace.passes[node]) / f"attempt-{trace.attempt.get(node, 1)}"
                           if folder is not None else None)
            inner = _inner(workflow, node, events[trace.since[node] + 1:], attempt_dir, moving, depth)
            if inner is not None:
                info["inner"] = inner
        nodes[node] = info

    def left_of(node: str) -> float:
        """What is left of an open node: all of it, none once it finished, or the part not done yet."""
        if node in trace.routing:
            return 0.0
        if node not in trace.running:
            return weight[node]
        info = nodes[node]
        if info.get("items"):  # what it did beats how long it took
            share = info.get("items_done", 0) / info["items"]
        elif info.get("inner"):
            share = info["inner"]["percent"] / 100
        elif timed and weight[node]:  # its own mean time, or the mean of the steps for a step that no earlier run ran
            share = (now - _seconds(trace.running[node])) / weight[node]
        else:
            share = 0.0
        return weight[node] * (1 - min(max(share, 0.0), 1.0))

    if open_nodes:  # of nodes with as much left, the one with the least done decides, then file order
        deciding = max(sorted(open_nodes, key=shape.order.index),
                       key=lambda n: (left_of(n) + tails[n], left_of(n) - depths[n]))
        lap_done, lap_left = depths[deciding] - left_of(deciding), left_of(deciding) + tails[deciding]
        steps = max(step_of[n] + steps_after[n] for n in open_nodes)
    else:
        lap_done, lap_left = max((depths[n] for n in trace.done), default=0.0), 0.0
        steps = max(step_of.values(), default=0.0)
    laps = still = 0.0
    loops = []
    for loop in shape.loops:
        if loop.route is not None:
            taken = trace.taken.get((loop.last, loop.route), 0)
        else:  # a needs edge goes back
            taken = trace.back.get((loop.last, loop.first), 0)
        size = shape.span(loop, weight)
        expected = learnt.taken.get((loop.last, loop.route)) if learnt and loop.route is not None else None
        laps += taken * size
        if loop.last in ahead:  # the run can still go back
            more = expected - taken if timed and expected is not None else (loop.max or taken) - taken
            still += max(more, 0) * size
        loops.append({"from": loop.last, "to": loop.first, "route": loop.route, "taken": taken, "max": loop.max,
                      **({"expected": round(expected, 2)} if expected is not None else {})})
    done, left = laps + lap_done, lap_left + still

    at = [n for n in shape.order if n in trace.running or n in trace.failed]
    if status == "succeeded":
        step, percent, left = steps, 100, 0.0
    else:  # between steps: the step that starts next
        step = max((step_of[n] for n in at or trace.pending or trace.done), default=0.0)
        percent = min(round(100 * done / (done + left)), 99) if done + left else 0
    at.sort(key=lambda n: (-step_of[n], shape.order.index(n)))
    return {"step": round(step), "steps": round(max(steps, step)), "at": at, "percent": percent,
            "estimate": "earlier runs" if timed else "steps", "earlier_runs": learnt.runs if learnt else 0,
            "left_s": round(left) if timed else None, "loops": loops, "nodes": nodes}


def _state(node: str, trace: _Trace, ahead: set[str], moving: bool) -> str:
    if node in trace.running:
        return "running" if moving else "paused"
    if node in trace.failed:
        return "failed"
    if node in trace.done:  # in this lap: a loop can lead back to it, but it has done its part for now
        return "done"
    if node in ahead:
        return "waiting"
    return "done" if node in trace.ran else "skipped"


def _items_done(folder: Path, node: str, pass_no: int, items: int) -> int:
    pass_dir = folder / "nodes" / node / str(pass_no)
    return sum((pass_dir / str(index) / "output.json").exists() for index in range(items))


def _inner(workflow: spec.Workflow, node: str, events: list[dict], folder: Path | None, moving: bool,
           depth: int) -> dict | None:
    """The progress of a workflow node's sub-workflow, from the events it passes up (node/inner).
    Its items that finished are in `folder`, the attempt's own folder."""
    if depth >= 10:
        return None
    try:
        sub = spec.load(Path(workflow.path).parent / workflow.nodes[node].workflow)
    except (spec.SpecError, OSError, TypeError):
        return None
    prefix = f"{node}/"
    inner = [{**e, "node": e["node"].removeprefix(prefix)} for e in events if str(e.get("node", "")).startswith(prefix)]
    found = estimate(sub, inner, "running" if moving else "paused", folder=folder, depth=depth + 1)
    return {key: found[key] for key in ("step", "steps", "at", "percent")}


def _seconds(when) -> float:
    try:
        return datetime.fromisoformat(when).timestamp()
    except (TypeError, ValueError):
        return time.time()


# ---------- in words ----------


def describe(found: dict) -> str:
    """step 3 of 6 (review), review → implement 1 of at most 3, about 40%, about 12 min left"""
    parts = []
    if found["steps"]:
        at = [_where(name, found["nodes"][name]) for name in found["at"]]
        parts.append(f"step {found['step']} of {found['steps']}" + (f" ({', '.join(at)})" if at else ""))
    parts += [f"{loop['from']} → {loop['to']} {loop['taken']} of at most {loop['max']}"
              for loop in found["loops"] if loop["taken"] and loop["max"]]
    parts.append(f"about {found['percent']}%")
    if found["left_s"] is not None:
        parts.append(f"about {duration(found['left_s'])} left" if found["left_s"] >= 60 else "less than a minute left")
    return ", ".join(parts)


def _where(name: str, info: dict) -> str:
    if "items_done" in info:
        return f"{name}: {info['items_done']} of {info['items']} items done"
    if "inner" in info:
        return f"{name}: step {info['inner']['step']} of {info['inner']['steps']}"
    return name


def duration(seconds: float) -> str:
    """45 s, 12 min, 1 h 5 min"""
    seconds = round(seconds)
    if seconds < 60:
        return f"{seconds} s"
    if seconds < 3600:
        return f"{round(seconds / 60)} min"
    hours, minutes = divmod(round(seconds / 60), 60)
    return f"{hours} h {minutes} min" if minutes else f"{hours} h"
