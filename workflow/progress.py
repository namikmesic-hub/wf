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

A follow-up (`wf continue`) starts a new lap from the nodes it names. The
nodes those can lead to are ahead again, and each loop counts from zero.

A step in progress counts in part: the share of its items that finished, or
the share of its sub-workflow that is done. With earlier runs, a step with no
items and no sub-workflow counts its time so far against its weight. While the
run does not move, that time stops at its last event.

A Tracker plays the events one at a time, so wf ui gives it only the new ones
at each refresh. It also gives the state of each step in a sub-workflow, from
the events of the current attempt of the workflow node around it.
"""

import hashlib
import re
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
    failed: set[str]
    routing: set[str]  # finished, and its route not chosen yet
    done: set[str]  # finished in this lap
    ran: set[str]  # finished at least once
    passes: dict[str, int]
    items: dict[str, int | None]
    taken: dict[tuple[str, int], int]  # (node, route index) -> times taken
    back: dict[tuple[str, str], int]  # (node, the node that needs it) -> times a needs edge went back


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
    taken: dict[tuple[str, int], float]  # (node, route index) -> times taken per run, or per follow-up of one


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


_LEARNT: dict[tuple, Earlier] = {}  # by the size of each run's events: a run that `wf continue` gives a follow-up grows
_LEARNT_MOST = 32
_LEARNING = threading.Lock()  # wf ui asks from several threads


def learn(workflow: spec.Workflow, runs: list[Run]) -> Earlier:
    """What earlier runs say about each step's time and each route's use, from `wf report`. The
    numbers belong to this version of the workflow, with its nodes and routes."""
    key = (hashlib.sha256(workflow.model_dump_json().encode()).hexdigest(), tuple((str(r.dir), _size(r)) for r in runs))
    with _LEARNING:
        if key in _LEARNT:
            return _LEARNT[key]
    found = report.build(runs)
    count = sum(1 + summary["follow_ups"] for summary in found["runs"])  # each follow-up is a lap of its own
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


class Tracker:
    """A run of a workflow, as its events tell it. add() plays the next event, estimate() says how
    far the run is, and states() what each step does. wf ui keeps a tracker for each run, so that a
    refresh plays only the events that came since the last one.

    `folder` is the run folder, where the items that finished are. A workflow node, and each of its
    items, has a tracker of its own for its sub-workflow in its current attempt. The depth is how
    deep in sub-workflows this one is."""

    def __init__(self, workflow: spec.Workflow, folder: Path | None = None, depth: int = 0,
                 shape: Shape | None = None, subs: dict | None = None):
        self.workflow, self.folder, self.depth = workflow, folder, depth
        self.shape = shape or Shape(workflow)
        self.trace = _Trace(set(triggered(self.shape.topo, START, None)), {}, set(), set(), set(), set(), {}, {}, {}, {})
        self.needs_back = {(loop.last, loop.first) for loop in self.shape.loops if loop.route is None}
        self.last: str | None = None  # the time of the last event
        self.inner: dict[str, Tracker] = {}  # rules, or build[2] for an item -> the sub-workflow of its current attempt
        self.attempt: dict[str, int] = {}  # rules, or build[2] -> the number of its current attempt
        self.ended: set[str] = set()  # rules, or build[2]: its attempt ended, so no step inside it runs any more
        self.unread: dict[str, list[dict]] = {}  # rules, or build[2] -> the events of its sub-workflow, which can't be read now
        self.subs = {} if subs is None else subs  # (workflow file, node) -> its sub-workflow and shape, read once

    def add(self, event: dict) -> None:
        """Play one event: of a node of this workflow, or of a step in one of its sub-workflows (build[2]/implement)."""
        self.last = event.get("time", self.last)
        node = event.get("node")
        if event.get("event") == "follow_up":  # `wf continue`: a new lap from the nodes it names, with fresh route counters
            found, topo = self.trace, self.shape.topo
            targets = [t for t in event.get("to") or [] if t in topo.needs]
            found.pending, found.taken, found.back = set(targets), {}, {}
            found.done -= topo.reachable(targets)
            found.ran -= topo.reachable(targets)  # in this lap they haven't run yet, so a node it skips is skipped
            return
        if not isinstance(node, str):  # run-level events
            return
        if node in self.shape.topo.needs:
            self._play(node, event)
        elif (inside := re.fullmatch(r"([^/\[]+)(?:\[(\d+)\])?/(.+)", node)) and inside[1] in self.shape.topo.needs:
            outer, index = inside[1], int(inside[2]) if inside[2] else None
            key, child = _key(outer, index), {**event, "node": inside[3]}
            tracker = self.inner.get(key) or self._begin(outer, index)
            if tracker is not None:
                tracker.add(child)
            elif key in self.unread:  # played once its sub-workflow can be read
                self.unread[key].append(child)

    def _play(self, node: str, event: dict) -> None:
        found, topo, kind, index = self.trace, self.shape.topo, event.get("event"), event.get("index")
        if kind == "node_started":
            found.pending.discard(node)
            found.failed.discard(node)
            found.done.discard(node)
            found.running[node] = event.get("time", "")
            found.passes[node] = event.get("pass") or found.passes.get(node, 0) + 1
            found.items[node] = event.get("items")
            for keys in (self.inner, self.attempt, self.unread):  # a new pass: its attempts and items start over
                for key in [k for k in keys if _outer(k) == node]:
                    del keys[key]
            self.ended = {k for k in self.ended if _outer(k) != node}
            for item in range(event["items"]) if isinstance(event.get("items"), int) else [None]:
                self._begin(node, item)
        elif kind == "node_finished":
            found.running.pop(node, None)
            found.done.add(node)
            found.ran.add(node)
            if topo.routes.get(node):
                found.routing.add(node)  # the route event follows
            else:
                targets = topo.dependents.get(node, ())
                # A lap needs the loop's first step to have run, because start can also lead into its last
                # step. And the first step isn't pending already: then the edge doesn't send the run back
                back = [t for t in targets if (node, t) in self.needs_back and t in found.ran and t not in found.pending]
                found.pending.update(targets)
                for target in back:
                    found.back[(node, target)] = found.back.get((node, target), 0) + 1
                    found.done -= self.shape.after(target)  # a loop: its steps are ahead again
        elif kind == "node_failed":
            found.running.pop(node, None)
            found.failed.add(node)
        elif kind == "route":
            found.routing.discard(node)
            route = _route_index(topo, node, event)
            if route is not None:
                found.taken[(node, route)] = event.get("taken") or found.taken.get((node, route), 0) + 1
            for target in (t for t in event.get("to") or [] if t in topo.needs):
                found.pending.add(target)
                if target in found.ran:  # a loop: its steps are ahead again
                    found.done -= self.shape.after(target)
        elif kind == "item_failed":
            self.ended.add(_key(node, index))
        elif kind in _RETRIED:
            if node in found.failed:
                found.failed.discard(node)
                found.running[node] = event.get("time", "")
            key = _key(node, index)
            # A sub-workflow's next attempt starts it over. The same attempt again goes on: after a limit, a question or a crash
            if kind == "attempt_started" and isinstance(event.get("attempt"), int):
                self.ended.discard(key)
                if event["attempt"] != self.attempt.get(key, 1):
                    self.attempt[key] = event["attempt"]
                    self.unread.pop(key, None)
                    self._begin(node, index)
            elif kind in ("attempt_failed", "harness_unavailable"):
                self.ended.add(key)

    def _begin(self, node: str, index: int | None) -> "Tracker | None":
        """A new tracker for the sub-workflow of a workflow node's attempt, or of its item's, if it has one.
        When its file can't be read now, it keeps the events of that attempt until a later call can."""
        key = _key(node, index)
        sub = self._sub(node)
        if sub is None:
            if (self.workflow.path, node) not in self.subs:  # a read error that can pass
                self.unread.setdefault(key, [])
            return None
        folder = None
        if self.folder is not None:
            folder = self.folder / "nodes" / node / str(self.trace.passes.get(node, 1))
            folder = (folder if index is None else folder / str(index)) / f"attempt-{self.attempt.get(key, 1)}"
        self.inner[key] = tracker = Tracker(sub[0], folder, self.depth + 1, sub[1], self.subs)
        for event in self.unread.pop(key, []):
            tracker.add(event)
        return tracker

    def _sub(self, node: str) -> tuple[spec.Workflow, Shape] | None:
        """The sub-workflow of a workflow node, read once. A read error that can pass isn't kept."""
        where = (self.workflow.path, node)
        if where not in self.subs:
            found = None
            if self.depth < 10 and self.workflow.nodes[node].kind == "workflow":
                try:
                    sub = spec.load(Path(self.workflow.path).parent / self.workflow.nodes[node].workflow)
                    found = (sub, Shape(sub))
                except (spec.SpecError, OSError, TypeError) as error:
                    if read_may_pass(error):
                        return None
            self.subs[where] = found
        return self.subs[where]

    def _read_again(self) -> None:
        """Read each sub-workflow that couldn't be read before, and play the events that it kept."""
        for key in list(self.unread):
            node, _, item = key.partition("[")
            self._begin(node, int(item.rstrip("]")) if item else None)

    def _open(self, status: str | None) -> set[str]:
        """The nodes that run, wait to start, failed, or finished and wait for their route."""
        trace = self.trace
        return trace.pending | set(trace.running) | trace.failed | trace.routing if status != "succeeded" else set()

    def states(self, status: str | None, cut: bool = False) -> dict[str, str]:
        """The state of each step, also of each step in a sub-workflow, by the name that `wf report` gives it.
        That is rules/print, or build[]/implement for the implement of each item together. With `cut`, the
        attempt of the workflow node around this one ended. So a step that ran in it failed."""
        self._read_again()
        moving = status in ("running", None)
        ahead = self.shape.topo.reachable(self._open(status))
        own = {node: _state(node, self.trace, ahead, moving, cut) for node in self.shape.order}
        inside: dict[str, list[str]] = {}
        for key, tracker in self.inner.items():
            node = _outer(key)
            ended = cut or key in self.ended or node not in self.trace.running
            name = f"{node}[]" if "[" in key else node
            # A workflow node that is done: the steps that its sub-workflow didn't reach, it skipped
            inner = "succeeded" if own[node] == "done" else "running" if moving else "paused"
            for path, state in tracker.states(inner, ended).items():
                inside.setdefault(f"{name}/{path}", []).append("waiting" if own[node] == "waiting" else state)
        return own | {path: max(states, key=_RANK.index) for path, states in inside.items()}

    def estimate(self, status: str | None, learnt: Earlier | None = None, now: float | None = None) -> dict:
        """The progress of the run so far. It counts the items that finished in the folder. Without
        `now`, the time is now, or the last event's while the run does not move."""
        self._read_again()
        moving = status in ("running", None)
        if now is None:
            now = _seconds(self.last) if not moving else time.time()
        shape, trace, folder = self.shape, self.trace, self.folder
        open_nodes = self._open(status)
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
            if node in trace.running and info.get("items") is None and node in self.inner:
                inner = self.inner[node].estimate("running" if moving else "paused")
                info["inner"] = {key: inner[key] for key in ("step", "steps", "at", "percent")}
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


