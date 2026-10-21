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
that differs is a SchemaMismatch, and the export writes nothing. It doesn't check metric_points,
which nothing writes yet.

An export of a run folder reads its trace.jsonl from the byte offset where the last export stopped.
A record that starts or ends something gives a row. An `attempt` gives a row with the outcome
`running`, and its `attempt_end` gives the whole row again. Turns and tool calls go the same way, and
a tool call keeps the start of its first record and the tool of its last. Each table is a
ReplacingMergeTree on the natural key of its rows (run, node, pass, item, attempt, ...). So the later
row replaces the earlier one. A node inside a sub-workflow has, in front of its id, the trace id of
the attempt that runs the sub-workflow, as in build#1[3].a1/implement. So each pass and each attempt
of a workflow node has rows of its own. A job that isn't an item has the item -1.

export.json in the run folder keeps the offset and the identity of the tables that took the rows. It
also keeps what the next export needs of the records before the offset. These are the attempts, turns
and tool calls that haven't ended, and the times and spending of the run. A line that wf still writes
waits for the next export. The duration of a run is the time that wf drove it: from each `run` record
to its `run_stopped`. Its tokens and cost are the sums of its rows in `attempts`.

The process that drives a run holds the run's lock. When the trace says that a run runs, but no
process holds its lock, the process stopped without its `run_stopped`. It crashed or something killed
it, as `wf status` says. The run is then `stopped` at its last record, and each attempt and turn that
it left running is `cancelled` there. When `wf retry` runs them again, their new rows replace these.

So exporting twice changes nothing: the second export finds nothing new and writes nothing. When
ClickHouse makes the tables again, as after `docker compose down -v`, the identity changes. Then the
next export of each run starts again from the start of its file. So does the export of a run whose
export.json is gone. Each row that it writes replaces an equal one.

A run folder of a wf from before the trace has only events.jsonl. The export reads that file whole,
once, and gets less from it (_events). It gets the run, the attempts that events name, and their
failures. It also gets the names and times of the tool calls, the routes, and the tokens and cost of
each pass. It gets no turns, no durations or results of tool calls, and no session ids. When today's
wf drives such a run again, its trace starts with `wf retry` or `wf continue`. Then the events before
the trace give the rows of the run before it.

The errors have classes, so that a caller can tell what to do:

    StoreDown          ClickHouse doesn't answer, answers as another program, or refuses a request for a
                       reason other than its tables
    SchemaMismatch     a table differs from clickhouse.sql, or ClickHouse refuses rows for their table
    UnreadableFolder   wf can't read the run folder, or can't keep its offset in export.json

