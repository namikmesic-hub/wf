"""Trace: an append-only record of what a run read, decided and did, in trace.jsonl.

events.jsonl tells how a run went, for progress lines and `wf report`. The trace
keeps what you need to debug a run or review it later, one JSON object per line.
It is in the top-level run folder, and sub-workflows write to it too:

    kind           written when                    holds
    run            a process starts driving it     started or retried, the workflow, the input
    pass           a node starts a pass            for a for_each node, how many items
    job            a pass or an item starts        the item, the number of its first attempt
    render         a template or expression ran    the field, its template, each variable path it
                                                   read with its value, and the result or error
    routes         a node chose where to go        each route in order: its `when`, what it read,
                                                   its result, if its `max` was used up. Then the
                                                   route taken
    attempt        an attempt starts               harness, model, effort, access, workspace, and
                                                   how its session starts (fresh, fork or load)
    turn           a message goes to the agent     `sends` (instruction, reminder or steer), the full text
    message        the agent writes text           the text
    thinking       a block of thinking ends        how long it took (none: the turn ended first), and
                                                   its text. Only Cursor's SDK reports it
    request        the runtime names a request     its id. Only Cursor's SDK reports it
    tool_call      the agent calls a tool          the tool and its arguments
    tool_result    the tool answers                the answer, and if it is an error
    submit_result  the agent calls submit_result   the arguments, and the problems or "accepted"
    turn_end       the agent's turn ends           how it ended, and what it used: the tokens, the
                                                   cost, and the parts of the tokens (harnesses.Usage)
    attempt_end    the attempt ends                all of `attempt`, the session id, start, end,
                                                   how it ended and what it used, as in turn_end.
                                                   For a command: its text, env, exit code, and
                                                   the files that hold its stdout and stderr
    job_end        the job ends                    its output or error (or that it waits for a person)
    run_stopped    the process stops driving it    the run's status, and its output once it succeeds

Every record has `time`, `kind` and `id`. A record about a node also has `node`,
`pass`, `index` (for an item), `where` (inside a sub-workflow, like `build[1]/`),
`attempt` and `turn`. Ids nest, and `parent` names the id that holds a record:

    run                        the run
    review#2                   pass 2 of review (also its render, routes and job records)
    build#1[3]                 item 3 of build's pass 1
    review#2.a1                attempt 1 of that pass
    review#2.a1.t2             its turn 2: the first reminder
    review#2.a1.t2.c4          the 4th tool call of that turn (.s1 a submit_result call, .m1 a message,
                               .k1 a block of thinking, .r1 a request)
    end                        the end outputs
    build#1[3].a1/implement#2  a pass in the sub-workflow that attempt 1 of build[3] runs

A value longer than CAP characters goes to blobs/<sha256>.txt (text) or .json
(any other value). The trace writes that file once, however often the value
occurs. The record then holds {"$blob": "blobs/<sha256>.txt", "size": ..., "start": ...}.

Secrets never go into the trace. A command node's `secret_env` names env values
that are secret. The function find() finds the variable paths those values come
from. These are what their templates read, also through `item` to what the
for_each list reads, and through `results.<node>` to the node's output and items.
They go through a sub-workflow's input to the parent's
`with`, and through a workflow node's output to the sub-workflow's end output.
An `item` path counts only in the templates of its own node. A value at such a
path becomes [secret] in each record that has it there: a read, the input, an
output, a submit_result call. So does a secret env value, a `with` value that
feeds one, and an end output that feeds one. Then the text of each such value,
however short, goes out of every later record of the process. A wf tool's token
never gets in (relay.py calls keep_out()). Nor does what a wf tool answers: its
tool_result holds only the size, as the attempt's transcript does.

Writing the trace never stops a run. When wf can't write it, wf says so once,
and the run continues.
"""

import hashlib
import json
import os
import sys
import threading
import time
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from . import output_tool, templates
from .schemas import EACH

