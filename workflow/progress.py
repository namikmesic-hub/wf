"""Progress: where a run is on its way from start to end, and how far that is, as an estimate.

    wf status <run-id>          step 3 of 6 (review), review → implement 1 of at most 3, about 40%
    wf status <run-id> --json   the same under "progress", with the state of each node

It reads what `wf report` reads: the copied workflow files and events.jsonl.
So it works for each run, finished or not, and costs nothing.

The way from start to end. A walk from start, depth first and in file order,
finds the route edges that go back. Each of them closes a loop. Without them
the graph has no cycles. A node's step is the number of nodes on the longest
path from start to it. The straight path is the longest path from start to
end, and its length is the number of steps.

The estimate. Each step weighs 1. A loop repeats its steps, from the step it
goes back to, to the step whose route goes back.

    done    = each loop's steps, times the times the run took it,
              plus the steps of this lap that are done
    left    = the steps of this lap that are not done,
              plus each loop's steps, times the times it can still be taken
    percent = done / (done + left)

Without earlier runs, each loop counts as if the run takes it up to its `max`.
So the percentage is low rather than high, and it does not go down.

When the runs folder holds earlier runs of the workflow that succeeded, the
estimate uses the newest HISTORY of them. A dry run uses only dry runs.
Each step weighs its mean time per pass in those runs, times the share of
those runs that got to it. Each loop counts as many times as those runs took
it on average. `left` is then in seconds: the time left.

A step in progress counts in part. That part is the share of its items that
finished, or of its sub-workflow that is done. With earlier runs, it can also
be its time so far against its mean time.
"""

import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import report, spec
from .runs import Run
from .scheduler import END, START, Topology, triggered

HISTORY = 10
_RETRIED = {"attempt_started", "attempt_failed", "agent_tool", "harness_unavailable", "session_started",
            "session_fresh", "session_continued", "slot_waiting", "slot_taken"}  # a failed pass that `wf retry` reruns


@dataclass(frozen=True)
class Loop:
    first: str  # the step it goes back to
    last: str  # the step whose route goes back
    node: str  # the node whose route counts the loop: `last`, unless that route has no `max`
    route: int | None
    max: int | None
    body: frozenset[str]  # the steps it repeats


class Shape:
    """A workflow's graph without its loops: the steps from start to end."""

    def __init__(self, workflow: spec.Workflow):
        self.topo = topo = Topology.of(workflow)
        self.order = topo.order
        self.forward: dict[str, list[str]] = {node: [] for node in (START, *topo.order)}
        self.edges: list[tuple[str, str, str, int | None]] = []  # (from, to, needs | route | loop, route index)
        back: list[tuple[str, int | None, str]] = []
        state: dict[str, int] = {}  # 1 while the walk is inside the node, 2 once it left it
        finished: list[str] = []

        def visit(node: str) -> None:
            state[node] = 1
            for index, target in _out(topo, node):
                if target == END:
                    self.edges.append((node, END, "route", index))
                elif state.get(target) == 1:
                    back.append((node, index, target))
                    self.edges.append((node, target, "loop", index))
                else:
                    self.edges.append((node, target, "needs" if index is None else "route", index))
                    if target not in self.forward[node]:
                        self.forward[node].append(target)
                    if target not in state:
                        visit(target)
            state[node] = 2
            finished.append(node)

        for node in (START, *topo.order):  # validate makes each node reachable from start: the others guard a damaged copy
            if node not in state:
                visit(node)
        self.sorted = [node for node in reversed(finished) if node != START]
        self.loops = [self._loop(*edge) for edge in back]
        self.step = {node: int(depth) for node, depth in self.depth({node: 1.0 for node in self.order}).items()}
        self.steps = max(self.step.values(), default=0)

    def depth(self, weight: dict[str, float]) -> dict[str, float]:
        """The weight of the heaviest path from start to each node, the node included."""
        found: dict[str, float] = {START: 0.0}
        for node in [START, *self.sorted]:
            if node not in found:  # not reached from start: only in a damaged copy
                found[node] = weight[node]
            for target in self.forward[node]:
                found[target] = max(found.get(target, 0.0), found[node] + weight[target])
        del found[START]
        return found

    def after(self, node: str) -> set[str]:
        """The node, and each node that a path without loops leads to from it."""
        seen, todo = {node}, [node]
        while todo:
            for target in self.forward[todo.pop()]:
                if target not in seen:
                    seen.add(target)
                    todo.append(target)
        return seen

    def _loop(self, last: str, index: int | None, first: str) -> Loop:
        body = frozenset(n for n in self.after(first) if last in self.after(n))
        counter = (last, index, self.topo.routes[last][index].max) if index is not None else (last, None, None)
        if counter[2] is None:  # validate puts a `max` on each loop: here it is on another of its routes
            bounded = [(route.max, node, i) for node in self.order if node in body
                       for i, route in enumerate(self.topo.routes.get(node, ())) if route.max and set(route.targets) & body]
            if bounded:
                most, node, i = min(bounded)
                counter = (node, i, most)
        return Loop(first, last, counter[0], counter[1], counter[2], body)


