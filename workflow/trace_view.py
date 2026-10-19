"""Trace view: `wf trace`, which answers questions about one run from its trace.jsonl (trace.py).

    wf trace <run-id>                         everything, as an outline: passes, renders, routes,
                                              attempts, turns, tool calls, outputs
    wf trace <run-id> --node review --pass 2  only that node (any item, any sub-workflow), that pass
    wf trace <run-id> --field instruction     each render of a field, with what it read and gave.
                                              `routes` gives each route decision, `env` every env.*
    wf trace <run-id> --var review.blocking_issues
                                              the value a variable path had in every render that
                                              read it, pass by pass (also when a render read the
                                              whole of it, or only fields inside it)
    --json                                    the same, as JSON, with the values in blobs/ read back

It reads only the run folder, so it works on any run, finished or not, and costs nothing.
"""

import json
import re
from typing import Any

from . import templates
from .runs import Run
from .trace import BLOBS, FILE, stored

_BLOB = re.compile(rf"{BLOBS}/[0-9a-f]{{64}}\.(txt|json)")  # the only files a reference may name
_STEP = re.compile(r'\.?([A-Za-z_][A-Za-z0-9_]*)|\[(-?\d+)\]|\[("(?:[^"\\]|\\.)*")\]')
WIDTH = 100  # characters of a value shown on one line


class PathError(ValueError):
    pass


def records(run: Run) -> list[dict]:
    path = run.dir / FILE
    if not path.exists():
        return []
    found = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            found.append(json.loads(line))
        except json.JSONDecodeError:  # a line cut short by a crash
            continue
    return found


def full(run: Run, value: Any) -> Any:
    """A value as it was: read back from blobs/ if the record holds a reference to it. A
    reference to a blob that is gone, or to any other file, stays as it is."""
    if not stored(value) or not isinstance(value["$blob"], str) or not _BLOB.fullmatch(value["$blob"]):
        return value
    path = run.dir / value["$blob"]
    try:
        if not path.resolve().is_relative_to(run.dir.resolve() / BLOBS):  # blobs/ is a link to elsewhere
            return value
        text = path.read_text(encoding="utf-8")
        return text if path.suffix == ".txt" else json.loads(text)
    except (OSError, ValueError):
        return value


def expanded(run: Run, record: dict) -> dict:
    """A record with each value that it holds in blobs/ read back."""
    found = {}
    for key, value in record.items():
        if key == "reads":
            value = [{**read, "value": full(run, read.get("value"))} for read in value]
        elif key == "routes":
            value = [{**route, "reads": [{**read, "value": full(run, read.get("value"))} for read in route.get("reads", [])]}
                     for route in value]
        else:
            value = full(run, value)
        found[key] = value
    return found


def parse_path(text: str) -> tuple:
    """plan.tasks[0]["a b"] -> ("plan", "tasks", 0, "a b"). Raises PathError."""
    steps, at = [], 0
    while at < len(text):
        match = _STEP.match(text, at)
        first = not steps
        if match is None or (first and (not match.group(1) or text[at] == ".")) or (not first and match.group(1) and text[at] != "."):
            raise PathError(f"can't read `{text}` as a variable path like review.blocking_issues[0]")
        name, index, quoted = match.groups()
        steps.append(name if name is not None else int(index) if index is not None else json.loads(quoted))
        at = match.end()
    if not steps:
        raise PathError("the variable path is empty")
    return tuple(steps)


def select(found: list[dict], node: str | None = None, pass_no: int | None = None, field: str | None = None,
           renders: bool = False) -> list[dict]:
    """The records about `node`: its id anywhere, or its full path like build[1]/implement.
    Only those of pass `pass_no`, and only renders of `field` or of a field under it (env
    covers env.TOKEN). With `renders`, `routes` covers the renders of each `when`, not the decisions."""
    chosen = []
    for record in found:
        if node is not None and node not in (record.get("node"), f"{record.get('where', '')}{record.get('node')}"):
            continue
        if pass_no is not None and record.get("pass") != pass_no:
            continue
        if field is not None and not _of_field(record, field, renders):
            continue
        chosen.append(record)
    return chosen


def _of_field(record: dict, field: str, renders: bool = False) -> bool:
    """`routes` is the route decisions. routes[0].when is the renders of that `when`."""
    if not renders and (record.get("kind") == "routes" or field == "routes"):
        return record.get("kind") == "routes" and field == "routes"
    name = record.get("field")
    return record.get("kind") == "render" and isinstance(name, str) and (
        name == field or name.startswith((f"{field}.", f"{field}[")))