def of(run: Run, status: str | None = None) -> dict | None:
    """The run's progress, as the module's docstring says. None if the run's files can't be read,
    for example while it is discarded. The status comes from operate.state(), which knows a
    stopped run."""
    try:
        workflow = spec.load(run.workflow_file)
        status = status or run.status().get("status")
        return estimate(workflow, report.events(run), status, learned(run, workflow, status), run.dir)
    except (spec.SpecError, OSError, KeyError, ValueError):
        return None


def learned(run: Run, workflow: spec.Workflow, status: str | None) -> Earlier | None:
    """What the earlier runs of the workflow that succeeded say, for a run that hasn't succeeded yet."""
    history = earlier(run, workflow.name) if status != "succeeded" else []
    return learn(workflow, history) if history else None


def estimate(workflow: spec.Workflow, events: list[dict], status: str | None, learnt: Earlier | None = None,
             folder: Path | None = None, now: float | None = None) -> dict:
    """The progress of a run of `workflow`, from its events, as Tracker.estimate() gives it."""
    tracker = Tracker(workflow, folder)
    for event in events:
        tracker.add(event)
    return tracker.estimate(status, learnt, now)


def _state(node: str, trace: _Trace, ahead: set[str], moving: bool, cut: bool = False) -> str:
    if node in trace.running:
        return "failed" if cut else "running" if moving else "paused"
    if node in trace.failed:
        return "failed"
    if node in trace.done:  # in this lap: a loop can lead back to it, but it has done its part for now
        return "done"
    if node in ahead:
        return "waiting"
    return "done" if node in trace.ran else "skipped"


_RANK = ["skipped", "done", "waiting", "failed", "paused", "running"]  # a step in several items has the state furthest on


def read_may_pass(error: BaseException) -> bool:
    """Is this an error in reading a file that a later read may not get? A file that is missing,
    or damaged, stays so."""
    cause = error.__cause__ if isinstance(error, spec.SpecError) else error
    return isinstance(cause, OSError) and not isinstance(cause, (FileNotFoundError, NotADirectoryError))


def _key(node: str, index: int | None) -> str:
    return node if index is None else f"{node}[{index}]"


def _outer(key: str) -> str:
    return key.partition("[")[0]


def _items_done(folder: Path, node: str, pass_no: int, items: int) -> int:
    pass_dir = folder / "nodes" / node / str(pass_no)
    return sum((pass_dir / str(index) / "output.json").exists() for index in range(items))


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


def _size(run: Run) -> int:
    """The size of a run's events.jsonl, which grows while it runs, and again in a follow-up."""
    try:
        return (run.dir / "events.jsonl").stat().st_size
    except OSError:
        return 0
