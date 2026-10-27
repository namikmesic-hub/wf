"""Report: what runs of a workflow actually did, as evidence for improving it.

    wf report <run-id> ...           one or more runs
    wf report --workflow <name>      every run of that workflow in this folder
    --json                           for an agent to read

It reads only the run folders: status.json, run.json, the copied workflow
file, and events.jsonl, the progress events every run records. So it works on
any run, finished or not, and costs nothing. A run's follow-ups (`wf continue`)
count in it, and the time the run waited for each one doesn't.

Per node (items of a for_each node, and nodes inside sub-workflows, are counted
together under one name, e.g. build[]/implement): how many runs reached it,
passes, attempts and failed attempts with their errors, items and failed
items, tokens, cost and time. Per route, sub-workflows' included under their
node's path: how often it was taken, and in how many runs it used up its `max`
(in any one item), in file order, so a route never taken shows too. `notes` names the facts most worth acting on: loops that ran out, retries,
unavailable harnesses, where the cost went, how runs ended.

The report states facts. Deciding what to change is the job of the
wf-workflows skill (skills/wf-workflows/improving.md).

A Tally counts one run, one event at a time. wf ui keeps one for each run, and
adds what each pass that runs has done so far (Tally.report).
"""

import hashlib
import re
import threading
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from . import spec
from .runs import Run, read_events


def build(runs: list[Run]) -> dict:
    nodes, routes, summaries, failures_at = defaultdict(_empty), {}, [], Counter()
    for run in runs:
        tally = Tally(run, nodes, routes)
        for event in events(run):
            tally.add(event)
        status = run.status()
        if status.get("status") == "failed":
            failures_at[status.get("node", "?")] += 1
        for key in tally.used_up():
            routes[key]["max_reached"] += 1
        summaries.append(tally.summary(status))
    return {"runs": summaries, "nodes": {n: _finish(v) for n, v in nodes.items()},
            "routes": list(routes.values()), "notes": _notes(summaries, nodes, routes, failures_at)}