After any of them, export.json stays as it was, so the next export writes the same rows again. One
export of a run folder at a time holds the folder's export.lock, from the read of export.json to its
write. The export writes nothing else in the run folder, and it never holds the run's lock. It takes
that lock only for a moment, as `wf status` does, to see whether a process drives a run that runs.
So no error of the export can fail a run or make it slower.
"""

import fcntl
import functools
import hashlib
import http.client
import json
import os
import re
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlencode, urlsplit

from . import runs, settings, trace_view
from .runs import Run
from .trace import FILE as TRACE

SCHEMA = Path(__file__).with_name("clickhouse.sql")
PROGRESS = "export.json"
LOCK = "export.lock"
EVENTS = "events.jsonl"
URL_ENV = "WF_CLICKHOUSE_URL"
TELEMETRY = ("clickhouse_url",)  # the settings under `telemetry:` in config.yaml
USER = KEY = "wf"  # the user and password when the URL names no user: those of wf's metrics stack
TIMEOUT_S = 30
NO_ITEM = -1
WRITTEN = ("runs", "attempts", "turns", "tool_calls", "routes")  # the tables that the export writes
COUNTS = ("tokens", "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "reasoning_tokens")
CALL = re.compile(r"(.+)\.(t\d+\.c\d+)")  # a tool call's trace id: its attempt's id, and its own id in the attempt
ITEM_FAILED = re.compile(r"item (\d+): (.*)", re.S)  # node_failed when an item failed (on_item_error: fail)
# Errors after the last attempt of a job succeeded: in the routes, in the write of output.json, or in the commit.
# The commit runs git status, add, diff and commit, and can fail to sign. The reset before an attempt is a part
# of that attempt, so its errors aren't in the list.
AFTER_SUCCESS = ("routes: ", "can't write output.json: ", "workspace: git status ", "workspace: git add ",
                 "workspace: git diff ", "workspace: git commit ", "workspace: can't sign the commit ")
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
    """ClickHouse doesn't answer, answers as another program, or refuses a request for a reason other
    than its tables."""


class SchemaMismatch(ExportError):
    """A table of ClickHouse differs from clickhouse.sql, or ClickHouse refuses rows for their table."""


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
        for row in self._select("SELECT table, name, type FROM system.columns WHERE database = 'wf'", "table", "name", "type"):
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
        query = "SELECT name, toString(uuid) AS uuid, engine, sorting_key FROM system.tables WHERE database = 'wf'"
        return {row["name"]: row for row in self._select(query, "name", "uuid", "engine", "sorting_key")}

    def _select(self, query: str, *names: str) -> list[dict]:
        """The rows that `query` gives, each with the columns `names`. Raises StoreDown for an answer that
        isn't ClickHouse's, such as a web page."""
        answer = self._post(f"{query} FORMAT JSONEachRow")
        try:
            rows = [json.loads(line) for line in answer.splitlines() if line.strip()]
        except ValueError:
            rows = None
        if rows is None or not all(isinstance(row, dict) and all(name in row for name in names) for row in rows):
            said = " ".join(answer.decode("utf-8", "replace").split())[:200]
            raise StoreDown(f"{self.where} answers, but not as ClickHouse does: {said}")
        return rows

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
    """How the tables that the export writes differ from `wanted` (schema()). A difference is a table or a
    column that is missing, a column of another type, another engine or another key."""
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


# ---------- the runs ----------


def runs_in(folder: Path, every_repo: bool = False) -> list[Run]:
    """The runs in the runs folder of a repo, oldest first. With every_repo, the runs in the folder of
    each repo in a runs folder, also oldest first and not repo by repo. A run is a folder with a
    workflow/manifest.json. A folder that wf can't look into is a run too, so that export() says that it
    is unreadable, and such folders come first. Raises OSError when wf can't list a folder of runs."""
    if not folder.is_dir():
        return []
    found = []
    for parent in _folders(folder) if every_repo else [folder]:
        for path in _folders(parent):
            made = _made(path)
            if made is not None:
                found.append((made, path))
    return [_run_at(path) for _, path in sorted(found)]


def _folders(folder: Path) -> list[Path]:
    return [path for path in folder.iterdir() if not path.name.startswith(".") and path.is_dir()]


def _made(folder: Path) -> float | None:
    """When wf made the run in `folder`: the time of its workflow/manifest.json. It is 0 when wf can't look into
    the folder, and None when the folder holds no run."""
    try:
        found = (folder / "workflow" / "manifest.json").stat()
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError:
        return 0.0
    return found.st_mtime if stat.S_ISREG(found.st_mode) else None


def _run_at(folder: Path) -> Run:
    """The run in `folder`, with the repo that its run.json names. The export doesn't need the repo, so a run
    whose repo wf can't tell has its folder as its repo."""
    try:
        return Run.at(folder)
    except ValueError:
        return Run(folder.name, folder, folder)


# ---------- a run folder ----------