def _out(topo: Topology, node: str) -> list[tuple[int | None, str]]:
    """The edges from a node, in file order: its needs edges (index None), then its routes."""
    edges = [(None, target) for target in topo.dependents.get(node, ())]
    return edges + [(i, target) for i, route in enumerate(topo.routes.get(node, ())) for target in route.targets]


# ---------- what the run did ----------


@dataclass
class _Trace:
    pending: set[str]
    running: dict[str, str]  # node -> when its pass started (or `wf retry` reran it)
    since: dict[str, int]  # node -> the index of the event that started its current attempt
    failed: set[str]
    done: set[str]  # finished in this lap
    ran: set[str]  # finished at least once
    passes: dict[str, int]
    items: dict[str, int | None]
    taken: dict[tuple[str, int], int]


def _trace(shape: Shape, events: list[dict]) -> _Trace:
    """Play the run's events for this workflow's own nodes: what is pending, running, failed and done."""
    topo = shape.topo
    found = _Trace(set(triggered(topo, START, None)), {}, {}, set(), set(), set(), {}, {}, {})
    for number, event in enumerate(events):
        kind, node = event.get("event"), event.get("node")
        if not isinstance(node, str) or node not in topo.needs:  # run-level events, and nodes in sub-workflows (build[2]/x)
            continue
        if kind == "node_started":
            found.pending.discard(node)
            found.failed.discard(node)
            found.done.discard(node)
            found.running[node], found.since[node] = event.get("time", ""), number
            found.passes[node] = event.get("pass") or found.passes.get(node, 0) + 1
            found.items[node] = event.get("items")
        elif kind == "node_finished":
            found.running.pop(node, None)
            found.done.add(node)
            found.ran.add(node)
            if not topo.routes.get(node):
                found.pending.update(topo.dependents.get(node, ()))
        elif kind == "node_failed":
            found.running.pop(node, None)
            found.failed.add(node)
        elif kind == "route":
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
    weight: dict[str, float]  # node -> seconds per pass, times the share of runs that got to it
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


_LEARNT: dict[tuple, Earlier] = {}  # finished runs don't change, so their numbers are kept by run ids


def learn(workflow: spec.Workflow, runs: list[Run]) -> Earlier:
    """What earlier runs say about each step's time and each route's use, from `wf report`."""
    key = (workflow.name, tuple(str(r.dir) for r in runs))
    if key not in _LEARNT:
        found, count = report.build(runs), len(runs)
        mean = {name: node["duration_s"] / node["passes"] for name, node in found["nodes"].items()
                if name in workflow.nodes and node["passes"]}
        weight = {name: mean[name] * found["nodes"][name]["runs"] / count for name in mean}
        taken = {}
        for name, node in workflow.nodes.items():
            listed = [r for r in found["routes"] if r["from"] == name]
            for index, route in enumerate(node.routes):
                same = [r for r in listed if (r["to"], r["when"], r["max"]) == (list(route.targets), route.when, route.max)]
                at = listed[index] if index < len(listed) and listed[index] in same else (same or [None])[0]
                if at is not None:
                    taken[(name, index)] = at["taken"] / count
        if len(_LEARNT) >= 32:
            _LEARNT.pop(next(iter(_LEARNT)))
        _LEARNT[key] = Earlier(count, weight, mean, taken)
    return _LEARNT[key]


# ---------- the estimate ----------


def of(run: Run, status: str | None = None) -> dict | None:
    """The run's progress, as the module's docstring says. None if the run's copy of its workflow
    can't be read. The status comes from operate.state(), which knows a stopped run."""
    try:
        workflow = spec.load(run.workflow_file)
    except (spec.SpecError, OSError, KeyError, ValueError):
        return None
    status = status or run.status().get("status")
    history = earlier(run, workflow.name) if status != "succeeded" else []
    learnt = learn(workflow, history) if history else None
    return estimate(workflow, report.events(run), status, learnt, run.dir, time.time())