FILE = "trace.jsonl"
BLOBS = "blobs"
CAP = 1000  # characters a value may have in a record. A longer one goes to blobs/
PREVIEW = 80  # characters of a blob shown in its reference
HIDDEN = "[secret]"
_LONGEST = 32  # steps of a secret path. A longer one, from lists that read each other's results, is cut: that hides more
_OWN = frozenset({"kind", "id", "parent", "node", "where", "pass", "index", "attempt", "turn", "field", "started", "ended"})

_everywhere: set[str] = set()  # secrets of this process, such as wf tools' tokens


def keep_out(text: str | None) -> None:
    """Keep `text` out of every trace this process writes."""
    if isinstance(text, str) and text:
        _everywhere.add(text)


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


class Trace:
    """A run's trace.jsonl and blobs/. One per process that drives the run."""

    def __init__(self, folder: Path):
        self.folder = Path(folder)
        self.hidden: set[str] = set()  # secret text found so far
        self._lock = threading.Lock()
        self._failed = False

    def hide(self, value: Any) -> None:
        """Remove the text of `value`, and of everything in it, from every later record."""
        if isinstance(value, Mapping):
            for inner in value.values():
                self.hide(inner)
        elif isinstance(value, (list, tuple)):
            for inner in value:
                self.hide(inner)
        elif isinstance(value, (str, int, float)) and not isinstance(value, bool) and str(value):
            self.hidden.add(str(value))

    def write(self, record: dict) -> None:
        try:
            with self._lock:
                ready = self._ready(record)
                try:
                    line = (json.dumps(ready, ensure_ascii=False, default=str) + "\n").encode("utf-8")
                except UnicodeEncodeError:  # text with a lone surrogate, which UTF-8 can't hold: escape all of it
                    line = (json.dumps(ready, default=str) + "\n").encode("ascii")
                with (self.folder / FILE).open("ab") as file:
                    file.write(line)
        except (OSError, ValueError, TypeError) as error:
            if not self._failed:
                self._failed = True
                print(f"wf: can't write the run's trace ({error}). The run continues without it.", file=sys.stderr)

    def _ready(self, record: dict) -> dict:
        secrets = sorted(self.hidden | _everywhere, key=len, reverse=True)
        line = {"time": _now()}
        for key, value in record.items():
            if key in _OWN:  # what wf writes itself, which holds no secret
                line[key] = value
            elif key == "reads":
                line[key] = [self._read(read, secrets) for read in value]
            elif key == "routes":
                line[key] = [{**_scrub({k: v for k, v in route.items() if k != "reads"}, secrets),
                              "reads": [self._read(read, secrets) for read in route.get("reads", [])]} for route in value]
            else:
                line[key] = self._stored(_scrub(value, secrets))
        return line

    def _read(self, read: dict, secrets: list[str]) -> dict:
        return {"path": read["path"], "value": self._stored(_scrub(read["value"], secrets))}

    def _stored(self, value: Any) -> Any:
        """The value, or a reference to the blob that holds it if it's long."""
        data, suffix = (value, ".txt") if isinstance(value, str) else (json.dumps(value, ensure_ascii=False, default=str), ".json")
        if len(data) <= CAP and not stored(value):  # a value that looks like a reference goes to a blob too
            return value
        encoded = data.encode("utf-8", "backslashreplace")  # a lone surrogate becomes its \\u escape
        name = f"{BLOBS}/{hashlib.sha256(encoded).hexdigest()}{suffix}"
        path = self.folder / name
        if not path.exists():
            path.parent.mkdir(exist_ok=True)
            temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
            temp.write_bytes(encoded)
            temp.replace(path)
        return {"$blob": name, "size": len(data), "start": data[:PREVIEW]}


def stored(value: Any) -> bool:
    """Is `value` a reference to a blob, as a record holds it?"""
    return isinstance(value, Mapping) and set(value) == {"$blob", "size", "start"}


def _scrub(value: Any, secrets: list[str]) -> Any:
    if not secrets:
        return value
    if isinstance(value, str):
        for secret in secrets:
            if secret in value:
                value = value.replace(secret, HIDDEN)
        return value
    if isinstance(value, Mapping):
        return {key: _scrub(inner, secrets) for key, inner in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub(inner, secrets) for inner in value]
    if isinstance(value, (int, float)) and not isinstance(value, bool) and str(value) in secrets:
        return HIDDEN
    return value