class Tally:
    """One run's numbers, from its events in turn, as build() counts them. Runs that build() counts
    together share `nodes` and `routes`. wf ui keeps a tally for each run, and adds to it only the
    events that came since its last refresh."""

    def __init__(self, run: Run, nodes: dict | None = None, routes: dict | None = None):
        self.run = run
        self.nodes = defaultdict(_empty) if nodes is None else nodes
        self.routes = {} if routes is None else routes
        _routes_of(run, self.routes)
        self.version = version(run)
        self.first = self.last = None  # the times of the first and the last event
        self.follow_ups, self.waited = 0, 0.0  # `wf continue`: the follow-ups, and the seconds the run waited for them
        self.tokens, self.cost_usd = 0, 0.0
        self.seen: set[str] = set()
        self.started: dict = {}  # (node, pass) -> when the pass started, or `wf retry` ran it again: the passes that run
        self.counted: dict = {}  # (node, pass) -> what it has reported spending
        self.failed_pass: dict = {}  # node -> its pass that failed: `wf retry` reruns it, timed from its next event
        self.taken_here: Counter = Counter()  # by the route's key and the exact place (build[2]/review)
        self.attempting: set[str] = set()  # the nodes without items whose attempt runs now
        self.attempt: dict[tuple, int] = {}  # (node, item) -> the number of its current attempt, when it isn't 1
        self.told: dict[tuple, tuple[int, float]] = {}  # an agent session -> what it last said its attempt has used
        self.spending: dict[str, tuple[int, float]] = {}  # node -> what its pass that runs has used so far, in its sub-workflow too

    def add(self, event: dict) -> None:
        kind, raw = event.get("event"), event.get("node")
        if kind == "override":
            return
        if kind == "follow_up" and self.last:  # the time the run waited for it doesn't count
            self.follow_ups += 1
            self.waited += _seconds(self.last, event["time"])
        if "time" in event:
            self.first, self.last = self.first or event["time"], event["time"]
        name = _name(raw) if isinstance(raw, str) else ""
        if not name:  # Run events have no node.
            return
        node, index, when = self.nodes[name], event.get("index"), event.get("time")
        if kind == "node_started":  # a new pass: not the one that `wf retry` reruns
            self.failed_pass.pop(raw, None)
        if raw in self.failed_pass and kind not in ("node_started", "node_failed"):  # the part of a pass that `wf retry` reruns
            self.started.setdefault((raw, self.failed_pass.pop(raw)), when)
        if kind in ("node_started", "node_finished", "node_failed"):
            self._cut(f"{raw}/", f"{raw}[")
        elif kind in ("attempt_failed", "harness_unavailable", "item_failed") or (
                kind == "attempt_started" and event.get("attempt") != self.attempt.get((raw, index), 1)):
            self._cut(f"{raw}/" if index is None else f"{raw}[{index}]/")  # an attempt with the same number goes on
        if kind == "attempt_started":
            self.attempt[(raw, index)] = event.get("attempt")
        if index is None and "items" not in event:  # an attempt that waits for its agent slot hasn't started
            if kind in ("node_started", "attempt_started", "slot_taken"):
                self.attempting.add(raw)
            elif kind in ("attempt_failed", "harness_unavailable", "node_finished", "node_failed", "slot_waiting"):
                self.attempting.discard(raw)
        if kind == "node_started":
            node["passes"] += 1
            node["items"] += event.get("items", 0)
            if name not in self.seen:
                self.seen.add(name)
                node["runs"] += 1
            place = (raw, event.get("pass"))
            if place in self.counted:  # started again: a sub-workflow attempt that starts over (`wf retry`
                self.counted[place] = (0, 0.0)  # reruns the pass's jobs without a new node_started)
            self.started[place] = when
            self.spending.pop(raw, None)
            self.attempt = {key: number for key, number in self.attempt.items() if key[0] != raw}
        elif kind in ("node_finished", "node_failed"):
            node["successes"] += kind == "node_finished"
            node["final_failures"] += kind == "node_failed"
            # A pass's spending so far: a pass that failed, ran again after `wf retry` and finished reports
            # twice, the second time including the first, so count what's new
            place = (raw, event.get("pass"))
            before = self.counted.get(place, (0, 0.0))
            reported = (event.get("tokens", 0), event.get("cost_usd", 0.0))
            tokens, cost = (now - then if now >= then else now for now, then in zip(reported, before))
            self.counted[place] = reported
            node["tokens"] += tokens
            node["cost_usd"] += cost
            if "/" not in name:  # a sub-workflow's spending is already in its workflow node's
                self.tokens += tokens
                self.cost_usd += cost
            begun = self.started.pop(place, None)
            if begun:
                node["duration_s"] += _seconds(begun, when)
            self.spending.pop(raw, None)
            if kind == "node_failed" and event.get("pass") is not None:  # no pass: the node failed before it started
                self.failed_pass[raw] = event["pass"]
        elif kind == "attempt_failed":
            node["failed_attempts"] += 1
            node["failure_counts"][event.get("error", "")] += 1
        elif kind == "harness_unavailable":
            node["failed_attempts"] += 1
            node["unavailable"][event.get("harness") or "?"] += 1
        elif kind == "item_failed":
            node["failed_items"] += 1
            node["final_failures"] += 1
        elif kind == "session_continued":
            node["sessions"]["continued"] += 1
        elif kind == "session_fresh":
            node["sessions"]["fresh"] += 1
        elif kind == "agent_usage":  # the attempt's numbers so far: add what changed to the node and each one around it
            session = (raw, event.get("pass"), index, event.get("attempt"))
            told, before = (event.get("tokens", 0), event.get("cost_usd", 0.0)), self.told.get(session, (0, 0.0))
            self.told[session] = told
            for place in _places(raw):
                tokens, cost = self.spending.get(place, (0, 0.0))
                self.spending[place] = (tokens + told[0] - before[0], cost + told[1] - before[1])
        elif kind == "route":
            key = _route_key(self.routes, name, event)
            if key:
                self.routes[key]["taken"] += 1
                place = (key, raw)  # the run's own count: a sub-workflow attempt that starts over restarts it
                self.taken_here[place] = max(self.taken_here[place], event.get("taken", self.taken_here[place] + 1))

    def _cut(self, *inside: str) -> None:
        """An attempt, item or pass ends, or starts over: nothing inside it (build[2]/...) runs any more."""
        self.started = {place: begun for place, begun in self.started.items() if not place[0].startswith(inside)}
        self.attempting = {node for node in self.attempting if not node.startswith(inside)}
        self.spending = {node: spent for node, spent in self.spending.items() if not node.startswith(inside)}
        self.told = {session: told for session, told in self.told.items() if not session[0].startswith(inside)}
        self.attempt = {key: number for key, number in self.attempt.items() if not key[0].startswith(inside)}

    def used_up(self) -> set:
        """The routes that this run took as often as their `max` allows, in any one place."""
        return {key for (key, _), count in self.taken_here.items() if self.routes[key]["max"] and count >= self.routes[key]["max"]}

    def summary(self, status: dict, now: float | None = None) -> dict:
        """The run's line of the report. With `now`, its time and spending go on to that time."""
        info = self.run.info()
        found = {
            "run": self.run.id, "workflow": info.get("workflow"), "status": status.get("status"),
            "dry_run": bool(info.get("dry_run")), "version": self.version, "started": self.first, "ended": self.last,
            "duration_s": _seconds(self.first, self.last) - self.waited if self.first else 0, "tokens": self.tokens,
            "cost_usd": round(self.cost_usd, 6), "follow_ups": self.follow_ups,
        }
        if now is not None and self.first:
            found["duration_s"] = round(_since(self.first, now) - self.waited, 1)
            for node, (tokens, cost) in self._spent().items():
                if "/" not in node:  # a sub-workflow's spending is in its workflow node's
                    found["tokens"] += tokens
                    found["cost_usd"] = round(found["cost_usd"] + cost, 6)
        return found

    def report(self, now: float | None = None) -> dict:
        """What build([run]) gives for this run. With `now`, each pass that runs adds what it has done
        until then. That is its attempt that runs, its time, and what its agent sessions have used."""
        status = self.run.status()
        used_up = self.used_up()
        routes = {key: {**route, "max_reached": int(key in used_up)} for key, route in self.routes.items()}
        failures_at = Counter([status.get("node", "?")] if status.get("status") == "failed" else [])
        summary = self.summary(status, now)
        nodes = {name: _finish(node) for name, node in self.nodes.items()}
        if now is not None:
            spent = self._spent()
            for (node, _), begun in self.started.items():
                row = nodes[_name(node)]
                tokens, cost = spent.get(node, (0, 0.0))
                row["attempts"] += node in self.attempting
                row["tokens"] += tokens
                row["cost_usd"] = round(row["cost_usd"] + cost, 6)
                row["duration_s"] = round(row["duration_s"] + _since(begun, now), 1)
        return {"runs": [summary], "nodes": nodes, "routes": list(routes.values()),
                "notes": _notes([summary], self.nodes, routes, failures_at)}

    def _spent(self) -> dict[str, tuple[int, float]]:
        """node -> what its pass that runs has used so far, in its sub-workflow too."""
        running = {node for node, _ in self.started}
        return {node: spent for node, spent in self.spending.items() if node in running}


