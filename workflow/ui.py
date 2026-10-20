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
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
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


@dataclass
class _Tree:
    """What attempts() found in a run folder, so that its next call reads only what can have changed."""
    seen: int = 0  # the events read
    last: dict | None = None  # the last of them: a new events.jsonl doesn't go on from it
    sessions: dict[str, dict] = field(default_factory=dict)  # transcript -> its session_started event
    waiting: dict[str, tuple[float, dict]] = field(default_factory=dict)  # transcript -> the attempt to show until it is there
    passes: dict[str, dict[int, list[tuple[float, dict]]]] = field(default_factory=dict)  # node -> pass -> its attempts
    open: set[str] = field(default_factory=set)  # the nodes that can still write a file with no event
    listed: list[dict] = field(default_factory=list)


_TREES: dict[Path, _Tree] = {}  # run folder -> its _Tree
_TREES_MOST = 64
_TREEING = threading.Lock()  # wf ui asks from several threads
_ENDED = ("node_finished", "node_failed", "route")  # its node wrote its files before it


def attempts(run: Run, events: list[dict] | None = None) -> list[dict]:
    """Each attempt's log in the run folder, sub-workflows' too, oldest first, with the harness and
    model of its agent session when its events name them. An agent session that has written no
    transcript yet is there too, with no log.

    A node writes its files before the event that ends it. So after the first call, a node's
    folder is read again only when an event names the node or while the node runs, and then only
    from its newest pass that was read before. The page asks every 2 seconds, and each time it
    reads the folders of the nodes that run, not the whole run."""
    events = report.events(run) if events is None else events
    with _TREEING:
        tree = _TREES.pop(run.dir, None)
        if tree is None or len(events) < tree.seen or (tree.seen and events[tree.seen - 1] != tree.last):
            tree, stale = _Tree(), {p.name for p in _folders(run.dir / "nodes")}
        else:
            stale = set(tree.open)
        for event in events[tree.seen:]:
            node = event.get("node")
            if not isinstance(node, str) or not (top := re.match(r"[^\[/]*", node)[0]):
                continue
            stale.add(top)
            if node == top and event.get("event") in _ENDED:
                tree.open.discard(top)
            else:
                tree.open.add(top)
            if event.get("event") == "session_started" and event.get("log"):
                tree.sessions[event["log"]], tree.waiting[event["log"]] = event, _waiting(run, event)
        try:
            for node in sorted(stale):
                _read_node(run, tree, node)
        except OSError:  # a run being discarded: start over next time
            return _listed(tree)
        if stale:
            tree.listed = _listed(tree)
        tree.seen, tree.last = len(events), events[-1] if events else None
        _TREES[run.dir] = tree
        while len(_TREES) > _TREES_MOST:
            del _TREES[next(iter(_TREES))]
        return list(tree.listed)


def _read_node(run: Run, tree: _Tree, node: str) -> None:
    """Read the node's passes from the newest that was read before: the older ones have ended."""
    known = tree.passes.setdefault(node, {})
    newest = max(known, default=0)
    for number in sorted(int(p.name) for p in _folders(run.dir / "nodes" / node) if p.name.isdigit()):
        if number >= newest:
            known[number] = _read_pass(run, run.dir / "nodes" / node / str(number), node, number)


def _read_pass(run: Run, folder: Path, node: str, pass_no: int, index: int | None = None) -> list[tuple[float, dict]]:
    """The attempts of a pass folder, its items' and its sub-workflows' too, each with the time
    its log last changed."""
    where = node + (f"[{index}]" if index is not None else "")
    found = []
    for child in sorted(folder.iterdir()):
        if index is None and child.is_dir() and child.name.isdigit():
            found += _read_pass(run, child, node, pass_no, int(child.name))
        elif match := re.fullmatch(r"attempt-(\d+)\.log", child.name):
            found.append((child.stat().st_mtime, {
                "node": where, "pass": pass_no, "attempt": int(match[1]), "log": str(child.relative_to(run.dir)),
                "instruction": _maybe(run, folder / "instruction.md"), "output": _maybe(run, folder / "output.json")}))
        elif re.fullmatch(r"attempt-\d+", child.name) and (child / "nodes").is_dir():
            for inner in sorted(_folders(child / "nodes")):
                for pass_dir in sorted((p for p in _folders(inner) if p.name.isdigit()), key=lambda p: int(p.name)):
                    found += _read_pass(run, pass_dir, f"{where}/{inner.name}", int(pass_dir.name))
    return found


def _waiting(run: Run, event: dict) -> tuple[float, dict]:
    """The attempt of an agent session whose transcript isn't there yet."""
    instruction = (run.dir / event["log"]).with_name("instruction.md")
    return observe.timestamp(event.get("time")), {
        "node": observe.where(event), "pass": event.get("pass"), "attempt": event.get("attempt"), "log": None,
        "instruction": _maybe(run, instruction), "output": None}


def _listed(tree: _Tree) -> list[dict]:
    """The attempts oldest first, each with its agent session's harness, model and start."""
    found = [(at, attempt, attempt["log"]) for node in sorted(tree.passes) for pass_no in sorted(tree.passes[node])
             for at, attempt in tree.passes[node][pass_no]]
    logged = {log for _, _, log in found}
    found += [(at, attempt, log) for log, (at, attempt) in tree.waiting.items() if log not in logged]
    found.sort(key=lambda timed: timed[0])
    listed = []
    for _, attempt, log in found:
        session = tree.sessions.get(log, {})
        listed.append({**attempt, "harness": session.get("harness"), "model": session.get("model"),
                       "started": session.get("time")})
    return listed


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