# ---------- what is secret ----------


@dataclass(frozen=True)
class Secrets:
    """Where a workflow's secret values are. The paths are variable paths anywhere in the
    workflow. The items are, for each for_each node, the paths under its `item`. Only that
    node's templates read those. A path is a tuple of names and indexes, with EACH for any
    element."""

    paths: frozenset[tuple] = frozenset()
    items: Mapping[str, frozenset[tuple]] = field(default_factory=dict)

    def at(self, node: str | None) -> frozenset[tuple]:
        """The secret paths in the templates of `node` (None: of no node, such as the input)."""
        return self.paths | self.items.get(node, frozenset()) if node else self.paths


def find(workflow, load: Callable[[Path], Any], outputs: frozenset[str] = frozenset(), seen: tuple = ()) -> Secrets:
    """The variable paths whose values become an env value that a command node marks secret.
    These are what the env templates read, through `item` to what the for_each list reads, and
    through `results.<node>` to the node's output and items.
    They go through a sub-workflow's input to what the parent's `with` reads. They also go
    through a workflow node's output to what the sub-workflow's end output reads. The set
    `outputs` names the end outputs that feed a secret in the parent workflow. The function
    `load` reads a sub-workflow file."""
    found: set[tuple] = set()
    items: dict[str, set[tuple]] = {}

    def add(node_id: str | None, source: str) -> None:
        anywhere, inside = _sources(workflow, node_id, source)
        found.update(anywhere)
        for node, paths in inside.items():
            items.setdefault(node, set()).update(paths)

    for node_id, node in workflow.nodes.items():
        if node.kind == "command":
            env = workflow.setting(node_id, "env")
            for name in workflow.setting(node_id, "secret_env"):
                if name in env:
                    add(node_id, env[name])
    for name in outputs:
        value = workflow.end.output.get(name)
        source = value.get("value") if isinstance(value, dict) else value
        if isinstance(source, str):
            add(None, source)
    subs = []
    for node_id, node in workflow.nodes.items():
        if node.kind == "workflow" and workflow.path is not None:
            path = (Path(workflow.path).parent / node.workflow).resolve()
            if path not in seen:
                subs.append((node_id, node, path, load(path)))
    while subs:  # what one `with` reads can reach another workflow node's output: go on until nothing new
        size = len(found) + sum(map(len, items.values()))
        for node_id, node, path, sub in subs:
            inputs = secret_inputs(sub, load, outputs_reached(node_id, node, found, sub), (*seen, path))
            for name, value in node.with_.items():
                if isinstance(value, str) and name in inputs:
                    add(node_id, value)
        if len(found) + sum(map(len, items.values())) == size:
            break
    found |= {("history", path[0], EACH, *path[1:]) for path in found if path[0] in workflow.nodes}
    found |= {("results", path[0], EACH, "output", *path[2:]) for path in found
              if path[0] in workflow.nodes and workflow.nodes[path[0]].for_each is not None}
    found |= {(where, node, EACH, "item", *path[1:]) for node, paths in items.items() for path in paths
              if path[0] == "item" for where in ("results", "errors")}  # a new process learns an item's text only from these
    return Secrets(frozenset(found), {node: frozenset(paths) for node, paths in items.items()})


def secret_inputs(workflow, load: Callable[[Path], Any], outputs: frozenset[str] = frozenset(), seen: tuple = ()) -> set[str]:
    """The start inputs of `workflow` that feed a secret env value, or one of its `outputs`."""
    paths = find(workflow, load, outputs, seen).paths
    if ("start",) in paths:
        return set(workflow.start.input)
    return {path[1] for path in paths if len(path) > 1 and path[0] == "start" and isinstance(path[1], str)}