def version(run: Run) -> str:
    """The workflow files a run used, as a short hash: runs with the same one ran the same files."""
    digest = hashlib.sha256()
    folder = run.dir / "workflow"
    for path in sorted(folder.rglob("*")):
        if path.is_file() and path.name != "manifest.json":
            digest.update(str(path.relative_to(folder)).encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()[:8]


def _empty() -> dict:
    return {"runs": 0, "passes": 0, "successes": 0, "failed_attempts": 0, "final_failures": 0, "failure_counts": Counter(),
            "unavailable": Counter(), "items": 0, "failed_items": 0, "tokens": 0, "cost_usd": 0.0,
            "duration_s": 0.0, "sessions": Counter()}


def _finish(node: dict) -> dict:
    successes = node["successes"] if not node["items"] else max(node["items"] - node["failed_items"], 0)
    return {
        "runs": node["runs"], "passes": node["passes"],
        "attempts": successes + node["failed_attempts"], "failed_attempts": node["failed_attempts"],
        "failures": [{"error": e, "count": c} for e, c in node["failure_counts"].most_common(5)],
        "unavailable": dict(node["unavailable"]),
        "items": node["items"], "failed_items": node["failed_items"],
        "tokens": node["tokens"], "cost_usd": round(node["cost_usd"], 6),
        "duration_s": round(node["duration_s"], 1), "sessions": dict(node["sessions"]),
    }


def _routes_of(run: Run, routes: dict) -> None:
    """Every route of the run's workflow files, in file order (each added once across runs)."""
    try:
        main = run.workflow_file
    except (OSError, KeyError, ValueError):  # ValueError: not JSON, or not UTF-8
        return
    _add_routes(Path(main), "", routes, depth=0)


def _add_routes(path: Path, prefix: str, routes: dict, depth: int) -> None:
    """A workflow's routes, then its sub-workflows' under the calling node's path: a node
    `build` with for_each that runs review.yaml puts review's routes under build[]/."""
    if depth > 20:  # validate rejects cycles; this only guards against a damaged copy
        return
    try:
        workflow = spec.load(path)
    except (spec.SpecError, OSError):
        return
    for node_id, node in workflow.nodes.items():
        name = prefix + node_id
        for index, route in enumerate(node.routes):
            routes.setdefault((name, index), {"from": name, "to": list(route.targets), "when": route.when,
                                              "max": route.max, "taken": 0, "max_reached": 0})
        if node.kind == "workflow":
            inner = f"{name}{'[]' if node.for_each else ''}/"
            _add_routes(path.parent / node.workflow, inner, routes, depth + 1)


def _route_key(routes: dict, name: str, event: dict):
    if (name, event.get("route")) in routes:
        return name, event["route"]
    for key, route in routes.items():  # older runs: no route index in the event
        if key[0] == name and (route["to"], route["when"], route["max"]) == (event.get("to"), event.get("when"), event.get("max")):
            return key
    return None


def _notes(summaries: list[dict], nodes: dict, routes: dict, failures_at: Counter) -> list[str]:
    total = len(summaries)
    runs_word = "run" if total == 1 else "runs"
    notes = []
    for route in routes.values():
        if route["max_reached"]:
            notes.append(f"{route['from']} → {', '.join(route['to'])} reached its max ({route['max']}) "
                         f"in {route['max_reached']} of {total} {runs_word}: the loop ran out before its exit condition held")
    for name, node in nodes.items():
        retried = node["failed_attempts"] - node["final_failures"]  # the others were the last attempt of a failure
        if retried > 0:
            top = node["failure_counts"].most_common(1)
            reason = f"; most often: {top[0][0]}" if top else ""
            notes.append(f"{name} needed a retry: {retried} failed attempt(s) were followed by another; "
                         f"it ran in {node['runs']} of {total} {runs_word}{reason}")
        if node["failed_items"]:
            notes.append(f"{name}: {node['failed_items']} of {node['items']} items failed")
        for harness, count in node["unavailable"].items():
            notes.append(f"{name}: {harness} was unavailable {count} time(s)")
    top = {name: node for name, node in nodes.items() if "/" not in name}  # the run's spending, counted once
    cost = sum(n["cost_usd"] for n in top.values())
    tokens = sum(n["tokens"] for n in top.values())
    for name, node in nodes.items():
        if any(other.startswith((f"{name}/", f"{name}[]/")) for other in nodes):
            continue  # a workflow node: its sub-workflow's nodes say where its spending went
        if cost and node["cost_usd"] / cost > 0.5:
            notes.append(f"{name} spent {node['cost_usd'] / cost:.0%} of the cost")
        elif not cost and tokens and node["tokens"] / tokens > 0.5:
            notes.append(f"{name} used {node['tokens'] / tokens:.0%} of the tokens")
    versions = Counter(s["version"] for s in summaries)
    if len(versions) > 1:
        notes.append(f"these runs used {len(versions)} versions of the workflow files ("
                     + ", ".join(f"{v}: {n} run(s)" for v, n in versions.most_common())
                     + "): compare like with like, e.g. --version " + summaries[-1]["version"])
    dry = sum(s["dry_run"] for s in summaries)
    if dry and dry < total:
        notes.append(f"{dry} of {total} runs were dry runs: their failures and spending were scripted (--real leaves them out)")
    ended = Counter(s["status"] for s in summaries)
    if total > 1 or ended.get("failed"):
        notes.append("runs ended: " + ", ".join(f"{n} {s}" for s, n in ended.most_common()))
    for node, count in failures_at.most_common(3):
        notes.append(f"{count} run(s) failed at {node}")
    return notes


def text(found: dict) -> str:
    """The report as a table, for people."""
    lines = [f"{len(found['runs'])} run(s): " + ", ".join(
        f"{r['run']} {r['status']}{' (dry run)' if r['dry_run'] else ''} v{r['version']}" for r in found["runs"])]
    header = f"{'node':<28} {'runs':>4} {'passes':>6} {'attempts':>8} {'failed':>6} {'items':>5} {'tokens':>9} {'cost $':>8} {'time s':>7}"
    lines += ["", header, "-" * len(header)]
    for name, node in found["nodes"].items():
        lines.append(f"{name[:28]:<28} {node['runs']:>4} {node['passes']:>6} {node['attempts']:>8} {node['failed_attempts']:>6} "
                     f"{node['items']:>5} {node['tokens']:>9} {node['cost_usd']:>8.2f} {node['duration_s']:>7.0f}")
    if found["routes"]:
        lines += ["", "routes:"]
        for route in found["routes"]:
            limit = f", max {route['max']} (used up in {route['max_reached']} run(s))" if route["max"] else ""
            when = f" when {route['when']}" if route["when"] else ""
            lines.append(f"  {route['from']} → {', '.join(route['to'])}{when}: taken {route['taken']}{limit}")
    if found["notes"]:
        lines += ["", "notes:"] + [f"  - {note}" for note in found["notes"]]
    return "\n".join(lines)


_READ: dict[Path, tuple[int, int, list[dict]]] = {}  # events.jsonl -> (its inode, the bytes read, their events)
_READ_MOST = 64
_READING = threading.Lock()  # wf ui asks from several threads


def events(run: Run) -> list[dict]:
    """The run's events.jsonl, one dict per line (a damaged line is left out). The file only grows,
    so a later call reads only the lines added since the last one: wf ui asks every few seconds."""
    path = run.dir / "events.jsonl"
    try:
        stat = path.stat()
    except OSError:  # no events yet, or the run is being discarded
        return []
    with _READING:
        inode, offset, found = _READ.get(path, (None, 0, []))
    if inode != stat.st_ino or stat.st_size < offset:
        offset, found = 0, []
    new, offset = read_events(path, offset)
    found = found + new
    with _READING:
        _READ.pop(path, None)
        _READ[path] = (stat.st_ino, offset, found)
        while len(_READ) > _READ_MOST:
            del _READ[next(iter(_READ))]
    return list(found)


def _name(node: str) -> str:
    """build[2]/implement -> build[]/implement: items are counted together."""
    return re.sub(r"\[\d+\]", "[]", node)


def _places(node: str) -> list[str]:
    """build[2]/implement -> build, build[2]/implement: each workflow node that the node runs inside, and the node."""
    parts = node.split("/")
    return ["/".join(parts[:k] + [re.sub(r"\[\d+\]$", "", parts[k])]) for k in range(len(parts) - 1)] + [node]


def _since(start: str, now: float) -> float:
    try:
        return max(now - datetime.fromisoformat(start).timestamp(), 0.0)
    except (TypeError, ValueError):
        return 0.0


def _seconds(start: str, end: str) -> float:
    try:
        return (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()
    except (TypeError, ValueError):
        return 0.0