def export(run: Run, store) -> dict[str, int]:
    """Export a run folder into `store` (a prepared Store) from where its last export stopped, and keep
    the new offset in its export.json. Returns the number of rows written, by table: none when nothing
    is new. Raises UnreadableFolder, StoreDown or SchemaMismatch, and export.json then stays as it was."""
    folder = run.dir
    path = _source(folder)
    with _locked(folder):
        kept = _kept(folder)
        same = kept.get("store") == store.identity and kept.get("file") == path.name
        offset, state = (kept["offset"], kept["state"]) if same else (0, _fresh())
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
                events = []
                for record, after in _lines(path, 0):
                    events.append(record)
                    end = after
                rows = _events(run, events)
            else:
                rows = {}
        except OSError as error:
            raise UnreadableFolder(f"can't read {path}: {error.strerror or error}") from None
        rows = {table: found for table, found in rows.items() if found}
        if same and end == offset and not rows:
            return {}
        for table, found in rows.items():
            store.insert(table, found)
        try:
            runs.write_json(folder / PROGRESS, {"store": store.identity, "file": path.name, "offset": end, "state": state})
        except OSError as error:
            raise UnreadableFolder(f"can't keep the offset in {folder / PROGRESS}: {error.strerror or error}") from None
        return {table: len(found) for table, found in rows.items()}


def _source(folder: Path) -> Path:
    """The run folder's trace.jsonl, or else its events.jsonl. Raises UnreadableFolder."""
    for path in (folder / TRACE, folder / EVENTS):
        if stat.S_ISREG(_mode(path)):
            return path
    raise UnreadableFolder(f"{folder} has neither {TRACE} nor {EVENTS}" if stat.S_ISDIR(_mode(folder)) else f"{folder} isn't a folder")


def _mode(path: Path) -> int:
    """The type and mode of `path`, or 0 when it isn't there. Raises UnreadableFolder when wf can't look."""
    try:
        return path.stat().st_mode
    except (FileNotFoundError, NotADirectoryError):
        return 0
    except OSError as error:
        raise UnreadableFolder(f"can't read {path}: {error.strerror or error}") from None


@contextmanager
def _locked(folder: Path) -> Iterator[None]:
    """Hold the run folder's export.lock, so that one export of the run at a time reads export.json,
    inserts its rows and writes export.json. Raises UnreadableFolder."""
    try:
        fd = os.open(folder / LOCK, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as error:
        raise UnreadableFolder(f"can't open {folder / LOCK}: {error.strerror or error}") from None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)  # another export of the run: wait until it ends
        except OSError as error:
            raise UnreadableFolder(f"can't lock {folder / LOCK}: {error.strerror or error}") from None
        yield
    finally:
        os.close(fd)


def _fresh() -> dict:
    """The state of an export from the start of a trace (_Trace)."""
    return {"started": None, "driving": None, "driven_s": 0.0, "status": "", "ended": None, "last": None, "told": "",
            "before": [0, 0.0, 0.0], "spent": {}, "attempts": {}, "turns": {}, "calls": {}}


