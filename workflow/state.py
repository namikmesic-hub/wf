"""State: what a run's graph remembers, and how the `schedule` node changes it.

RunState is the LangGraph state. LangGraph saves it in a checkpoint after every
step, so a run can stop (a crash, a question for a person, a failure) and
`wf retry` picks up from the last checkpoint. It holds plain data only (dicts,
lists, strings, numbers), so checkpoints are easy to read and can't break when
a class changes.

Reducers decide how a node's update is merged into the state. Almost every key
here has exactly one writer, `schedule`, so the default (the new value
replaces the old one) is right. `inbox` is different: in one step, every
`execute` task returns {"inbox": [its result]}, and operator.add concatenates
those lists instead of keeping only the last one. `schedule` reads the inbox
and empties it by returning Overwrite([]), which bypasses the reducer.

Progress is a working copy of RunState with the operations that `schedule` needs. Its record() takes
in a finished job, and its starts_now() starts the nodes whose needs are settled. Its retry() reruns
what failed, and follow_up() takes in a request from `wf continue`. It uses the rules in scheduler.py, and
evaluates `when` and `for_each` with templates.py. It does no I/O, so it's easy to test without LangGraph.
Progress events go to `emit`. The run's trace (trace.Scope, when given) gets each pass, each route
decision, and each `for_each` and `when` with what it read.
"""

import copy
import operator
from collections.abc import Callable
from typing import Annotated, Any, NotRequired, TypedDict

from . import scheduler, schemas, templates
from .scheduler import START, Topology
from .spec import Workflow


class Job(TypedDict):
    """One unit of work for `execute`: a pass of a node, or one item of a for_each node.
    It's the payload of Send("execute", job), so it carries everything the job needs."""

    node: str
    pass_no: int
    index: int | None  # the item's position, for for_each nodes
    item: Any
    attempt: int  # the number of its first attempt (after `wf retry`, numbering continues)
    vars: dict  # template variables, as they were when the node started
    session: NotRequired[dict | None]  # the session of the node's last pass, or a workflow node's {sessions} of it
    follow_up_section: NotRequired[bool]  # wf adds the follow-up request to the instruction (Progress._adds_request)


class Finished(TypedDict):
    """What `execute` reports back for one job, through the inbox."""

    node: str
    pass_no: int
    index: int | None
    ok: bool
    output: Any
    error: str | None
    attempts: int  # the number of the last attempt made
    workspace: dict | None
    usage: dict  # {"cost_usd": float, "tokens": int}
    stopped: NotRequired[str]  # "limit": a limit stopped the job (a retry didn't start, or a sub-workflow paused)
    session: NotRequired[dict]  # the agent session this pass ended in {harness, model, id, cost_usd, streak}, or {sessions}


class RunState(TypedDict, total=False):
    input: dict  # the workflow's start input (the graph's input)
    run: dict  # {id, dir, base, branch, repo}: fixed for the whole run
    status: str  # running | failed | limit | succeeded
    pending: list[str]  # nodes waiting to start, in the order they became pending
    active: dict[str, dict]  # node -> the pass in progress (see Progress.start)
    passes: dict[str, int]  # node -> how many passes have started
    taken: dict[str, int]  # "node:route index" -> times taken, for `max`
    outputs: dict[str, Any]  # node -> latest output (start -> the input)
    history: dict[str, list]  # node -> every output, oldest first
    errors: dict[str, list]  # node -> failed items, for on_item_error: continue
    workspaces: dict[str, Any]  # node -> {path, branch, base} of its latest pass
    sessions: dict[str, dict]  # node -> the agent session of its latest pass, for session: continue
    usage: dict  # {"cost_usd", "tokens"} so far
    elapsed_s: float  # running time so far, across `wf retry`
    failure: dict | None  # {"node", "error"} when the run stopped on a failure
    limit_reason: str  # which limit stopped the run, in words, for status `limit`
    limit_name: str | None  # ...and its name (limits.py)
    retry: bool  # set when `wf retry` continues a failed run
    output: dict | None  # the end output, once the run succeeded
    follow_up: dict  # the latest request from `wf continue`: {request, number, limits, and pending or from and to}
    inbox: Annotated[list[Finished], operator.add]  # results of the last step's jobs


