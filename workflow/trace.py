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
    turn           a message goes to the agent     `sends` (instruction or reminder), the full text
    message        the agent writes text           the text
    tool_call      the agent calls a tool          the tool and its arguments
    tool_result    the tool answers                the answer, and if it is an error
    submit_result  the agent calls submit_result   the arguments, and the problems or "accepted"
    turn_end       the agent's turn ends           how it ended, and what it used
    attempt_end    the attempt ends                all of `attempt`, the session id, start, end,
                                                   how it ended and what it used. For a command:
                                                   its text, env, exit code, and the files that
                                                   hold its stdout and stderr
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
    review#2.a1.t2.c4          the 4th tool call of that turn (.s1 a submit_result call, .m1 a message)
    end                        the end outputs
    build#1[3].a1/implement#2  a pass in the sub-workflow that attempt 1 of build[3] runs

A value longer than CAP characters goes to blobs/<sha256>.txt (text) or .json
(any other value). The trace writes that file once, however often the value
occurs. The record then holds {"$blob": "blobs/<sha256>.txt", "size": ..., "start": ...}.

Secrets never go into the trace. A command node's `secret_env` names env values
that are secret. The function secret_paths() finds the variable paths those
values come from. These are what their templates read, also through `item` to
the for_each list, or through a sub-workflow's input to the parent's `with`. A
value at such a path becomes [secret] in each record that has it there: a read,
the input, an output, a submit_result call. So does a secret env value, and a
`with` value that feeds one. Then the text of each such value goes out of every
later record of the process, wherever it occurs. A wf tool's token never gets in
(relay.py calls keep_out()). Nor does what a wf tool answers: its tool_result
holds only the size, as the attempt's transcript does.

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
from datetime import datetime
from pathlib import Path
from typing import Any

from . import templates
from .schemas import EACH

FILE = "trace.jsonl"
BLOBS = "blobs"
CAP = 1000  # characters a value may have in a record. A longer one goes to blobs/
PREVIEW = 80  # characters of a blob shown in its reference
HIDDEN = "[secret]"
SHORTEST = 4  # secret text this long or longer is also removed from other text
_OWN = frozenset({"kind", "id", "parent", "node", "where", "pass", "index", "attempt", "turn", "field", "started", "ended"})

_everywhere: set[str] = set()  # secrets of this process, such as wf tools' tokens


def keep_out(text: str | None) -> None:
    """Keep `text` out of every trace this process writes."""
    if isinstance(text, str) and len(text) >= SHORTEST:
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
        elif isinstance(value, (str, int, float)) and not isinstance(value, bool) and len(str(value)) >= SHORTEST:
            self.hidden.add(str(value))

    def write(self, record: dict) -> None:
        try:
            with self._lock:
                line = json.dumps(self._ready(record), ensure_ascii=False, default=str)
                with (self.folder / FILE).open("a", encoding="utf-8") as file:
                    file.write(line + "\n")
        except OSError as error:
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
        if len(data) <= CAP:
            return value
        name = f"{BLOBS}/{hashlib.sha256(data.encode()).hexdigest()}{suffix}"
        path = self.folder / name
        if not path.exists():
            path.parent.mkdir(exist_ok=True)
            temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
            temp.write_text(data, encoding="utf-8")
            temp.replace(path)
        return {"$blob": name, "size": len(data), "start": data[:PREVIEW]}


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


def secret_paths(workflow, load: Callable[[Path], Any], seen: tuple = ()) -> frozenset[tuple]:
    """The variable paths whose values become an env value that a command node marks secret.
    These are the paths the env templates read, also through `item` to the for_each list.
    They include what the parent's `with` reads for a sub-workflow input that feeds one.
    `load` reads a sub-workflow file. Paths are tuples of names and indexes, with EACH
    for any element."""
    found: set[tuple] = set()
    for node_id, node in workflow.nodes.items():
        if node.kind == "command":
            env = workflow.setting(node_id, "env")
            for name in workflow.setting(node_id, "secret_env"):
                if name in env:
                    found |= _sources(workflow, node_id, env[name])
        elif node.kind == "workflow" and workflow.path is not None:
            path = (Path(workflow.path).parent / node.workflow).resolve()
            if path in seen:
                continue
            inputs = secret_inputs(load(path), load, (*seen, path))
            for name, value in node.with_.items():
                if isinstance(value, str) and name in inputs:
                    found |= _sources(workflow, node_id, value)
    found |= {("history", path[0], EACH, *path[1:]) for path in found if path[0] in workflow.nodes}
    return frozenset(found)


def secret_inputs(workflow, load: Callable[[Path], Any], seen: tuple = ()) -> set[str]:
    """The start inputs of `workflow` that feed a secret env value."""
    paths = secret_paths(workflow, load, seen)
    if ("start",) in paths:
        return set(workflow.start.input)
    return {path[1] for path in paths if len(path) > 1 and path[0] == "start" and isinstance(path[1], str)}