def _kept(folder: Path) -> dict:
    """export.json: where the last export stopped. One that is missing or damaged counts as none."""
    try:
        kept = json.loads((folder / PROGRESS).read_text(encoding="utf-8"))
        state = kept["state"]
        whole = isinstance(kept["offset"], int) and set(state) == set(_fresh()) and all(
            isinstance(state[name], dict) for name in ("spent", "attempts", "turns", "calls")) and len(state["before"]) == 3
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
    records before them left, and export.json keeps it for the next export. Its attempts, turns and
    calls are the rows of those that haven't ended, by trace id."""

    def __init__(self, run: Run, state: dict):
        self.run, self.state = run, state
        info = run.info()
        self.workflow, self.dry_run = str(info.get("workflow") or ""), bool(info.get("dry_run"))
        self.found: dict[str, dict] = {table: {} for table in WRITTEN}  # table -> natural key -> its last row
        self.changed = False  # did a record change the run's row?

    def add(self, record: dict | None) -> None:
        kind, time = (record or {}).get("kind"), (record or {}).get("time")
        if _utc(time) is None:
            return
        if kind in ("run", "run_stopped"):
            self._run(kind == "run", time, record)
        elif kind == "routes":
            self._routes(record)
        elif kind in ("attempt", "attempt_end", "turn", "turn_end", "tool_call", "tool_result"):
            place = _place(record)
            whole = kind in ("attempt", "attempt_end") or isinstance(record.get("turn"), int)
            if place is not None and whole and (not kind.startswith("tool_") or CALL.fullmatch(record["id"])):
                getattr(self, f"_{kind}")(record, place)
        self.state["last"] = time

    def rows(self) -> dict[str, list[dict]]:
        """The rows of the records so far, and the run's row when it changed. A run that runs by its trace
        stopped when no process holds its lock: then what it left running shows as cancelled."""
        state = self.state
        status = state["status"]
        if state["driving"]:
            status = "running" if self.run.driven() else "stopped"
            if status == "stopped" and state["told"] != status:
                self._cancel()
        if state["started"] is not None and (self.changed or status != state["told"]):
            self._run_row(status)
        return {table: list(found.values()) for table, found in self.found.items()}

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
            if state["driving"]:  # the process before stopped without run_stopped
                self._stopped()
            if state["started"] is None and record.get("action") != "started":
                self._before(time)
            state["started"] = state["started"] or _utc(time)
            state["driving"], state["status"], state["ended"] = time, "running", None
        else:
            if state["driving"]:
                state["driven_s"] += _seconds(state["driving"], time) or 0.0
            state["driving"], state["status"], state["ended"] = None, _word(record.get("status")), _utc(time)
        self.changed = True

    def _stopped(self) -> None:
        """The process that drove the run stopped at the last record, without run_stopped."""
        state = self.state
        self._cancel()
        state["driven_s"] += _seconds(state["driving"], state["last"]) or 0.0
        state["driving"], state["status"], state["ended"] = None, "stopped", _utc(state["last"])
        state["attempts"], state["turns"], state["calls"] = {}, {}, {}

    def _cancel(self) -> None:
        """Rows for the attempts and turns that haven't ended, as cancelled at the last record."""
        last = self.state["last"]
        for table, word in (("attempts", "outcome"), ("turns", "status")):
            for row in self.state[table].values():
                self._row(table, {**row, word: "cancelled", "ended": _utc(last), "duration_s": _seconds(row["started"], last)})

    def _before(self, time: str) -> None:
        """The rows of the events that a wf from before the trace wrote. The trace starts at `time`, in the
        middle of the run. Today's `wf retry --model` writes its `override` events just before the trace,
        so they aren't events of the run before it."""
        path = self.run.dir / EVENTS
        if not path.is_file():
            return
        cut, events = _moment(time).replace(microsecond=0), []  # events have whole seconds
        for event, _ in _lines(path, 0):
            moment = _moment((event or {}).get("time"))
            if moment is not None and moment < cut and event.get("event") != "override":
                events.append(event)
        rows = _events(self.run, events)
        for table in ("attempts", "tool_calls", "routes"):
            for row in rows[table]:
                self._row(table, row)
        for row in rows["runs"]:
            self.state["started"] = row["started"]
            self.state["before"] = [row["tokens"], row["cost_usd"], _seconds(events[0]["time"], events[-1]["time"]) or 0.0]

    def _run_row(self, status: str) -> None:
        state = self.state
        tokens, cost, before_s = state["before"]
        for used, paid in state["spent"].values():
            tokens, cost = tokens + used, cost + paid
        if status == "running":
            ended, driven = None, None
        elif state["driving"]:  # stopped at its last record, without run_stopped
            ended, driven = _utc(state["last"]), state["driven_s"] + (_seconds(state["driving"], state["last"]) or 0.0)
        else:
            ended, driven = state["ended"], state["driven_s"]
        self._row("runs", {
            "run_id": self.run.id, "repo_key": _repo_key(self.run.dir), "workflow": self.workflow, "status": status,
            "dry_run": self.dry_run, "started": state["started"], "ended": ended,
            "duration_s": None if driven is None else round(before_s + driven, 3), "cost_usd": round(cost, 6),
            "tokens": tokens, "source": "trace"})
        state["told"] = status

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
        self._forget(record["id"])  # what an earlier session of the attempt left, when its process stopped
        row = self._attempt_row(record, place, ended=False)
        self.state["attempts"][record["id"]] = row
        self._row("attempts", row)

    def _attempt_end(self, record: dict, place: dict) -> None:
        self._forget(record["id"])
        row = self._attempt_row(record, place, ended=True)
        if "usage" in record:  # by the row's key, so that the run's numbers are the sums of its rows
            self.state["spent"][json.dumps([row[name] for name in keys("attempts")])] = [row["tokens"], row["cost_usd"]]
        self._row("attempts", row)
        self.changed = True

    def _forget(self, ident: str) -> None:
        """Drop the attempt `ident`, and what runs inside it, from what hasn't ended."""
        for unended in (self.state["attempts"], self.state["turns"], self.state["calls"]):
            for key in [key for key in unended if key == ident or key.startswith((f"{ident}.", f"{ident}/"))]:
                del unended[key]

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
        row = self._turn_row(record, place, record["time"], ended=False)
        self.state["turns"][record["id"]] = row
        self._row("turns", row)

    def _turn_end(self, record: dict, place: dict) -> None:
        opened = self.state["turns"].pop(record["id"], None)
        self._row("turns", self._turn_row(record, place, opened["started"] if opened else None, ended=True))

    def _turn_row(self, record: dict, place: dict, started: str | None, ended: bool) -> dict:
        return {"run_id": self.run.id, **place, "turn": record["turn"],
                "started": _utc(started) or _utc(record["time"]), "ended": _utc(record["time"]) if ended else None,
                "duration_s": _seconds(started, record["time"]) if ended else None,
                "status": _word(record.get("status")) if ended else "running",
                "message": self._text(record.get("message")) if ended else "",
                **_usage(record.get("usage") if ended else None)}

    # ----- tool calls -----

    def _tool_call(self, record: dict, place: dict) -> None:
        opened = self.state["calls"].get(record["id"])  # the harness reports the call again, as it learns more
        tool = _word(record.get("tool")) or (opened["tool"] if opened else "")
        row = self._call_row(record, place, tool, opened["started"] if opened else record["time"])
        self.state["calls"][record["id"]] = row
        self._row("tool_calls", row)

    def _tool_result(self, record: dict, place: dict) -> None:
        opened = self.state["calls"].pop(record["id"], None)
        took = _seconds(opened["started"], record["time"]) if opened else None  # a result without its call: no duration
        row = opened or self._call_row(record, place, "", record["time"])
        self._row("tool_calls", {**row, "duration_ms": None if took is None else round(took * 1000, 3),
                                 "is_error": bool(record.get("is_error"))})

    def _call_row(self, record: dict, place: dict, tool: str, started: str) -> dict:
        attempt, call = CALL.fullmatch(record["id"]).groups()
        running = self.state["attempts"].get(attempt) or {}
        return {"run_id": self.run.id, "workflow": self.workflow, **place, "turn": record["turn"], "call_id": call,
                "tool": tool, "harness": running.get("harness", ""), "model": running.get("model", ""),
                "started": _utc(started), "duration_ms": None, "is_error": None}


