"""Export: the records of run folders as rows of the tables of wf runs in ClickHouse.

    wf metrics export [<run-id>...] [--all-repos] [--runs-dir DIR]

ClickHouse keeps the tables in its database `wf`, as clickhouse.sql makes them:

    runs           a row per run: workflow, status, start and end, and what its attempts used
    attempts       a row per attempt of a pass or an item: harness, model, how it ended, its error
                   and its class, and what it used
    turns          a row per turn of an agent attempt
    tool_calls     a row per tool call of an agent: when it started, how long it took, if it failed
    routes         a row per route decision of a pass
    metric_points  the points of custom metrics, which nothing writes yet

Store.prepare() makes the database and each table that is missing. Then it checks each table that
the export writes against clickhouse.sql: each column with its type, the engine and the key. A table
that differs is a SchemaMismatch, and the export writes nothing.

An export of a run folder reads its trace.jsonl from the byte offset where the last export stopped.
A record that starts or ends something gives a row: an `attempt` gives a row with the outcome
`running`, and its `attempt_end` gives the whole row again. Turns and tool calls go the same way.
Each table is a ReplacingMergeTree on the natural key of its rows (run, node, pass, item, attempt,
...), so the later row replaces the earlier one. A node in a sub-workflow has its path as its name,
as events name it: build[1]/implement. So when a workflow node runs its sub-workflow again in a later
attempt, the rows of that attempt replace the rows of the earlier one. A job that isn't an item has
the item -1.

export.json in the run folder keeps the offset, the identity of the tables that took the rows, and
what the next export needs of the records before the offset: the attempts that run, the turns and
tool calls that haven't ended, and the times and spending of the run. A line that is still being
written waits for the next export. The duration of a run is the time that wf drove it: from each
`run` record to its `run_stopped`. Its tokens and cost are the sums of its rows in `attempts`.

So exporting twice changes nothing: the second export finds nothing new and writes nothing. When the
tables are made again, as after `docker compose down -v`, the identity changes, and the next export
of each run starts again from the start of its file. Each row that it writes replaces an equal one.

A run folder of a wf from before the trace has only events.jsonl. The export reads that file whole,
once, and gets less from it (_events): the run, the attempts that events name, their failures, the
names and times of the tool calls, the routes, and the tokens and cost of each pass. It gets no
turns, no durations or results of tool calls, and no session ids.

The errors have classes, so that a caller can tell what to do:

    StoreDown          ClickHouse doesn't answer, or refuses a request for a reason other than its tables
    SchemaMismatch     a table differs from clickhouse.sql
    UnreadableFolder   wf can't read the run folder, or can't keep its offset in export.json

After any of them, export.json stays as it was, so the next export writes the same rows again. The
export never takes the run's lock, and it writes nothing in the run folder but export.json, so no
error of the export can fail a run or make it slower.
"""

import functools
import hashlib
import http.client
import json
import os
import re
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlencode, urlsplit

from . import runs, settings, trace_view
from .runs import Run
from .trace import FILE as TRACE