def _sources(workflow, node_id: str, source: str) -> set[tuple]:
    paths = set()
    try:
        refs = templates.references(source)
    except templates.TemplateError:  # validate refuses it, and the run fails when it renders
        return paths
    each = workflow.nodes[node_id].for_each
    listed = templates.pure_path(each, expression=True) if each else None
    for ref in refs:
        path = (ref.name, *(ref.path or ()))
        paths.add(path)
        if ref.name == "item" and listed:
            paths.add((listed[0], *listed[1], EACH, *path[1:]))
    return paths


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

    def __init__(self, trace: Trace, workflow, load: Callable[[Path], Any], parent: str = "run", where: str = ""):
        self.trace, self.parent, self.where = trace, parent, where
        self.prefix = "" if parent == "run" else f"{parent}/"
        self.load = load
        self.paths = secret_paths(workflow, load)

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

    def at(self, path: tuple, value: Any) -> Any:
        """`value`, found at the variable path `path`, with every secret part of it [secret]."""
        if any(len(secret) <= len(path) and _matches(secret, path[:len(secret)]) for secret in self.paths):
            self.trace.hide(value)
            return HIDDEN
        inner = [secret[len(path):] for secret in self.paths if len(secret) > len(path) and _matches(secret[:len(path)], path)]
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
        if self.paths:
            values = variables()
            for path in self.paths:
                for found in _walk(values, path):
                    self.trace.hide(found)

    def secret_inputs(self, workflow) -> set[str]:
        return secret_inputs(workflow, self.load)

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
        item = {"item": self.at(("item",), job["item"])} if job.get("index") is not None else {}
        self.write("job", self.owner(job), self._job_parent(job), **place(job), first_attempt=job["attempt"], **item)

    def job_end(self, job: Mapping, finished: Mapping | None) -> None:
        """How a job ended: its Finished record, or None when it waits for a person."""
        if finished is None:
            fields = {"ok": None, "waiting": True}
        else:
            fields = {"ok": finished["ok"], "attempts": finished["attempts"], "usage": finished["usage"],
                      "workspace": finished["workspace"]}
            if finished["ok"]:
                fields["output"] = self.at(self.output_path(job), finished["output"])
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
                   template=source, reads=self.reads(reads), **fields)

    def reads(self, reads: list) -> list[dict]:
        return [{"path": templates.path_text(read.path), "value": self.at(read.path, read.value)} for read in reads]

    def routes(self, job: Mapping, routes: list[dict], taken: int | None) -> None:
        """A node's route decision: `routes` as they were checked (see state.Progress), and the
        index of the one taken (None: a `when` failed)."""
        checked = [{**route, "reads": self.reads(route.get("reads", []))} for route in routes]
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

    async def turn(self, number: int, text: str, sending) -> Any:
        """Await `sending`, the harness's turn with `text`, between a turn and a turn_end record."""
        self._turn = number
        turn = f"{self.id}.t{number}"
        self._write("turn", turn, self.id, sends="instruction" if number == 1 else "reminder", text=text)
        try:
            end = await sending
        except BaseException as error:
            ended = "cancelled" if not isinstance(error, Exception) else "failed"
            self._write("turn_end", turn, self.id, status=ended, message=f"{type(error).__name__}: {error}".rstrip(": "))
            raise
        self._write("turn_end", turn, self.id, status=end.status, **({"message": end.message} if end.message else {}),
                    usage={"tokens": end.usage.tokens, "cost_usd": end.usage.cost_usd})
        return end

    # ----- harnesses.Recorder: what the adapter saw -----

    def text(self, text: str) -> None:
        self._write("message", self._next("m"), self._turn_id(), text=text)

    def tool_call(self, call: str, tool: str, arguments: Any) -> None:
        self._calls[str(call)] = ours = self._next("c")
        self._write("tool_call", ours, self._turn_id(), tool=tool, arguments=arguments)

    def tool_result(self, call: str, result: Any, is_error: bool = False, kept: bool = True) -> None:
        ours = self._calls.pop(str(call), None) or self._next("c")
        answer = {"result": result} if kept else {"kept": False, "characters": len(json.dumps(result, default=str))}
        self._write("tool_result", ours, ours.rsplit(".", 1)[0], is_error=is_error, **answer)

    # ----- output_tool.ResultTool -----

    def submitted(self, arguments: Any, problems: list[str], reply: str) -> None:
        self._write("submit_result", self._next("s"), self._turn_id(), accepted=not problems, problems=problems,
                    reply=reply, arguments=self.scope.at(self.scope.output_path(self.job), arguments))

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
