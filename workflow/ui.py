"""UI: `wf ui`, a web page on this machine that shows runs as they happen.

    wf ui [--port N] [--all-repos] [--open] [--runs-dir DIR]

It serves a small page (ui/page.html, page.js and page.css: plain files, with no
build step) and the JSON that the page reads, on 127.0.0.1 only. The page lists
the runs of this repo, or with --all-repos the runs of each repo in the runs folder,
newest first. For one run it draws the workflow graph (diagram.layout), with
each node in the color of its state and each loop's count. It also shows the
progress estimate (progress.py). It shows the numbers of each step as `wf
report` counts them, also of the steps in sub-workflows, each with its state. A
step that runs adds what its pass has done so far. Then it shows the agent
sessions that run now (observe.py), the event stream, and the transcript and
instruction of each attempt. It asks for new data on its own: the run every 2
seconds, its events every second, and the list of runs every 5 seconds. For
each run, the server reads only the events that came since it last read them.
It looks only at the files of attempts that can still change (_View).

    GET /                          the page
    GET /api/runs                  the runs, newest first, with their state and progress
    GET /api/run?id=ID             one run: state, progress, graph, states, metrics, sessions, attempts
    GET /api/events?id=ID&after=N  the events after byte N of events.jsonl (the last EVENTS_BACK without N)
    GET /api/file?id=ID&path=P     a transcript, instruction, output or session file of the run

The server only reads. It answers a request only when its Host header names
the address of the server. So a web page cannot read it through a DNS name
that points to 127.0.0.1. Each server makes a new random key, and the address
that it prints holds the key after `#`. The page sends the key with each
request for data (X-WF-Token), and the server refuses a request without it. So
another program or user on the machine cannot read the transcripts through
it. Its Content-Security-Policy lets the page run only its own script, and the
page shows what runs wrote as text, never as HTML.
"""

import errno
import hmac
import json
import os
import re
import secrets
import stat
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import diagram, observe, operate, progress, report, runs, spec
from .runs import Run

