"""Report: what runs of a workflow actually did, as evidence for improving it.

    wf report <run-id> ...           one or more runs
    wf report --workflow <name>      every run of that workflow in this folder
    --json                           for an agent to read

It reads only the run folders: status.json, run.json, the copied workflow
file, and events.jsonl, the progress events every run records. So it works on
any run, finished or not, and costs nothing.

Per node (items of a for_each node, and nodes inside sub-workflows, are counted
together under one name, e.g. build[]/implement): how many runs reached it,
passes, attempts and failed attempts with their errors, items and failed
items, tokens, cost and time. Per route, sub-workflows' included under their
node's path: how often it was taken, and in how many runs it used up its `max`
(in any one item), in file order, so a route never taken shows too. `notes` names the facts most worth acting on: loops that ran out, retries,
unavailable harnesses, where the cost went, how runs ended.

The report states facts. Deciding what to change is the job of the
wf-workflows skill (skills/wf-workflows/improving.md).
"""

import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from . import spec
from .runs import Run


def build(runs: list[Run]) -> dict:
    summaries, nodes, routes, failures_at = [], defaultdict(_empty), {}, Counter()
    for run in runs:
        events = _events(run)
        status = run.status()
        summary = {
            "run": run.id, "workflow": run.info().get("workflow"), "status": status.get("status"),
            "dry_run": bool(run.info().get("dry_run")), "version": version(run),
            "started": events[0]["time"] if events else None, "ended": events[-1]["time"] if events else None,
            "duration_s": _seconds(events[0]["time"], events[-1]["time"]) if events else 0,
            "tokens": 0, "cost_usd": 0.0,
        }
        if status.get("status") == "failed":
            failures_at[status.get("node", "?")] += 1
        _routes_of(run, routes)
        seen, started, counted = set(), {}, {}  # counted: what each (node, pass) has reported spending
        failed_pass: dict = {}  # node -> its pass that failed: `wf retry` reruns it, timed from its next event
        taken_here: Counter = Counter()  # by the route's key and the exact place (build[2]/review)
        for event in events:
            kind, name = event.get("event"), _name(event.get("node") or "")
            if not name:  # run-level events (limit, warnings) belong to no node
                continue
            node = nodes[name]
            raw = event.get("node")
            if raw in failed_pass and kind not in ("node_started", "node_failed"):  # the part of a pass that `wf retry` reruns
                started.setdefault((raw, failed_pass.pop(raw)), event["time"])
            if kind == "node_started":
                node["passes"] += 1
                node["items"] += event.get("items", 0)
                if name not in seen:
                    seen.add(name)
                    node["runs"] += 1
                place = (event.get("node"), event.get("pass"))
                if place in counted:  # started again: a sub-workflow attempt that starts over (`wf retry`
                    counted[place] = (0, 0.0)  # reruns the pass's jobs without a new node_started)
                started[place] = event["time"]
            elif kind in ("node_finished", "node_failed"):
                node["successes"] += kind == "node_finished"
                node["final_failures"] += kind == "node_failed"
                # A pass's spending so far: a pass that failed, ran again after `wf retry` and finished reports
                # twice, the second time including the first, so count what's new
                place = (event.get("node"), event.get("pass"))
                before = counted.get(place, (0, 0.0))
                reported = (event.get("tokens", 0), event.get("cost_usd", 0.0))
                tokens, cost = (now - then if now >= then else now for now, then in zip(reported, before))
                counted[place] = reported
                node["tokens"] += tokens
                node["cost_usd"] += cost
                if "/" not in name:  # a sub-workflow's spending is already in its workflow node's
                    summary["tokens"] += tokens
                    summary["cost_usd"] += cost
                begun = started.pop((event.get("node"), event.get("pass")), None)
                if begun:
                    node["duration_s"] += _seconds(begun, event["time"])
                if kind == "node_failed":
                    failed_pass[raw] = event.get("pass")
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
            elif kind == "route":
                key = _route_key(routes, name, event)
                if key:
                    routes[key]["taken"] += 1
                    place = (key, event["node"])  # the run's own count: a sub-workflow attempt that starts over restarts it
                    taken_here[place] = max(taken_here[place], event.get("taken", taken_here[place] + 1))
        used_up = {key for (key, _), count in taken_here.items() if routes[key]["max"] and count >= routes[key]["max"]}
        for key in used_up:
            routes[key]["max_reached"] += 1
        summary["cost_usd"] = round(summary["cost_usd"], 6)
        summaries.append(summary)
    return {"runs": summaries, "nodes": {n: _finish(v) for n, v in nodes.items()},
            "routes": list(routes.values()), "notes": _notes(summaries, nodes, routes, failures_at)}


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


def _events(run: Run) -> list[dict]:
    path = run.dir / "events.jsonl"
    if not path.exists():
        return []
    found = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            found.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return found


def _name(node: str) -> str:
    """build[2]/implement -> build[]/implement: items are counted together."""
    return re.sub(r"\[\d+\]", "[]", node)


def _seconds(start: str, end: str) -> float:
    try:
        return (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()
    except (TypeError, ValueError):
        return 0.0