def values(run: Run, found: list[dict], path: tuple) -> list[dict]:
    """Every value the variable at `path`, or something inside it, had in the renders in `found`."""
    matches = []
    for record in found:
        if record.get("kind") != "render":
            continue
        for read in record.get("reads", []):
            try:
                read_path = parse_path(read["path"])
            except PathError:
                continue
            if read_path[:len(path)] == path:  # this path, or a field inside it
                value = full(run, read["value"])
            elif path[:len(read_path)] == read_path:  # the template read all of it
                inside, value = _inside(full(run, read["value"]), path[len(read_path):])
                if not inside:
                    continue
                read_path = path
            else:
                continue
            matches.append({"id": record["id"], **{k: record[k] for k in ("node", "where", "pass", "index") if k in record},
                            "field": record.get("field"), "path": templates.path_text(read_path), "value": value})
    return matches


def _inside(value: Any, path: tuple) -> tuple[bool, Any]:
    for step in path:
        if isinstance(value, dict) and step in value:
            value = value[step]
        elif isinstance(value, list) and isinstance(step, int) and -len(value) <= step < len(value):
            value = value[step]
        else:
            return False, None
    return True, value


# ---------- text ----------


def printable(text: str) -> str:
    """`text` with each character that UTF-8 can't hold, such as a lone surrogate from an agent,
    as its \\u escape. Inside a JSON string, that is the JSON escape for it."""
    return text.encode("utf-8", "backslashreplace").decode("utf-8")


def text(run: Run, found: list[dict], field: str | None = None) -> str:
    """The records as an outline, or with `field`, each render of it in turn."""
    if field is not None and field != "routes":
        return _renders(run, found)
    lines = []
    for record in found:
        lines += _lines(run, record)
    return "\n".join(lines)


def values_text(matches: list[dict], path: str) -> str:
    if not matches:
        return f"no render read {path}"
    width = max(len(m["id"]) for m in matches)
    fields = max(len(m["field"] or "") for m in matches)
    return "\n".join(f"{m['id']:<{width}}  {m['field'] or '':<{fields}}  {m['path']} = {_short(m['value'])}" for m in matches)


def _renders(run: Run, found: list[dict]) -> str:
    lines, shown = [], set()
    for record in found:
        if record.get("kind") != "render":
            continue
        template = full(run, record.get("template"))
        text = template if isinstance(template, str) else _short(template)  # a blob that is gone: its reference
        key = (record.get("where"), record.get("node"), record.get("field"), text)
        if key not in shown:  # the template once, where it first rendered
            shown.add(key)
            lines += ["", f"{record.get('field')} of {_where(record)}:"] + [f"    {line}" for line in text.splitlines()]
        lines.append(f"  {record['id']}")
        lines += [f"    {read['path']} = {_short(read['value'])}" for read in record.get("reads", [])]
        lines.append(f"    ✗ {record['error']}" if "error" in record else f"    → {_short(record.get('result'))}")
    return "\n".join(lines).lstrip("\n")


def _lines(run: Run, record: dict) -> list[str]:
    kind, pad = record.get("kind"), "    " * record["id"].count("/")
    label = record["id"]
    if kind == "run":
        inputs = f", input {_short(record['inputs'])}" if "inputs" in record else ""
        return [f"run {record.get('action')}: {record.get('workflow')}{inputs}{' (dry run)' if record.get('dry_run') else ''}"]
    if kind == "run_stopped":
        detail = {k: v for k, v in record.items() if k not in ("time", "kind", "id", "parent", "status")}
        return [f"run stopped: {record.get('status')}" + (f" {_short(detail)}" if detail else "")]
    if kind == "pass":
        return [f"{pad}{label}" + (f"  {record['items']} items" if "items" in record else "")]
    if kind == "job":
        if "index" not in record and record.get("first_attempt", 1) == 1:
            return []  # a pass of a plain node: the pass line says it
        item = f"  item {_short(record['item'])}" if "item" in record else ""
        again = f"  from attempt {record['first_attempt']}" if record.get("first_attempt", 1) > 1 else ""
        return [f"{pad}  {label}{item}{again}"]
    if kind == "render":
        if str(record.get("field")).startswith("routes["):
            return []  # the routes record shows it, with the decision
        more = "".join(f", {k} {record[k]}" for k in ("harness", "continued") if record.get(k))
        head = f"{pad}  {label} {record.get('field')}{more}"
        reads = [f"{pad}      {read['path']} = {_short(read['value'])}" for read in record.get("reads", [])]
        outcome = f"✗ {record['error']}" if "error" in record else f"→ {_short(record.get('result'))}"
        return [f"{head} {outcome}", *reads]
    if kind == "routes":
        taken = record.get("taken")
        head = f"{pad}  {label} routes → " + (", ".join(record.get("to", [])) + f" (route {taken})" if taken is not None else "none (a `when` failed)")
        return [head, *(_route(route, pad) for route in record.get("routes", []))]
    if kind == "attempt":
        return [f"{pad}  {label} {_attempt(record)}"]
    if kind == "attempt_end":
        return [f"{pad}  {label} {_ended(record)}"]
    if kind == "turn":
        sent = full(run, record.get("text"))
        return [f"{pad}    {label} {record.get('sends')} ({len(str(sent)):,} characters): {_short(sent)}"]
    if kind == "turn_end":
        usage = record.get("usage") or {}
        spent = f", {usage.get('tokens', 0):,} tokens, ${usage.get('cost_usd', 0.0):.4f}" if usage else ""
        return [f"{pad}    {label} ended: {record.get('status')}{spent}" + (f": {record['message']}" if record.get("message") else "")]
    if kind == "message":
        return [f"{pad}      {label} says {_short(record.get('text'))}"]
    if kind == "tool_call":
        return [f"{pad}      {label} {record.get('tool')} {_short(record.get('arguments'))}"]
    if kind == "tool_result":
        answer = (f"{record.get('characters', 0):,} characters, not kept" if record.get("kept") is False
                  else _short(record.get("result")))
        return [f"{pad}      {label} {'✗ ' if record.get('is_error') else '→ '}{answer}"]
    if kind == "submit_result":
        verdict = "accepted" if record.get("accepted") else "refused: " + "; ".join(record.get("problems", []))
        return [f"{pad}      {label} submit_result {verdict}  {_short(record.get('arguments'))}"]
    if kind == "job_end":
        if record.get("waiting"):
            return [f"{pad}  {label} waits for a person"]
        if record.get("ok"):
            return [f"{pad}  {label} ✓ {_short(record.get('output'))}"]
        return [f"{pad}  {label} ✗ {record.get('error')}"]
    return [f"{pad}  {label} {kind}"]