def outputs_reached(node_id: str, node, paths, sub) -> frozenset[str]:
    """The end outputs of `sub`, which workflow node `node_id` runs, that a secret path reaches."""
    every = frozenset(sub.end.output)
    reached = set()
    for path in paths:
        if path[0] == "history":
            if len(path) == 1 or path[1] is EACH:  # all of history, or any node's
                return every
            if path[1] != node_id:
                continue
            if len(path) == 2:  # every output this node has given
                return every
            rest = path[3:]  # inside one of them
        elif path[0] == "results":
            if len(path) == 1 or path[1] is EACH:
                return every
            if path[1] != node_id:
                continue
            if len(path) < 4 or path[3] is EACH:  # all of an item's entry
                return every
            if path[3] != "output":
                continue
            rest = (path[2], *path[4:])  # as in the node's own list: an item, then inside its output
        elif path[0] == node_id:
            rest = path[1:]
        else:
            continue
        if node.for_each is not None:  # the node's output is a list of the sub-workflow's outputs
            if not rest:
                return every
            rest = rest[1:]
        if not rest or rest[0] is EACH:
            return every
        if rest[0] in every:
            reached.add(rest[0])
    return frozenset(reached)


def _sources(workflow, node_id: str | None, source: str) -> tuple[set[tuple], dict[str, set[tuple]]]:
    """The paths a template of `node_id` reads: (anywhere in the workflow, under the `item` of each
    node). An item path also counts at what that node's for_each list reads (_listed). A path in
    `results.<node>` also counts at the node's output, or at its `item` (_entry_sources). Each path
    found so counts in turn, as when a for_each list is `results.<other>`."""
    anywhere, inside = set(), {}
    todo = [(node_id, path) for path in _read_paths(source)]
    while todo:
        owner, path = todo.pop()
        path = path[:_LONGEST]
        if path[0] in ("item", "index"):
            if owner is None or not workflow.nodes[owner].for_each or path in inside.get(owner, ()):
                continue  # only a for_each node has them (validate makes sure)
            inside.setdefault(owner, set()).add(path)
            todo += [(None, listed) for listed in _listed(workflow, owner, path)]
        elif path not in anywhere:
            anywhere.add(path)
            if path[0] == "results":
                todo += _entry_sources(workflow, path)
    return anywhere, inside


def _entry_sources(workflow, path: tuple) -> list[tuple[str | None, tuple]]:
    """Where a path in `results.<node>` takes its value: (None, a path in the node's output), or
    (the node, a path in its `item`)."""
    named = path[1] if len(path) > 1 and path[1] is not EACH else None  # None: every node's results
    field = path[3] if len(path) > 3 else EACH  # EACH: all of an entry
    found = []
    for node in [n for n, spec in workflow.nodes.items() if spec.for_each is not None and named in (None, n)]:
        if field in ("output", EACH):
            found.append((None, (node, EACH, *path[4:])))  # an entry's position isn't its place in the node's list
        if field in ("item", EACH):
            found.append((node, ("item", *path[4:])))
    return found


def _listed(workflow, node_id: str, path: tuple) -> set[tuple]:
    """What an `item` path of a for_each node reads: the element of its list when the list is a
    plain path, or else all that the for_each expression reads. An `index` reads nothing."""
    if path[0] == "index":
        return set()
    each = workflow.nodes[node_id].for_each
    listed = templates.pure_path(each, expression=True)
    if listed:
        return {_general((listed[0], *listed[1], EACH, *path[1:]))}
    return _read_paths(each, expression=True)


def _read_paths(source: str, expression: bool = False) -> set[tuple]:
    try:
        refs = templates.references(source, expression=expression)
    except templates.TemplateError:  # validate refuses it, and the run fails when it renders
        return set()
    return {_general((ref.name, *(ref.path or ()))) for ref in refs}


def _general(path: tuple) -> tuple:
    """A negative index, as in x[-1], counts as any element: the list's length isn't known yet. So does
    a position that the template computes, as in x[index]."""
    return tuple(EACH if isinstance(step, templates.At) or (type(step) is int and step < 0) else step for step in path)


def _matches(pattern: tuple, path: tuple) -> bool:
    return len(pattern) == len(path) and all(p is EACH or p == q for p, q in zip(pattern, path))


# ---------- writing ----------


def place(job: Mapping) -> dict:
    """Where a job is: node, pass and (for an item) index."""
    found = {"node": job["node"], "pass": job["pass_no"]}
    if job.get("index") is not None:
        found["index"] = job["index"]
    return found