HOST = "127.0.0.1"
EVENTS_BACK = 300
FILE_BYTES = 1_000_000  # a longer file is sent from this many bytes before its end
PAGE = Path(__file__).parent / "ui"
STATIC = {"/": ("page.html", "text/html"), "/page.js": ("page.js", "text/javascript"), "/page.css": ("page.css", "text/css")}
FILES = re.compile(r"instruction\.md|output\.json|session\.json|attempt-\d+\.log")
POLICY = ("default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; "
          "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
MOVING = ("running", "waiting")  # the runs whose progress the list shows


class Source:
    """The runs the page shows: this repo's, or (every_repo) each repo's in the runs folder."""

    def __init__(self, repo: Path, runs_dir: Path | None = None, every_repo: bool = False):
        self.repo, self.runs_dir, self.every_repo = repo, runs_dir, every_repo
        self.folder = runs.runs_folder(repo, runs_dir)
        if every_repo:
            self.folder = self.folder.parent

    def runs(self) -> dict[str, Run]:
        """By id, newest first. With every_repo, an id is <repo key>/<run id>."""
        if not self.every_repo:
            return {run.id: run for run in reversed(runs.all_runs(self.repo, self.runs_dir))}
        found = []
        for repo in (p for p in _folders(self.folder) if not p.name.startswith(".")):
            for folder in _folders(repo):
                manifest = folder / "workflow" / "manifest.json"
                if folder.name.startswith(".") or not manifest.is_file():
                    continue
                try:
                    found.append((manifest.stat().st_mtime, f"{repo.name}/{folder.name}", Run.at(folder)))
                except (ValueError, OSError):  # run.json doesn't name its repo: wf can't work with it either
                    continue
        return {key: run for _, key, run in sorted(found, key=lambda f: (f[0], f[1]), reverse=True)}

    def get(self, key: str) -> Run | None:
        """The run with this id, found by its folder, not by listing them all."""
        if not self.every_repo:
            try:
                return runs.find(self.repo, key, self.runs_dir)
            except FileNotFoundError:
                return None
        repo, _, run_id = key.partition("/")
        if not all(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", part) and ".." not in part for part in (repo, run_id)):
            return None
        folder = self.folder / repo / run_id
        if not (folder / "workflow" / "manifest.json").is_file():
            return None
        try:
            return Run.at(folder)
        except (ValueError, OSError):
            return None


def _folders(folder: Path) -> list[Path]:
    try:
        return [p for p in folder.iterdir() if p.is_dir()]
    except OSError:
        return []


# ---------- what the page reads ----------


def run_list(source: Source) -> dict:
    found = []
    for key, run in source.runs().items():
        state = operate.state(run)
        item = {"id": key, "run": run.id, "workflow": state.get("workflow"), "status": state.get("status"),
                "dry_run": state.get("dry_run"), "updated": state.get("updated")}
        if state.get("status") in MOVING:
            try:
                with _viewing(run) as view:
                    estimate = view.progress(state["status"])
            except Exception:  # a damaged run: the list goes on without its progress, and its own page says why
                estimate = None
            if estimate:
                item["progress"] = {k: estimate[k] for k in ("step", "steps", "percent", "left_s")}
        found.append(item)
    return {"folder": str(source.folder), "every_repo": source.every_repo, "runs": found}


def run_detail(run: Run) -> dict:
    state = operate.state(run)
    status = state.get("status")
    with _viewing(run) as view:  # after the state: the events go at least as far as it does
        return {"state": state, "progress": view.progress(status), "graph": view.graph, "states": view.states(status),
                "metrics": view.metrics(status), "sessions": view.open.live(), "attempts": view.attempts.listed()}


def attempts(run: Run) -> list[dict]:
    """Each attempt's log in the run folder, as the page lists them (_Attempts)."""
    with _viewing(run) as view:
        return view.attempts.listed()


def events_after(run: Run, after: int | None) -> dict:
    """The events after byte `after` of events.jsonl, as lines. Without `after`, the last EVENTS_BACK."""
    found, offset = observe.read_events(run.dir / "events.jsonl", after or 0)
    lines = [{"time": e.get("time"), "event": e.get("event"), "text": text} for e in found
             if (text := observe.line(e)) is not None]
    return {"next": offset, "events": lines if after is not None else lines[-EVENTS_BACK:]}


class _View:
    """What wf ui knows of a run, from the part of its events.jsonl that it has read. That is the
    numbers (report.Tally), the progress and the state of each step (progress.Tracker), the open
    agent sessions (observe.Open) and the attempts (_Attempts). Each refresh reads only the events
    that came since the last one, and looks only at the files of attempts that can still change.
    So it costs no more for a long run, or for each tab that shows the run."""

    def __init__(self, run: Run):
        self.run, self.lock = run, threading.Lock()
        self.tally: report.Tally | None = None  # None until the first refresh
        self.asked = time.monotonic()  # when a page last asked for the run

    def _start(self) -> None:
        run = self.run
        self.inode, self.offset, self.tail = None, 0, b""  # events.jsonl, read up to `offset`: the last line read is `tail`
        self.last: str | None = None  # the time of the last event
        self.again = False  # the workflow files couldn't be read, but a later read may: start over at the next refresh
        try:
            self.workflow = spec.load(run.workflow_file)
            self.graph = diagram.layout(self.workflow)
            self.tracker = progress.Tracker(self.workflow, run.dir)
        except (spec.SpecError, OSError, KeyError, ValueError) as error:
            self.workflow = self.graph = self.tracker = None
            self.again = progress.read_may_pass(error)
        self.tally = report.Tally(run)
        self.open = observe.Open(run)
        self.attempts = _Attempts(run)

    def refresh(self) -> None:
        new = self._read() if self.tally is not None and not self.again else None
        if new is None:  # the first refresh, or events.jsonl doesn't go on from what was read: start over
            self._start()
            new = self._read() or []
        for event in new:
            self.last = event.get("time", self.last)
            self.tally.add(event)
            self.open.add(event)
            self.attempts.add(event)
            if self.tracker is not None:
                self.tracker.add(event)
        self.attempts.check()

    def _read(self) -> list[dict] | None:
        """The events after those read before. None when events.jsonl is another file now, or `wf retry`
        cut it back and it grew again."""
        try:
            with (self.run.dir / "events.jsonl").open("rb") as file:
                inode = os.fstat(file.fileno()).st_ino
                file.seek(self.offset - len(self.tail))
                data = file.read()
        except FileNotFoundError:
            return None if self.offset else []
        except OSError:  # try again at the next refresh
            return []
        if self.offset and (inode != self.inode or not data.startswith(self.tail)):
            return None
        self.inode = inode
        whole = data[len(self.tail):data.rfind(b"\n") + 1]  # a line still being written waits for the next refresh
        if whole:
            self.offset += len(whole)
            self.tail = whole[whole.rfind(b"\n", 0, -1) + 1:]
        return runs.parse_events(whole)

    def progress(self, status: str | None) -> dict | None:
        if self.tracker is None:
            return None
        try:
            return self.tracker.estimate(status, progress.learned(self.run, self.workflow, status))
        except (spec.SpecError, OSError, KeyError, ValueError):
            return None

    def states(self, status: str | None) -> dict[str, str]:
        return self.tracker.states(status) if self.tracker is not None else {}

    def metrics(self, status: str | None) -> dict:
        """The numbers of `wf report`, with what each pass that runs has done so far. That is until now,
        or until the last event while the run doesn't move."""
        now = time.time() if status in ("running", None) or self.last is None else observe.timestamp(self.last)
        found = self.tally.report(now)
        return {"run": found["runs"][0], "nodes": found["nodes"], "notes": found["notes"]}


_VIEWS: dict[Path, _View] = {}  # run folder -> its _View, the one asked for last at the end
_VIEWS_MOST = 64  # more stay while a page asks for them: the list asks for each run that moves
_VIEWS_KEEP_SECONDS = 60
_VIEWING = threading.Lock()  # wf ui asks from several threads


@contextmanager
def _viewing(run: Run) -> Iterator[_View]:
    """The run's view, up to date, for this thread alone. A view that fails is made again at the next request."""
    with _VIEWING:
        view = _VIEWS.pop(run.dir, None) or _View(run)
        view.asked = time.monotonic()
        _VIEWS[run.dir] = view
        while len(_VIEWS) > _VIEWS_MOST and view.asked - next(iter(_VIEWS.values())).asked > _VIEWS_KEEP_SECONDS:
            del _VIEWS[next(iter(_VIEWS))]
    with view.lock:
        try:
            view.refresh()
            yield view
        except BaseException:
            with _VIEWING:
                if _VIEWS.get(run.dir) is view:
                    del _VIEWS[run.dir]
            raise


@dataclass
class _Attempt:
    node: str  # review[1], or build[2]/implement inside a sub-workflow
    pass_no: int
    number: int
    folder: str  # its pass folder, or its item's, in the run folder
    session: dict = field(default_factory=dict)  # its session_started event
    files: dict[str, str] = field(default_factory=dict)  # log, instruction, output -> the file, once it is there
    ended: bool = False

    def paths(self) -> dict[str, str]:
        return {"log": self.session.get("log") or f"{self.folder}/attempt-{self.number}.log",
                "instruction": f"{self.folder}/instruction.md", "output": f"{self.folder}/output.json"}


class _Attempts:
    """The attempts of a run, sub-workflows' too, from its events in turn, with the files of each one.

    A node writes its files before the event that ends its attempt, item or pass. So check() looks for
    the files of an attempt at each refresh while it runs, and once more after the event that ends it.
    A finished job, with its output.json, ends it too. A check that fails for another reason than a
    file that isn't there goes again at the next refresh."""

    def __init__(self, run: Run):
        self.run = run
        self.all: dict[tuple, _Attempt] = {}  # (folder, attempt) -> the attempt, in the order they started
        self.open: dict[tuple, _Attempt] = {}  # the attempts whose files can still change
        self.places: dict[str, tuple[int, dict]] = {}  # node (build[2]/implement) -> its pass, and the attempt of each item

    def add(self, event: dict) -> None:
        node, kind, number = event.get("node"), event.get("event"), event.get("attempt")
        if not isinstance(node, str):
            return
        index = event.get("index") if isinstance(event.get("index"), int) else None
        pass_no = event.get("pass") if isinstance(event.get("pass"), int) else self.places.get(node, (None,))[0]
        if kind == "node_started" and isinstance(event.get("pass"), int):
            self.places[node] = (pass_no, {})
            for item in range(event["items"]) if isinstance(event.get("items"), int) else [None]:
                self._started(node, pass_no, item, 1)
        elif kind in ("attempt_started", "session_started") and isinstance(number, int):
            if kind == "attempt_started" and node in self.places:
                self.places[node][1][index] = number
            attempt = self._started(node, pass_no, index, number)
            if attempt is not None and kind == "session_started":
                attempt.session = event
        elif kind in ("attempt_failed", "harness_unavailable") and isinstance(number, int):
            self._end(self._folder(node, pass_no, index), number)
        elif kind in ("node_finished", "node_failed", "item_failed"):
            self._end(self._folder(node, pass_no, index if kind == "item_failed" else None))

    def _started(self, node: str, pass_no: int | None, index: int | None, number: int) -> _Attempt | None:
        folder = self._folder(node, pass_no, index)
        if folder is None:
            return None
        key = (folder, number)  # a sub-workflow attempt that starts over has its steps' passes and attempts again
        if key not in self.all:
            self.all[key] = _Attempt(node + (f"[{index}]" if index is not None else ""), pass_no, number, folder)
        self.open[key] = attempt = self.all[key]
        attempt.ended = False
        return attempt

    def _folder(self, node: str, pass_no: int | None, index: int | None) -> str | None:
        """nodes/<node>/<pass>, and /<index> for an item, inside the folder of each workflow node's
        attempt around it. None when the events don't say where that is."""
        parts = node.split("/")
        folder = "nodes"
        for depth, part in enumerate(parts[:-1]):
            name, _, item = part.partition("[")
            item = int(item.rstrip("]")) if item else None
            pass_around, attempts = self.places.get("/".join(parts[:depth] + [name]), (None, {}))
            if pass_around is None:
                return None
            folder += f"/{name}/{pass_around}" + (f"/{item}" if item is not None else "") + f"/attempt-{attempts.get(item, 1)}/nodes"
        if pass_no is None:
            return None
        return f"{folder}/{parts[-1]}/{pass_no}" + (f"/{index}" if index is not None else "")

    def _end(self, folder: str | None, number: int | None = None) -> None:
        """The attempt `number` in the folder has ended, and each attempt in its sub-workflow. Without a
        number, each attempt in the folder, and in the folders under it."""
        if folder is None:
            return
        inside = f"{folder}/attempt-{number}/" if number is not None else f"{folder}/"
        for attempt in self.open.values():
            if (attempt.folder == folder and number in (None, attempt.number)) or attempt.folder.startswith(inside):
                attempt.ended = True

    def check(self) -> None:
        for key, attempt in list(self.open.items()):
            sure = True
            for name, path in attempt.paths().items():
                if name not in attempt.files:
                    there = _is_file(self.run.dir / path)
                    if there:
                        attempt.files[name] = path
                    sure = sure and there is not None
            if sure and (attempt.ended or "output" in attempt.files):
                del self.open[key]

    def listed(self) -> list[dict]:
        """The attempts, oldest first, each with its agent session's harness, model and start. An attempt
        is there once its transcript is, or its agent session started."""
        return [{"node": a.node, "pass": a.pass_no, "attempt": a.number, "log": a.files.get("log"),
                 "instruction": a.files.get("instruction"), "output": a.files.get("output"),
                 "harness": a.session.get("harness"), "model": a.session.get("model"), "started": a.session.get("time")}
                for a in self.all.values() if "log" in a.files or a.session]


def _is_file(path: Path) -> bool | None:
    """None when the check failed, for another reason than a file that isn't there."""
    try:
        return stat.S_ISREG(path.stat().st_mode)
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError:
        return None


def run_file(run: Run, relative: str) -> dict | None:
    """A file of the run's passes, or None if `relative` names no such file. At most FILE_BYTES,
    from the end, starting at a whole line."""
    base = run.dir.resolve()
    target = (base / relative).resolve()
    if not relative.startswith("nodes/") or not target.is_relative_to(base / "nodes") or not FILES.fullmatch(target.name):
        return None
    try:
        with target.open("rb") as file:
            size = file.seek(0, 2)
            file.seek(max(size - FILE_BYTES, 0))
            data = file.read(FILE_BYTES)
    except OSError:
        return None
    cut = max(size - FILE_BYTES, 0)
    if cut:
        start = data.find(b"\n") + 1
        cut, data = cut + start, data[start:]
    return {"path": relative, "text": data.decode("utf-8", errors="replace"), "cut_bytes": cut}


# ---------- the server ----------


class Server(ThreadingHTTPServer):
    daemon_threads = True  # Ctrl-C doesn't wait for a page that is still loading

    def __init__(self, source: Source, port: int):
        super().__init__((HOST, port), _Handler)
        self.source = source
        self.hosts = {f"{HOST}:{self.port}", f"localhost:{self.port}"}
        self.token = secrets.token_urlsafe(24)

    @property
    def port(self) -> int:
        return self.server_address[1]

    @property
    def url(self) -> str:
        """The page's address, with its key after `#`, which the browser never sends."""
        return f"http://{HOST}:{self.port}/#token={self.token}"


class _Handler(BaseHTTPRequestHandler):
    server: Server

    def do_GET(self) -> None:
        if self.headers.get("Host") not in self.server.hosts:
            return self._send(403, b"wf ui answers only requests for its own address\n", "text/plain")
        url = urlsplit(self.path)
        query = {key: values[0] for key, values in parse_qs(url.query).items()}
        try:
            if url.path in STATIC:  # the page itself holds nothing of the runs
                name, kind = STATIC[url.path]
                return self._send(200, (PAGE / name).read_bytes(), kind)
            if not hmac.compare_digest(self.headers.get("X-WF-Token", "").encode(), self.server.token.encode()):
                return self._json({"error": "this page needs the key in the address that wf ui printed"}, 401)
            if url.path == "/api/runs":
                return self._json(run_list(self.server.source))
            run = self.server.source.get(query.get("id", "")) if url.path.startswith("/api/") else None
            if url.path == "/api/run" and run:
                return self._json(run_detail(run))
            if url.path == "/api/events" and run:
                after = query.get("after", "")
                return self._json(events_after(run, int(after) if after.isdigit() else None))
            if url.path == "/api/file" and run and (found := run_file(run, query.get("path", ""))):
                return self._json(found)
            return self._json({"error": "not found"}, 404)
        except Exception as error:  # a damaged run folder: say so on the page, and keep serving
            return self._json({"error": f"{type(error).__name__}: {error}"}, 500)

    def _json(self, value, status: int = 200) -> None:
        self._send(status, json.dumps(value, ensure_ascii=False, default=str).encode(), "application/json")

    def _send(self, status: int, body: bytes, kind: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", f"{kind}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", POLICY)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:
        pass  # the terminal shows the address, not each request


def serve(source: Source, port: int, say: Callable[[str], None] = lambda text: print(text, flush=True),
          opened: Callable[[str], object] | None = None) -> int:
    """Serve the page on `port` (ui.port in config/) until Ctrl-C. A port that is taken gives way to a free one."""
    try:
        server = Server(source, port)
    except OSError as error:
        if error.errno != errno.EADDRINUSE or port == 0:
            raise
        server = Server(source, 0)
        say(f"Port {port} is in use, so wf ui uses port {server.port}.")
    say(f"wf ui: {server.url} shows the runs in {source.folder}. Ctrl-C stops it.")
    try:
        if opened:
            opened(server.url)
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