SCHEMA = Path(__file__).with_name("clickhouse.sql")
PROGRESS = "export.json"
EVENTS = "events.jsonl"
URL_ENV = "WF_CLICKHOUSE_URL"
TELEMETRY = ("clickhouse_url",)  # the settings under `telemetry:` in config.yaml
USER = KEY = "wf"  # the user and password when the URL names no user: those of wf's metrics stack
TIMEOUT_S = 30
NO_ITEM = -1
WRITTEN = ("runs", "attempts", "turns", "tool_calls", "routes")  # the tables that the export writes
COUNTS = ("tokens", "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "reasoning_tokens")
CALL = re.compile(r"(.+)\.(t\d+\.c\d+)")  # a tool call's trace id: its attempt's id, and its own id in the attempt
# ClickHouse's error codes for a table, column or type that isn't what a request expects, and for a value
# that doesn't fit its column. ClickHouse answers them with HTTP 400, 404 or 500.
SCHEMA_CODES = {"6", "8", "16", "27", "47", "53", "60", "81", "117"}
ERROR_CLASSES = (  # the first class whose pattern the error matches, else "other"
    ("rate_limit", r"resource_exhausted|\b429\b|usage.?limit|rate.?limit|quota"),
    ("dropped_connection", r"stream closed|econnreset|\[canceled\]|\[aborted\]|connection (stalled|failed|reset|closed|lost)"
                           r"|enotfound|can't reach"),
    ("timeout", r"timed out|timeout"),
    ("auth", r"unauthori[sz]ed|unauthenticated|authenticat|log ?in\b|logged in|api key"),
    ("no_result", r"never made a valid submit_result call"),
)


class ExportError(Exception):
    """An export that didn't finish."""


class StoreDown(ExportError):
    """ClickHouse doesn't answer, or refuses a request for a reason other than its tables."""


class SchemaMismatch(ExportError):
    """A table of ClickHouse differs from clickhouse.sql."""


class UnreadableFolder(ExportError):
    """wf can't read the run folder, or can't keep its offset in export.json."""


# ---------- where ClickHouse is ----------


def address() -> str | None:
    """The URL of the ClickHouse to export to: WF_CLICKHOUSE_URL, else `telemetry.clickhouse_url` in
    ~/.wf/config.yaml, else None. wf never writes either. The file is checked even when the variable
    is set. Raises settings.SettingsError."""
    block = settings.get("telemetry")
    if block is not None and not isinstance(block, dict):
        raise settings.SettingsError(f"telemetry in {settings.path()} must hold settings by name, "
                                     "like `clickhouse_url: http://127.0.0.1:8123`")
    unknown = [f"telemetry.{name}" for name in block or {} if name not in TELEMETRY]
    if unknown:
        raise settings.SettingsError(f"{settings.path()}: unknown setting {', '.join(unknown)} "
                                     f"(known: {', '.join(f'telemetry.{name}' for name in TELEMETRY)})")
    configured = (block or {}).get("clickhouse_url")
    if configured is not None:
        _check_url(configured, f"telemetry.clickhouse_url in {settings.path()}")
    given = os.environ.get(URL_ENV, "").strip()
    if given:
        _check_url(given, URL_ENV)
    return given or configured


def _check_url(url: object, where: str) -> None:
    try:
        parts = urlsplit(url) if isinstance(url, str) else None
        good = bool(parts and parts.scheme in ("http", "https") and parts.hostname and not parts.query
                    and not parts.fragment and (parts.port is None or parts.port > 0))
    except ValueError:  # a port that isn't a number
        good = False
    if not good:
        raise settings.SettingsError(f"{where} must be the address of ClickHouse's HTTP port, like http://127.0.0.1:8123. "
                                     "A user and a password go into it as http://user:password@127.0.0.1:8123")


# ---------- ClickHouse ----------


class Store:
    """A ClickHouse that takes the rows, through its HTTP interface. The user and the password go in
    the headers X-ClickHouse-User and X-ClickHouse-Key, as wf's metrics stack requires."""

    def __init__(self, url: str):
        parts = urlsplit(url)
        self.connection = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
        self.host, self.port, self.path = parts.hostname, parts.port, parts.path or "/"
        self.where = f"{parts.scheme}://{parts.netloc.rpartition('@')[2]}{parts.path}"  # for messages: no password
        named = parts.username is not None
        self.user = unquote(parts.username) if named else USER
        self.key = unquote(parts.password or "") if named else KEY
        self.identity = ""  # prepare() sets it

    def prepare(self) -> None:
        """Make the database and each table of clickhouse.sql that is missing, then check the tables that
        the export writes. `identity` then names those tables: a table that is made again has a new one.
        Raises StoreDown or SchemaMismatch."""
        wanted = schema()
        tables = self._tables()
        if any(name not in tables for name in wanted):
            for statement in _statements(SCHEMA.read_text(encoding="utf-8")):
                self._post(statement)
            tables = self._tables()
        columns: dict[str, dict[str, str]] = {}
        for row in self._select("SELECT table, name, type FROM system.columns WHERE database = 'wf'"):
            columns.setdefault(row["table"], {})[row["name"]] = row["type"]
        problems = differences(wanted, tables, columns)
        if problems:
            raise SchemaMismatch(f"the tables in ClickHouse at {self.where} differ from {SCHEMA.name}: {'; '.join(problems)}. "
                                 "Drop those tables, and the next export makes them again and exports each run again")
        named = [[name, tables[name]["uuid"]] for name in WRITTEN]
        self.identity = hashlib.sha256(json.dumps(named).encode()).hexdigest()[:16]

    def insert(self, table: str, rows: list[dict]) -> None:
        body = "".join(json.dumps(row) + "\n" for row in rows).encode()
        self._post(f"INSERT INTO wf.{table} FORMAT JSONEachRow", body)

    def _tables(self) -> dict[str, dict]:
        return {row["name"]: row for row in self._select(
            "SELECT name, toString(uuid) AS uuid, engine, sorting_key FROM system.tables WHERE database = 'wf'")}

    def _select(self, query: str) -> list[dict]:
        return [json.loads(line) for line in self._post(f"{query} FORMAT JSONEachRow").splitlines() if line.strip()]

    def _post(self, query: str, data: bytes | None = None) -> bytes:
        """Run `query` on the data `data`, or with no data, and give the answer."""
        params = {"wait_end_of_query": "1", "input_format_skip_unknown_fields": "0", **({"query": query} if data is not None else {})}
        connection = self.connection(self.host, self.port, timeout=TIMEOUT_S)
        try:
            connection.request("POST", f"{self.path}?{urlencode(params)}", query.encode() if data is None else data,
                               {"X-ClickHouse-User": self.user, "X-ClickHouse-Key": self.key})
            response = connection.getresponse()
            answer = response.read()
        except (OSError, http.client.HTTPException) as error:
            raise StoreDown(f"ClickHouse at {self.where} doesn't answer: {error}") from None
        finally:
            connection.close()
        if response.status == 200:
            return answer
        said = " ".join(answer.decode("utf-8", "replace").split())
        if response.getheader("X-ClickHouse-Exception-Code") in SCHEMA_CODES:
            raise SchemaMismatch(f"ClickHouse at {self.where} refused the rows: {said}")
        raise StoreDown(f"ClickHouse at {self.where} refused a request ({response.status}): {said}")


@functools.cache
def schema() -> dict[str, dict]:
    """The tables of clickhouse.sql, by name: {"columns": {name: type}, "engine": ..., "key": "run_id, node, ..."}."""
    found = {}
    for statement in _statements(SCHEMA.read_text(encoding="utf-8")):
        head = re.match(r"CREATE TABLE IF NOT EXISTS wf\.(\w+) \(", statement)
        if not head:
            continue
        depth, at = 1, head.end()
        while depth:
            depth += {"(": 1, ")": -1}.get(statement[at], 0)
            at += 1
        rest = statement[at:]
        key = re.search(r"ORDER BY (.+)$", rest, re.S)[1].strip().removeprefix("(").removesuffix(")")
        found[head[1]] = {"columns": _columns(statement[head.end():at - 1]), "engine": re.search(r"ENGINE = (\w+)", rest)[1],
                          "key": ", ".join(part.strip() for part in key.split(","))}
    return found


def _statements(sql: str) -> list[str]:
    text = "\n".join(line for line in sql.splitlines() if not line.lstrip().startswith("--"))
    return [part.strip() for part in text.split(";") if part.strip()]


def _columns(text: str) -> dict[str, str]:
    """name -> type, of a column list like `a String, b Map(String, UInt8), c UInt8 DEFAULT 1`."""
    found, depth, start = {}, 0, 0
    for at, char in enumerate(text + ","):
        depth += (char == "(") - (char == ")")
        if char == "," and depth == 0:
            name, kind = text[start:at].split(None, 1)
            found[name] = kind.split(" DEFAULT ")[0].strip()
            start = at + 1
    return found


def differences(wanted: dict, tables: dict, columns: dict) -> list[str]:
    """How the tables that the export writes differ from `wanted` (schema()): a table or a column that is
    missing, a column of another type, another engine or another key."""
    found = []
    for name in WRITTEN:
        table, have = wanted[name], tables.get(name)
        if have is None:
            found.append(f"there's no table wf.{name}")
            continue
        if (have["engine"], have["sorting_key"]) != (table["engine"], table["key"]):
            found.append(f"wf.{name} is a {have['engine']} on ({have['sorting_key']}), not a {table['engine']} on ({table['key']})")
        for column, kind in table["columns"].items():
            given = columns.get(name, {}).get(column)
            if given is None:
                found.append(f"wf.{name} has no column {column}")
            elif given != kind:
                found.append(f"wf.{name}.{column} is {given}, not {kind}")
    return found


# ---------- a run folder ----------


def export(run: Run, store) -> dict[str, int]:
    """Export a run folder into `store` (a prepared Store) from where its last export stopped, and keep
    the new offset in its export.json. Returns the number of rows written, by table: none when nothing
    is new. Raises UnreadableFolder, StoreDown or SchemaMismatch, and export.json then stays as it was."""
    folder = run.dir
    path = next((folder / name for name in (TRACE, EVENTS) if (folder / name).is_file()), None)
    if path is None:
        raise UnreadableFolder(f"{folder} has neither {TRACE} nor {EVENTS}" if folder.is_dir() else f"{folder} isn't a folder")
    kept = _kept(folder)
    same = kept.get("store") == store.identity and kept.get("file") == path.name
    offset, state = (kept["offset"], kept["state"]) if same else (0, _fresh())
    records: list[dict | None] = []
    try:
        size = path.stat().st_size
        if size < offset:  # not the file that the last export read
            offset, state = 0, _fresh()
        end = offset
        if path.name == TRACE:
            fold = _Trace(run, state)
            for record, after in _lines(path, offset):
                fold.add(record)
                end = after
            rows = fold.rows()
        elif size != offset:  # events.jsonl no longer grows, so it's read whole, once
            for record, after in _lines(path, 0):
                records.append(record)
                end = after
            rows = _events(run, records)
        else:
            rows = {}
    except OSError as error:
        raise UnreadableFolder(f"can't read {path}: {error.strerror or error}") from None
    if same and end == offset:
        return {}
    for table, found in rows.items():
        if found:
            store.insert(table, found)
    try:
        runs.write_json(folder / PROGRESS, {"store": store.identity, "file": path.name, "offset": end, "state": state})
    except OSError as error:
        raise UnreadableFolder(f"can't keep the offset in {folder / PROGRESS}: {error.strerror or error}") from None
    return {table: len(found) for table, found in rows.items() if found}


def _fresh() -> dict:
    return {"started": None, "driving": None, "driven_s": 0.0, "status": "", "ended": None,
            "spent": {}, "attempts": {}, "turns": {}, "calls": {}}


def _kept(folder: Path) -> dict:
    """export.json: where the last export stopped. One that is missing or damaged counts as none."""
    try:
        kept = json.loads((folder / PROGRESS).read_text(encoding="utf-8"))
        state = kept["state"]
        whole = isinstance(kept["offset"], int) and set(state) == set(_fresh()) and all(
            isinstance(state[name], dict) for name in ("spent", "attempts", "turns", "calls"))
    except (OSError, ValueError, KeyError, TypeError):
        return {}
    return kept if whole else {}


def _lines(path: Path, offset: int) -> Iterator[tuple[dict | None, int]]:
    """For each whole line after byte `offset`: its record (None for a damaged line), and the offset after it."""
    with path.open("rb") as file:
        file.seek(offset)
        for line in file:
            if not line.endswith(b"\n"):  # still being written
                return
            offset += len(line)
            try:
                record = json.loads(line)
            except ValueError:
                record = None
            yield (record if isinstance(record, dict) else None), offset


def _repo_key(folder: Path) -> str:
    """The key of the run's repo, as a runs folder names the repo's folder (runs.repo_key)."""
    if folder.parent.name == runs.ROOT.name and folder.parent.parent.name == runs.ROOT.parent.name:
        return runs.repo_key(folder.parent.parent.parent.resolve())  # <repo>/.wf/runs/<run-id>
    return folder.parent.name


def error_class(error: str) -> str:
    """The class of an attempt's error, from its text: rate_limit, dropped_connection, timeout, auth,
    no_result or other. "" for no error."""
    if not error:
        return ""
    return next((name for name, pattern in ERROR_CLASSES if re.search(pattern, error, re.I)), "other")


# ---------- trace.jsonl ----------


class _Trace:
    """The rows that the records of a trace give, one record after the other. `state` holds what the
    records before them left, and export.json keeps it for the next export."""

    def __init__(self, run: Run, state: dict):
        self.run, self.state = run, state
        info = run.info()
        self.workflow, self.dry_run = str(info.get("workflow") or ""), bool(info.get("dry_run"))
        self.found: dict[str, dict] = {table: {} for table in WRITTEN}  # table -> natural key -> its last row

    def rows(self) -> dict[str, list[dict]]:
        return {table: list(found.values()) for table, found in self.found.items()}

    def add(self, record: dict | None) -> None:
        kind, time = (record or {}).get("kind"), (record or {}).get("time")
        if _utc(time) is None:
            return
        if kind in ("run", "run_stopped"):
            self._run(kind == "run", time, record)
        elif kind == "routes":
            self._routes(record)
        elif kind in ("attempt", "attempt_end", "turn", "turn_end", "tool_call", "tool_result"):
            place, ident = _place(record), record.get("id")
            whole = isinstance(ident, str) and (kind in ("attempt", "attempt_end") or isinstance(record.get("turn"), int))
            if place is not None and whole and (not kind.startswith("tool_") or CALL.fullmatch(ident)):
                getattr(self, f"_{kind}")(record, place)

    def _row(self, table: str, row: dict) -> None:
        self.found[table][tuple(row[name] for name in keys(table))] = row

    def _text(self, value: Any) -> str:
        """A text of a record, read back from blobs/ when the record holds a reference to it."""
        value = trace_view.full(self.run, value)
        return "" if value is None else value if isinstance(value, str) else json.dumps(value)

    # ----- the run -----

    def _run(self, starts: bool, time: str, record: dict) -> None:
        state = self.state
        if starts:
            state["started"] = state["started"] or _utc(time)
            state["driving"], state["status"], state["ended"] = time, "running", None
        else:
            if state["driving"]:
                state["driven_s"] += _seconds(state["driving"], time) or 0.0
            state["driving"], state["status"], state["ended"] = None, _word(record.get("status")), _utc(time)
        self._run_row()

    def _run_row(self) -> None:
        state = self.state
        if state["started"] is None:  # the trace began without a `run` record
            return
        spent = state["spent"].values()
        self._row("runs", {
            "run_id": self.run.id, "repo_key": _repo_key(self.run.dir), "workflow": self.workflow, "status": state["status"],
            "dry_run": self.dry_run, "started": state["started"], "ended": state["ended"],
            "duration_s": round(state["driven_s"], 3) if state["ended"] else None,
            "cost_usd": round(sum(cost for _, cost in spent), 6), "tokens": sum(tokens for tokens, _ in spent), "source": "trace"})

    def _routes(self, record: dict) -> None:
        place = _place(record, attempt=False)
        if place is None:
            return
        taken, to = record.get("taken"), record.get("to")
        self._row("routes", {"run_id": self.run.id, "node": place["node"], "pass": place["pass"], "time": _utc(record["time"]),
                             "taken": taken if isinstance(taken, int) else -1,
                             "to": [str(target) for target in to] if isinstance(to, list) else []})

    # ----- attempts -----

    def _attempt(self, record: dict, place: dict) -> None:
        self.state["attempts"][record["id"]] = [_word(record.get("harness")), _word(record.get("model"))]
        self._row("attempts", self._attempt_row(record, place, ended=False))

    def _attempt_end(self, record: dict, place: dict) -> None:
        state, ident = self.state, record["id"]
        for unended in (state["attempts"], state["turns"], state["calls"]):  # nothing inside an attempt outlives it
            for key in [key for key in unended if key == ident or key.startswith((f"{ident}.", f"{ident}/"))]:
                del unended[key]
        row = self._attempt_row(record, place, ended=True)
        if "usage" in record:  # by the row's key, so that the run's numbers are the sums of its rows
            state["spent"][json.dumps([row[name] for name in keys("attempts")])] = [row["tokens"], row["cost_usd"]]
        self._row("attempts", row)
        self._run_row()

    def _attempt_row(self, record: dict, place: dict, ended: bool) -> dict:
        session = record.get("session") if isinstance(record.get("session"), dict) else {}
        error = self._text(record.get("error")) if ended else ""
        exit_code = record.get("exit_code")
        return {
            "run_id": self.run.id, "workflow": self.workflow, **place,
            "kind": "command" if "command" in record else "workflow" if "workflow" in record else "agent",
            "harness": _word(record.get("harness")), "model": _word(record.get("model")), "effort": _word(record.get("effort")),
            "access": _word(record.get("access")), "session_id": _word(session.get("id")),
            "session_start": _word(session.get("start")),
            "started": _utc(record.get("started")) or _utc(record["time"]),
            "ended": _utc(record.get("ended")) if ended else None,
            "duration_s": _number(record.get("duration_s")) if ended else None,
            "outcome": _word(record.get("outcome")) if ended else "running", "error": error, "error_class": error_class(error),
            **_usage(record.get("usage")), "exit_code": exit_code if isinstance(exit_code, int) else None}

    # ----- turns -----

    def _turn(self, record: dict, place: dict) -> None:
        self.state["turns"][record["id"]] = record["time"]
        self._row("turns", self._turn_row(record, place, record["time"], ended=False))

    def _turn_end(self, record: dict, place: dict) -> None:
        started = self.state["turns"].pop(record["id"], None)
        self._row("turns", self._turn_row(record, place, started, ended=True))

    def _turn_row(self, record: dict, place: dict, started: str | None, ended: bool) -> dict:
        return {"run_id": self.run.id, **place, "turn": record["turn"],
                "started": _utc(started) or _utc(record["time"]), "ended": _utc(record["time"]) if ended else None,
                "duration_s": _seconds(started, record["time"]) if ended else None,
                "status": _word(record.get("status")) if ended else "running",
                "message": self._text(record.get("message")) if ended else "",
                **_usage(record.get("usage") if ended else None)}

    # ----- tool calls -----

    def _tool_call(self, record: dict, place: dict) -> None:
        if record["id"] in self.state["calls"]:  # the harness gave more of the arguments: the call started before
            return
        self.state["calls"][record["id"]] = [_word(record.get("tool")), record["time"]]
        self._row("tool_calls", self._call_row(record, place, _word(record.get("tool")), record["time"], None, None))

    def _tool_result(self, record: dict, place: dict) -> None:
        tool, started = self.state["calls"].pop(record["id"], ["", None])
        took = _seconds(started, record["time"])
        self._row("tool_calls", self._call_row(record, place, tool, started or record["time"],
                                               None if took is None else round(took * 1000, 3), bool(record.get("is_error"))))

    def _call_row(self, record: dict, place: dict, tool: str, started: str, took_ms: float | None,
                  is_error: bool | None) -> dict:
        attempt, call = CALL.fullmatch(record["id"]).groups()
        harness, model = self.state["attempts"].get(attempt, ["", ""])
        return {"run_id": self.run.id, "workflow": self.workflow, **place, "turn": record["turn"], "call_id": call,
                "tool": tool, "harness": harness, "model": model, "started": _utc(started), "duration_ms": took_ms,
                "is_error": is_error}


@functools.cache
def keys(table: str) -> tuple[str, ...]:
    """The columns of a table's natural key, its ORDER BY in clickhouse.sql."""
    return tuple(schema()[table]["key"].split(", "))


def _place(record: dict, attempt: bool = True) -> dict | None:
    """Where a record is: node (with its path in a sub-workflow), pass, item, and attempt. None when it doesn't say."""
    node, pass_no, item = record.get("node"), record.get("pass"), record.get("index", NO_ITEM)
    numbers = (pass_no, item, record.get("attempt")) if attempt else (pass_no, item)
    if not isinstance(node, str) or not all(isinstance(number, int) for number in numbers):
        return None
    place = {"node": _word(record.get("where")) + node, "pass": pass_no, "item": item}
    return {**place, "attempt": record["attempt"]} if attempt else place


def _usage(used: Any) -> dict:
    """The tokens and cost in a record's `usage`: 0 for each that it doesn't hold."""
    used = used if isinstance(used, dict) else {}
    return {**{name: max(int(_number(used.get(name)) or 0), 0) for name in COUNTS},
            "cost_usd": float(_number(used.get("cost_usd")) or 0.0)}


def _word(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _number(value: Any) -> float | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _utc(text: Any) -> str | None:
    """A time of the run folder as ClickHouse reads a DateTime64(3, 'UTC'), or None."""
    try:
        moment = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def _seconds(start: Any, end: Any) -> float | None:
    try:
        return round((datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds(), 3)
    except (TypeError, ValueError):
        return None


# ---------- events.jsonl ----------


def _events(run: Run, events: list[dict | None]) -> dict[str, list[dict]]:
    """The rows that the events of a run folder without a trace give:

    - runs: the run's start is its first event, and its end the `run_finished` event, or else its last
      event when status.json doesn't say `running`. Its tokens and cost are those of its passes.
    - attempts: each attempt that an event names, by node, pass, item and attempt. It starts at its
      first event, and ends at its `session_ended`, its failure, or else the end of its pass, when the
      node has no items. It failed with `attempt_failed` (timed_out, when the node's timeout ended it),
      was unavailable with `harness_unavailable`, and succeeded when its pass finished without either.
      Its tokens and cost are those of its last `agent_usage`. A pass without `agent_usage` gives its own
      (`node_finished`) to its last attempt, when the node has no items.
    - tool_calls: each `agent_tool`, as the call e1, e2, ... of its attempt, in turn 0, with no duration.
    - routes: each `route`, in the pass of its node that started last.
    """
    info = run.info()
    workflow = str(info.get("workflow") or "")
    attempts: dict[tuple, dict] = {}  # (node, pass, item, attempt) -> what its events say
    passes: dict[tuple, dict] = {}  # (node, pass) -> the last node_finished or node_failed
    started: dict[str, int] = {}  # node -> its pass that started last
    routes, first, last, finished = [], None, None, None
    for event in events:
        time = (event or {}).get("time")
        if _utc(time) is None:
            continue
        first, last = first or time, time
        kind, node, pass_no = event.get("event"), event.get("node"), event.get("pass")
        if kind == "run_finished":
            finished = event
        if not isinstance(node, str):
            continue
        if kind == "node_started" and isinstance(pass_no, int):
            started[node] = pass_no
        elif kind in ("node_finished", "node_failed") and isinstance(pass_no, int):
            passes[(node, pass_no)] = event
        elif kind == "route" and node in started:
            taken, to = event.get("route"), event.get("to")
            routes.append({"run_id": run.id, "node": node, "pass": started[node], "time": _utc(time),
                           "taken": taken if isinstance(taken, int) else -1,
                           "to": [str(target) for target in to] if isinstance(to, list) else []})
        item, number = event.get("index", NO_ITEM), event.get("attempt")
        if all(isinstance(value, int) for value in (pass_no, item, number)):
            _seen(attempts.setdefault((node, pass_no, item, number), {"started": time, "tools": []}), kind, time, event)

    rows: dict[str, list[dict]] = {"runs": [], "attempts": [], "tool_calls": [], "routes": routes}
    for (node, pass_no, item, number), seen in attempts.items():
        ended_pass = passes.get((node, pass_no))
        siblings = [key for key in attempts if key[:2] == (node, pass_no)]
        last_one = item == NO_ITEM and number == max(key[3] for key in siblings)
        harness = seen.get("harness", "")
        used = _usage(seen.get("usage"))
        if harness and last_one and ended_pass and not any(attempts[key].get("usage") for key in siblings):
            used = _usage(ended_pass)
        ended = seen.get("ended") or (ended_pass["time"] if ended_pass and last_one else None)
        error = seen.get("error", "")
        session = seen.get("session", "")
        succeeded = ended_pass is not None and ended_pass.get("event") == "node_finished"
        rows["attempts"].append({
            "run_id": run.id, "workflow": workflow, "node": node, "pass": pass_no, "item": item, "attempt": number,
            "kind": "agent" if harness else "", "harness": harness, "model": seen.get("model", ""),
            "effort": seen.get("effort", ""), "access": "", "session_id": "",
            "session_start": "load" if session == "fork" and harness == "cursor" else session,
            "started": _utc(seen["started"]), "ended": _utc(ended), "duration_s": _seconds(seen["started"], ended),
            "outcome": seen.get("outcome") or ("succeeded" if succeeded else ""), "error": error,
            "error_class": error_class(error), **used, "exit_code": None})
        for count, (time, tool) in enumerate(seen["tools"], 1):
            rows["tool_calls"].append({
                "run_id": run.id, "workflow": workflow, "node": node, "pass": pass_no, "item": item, "attempt": number,
                "turn": 0, "call_id": f"e{count}", "tool": tool, "harness": harness, "model": seen.get("model", ""),
                "started": _utc(time), "duration_ms": None, "is_error": None})
    if first is not None:
        status = _word((finished or {}).get("status")) or _word(run.status().get("status"))
        ended = finished["time"] if finished else last if status != "running" else None
        spent = [_usage(event) for (node, _), event in passes.items() if "/" not in node]  # a sub-workflow's is in its node's
        rows["runs"].append({
            "run_id": run.id, "repo_key": _repo_key(run.dir), "workflow": workflow, "status": status,
            "dry_run": bool(info.get("dry_run")), "started": _utc(first), "ended": _utc(ended),
            "duration_s": _seconds(first, ended), "cost_usd": round(sum(used["cost_usd"] for used in spent), 6),
            "tokens": sum(used["tokens"] for used in spent), "source": "events"})
    return rows


def _seen(seen: dict, kind: str, time: str, event: dict) -> None:
    """Add what an event says about its attempt to `seen`."""
    if isinstance(event.get("harness"), str) and event["harness"]:
        seen["harness"] = event["harness"]
    if kind in ("agent_model", "session_started"):
        seen["model"] = _word(event.get("model")) or seen.get("model", "")
        seen["effort"] = _word(event.get("effort")) or seen.get("effort", "")
    if kind == "session_started":
        seen.setdefault("session", "fresh")
    elif kind == "session_continued":
        seen["session"] = "fork"
    elif kind == "session_fresh":
        seen["session"] = "fresh"
    elif kind == "session_ended":
        seen["ended"] = time
    elif kind == "agent_usage":
        seen["usage"] = event
    elif kind == "agent_tool":
        seen["tools"].append((time, _word(event.get("tool"))))
    elif kind in ("attempt_failed", "harness_unavailable"):
        seen["ended"], seen["error"] = time, _word(event.get("error"))
        timed_out = seen["error"].startswith("timed out after ")
        seen["outcome"] = "unavailable" if kind == "harness_unavailable" else "timed_out" if timed_out else "failed"