class Scope:
    """The trace as one workflow writes it: the run's own, or a sub-workflow's. A sub-workflow's
    ids start with the id of the attempt that runs it (`parent`). Its `where` is the
    sub-workflow's path, like build[1]/."""

    def __init__(self, trace: Trace, workflow, load: Callable[[Path], Any], parent: str = "run", where: str = "",
                 outputs: frozenset[str] = frozenset()):
        self.trace, self.parent, self.where = trace, parent, where
        self.prefix = "" if parent == "run" else f"{parent}/"
        self.workflow, self.load = workflow, load
        self.outputs = outputs  # end outputs that feed a secret of the parent workflow
        self.secrets = find(workflow, load, outputs)

    def write(self, kind: str, id: str, parent: str | None, **fields) -> None:
        self.trace.write({"kind": kind, "id": id, "parent": parent, **({"where": self.where} if self.where else {}), **fields})

    def owner(self, job: Mapping | None) -> str:
        """The id of a pass or item (a job), or with None, of the end outputs."""
        if job is None:
            return f"{self.prefix}end"
        index = job.get("index")
        return f"{self.prefix}{job['node']}#{job['pass_no']}" + (f"[{index}]" if index is not None else "")

    def output_path(self, job: Mapping) -> tuple:
        """The variable path a job's output has: the node's, or one element of it for an item."""
        return (job["node"],) if job.get("index") is None else (job["node"], job["index"])

    # ----- secrets -----

    def at(self, path: tuple, value: Any, node: str | None = None) -> Any:
        """`value`, found at the variable path `path` in a template of `node`, with every secret
        part of it [secret]."""
        secrets = self.secrets.at(node)
        if any(len(secret) <= len(path) and _matches(secret, path[:len(secret)]) for secret in secrets):
            self.trace.hide(value)
            return HIDDEN
        inner = [secret[len(path):] for secret in secrets if len(secret) > len(path) and _matches(secret[:len(path)], path)]
        return self._inside(value, inner) if inner else value

    def _inside(self, value: Any, rests: list[tuple]) -> Any:
        if isinstance(value, Mapping):
            items, made = value.items(), {}
        elif isinstance(value, (list, tuple)):
            items, made = enumerate(value), []
        else:
            return value
        for key, child in items:
            here = [rest[1:] for rest in rests if rest[0] is EACH or rest[0] == key]
            if any(not rest for rest in here):
                self.trace.hide(child)
                child = HIDDEN
            elif here:
                child = self._inside(child, here)
            if isinstance(made, dict):
                made[key] = child
            else:
                made.append(child)
        return made

    def know(self, variables: Callable[[], Mapping]) -> None:
        """Note the secret values that the run's variables hold now. Then a later record that
        holds one somewhere else has it removed. A run that `wf retry` continues learns them
        again here."""
        if self.secrets.paths:
            values = variables()
            for path in self.secrets.paths:
                for found in _walk(values, path):
                    self.trace.hide(found)

    def sub_secrets(self, node_id: str, sub) -> tuple[set[str], frozenset[str]]:
        """For workflow node `node_id`, which runs `sub`: the inputs of `sub` that feed a secret,
        and its end outputs that feed one here."""
        outputs = outputs_reached(node_id, self.workflow.nodes[node_id], self.secrets.paths, sub)
        return secret_inputs(sub, self.load, outputs), outputs

    # ----- records -----

    def run(self, action: str, **fields) -> None:
        if "inputs" in fields:
            fields["inputs"] = self.at(("start",), fields["inputs"])
        self.write("run", "run", None, action=action, **fields)

    def stopped(self, status: dict, output: Any = None) -> None:
        details = {k: v for k, v in status.items() if k not in ("seq", "run", "updated")}
        self.write("run_stopped", "run", None, **details, **({"output": output} if output is not None else {}))

    def started(self, node: str, pass_no: int, items: int | None) -> None:
        self.write("pass", self.owner({"node": node, "pass_no": pass_no}), self.parent, node=node, **{"pass": pass_no},
                   **({"items": items} if items is not None else {}))

    def job(self, job: Mapping) -> None:
        item = {"item": self.at(("item",), job["item"], job["node"])} if job.get("index") is not None else {}
        self.write("job", self.owner(job), self._job_parent(job), **place(job), first_attempt=job["attempt"], **item)

    def job_end(self, job: Mapping, finished: Mapping | None) -> None:
        """How a job ended: its Finished record, or None when it waits for a person."""
        if finished is None:
            fields = {"ok": None, "waiting": True}
        else:
            fields = {"ok": finished["ok"], "attempts": finished["attempts"], "usage": finished["usage"],
                      "workspace": finished["workspace"]}
            if finished["ok"]:
                fields["output"] = self.at(self.output_path(job), finished["output"], job["node"])
            else:
                fields["error"] = finished["error"]
            if finished.get("stopped"):
                fields["stopped"] = finished["stopped"]
        self.write("job_end", self.owner(job), self._job_parent(job), **place(job), **fields)

    def _job_parent(self, job: Mapping) -> str:
        return self.owner({**job, "index": None}) if job.get("index") is not None else self.parent

    def render(self, job: Mapping | None, field: str, source: str, variables: Mapping,
               call: Callable = templates.render, *, secret: bool = False, **more) -> Any:
        """call(source, variables), recorded: a render record for a job's field (None: an end output).
        A `secret` result is written as [secret], and its text kept out of later records."""
        try:
            value, reads = templates.watched(call, source, variables)
        except templates.TemplateError as error:
            self.rendered(job, field, source, error.reads, error=str(error), **more)
            raise
        if secret:
            self.trace.hide(value)
        self.rendered(job, field, source, reads, result=HIDDEN if secret else value, **more)
        return value

    def rendered(self, job: Mapping | None, field: str, source: str, reads: list, **fields) -> None:
        where = place(job) if job is not None else {}
        self.write("render", self.owner(job), self._job_parent(job) if job else self.parent, **where, field=field,
                   template=source, reads=self.reads(reads, job["node"] if job else None), **fields)

    def reads(self, reads: list, node: str | None) -> list[dict]:
        return [{"path": templates.path_text(read.path), "value": self.at(read.path, read.value, node)} for read in reads]

    def routes(self, job: Mapping, routes: list[dict], taken: int | None) -> None:
        """A node's route decision: `routes` as they were checked (see state.Progress), and the
        index of the one taken (None: a `when` failed)."""
        checked = [{**route, "reads": self.reads(route.get("reads", []), job["node"])} for route in routes]
        chosen = {"taken": taken, "to": checked[taken]["to"]} if taken is not None else {"taken": None}
        self.write("routes", self.owner(job), self.parent, **place(job), routes=checked, **chosen)

    def attempt(self, job: Mapping, number: int, harness: str | None, **facts) -> "Attempt":
        return Attempt(self, job, number, harness, facts)