def _key(index: int | None) -> str:
    """Checkpoint dicts use string keys; a plain node's single job is "-"."""
    return "-" if index is None else str(index)


class Progress:
    """A working copy of RunState that `schedule` changes, then returns as its update."""

    def __init__(self, state: RunState, workflow: Workflow, topo: Topology, emit: Callable[[dict], None],
                 run_usage: dict | None = None, trace=None):
        self.wf, self.topo, self.emit, self.trace = workflow, topo, emit, trace
        self.run_usage = run_usage  # a sub-workflow shows the whole run's spending in `run`
        self.s: dict = copy.deepcopy({k: v for k, v in state.items() if k not in ("inbox", "input")})
        for key, empty in (("pending", []), ("active", {}), ("passes", {}), ("taken", {}), ("outputs", {}),
                           ("history", {}), ("errors", {}), ("workspaces", {}), ("sessions", {})):
            self.s.setdefault(key, empty)
        self.s.setdefault("usage", {"cost_usd": 0.0, "tokens": 0})
        self.s.setdefault("status", "running")
        self.s.setdefault("failure", None)

    def update(self) -> dict:
        """Everything that changed, as a state update."""
        return self.s

    # ---------- reading ----------

    @property
    def stopped(self) -> bool:
        return self.s["failure"] is not None or self.s["status"] != "running"

    def running(self) -> list[str]:
        return [n for n, a in self.s["active"].items() if a["waiting"] > 0]

    def vars(self, elapsed_s: float | None = None) -> dict:
        """The template variables (see "Templates and expressions" in the spec)."""
        s, nodes = self.s, self.wf.nodes
        run = {**s["run"], **(self.run_usage or s["usage"]), "elapsed_s": round(elapsed_s if elapsed_s is not None else s.get("elapsed_s", 0.0), 1)}
        return {
            "start": s["outputs"].get(START, {}),
            **{n: s["outputs"].get(n) for n in nodes},
            "history": {n: s["history"].get(n, []) for n in nodes},
            "workspace": {n: s["workspaces"].get(n) for n in nodes},
            "errors": {n: s["errors"].get(n, []) for n in nodes},
            "run": {k: run.get(k) for k in ("id", "dir", "cost_usd", "tokens", "elapsed_s", "base")},
            "session": {"continued": False}, "follow_up": follow_up_variable(s.get("follow_up")),
        }

    # ---------- changing ----------

    def begin(self, inputs: dict, run: dict) -> None:
        """The first step: `start` finishes at once, with the input as its output."""
        self.s["run"] = run
        self.s["outputs"][START] = schemas.absent_as_none(inputs, schemas.object_schema(self.wf.start.input, self.wf.types))
        self._make_pending(scheduler.triggered(self.topo, START, None))

    def record(self, finished: Finished) -> None:
        """Take in one job's result. When it's the last job of its pass, the node completes."""
        node, active = finished["node"], self.s["active"].get(finished["node"])
        if active is None or active["pass"] != finished["pass_no"]:
            return  # a result for a pass that's no longer in progress
        key = _key(finished["index"])
        active["waiting"] -= 1
        if finished["ok"]:
            active["outputs"][key] = finished["output"]
            active["workspaces"][key] = finished["workspace"]
            if finished.get("session"):
                active.setdefault("sessions", {})[key] = finished["session"]
        else:  # its workspace too: an override's check renders the job's settings with it (overrides.py)
            active["failed"][key] = {"error": finished["error"], "attempts": finished["attempts"],
                                     "stopped": finished.get("stopped"), "workspace": finished["workspace"]}
        usage = finished.get("usage") or {}
        self.s["usage"]["cost_usd"] += usage.get("cost_usd", 0.0)
        self.s["usage"]["tokens"] += usage.get("tokens", 0)
        spent = active.setdefault("usage", {"cost_usd": 0.0, "tokens": 0})  # this pass's, for its events
        spent["cost_usd"] += usage.get("cost_usd", 0.0)
        spent["tokens"] += usage.get("tokens", 0)
        if active["waiting"] == 0:
            self._complete(node)

    def starts_now(self, elapsed_s: float) -> list[Job]:
        """Start every pending node whose needs are settled, and return their jobs."""
        jobs: list[Job] = []
        while not self.stopped:
            ready = scheduler.starts(self.topo, self.s["pending"], self.running())
            if not ready:
                break
            for node in ready:
                jobs += self._start(node, elapsed_s)
                if self.stopped:
                    break
        return jobs

    def stop_for_limit(self, reason: str, name: str | None = None) -> None:
        """A limit was reached: nothing new starts, and the run pauses with status `limit`
        until `wf retry` continues it (retry() then reruns what the limit stopped)."""
        self.s["status"] = "limit"
        self.s["failure"] = None
        self.s["limit_reason"] = reason
        self.s["limit_name"] = name  # cost_usd, tokens or duration: which --limit continues it
        self.emit({"event": "limit", "reason": reason})

    def retry(self) -> list[Job]:
        """`wf retry` on a failed run, or one a limit stopped: rerun what failed or was
        stopped. Finished items are kept."""
        self.s["failure"] = None
        self.s["status"] = "running"
        jobs: list[Job] = []
        for node, active in list(self.s["active"].items()):
            if active["failed"]:
                for key, failed in active["failed"].items():
                    index = None if key == "-" else int(key)
                    item = None if index is None else active["items"][index]
                    jobs.append(self._job(node, active, index, item, attempt=failed["attempts"] + 1))
                active["waiting"] = len(active["failed"])
                active["failed"] = {}
            elif active["waiting"] == 0:
                self._complete(node)  # it failed after its jobs finished, e.g. in a `when`
        return jobs

    def follow_up(self, elapsed_s: float) -> None:
        """Take in the request that `wf continue` gave a run that succeeded: a new pass of the run, from the
        nodes that take the request (scheduler.follow_up_targets, which `to` keeps). Every node they can lead to
        reads none until it runs again. Without `follow_up`, a node certain to have finished before the node that
        takes the request starts (scheduler.Order) keeps its output. So that node finds what its template reads
        without {% if %}, as on its first pass. The others keep their outputs. Routes count from zero again, the
        limits count from `from`, and the next pass of a `session: continue` node continues its last session."""
        targets, given = list(scheduler.follow_up_targets(self.wf, self.topo)), self.s["follow_up"]
        usage = self.s["usage"]
        self.s["follow_up"] = {**{k: v for k, v in given.items() if k != "pending"}, "to": targets,
                               "from": {"cost_usd": usage["cost_usd"], "tokens": usage["tokens"], "elapsed_s": elapsed_s}}
        self.s["status"], self.s["failure"], self.s["output"] = "running", None, None
        self.s["taken"] = {}
        again = self.topo.reachable(targets)
        if self.wf.follow_up is None:
            order = scheduler.Order(self.topo)
            again -= set().union(*(order.before(node) for node in targets))
        for node in again:
            self.s["outputs"].pop(node, None)
            self.s["errors"].pop(node, None)
        _fresh_streaks(self.s["sessions"])  # the two continued passes in a row count from here
        self.emit({"event": "follow_up", "number": given["number"], "to": targets})
        self._make_pending(targets)

    # ---------- inside ----------

    def _make_pending(self, nodes: list[str]) -> None:
        self.s["pending"] = scheduler.add_pending(self.topo, self.s["pending"], nodes)

    def _fail(self, node: str, error: str) -> None:
        self.s["failure"] = {"node": node, "error": error}
        self.emit({"event": "node_failed", "node": node, "error": error, **self._spent(node)})

    def _spent(self, node: str) -> dict:
        """What the node's current pass used, for its finished/failed event (and `wf report`)."""
        active = self.s["active"].get(node) or {}
        return {"pass": active.get("pass"), **active.get("usage", {"cost_usd": 0.0, "tokens": 0})}

    def _start(self, node: str, elapsed_s: float) -> list[Job]:
        self.s["pending"].remove(node)
        pass_no = self.s["passes"].get(node, 0) + 1
        variables = self.vars(elapsed_s)
        active = {"pass": pass_no, "items": None, "vars": variables, "waiting": 0,
                  "outputs": {}, "failed": {}, "workspaces": {}}
        spec_node = self.wf.nodes[node]
        if spec_node.for_each is not None:
            try:
                items = self._for_each(node, pass_no, spec_node.for_each, variables)
            except templates.TemplateError as error:
                return self._refuse(node, f"for_each: {error}")
            if not isinstance(items, (list, tuple)):
                return self._refuse(node, f"for_each must produce a list, not {type(items).__name__}")
            limit = self.wf.setting(node, "max_items")
            if len(items) > limit:
                return self._refuse(node, f"for_each produced {len(items)} items, more than max_items ({limit})")
            active["items"] = list(items)
        self.s["passes"][node] = pass_no
        self.s["active"][node] = active
        count = None if active["items"] is None else len(active["items"])
        if self.trace:
            self.trace.started(node, pass_no, count)
        self.emit({"event": "node_started", "node": node, "pass": pass_no, **({"items": count} if count is not None else {})})
        if active["items"] is None:
            jobs = [self._job(node, active, None, None)]
        else:
            jobs = [self._job(node, active, i, item) for i, item in enumerate(active["items"])]
        active["waiting"] = len(jobs)
        if not jobs:  # an empty list finishes the node at once, with []
            self._complete(node)
        return jobs

    def _for_each(self, node: str, pass_no: int, expression: str, variables: dict):
        if not self.trace:
            return templates.evaluate(expression, variables)
        return self.trace.render({"node": node, "pass_no": pass_no}, "for_each", expression, variables, templates.evaluate)

    def _trace_routes(self, node: str, pass_no: int, routes, taken: dict, checks: list[dict], chosen: int | None) -> None:
        """Record a route decision: every route in order, with what its `when` read and gave
        (checks, in the order choose_route asked), and whether its `max` was used up."""
        if not self.trace:
            return
        job = {"node": node, "pass_no": pass_no}
        asked, decided, stopped = iter(checks), [], False  # stopped: a `when` failed, so no later route was checked
        for index, route in enumerate(routes):
            used_up = scheduler.used_up(route, taken.get(index, 0))
            entry = {"index": index, "to": list(route.targets), "when": route.when, "max": route.max,
                     "taken_before": taken.get(index, 0), "used_up": used_up}
            if used_up or stopped or (chosen is not None and index > chosen):
                entry["checked"] = False
            elif route.when is None:
                entry |= {"checked": True, "result": True}
            else:
                check = next(asked)
                entry |= {"checked": True, **check}
                stopped = "error" in check
                self.trace.rendered(job, f"routes[{index}].when", route.when, check["reads"],
                                    **({"error": check["error"]} if stopped else {"result": check["result"]}))
            decided.append(entry)
        self.trace.routes(job, decided, chosen)

    def _refuse(self, node: str, error: str) -> list[Job]:
        """The node can't start. It stays pending, so `wf retry` tries again."""
        self.s["pending"].insert(0, node)
        self._fail(node, error)
        return []

    def _job(self, node: str, active: dict, index: int | None, item: Any, attempt: int = 1) -> Job:
        job = Job(node=node, pass_no=active["pass"], index=index, item=item, attempt=attempt, vars=active["vars"])
        spec_node = self.wf.nodes[node]
        continues = spec_node.kind == "workflow" or (spec_node.kind == "agent" and self.wf.setting(node, "session") == "continue")
        if continues and spec_node.for_each is None:  # a workflow node passes its sub-workflow's sessions on
            job["session"] = self.s["sessions"].get(node)
        if self._adds_request(node, active):
            job["follow_up_section"] = True
        return job

    def _adds_request(self, node: str, active: dict) -> bool:
        """Does wf add the follow-up request to the instruction of the node's pass? It does on the first pass in
        a follow-up of the node that takes the request in a workflow without `follow_up`, when its instruction
        doesn't read `follow_up` itself."""
        given = self.s.get("follow_up") or {}
        if self.wf.follow_up is not None or node not in given.get("to", ()) or active["vars"].get(node) is not None:
            return False
        try:
            return not any(ref.name == "follow_up" for ref in templates.references(self.wf.nodes[node].instruction))
        except templates.TemplateError:
            return False  # rendering the instruction fails the job and says why

    def _complete(self, node: str) -> None:
        """All of a pass's jobs reported: store its output, then pick what runs next."""
        active = self.s["active"][node]
        failed = active["failed"]
        limited = [f["error"] for f in failed.values() if f.get("stopped") == "limit"]
        if limited:  # a limit stopped a job; the node's results stay in `active` for retry()
            self.s["status"], self.s["failure"], self.s["limit_reason"] = "limit", None, limited[0]
            return  # schedule says which limit (stop_for_limit), once it has counted this step's spending
        if active["items"] is None:
            if failed:
                return self._fail(node, failed["-"]["error"])
            output, workspace, errors = active["outputs"]["-"], active["workspaces"]["-"], []
            if active.get("sessions", {}).get("-"):
                self.s["sessions"][node] = active["sessions"]["-"]
        else:
            if failed and self.wf.setting(node, "on_item_error") == "fail":
                first = min(failed, key=int)
                return self._fail(node, f"item {first}: {failed[first]['error']}")
            done = [str(i) for i in range(len(active["items"])) if str(i) in active["outputs"]]
            output = [active["outputs"][k] for k in done]
            workspace = [active["workspaces"][k] for k in done]
            errors = [{"index": int(k), "item": active["items"][int(k)], "error": f["error"]}
                      for k, f in sorted(failed.items(), key=lambda kv: int(kv[0]))]
            for e in errors:
                self.emit({"event": "item_failed", "node": node, "index": e["index"], "error": e["error"]})

        route, route_event = None, None
        routes = self.topo.routes.get(node)
        if routes:
            # In `when`, the node's own name, history and workspace include the pass it just finished
            base = self.vars()
            variables = {**base, node: output,
                         "history": {**base["history"], node: [*self.s["history"].get(node, []), output]},
                         "workspace": {**base["workspace"], node: workspace},
                         "errors": {**base["errors"], node: errors}}
            taken = {i: self.s["taken"].get(f"{node}:{i}", 0) for i in range(len(routes))}
            checks: list[dict] = []

            def is_true(when: str) -> bool:
                try:
                    value, reads = templates.watched(templates.evaluate, when, variables)
                except templates.TemplateError as error:
                    checks.append({"reads": error.reads, "error": str(error)})
                    raise
                checks.append({"reads": reads, "result": bool(value)})
                return bool(value)

            try:
                route = scheduler.choose_route(routes, taken, is_true)
            except templates.TemplateError as error:
                self._trace_routes(node, active["pass"], routes, taken, checks, None)
                return self._fail(node, f"routes: {error}")
            self._trace_routes(node, active["pass"], routes, taken, checks, route)
            key = f"{node}:{route}"
            self.s["taken"][key] = self.s["taken"].get(key, 0) + 1
            chosen = routes[route]
            route_event = {"event": "route", "node": node, "route": route, "to": list(chosen.targets), "when": chosen.when,
                           "taken": self.s["taken"][key], "max": chosen.max}

        del self.s["active"][node]
        self.s["outputs"][node] = output
        self.s["history"].setdefault(node, []).append(output)
        self.s["workspaces"][node] = workspace
        self.s["errors"][node] = errors
        self.emit({"event": "node_finished", "node": node, "pass": active["pass"],
                   **active.get("usage", {"cost_usd": 0.0, "tokens": 0})})
        if route_event:
            self.emit(route_event)
        self._make_pending(scheduler.triggered(self.topo, node, route))


def follow_up_variable(given: dict | None) -> dict | None:
    """The `follow_up` template variable: the latest request and its number, or none before the first one."""
    return {"request": given["request"], "number": given["number"]} if given else None


def _fresh_streaks(sessions: dict) -> None:
    """Count the continued passes in a row from zero again, also for the sessions of sub-workflows."""
    for session in sessions.values():
        if "sessions" in session:
            _fresh_streaks(session["sessions"])
        else:
            session["streak"] = 0