@functools.cache
def keys(table: str) -> tuple[str, ...]:
    """The columns of a table's natural key, its ORDER BY in clickhouse.sql."""
    return tuple(schema()[table]["key"].split(", "))


def _place(record: dict, attempt: bool = True) -> dict | None:
    """Where a record is: node, pass, item, and attempt. None when it doesn't say. Inside a sub-workflow,
    the node has in front of it the trace id of the attempt that runs the sub-workflow. The record's own
    id starts with that trace id."""
    node, pass_no, item, ident = record.get("node"), record.get("pass"), record.get("index", NO_ITEM), record.get("id")
    numbers = (pass_no, item, record.get("attempt")) if attempt else (pass_no, item)
    if not isinstance(node, str) or not isinstance(ident, str) or not all(isinstance(number, int) for number in numbers):
        return None
    inside = ident.rpartition("/")[0]
    place = {"node": f"{inside}/{node}" if inside else node, "pass": pass_no, "item": item}
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


def _moment(text: Any) -> datetime | None:
    """A time of the run folder, or of a row, which has no zone because it is in UTC."""
    try:
        moment = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _utc(text: Any) -> str | None:
    """A time as ClickHouse reads a DateTime64(3, 'UTC'), or None."""
    moment = _moment(text)
    return None if moment is None else moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def _seconds(start: Any, end: Any) -> float | None:
    first, last = _moment(start), _moment(end)
    return None if first is None or last is None else round((last - first).total_seconds(), 3)