def _route(route: dict, pad: str) -> str:
    when = f" when {route['when']}" if route.get("when") else ""
    limit = f", max {route['max']}, taken {route.get('taken_before', 0)} so far" if route.get("max") else ""
    if route.get("used_up"):
        outcome = "skipped: its max is used up"
    elif not route.get("checked"):
        outcome = "not checked"
    elif "error" in route:
        outcome = f"✗ {route['error']}"
    else:
        outcome = "true" if route.get("result") else "false"
    reads = "".join(f"\n{pad}          {read['path']} = {_short(read['value'])}" for read in route.get("reads", []))
    return f"{pad}      route {route['index']} → {', '.join(route.get('to', []))}{when}{limit}: {outcome}{reads}"


def _attempt(record: dict) -> str:
    if "command" in record:
        env = f", env {', '.join(record['env'])}" if record.get("env") else ""
        return f"runs {_short(record['command'])} in {record.get('cwd')}{env}"
    if "workflow" in record:
        return f"runs {record['workflow']} with {_short(record.get('inputs'))}"
    parts = [f"on {record.get('harness')}"]
    parts += [f"{key} {record[key]}" for key in ("model", "effort", "access") if record.get(key)]
    if record.get("web"):
        parts.append("web")
    if record.get("tools"):
        parts.append(f"tools {', '.join(record['tools'])}")
    session = record.get("session") or {}
    start = session.get("start", "fresh")
    parts.append(f"session {start}" + (f" of {session['from']}" if session.get("from") else "")
                 + (f" ({session['fresh_because']})" if session.get("fresh_because") else ""))
    workspace = record.get("workspace") or {}
    if workspace.get("path"):
        parts.append(f"in {workspace['path']}")
    return ", ".join(parts)


def _ended(record: dict) -> str:
    detail = []
    usage = record.get("usage")
    if usage:
        detail.append(f"{usage.get('tokens', 0):,} tokens, ${usage.get('cost_usd', 0.0):.4f}")
    if (record.get("session") or {}).get("id"):
        detail.append(f"session {record['session']['id']}")
    if "exit_code" in record:
        detail.append(f"exit code {record['exit_code']}, stdout {record.get('stdout')}, stderr {record.get('stderr')}")
    if record.get("error"):
        detail.append(record["error"])
    return f"{record.get('outcome')} after {record.get('duration_s', 0)}s" + (f": {'; '.join(detail)}" if detail else "")


def _where(record: dict) -> str:
    return "the end output" if record.get("node") is None else f"{record.get('where', '')}{record['node']}"


def _short(value: Any) -> str:
    if isinstance(value, dict) and "$blob" in value:
        return f"{json.dumps(value.get('start', ''), ensure_ascii=False)}… ({value.get('size', 0):,} characters in {value['$blob']})"
    shown = json.dumps(value, ensure_ascii=False, default=str)
    return shown if len(shown) <= WIDTH else shown[:WIDTH - 1] + "…"
