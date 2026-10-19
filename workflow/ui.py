"""UI: `wf ui`, a web page on this machine that shows runs as they happen.

    wf ui [--port N] [--all-repos] [--open] [--runs-dir DIR]

It serves a small page (ui/page.html, page.js and page.css: plain files, with no
build step) and the JSON that the page reads, on 127.0.0.1 only. The page lists
the runs of this repo, or with --all-repos the runs of each repo in the runs folder,
newest first. For one run it draws the workflow graph (diagram.layout), with
each node in the color of its state and each loop's count. It also shows the
progress estimate (progress.py), and each node's numbers as `wf report` counts
them. Then it shows the agent sessions that run now (observe.py), the event
stream, and the transcript and instruction of each attempt. It asks for new
data on its own: the run every 2 seconds, its events every second, and the
list of runs every 5 seconds.

    GET /                          the page
    GET /api/runs                  the runs, newest first, with their state and progress
    GET /api/run?id=ID             one run: state, progress, graph, metrics, sessions, attempts
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
import re
import secrets
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import diagram, observe, operate, progress, report, runs, spec
from .runs import Run

HOST = "127.0.0.1"
PORT = 8765
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
        if state.get("status") in MOVING and (estimate := progress.of(run, state["status"])):
            item["progress"] = {k: estimate[k] for k in ("step", "steps", "percent", "left_s")}
        found.append(item)
    return {"folder": str(source.folder), "every_repo": source.every_repo, "runs": found}


def run_detail(run: Run) -> dict:
    state = operate.state(run)
    try:
        graph = diagram.layout(spec.load(run.workflow_file))
    except (spec.SpecError, OSError, KeyError, ValueError):
        graph = None
    events = report.events(run)  # read once, for each part below
    found = report.build([run])
    return {"state": state, "progress": progress.of(run, state.get("status"), events), "graph": graph,
            "metrics": {"run": found["runs"][0], "nodes": found["nodes"], "notes": found["notes"]},
            "sessions": observe.sessions(run, events=events), "attempts": attempts(run, events)}


def events_after(run: Run, after: int | None) -> dict:
    """The events after byte `after` of events.jsonl, as lines. Without `after`, the last EVENTS_BACK."""
    found, offset = observe.read_events(run.dir / "events.jsonl", after or 0)
    lines = [{"time": e.get("time"), "event": e.get("event"), "text": text} for e in found
             if (text := observe.line(e)) is not None]
    return {"next": offset, "events": lines if after is not None else lines[-EVENTS_BACK:]}


def attempts(run: Run, events: list[dict] | None = None) -> list[dict]:
    """Each attempt's log in the run folder, sub-workflows' too, oldest first, with the harness and
    model of its agent session when its events name them. An agent session that has written no
    transcript yet is there too, with no log."""
    events = report.events(run) if events is None else events
    sessions = {e["log"]: e for e in events if e.get("event") == "session_started" and e.get("log")}
    found: list[dict] = []

    def nodes(folder: Path, prefix: str) -> None:
        for node in sorted(_folders(folder)):
            for pass_dir in sorted((p for p in _folders(node) if p.name.isdigit()), key=lambda p: int(p.name)):
                visit(pass_dir, prefix + node.name, int(pass_dir.name), None)

    def visit(folder: Path, node: str, pass_no: int, index: int | None) -> None:
        where = node + (f"[{index}]" if index is not None else "")
        for child in sorted(folder.iterdir()):
            if index is None and child.is_dir() and child.name.isdigit():
                visit(child, node, pass_no, int(child.name))
            elif match := re.fullmatch(r"attempt-(\d+)\.log", child.name):
                log = str(child.relative_to(run.dir))
                session = sessions.get(log, {})
                found.append({"node": where, "pass": pass_no, "attempt": int(match[1]), "log": log,
                              "instruction": _maybe(run, folder / "instruction.md"), "output": _maybe(run, folder / "output.json"),
                              "harness": session.get("harness"), "model": session.get("model"),
                              "started": session.get("time"), "_mtime": child.stat().st_mtime})
            elif re.fullmatch(r"attempt-\d+", child.name) and (child / "nodes").is_dir():
                nodes(child / "nodes", f"{where}/")

    try:
        nodes(run.dir / "nodes", "")
    except OSError:  # a run being discarded
        pass
    logged = {a["log"] for a in found}
    for log, e in sessions.items():
        if log not in logged:
            found.append({"node": observe.where(e), "pass": e.get("pass"), "attempt": e.get("attempt"), "log": None,
                          "instruction": _maybe(run, (run.dir / log).with_name("instruction.md")), "output": None,
                          "harness": e.get("harness"), "model": e.get("model"), "started": e.get("time"),
                          "_mtime": observe.timestamp(e.get("time"))})
    found.sort(key=lambda a: a.pop("_mtime"))
    return found


def _maybe(run: Run, path: Path) -> str | None:
    return str(path.relative_to(run.dir)) if path.is_file() else None


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


def serve(source: Source, port: int = PORT, say: Callable[[str], None] = lambda text: print(text, flush=True),
          opened: Callable[[str], object] | None = None) -> int:
    """Serve the page until Ctrl-C. A port that is taken gives way to a free one."""
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