# ---------- events.jsonl ----------


def _events(run: Run, events: list[dict | None]) -> dict[str, list[dict]]:
    """The rows that the events of a run folder without a trace give:

    - runs: the run's start is its first event, and its end the `run_finished` event, or else its last
      event when status.json doesn't say `running`. When it says `running` but no process holds the
      run's lock, the run is `stopped`. Its tokens and cost are those of its passes.
    - attempts: each attempt that an event names, by node, pass, item and attempt. It starts at its
      first event. It ends at its `session_ended` or its failure, or else, when the node has no items,
      at the end of its pass. It failed with `attempt_failed` (timed_out, when the node's timeout ended
      it), and was unavailable with `harness_unavailable`. An exception in an attempt writes neither.
      So when `item_failed` or `node_failed` says that a job failed, the last attempt of the job until
      then failed too. A failure in the routes, in the write of output.json or in the commit of its
      changes comes after the attempt. It doesn't fail the attempt, but a failure of the reset before an
      attempt does. Any other attempt succeeded when its pass ended. Its tokens and cost are those of
      its last `agent_usage`. When the node has no items, and the events of a pass have no `agent_usage`,
      the last agent attempt gets the pass's (`node_finished`).
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
            if kind == "node_failed":
                error = _word(event.get("error"))
                named = ITEM_FAILED.fullmatch(error)
                _failed(attempts, (node, pass_no, int(named[1]) if named else NO_ITEM), named[2] if named else error)
        elif kind == "item_failed" and node in started and isinstance(event.get("index"), int):
            _failed(attempts, (node, started[node], event["index"]), _word(event.get("error")))
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
        rows["attempts"].append({
            "run_id": run.id, "workflow": workflow, "node": node, "pass": pass_no, "item": item, "attempt": number,
            "kind": "agent" if harness else "", "harness": harness, "model": seen.get("model", ""),
            "effort": seen.get("effort", ""), "access": "", "session_id": "",
            "session_start": "load" if session == "fork" and harness == "cursor" else session,
            "started": _utc(seen["started"]), "ended": _utc(ended), "duration_s": _seconds(seen["started"], ended),
            "outcome": seen.get("outcome") or ("succeeded" if ended_pass else ""), "error": error,
            "error_class": error_class(error), **used, "exit_code": None})
        for count, (time, tool) in enumerate(seen["tools"], 1):
            rows["tool_calls"].append({
                "run_id": run.id, "workflow": workflow, "node": node, "pass": pass_no, "item": item, "attempt": number,
                "turn": 0, "call_id": f"e{count}", "tool": tool, "harness": harness, "model": seen.get("model", ""),
                "started": _utc(time), "duration_ms": None, "is_error": None})
    if first is not None:
        status = _word((finished or {}).get("status")) or _word(run.status().get("status"))
        if status == "running" and not run.driven():  # its wf process stopped without run_finished
            status = "stopped"
        ended = finished["time"] if finished else None if status == "running" else last
        spent = [_usage(event) for (node, _), event in passes.items() if "/" not in node]  # a sub-workflow's is in its node's
        rows["runs"].append({
            "run_id": run.id, "repo_key": _repo_key(run.dir), "workflow": workflow, "status": status,
            "dry_run": bool(info.get("dry_run")), "started": _utc(first), "ended": _utc(ended),
            "duration_s": _seconds(first, ended), "cost_usd": round(sum(used["cost_usd"] for used in spent), 6),
            "tokens": sum(used["tokens"] for used in spent), "source": "events"})
    return rows


def _failed(attempts: dict, job: tuple, error: str) -> None:
    """The job (node, pass, item) failed with `error`. So did its last attempt so far, when no event of that
    attempt says how it ended, and the job didn't fail after it succeeded."""
    numbers = [key[3] for key in attempts if key[:3] == job]
    if numbers and not error.startswith(AFTER_SUCCESS):
        seen = attempts[(*job, max(numbers))]
        if "outcome" not in seen:
            seen["outcome"], seen["error"] = "failed", error


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