class Attempt:
    """One attempt's records: its start and end, its turns, and what the agent did in them.
    An agent attempt hands it to the harness as SessionSpec.trace, and its submit_result
    reports each call to submitted()."""

    def __init__(self, scope: Scope, job: Mapping, number: int, harness: str | None, facts: dict):
        self.scope, self.job = scope, job
        self.parent = scope.owner(job)
        self.id = f"{self.parent}.a{number}"
        self.place = {**place(job), "attempt": number}
        self.facts = {**({"harness": harness} if harness else {}), **facts}
        self.started, self._clock = _now(), time.monotonic()
        self._turn = 0
        self._calls: dict[str, str] = {}  # the harness's call id -> the trace's
        self._counts: Counter = Counter()

    def begin(self, **facts) -> None:
        """The attempt starts, with these facts (harness, model, ...), which its end repeats."""
        self.facts.update(facts)
        self.scope.write("attempt", self.id, self.parent, **self.place, **self.facts, started=self.started)

    def end(self, outcome: str, error: str | None = None) -> None:
        """outcome: succeeded, failed, timed_out, unavailable, limit, waiting or cancelled."""
        self.scope.write("attempt_end", self.id, self.parent, **self.place, **self.facts, started=self.started,
                         ended=_now(), duration_s=round(time.monotonic() - self._clock, 1), outcome=outcome,
                         **({"error": error} if error else {}))

    async def turn(self, number: int, sends: str, text: str, sending) -> Any:
        """Await `sending`, the harness's turn with `text`, between a turn and a turn_end record.
        `sends` is what the text is: the instruction, a reminder, or a `wf steer` message (steer)."""
        self._turn = number
        turn = f"{self.id}.t{number}"
        self._write("turn", turn, self.id, sends=sends, text=text)
        try:
            end = await sending
        except BaseException as error:
            ended = "cancelled" if not isinstance(error, Exception) else "failed"
            self._write("turn_end", turn, self.id, status=ended, message=f"{type(error).__name__}: {error}".rstrip(": "))
            raise
        self._write("turn_end", turn, self.id, status=end.status, **({"message": end.message} if end.message else {}),
                    usage=asdict(end.usage))
        return end

    # ----- harnesses.Recorder: what the adapter saw -----

    def text(self, text: str) -> None:
        self._write("message", self._next("m"), self._turn_id(), text=text)

    def tool_call(self, call: str, tool: str, arguments: Any) -> None:
        """A call that the harness reports again, as it learns more, gets its record again with
        the same id. The last record of an id holds all its arguments."""
        if tool == output_tool.NAME:  # the agent's result, which the harness may report before submit_result sees it
            arguments = self._as_result(arguments)
        ours = self._calls.get(str(call)) or self._next("c")
        self._calls[str(call)] = ours
        self._write("tool_call", ours, ours.rsplit(".", 1)[0], tool=tool, arguments=arguments)

    def tool_result(self, call: str, result: Any, is_error: bool = False, kept: bool = True,
                    characters: int | None = None) -> None:
        ours = self._calls.pop(str(call), None) or self._next("c")
        size = len(json.dumps(result, default=str)) if characters is None else characters
        answer = {"result": result} if kept else {"kept": False, "characters": size}
        self._write("tool_result", ours, ours.rsplit(".", 1)[0], is_error=is_error, **answer)

    def thinking(self, text: str, duration_ms: int | None) -> None:
        self._write("thinking", self._next("k"), self._turn_id(), duration_ms=duration_ms, text=text)

    def request(self, request_id: str) -> None:
        self._write("request", self._next("r"), self._turn_id(), request_id=request_id)

    # ----- output_tool.ResultTool -----

    def submitted(self, arguments: Any, problems: list[str], reply: str) -> None:
        self._write("submit_result", self._next("s"), self._turn_id(), accepted=not problems, problems=problems,
                    reply=reply, arguments=self._as_result(arguments))

    def _as_result(self, value: Any) -> Any:
        """`value` with each map in it read as the job's output, with what's secret in it hidden.
        A harness's record of a submit_result call holds the result as one of its maps."""
        if isinstance(value, Mapping):
            value = self.scope.at(self.scope.output_path(self.job), value, self.job["node"])
            if isinstance(value, Mapping):
                return {key: self._as_result(inner) for key, inner in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._as_result(inner) for inner in value]
        return value

    def _turn_id(self) -> str:
        return f"{self.id}.t{self._turn}"

    def _next(self, letter: str) -> str:
        self._counts[(self._turn, letter)] += 1
        return f"{self._turn_id()}.{letter}{self._counts[(self._turn, letter)]}"

    def _write(self, kind: str, id: str, parent: str, **fields) -> None:
        self.scope.write(kind, id, parent, **self.place, turn=self._turn, **fields)


def _walk(value: Any, path: tuple):
    """The values at `path` inside `value` (EACH: every element)."""
    if not path:
        yield value
        return
    step, rest = path[0], path[1:]
    if isinstance(value, Mapping):
        children = value.values() if step is EACH else ([value[step]] if step in value else [])
    elif isinstance(value, (list, tuple)):
        children = value if step is EACH else ([value[step]] if isinstance(step, int) and -len(value) <= step < len(value) else [])
    else:
        children = []
    for child in children:
        yield from _walk(child, rest)