def estimate(workflow: spec.Workflow, events: list[dict], status: str | None, learnt: Earlier | None = None,
             folder: Path | None = None, now: float | None = None, depth: int = 0) -> dict:
    """The progress of a run of `workflow`, from its events. It counts the items that finished in
    `folder`, the run folder. The depth is how deep in sub-workflows this one is."""
    now = time.time() if now is None else now
    shape = Shape(workflow)
    trace = _trace(shape, events)
    timed = learnt is not None and any(learnt.weight.get(n) for n in shape.order)
    weight = {n: learnt.weight.get(n, 0.0) if timed else 1.0 for n in shape.order}
    depths = shape.depth(weight)
    moving = status in ("running", None)
    active = trace.pending | set(trace.running) | trace.failed if status != "succeeded" else set()
    ahead = shape.topo.reachable(active)

    nodes = {}
    for node in shape.order:
        info = {"state": _state(node, trace, ahead, moving), "step": shape.step[node], "passes": trace.passes.get(node, 0)}
        if trace.items.get(node) is not None:
            info["items"] = trace.items[node]
            if folder is not None and node in trace.running:
                info["items_done"] = _items_done(folder, node, trace.passes[node], trace.items[node])
        if node in trace.running and workflow.nodes[node].kind == "workflow" and info.get("items") is None:
            inner = _inner(workflow, node, events[trace.since[node] + 1:], depth)
            if inner is not None:
                info["inner"] = inner
        nodes[node] = info

    def part(node: str) -> float:
        """How much of a step in progress is done, from 0 to 1."""
        info, shares = nodes[node], [0.0]
        if info.get("items"):
            shares.append(info.get("items_done", 0) / info["items"])
        if info.get("inner"):
            shares.append(info["inner"]["percent"] / 100)
        if timed and learnt.mean.get(node) and trace.running.get(node):
            shares.append((now - _seconds(trace.running[node])) / learnt.mean[node])
        return min(max(shares), 1.0)

    position = max([depths[n] for n in trace.done] + [depths[n] - weight[n] * (1 - part(n)) for n in trace.running]
                   + [depths[n] - weight[n] for n in trace.failed], default=0.0)
    laps = still = 0.0
    loops = []
    for loop in shape.loops:
        taken = trace.taken.get((loop.node, loop.route), 0) if loop.route is not None else 0
        size = depths[loop.last] - depths[loop.first] + weight[loop.first]
        expected = learnt.taken.get((loop.node, loop.route)) if learnt and loop.route is not None else None
        laps += taken * size
        if loop.last in ahead:  # the run can still go back
            more = expected - taken if timed and expected is not None else (loop.max or taken) - taken
            still += max(more, 0) * size
        loops.append({"from": loop.last, "to": loop.first, "node": loop.node, "route": loop.route, "taken": taken,
                      "max": loop.max, **({"expected": round(expected, 2)} if expected is not None else {})})
    done = laps + position
    left = max(max(depths.values(), default=0.0) - position, 0.0) + still

    at = [n for n in shape.order if n in trace.running or n in trace.failed]
    if status == "succeeded":
        step, percent, left = shape.steps, 100, 0.0
    else:  # between steps: the step that starts next
        step = max((shape.step[n] for n in at or trace.pending or trace.done), default=0)
        percent = min(round(100 * done / (done + left)), 99) if done + left else 0
    at.sort(key=lambda n: (-shape.step[n], shape.order.index(n)))
    return {"step": step, "steps": shape.steps, "at": at, "percent": percent,
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


def _inner(workflow: spec.Workflow, node: str, events: list[dict], depth: int) -> dict | None:
    """The progress of a workflow node's sub-workflow, from the events it passes up (node/inner)."""
    if depth >= 10:
        return None
    try:
        sub = spec.load(Path(workflow.path).parent / workflow.nodes[node].workflow)
    except (spec.SpecError, OSError, TypeError):
        return None
    prefix = f"{node}/"
    inner = [{**e, "node": e["node"].removeprefix(prefix)} for e in events if str(e.get("node", "")).startswith(prefix)]
    found = estimate(sub, inner, "running", depth=depth + 1)
    return {key: found[key] for key in ("step", "steps", "at", "percent")}


def _seconds(when: str) -> float:
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
