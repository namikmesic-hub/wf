"""Cursor: agent sessions through Cursor's SDK (@cursor/sdk), run by the Cursor host.

The Cursor host (cursor_host/) is a TypeScript program on Cursor's SDK. wf starts one for each
session, as `node wf-cursor-host.js`, and speaks the wf-session protocol to it over its stdin and
stdout (remote.py does that for any such peer). This module only configures it: where the host is
and how it is installed, the API key, the folders for its state, and which sessions run through
cursor-agent's ACP instead (cursor_acp.py).

Why the SDK: in ACP mode cursor-agent switches its own reconnect loop off, so one dropped stream
ended a turn, and the runner started the whole turn again. The host sets the SDK's
enableAgentRetries, so the SDK reconnects and goes on from where the turn was. The SDK also
reports each turn's tokens, which count towards a run's limits.

Installing: the SDK can't be bundled into one file, so the host ships as its TypeScript source,
which Node 22.18 or later runs as it is, with package.json and package-lock.json beside it. The
first Cursor session installs the SDK there, from the lockfile, with `npm ci --omit=dev
--ignore-scripts` (ensure()). So a machine needs Node and npm, and nothing else. Another wf
version comes with its own folder, and installs again.

The API key: the SDK needs a Cursor API key, and cursor-agent's login doesn't work for it. wf
reads it from the macOS Keychain (`wf runtimes cursor signin` keeps it there), or from the file
that cursor.api_key_file in ~/.wf/config.toml names: never from the environment. It goes to the
host over stdin, in the protocol's hello, never on a command line. The host, and so the agent's
commands, get the environment without CURSOR_API_KEY and CURSOR_AUTH_TOKEN. With no key, read-only
and full sessions run through cursor-agent's ACP, as they did before the SDK, and the run's first
such session warns once and says what to do (FIX). A key that Cursor refuses makes Cursor
unavailable, and the message says what to do.

Access levels, as the host maps them (cursor_host/src/cursor.ts):
- read-only: none of Cursor's tools that read files or run commands. wf's file tools
  (files_tool.py) and its `git` tool read only the workspace and the files and folders that the
  instruction names, since Cursor's own search tools take any folder, the home folder too.
  Cursor's sandbox is on, and no MCP server but the session's own tools is offered.
- edit: through cursor-agent's ACP (ON_ACP), whose answer to each request keeps writes in the
  workspace. The SDK has no such answers, and no test has shown yet that Cursor's sandbox keeps
  writes there.
- full: all of Cursor's tools, with the sandbox off.
Web search and fetch follow `web`.

Models: the host maps cursor-agent's model ids, such as gpt-5.6-sol-max-fast, to an id in the
SDK's catalog and its parameters, and refuses an id it can't map (cursor_host/src/models.ts).
`effort` isn't used: a Cursor model id carries its own.

session: continue loads the previous pass's agent (start: load) from its state folder. Each
session's folder is ~/.wf/temp/<run-id>/cursor-sdk/<id>, and goes when the session closes. One
that a later pass may continue moves to the run's saved sessions, as cursor-sdk/<agent-id>
(workspaces.saved_sessions), which outlive the run's success, so that `wf continue` continues it.
That folder is the SDK's store of the agent: its agents, runs, run events and checkpoints. It holds
no API key and no account details. As with ACP, after a continued attempt fails, the next one
starts fresh.

For one release, cursor.runtime = "acp" in ~/.wf/config.toml runs every Cursor session through ACP, as
before, until the SDK has proven itself in real runs. WF_CURSOR_RUNTIME=acp does the same, and wins over the file.
paths.node names the Node program for a machine with more than one: else it is `node` on PATH.
"""

import asyncio
import contextlib
import fcntl
import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Callable
from pathlib import Path

import keyring
from keyring.errors import KeyringError

from .. import config, workspaces
from . import remote
from .base import Harness, SessionSpec, Unavailable, short, transcript

HOST = Path(__file__).parent / "cursor_host"
ENTRY = "wf-cursor-host.js"
NODE_MIN = "22.18 or later (23.6 or later on 23)"  # the Node versions that run TypeScript as it is
CLIENT = {"name": "wf", "version": "0.1.0"}
KEYCHAIN = ("wf runtimes", "cursor")  # the Keychain item's service and account
SECRETS = ("CURSOR_API_KEY", "CURSOR_AUTH_TOKEN")
ON_ACP = ("edit",)
EDIT_ON_ACP = ("Cursor's SDK can't refuse a write outside the workspace, and no test has shown that its sandbox "
               "keeps writes in it, so `edit` runs through cursor-agent's ACP")
FIX = ("Create a Cursor API key in the Cursor dashboard, then run `wf runtimes cursor signin` to keep it in the "
       "macOS Keychain, or name a file that holds it with `api_key_file` under [cursor] in ~/.wf/config.toml")
NO_KEY = ("no Cursor API key is set, so cursor runs read-only and full sessions through cursor-agent's ACP, "
          "which counts no tokens")
INSTALL_TIMEOUT = 600
LABELS = {  # Cursor's tool names, as progress shows them, the same as on ACP where there's one
    "shell": "command", "read": "read", "edit": "edit", "write": "edit", "delete": "delete", "grep": "search",
    "glob": "search", "semSearch": "search", "ls": "list", "readLints": "lints", "webSearch": "web search",
    "webFetch": "web fetch", "task": "subagent",
}


def create() -> Harness:
    """The adapter that cursor.runtime names: the Cursor host, or cursor-agent's ACP."""
    if runtime() == "acp":
        from .cursor_acp import CursorAcp

        return CursorAcp()
    return Cursor()


def runtime() -> str:
    """"sdk" or "acp": cursor.runtime (WF_CURSOR_RUNTIME, else config.toml, else sdk)."""
    return config.get().cursor.runtime


def api_key() -> str | None:
    """The Cursor API key: from the file that cursor.api_key_file names, else from the Keychain.
    None when there's none. Raises Unavailable when the place it should be can't be read."""
    return key_source()[0]


def key_source() -> tuple[str | None, str]:
    """The Cursor API key, and where it comes from. Raises Unavailable."""
    chosen = config.get()
    path = chosen.cursor.api_key_file
    if path is not None:
        try:
            return path.read_text(encoding="utf-8").strip() or None, f"in {path} (cursor.api_key_file in {chosen.file})"
        except OSError as error:
            raise Unavailable(f"can't read the Cursor API key from {path} (cursor.api_key_file in {chosen.file}): "
                              f"{error.strerror or error}") from error
    try:
        return keyring.get_password(*KEYCHAIN) or None, "in the macOS Keychain (wf runtimes, cursor)"
    except KeyringError as error:
        raise Unavailable(f"can't read the Cursor API key from the Keychain: {error or type(error).__name__}") from error


def save_key(key: str) -> None:
    """Keep the Cursor API key in the Keychain. Raises KeyringError."""
    keyring.set_password(*KEYCHAIN, key)


def env() -> dict:
    """The host's environment: this process's, without the Cursor secrets."""
    return {**{k: v for k, v in os.environ.items() if k not in SECRETS}, "NO_OPEN_BROWSER": "1"}


def find_node(node: str | None = None) -> str:
    """The Node program to run the host with (`node`, else paths.node, else node on PATH), if it runs
    TypeScript as it is. Node itself says so, which its version alone doesn't: 23.0 to 23.5 don't, and an
    option can switch it off. Raises Unavailable."""
    chosen = config.get() if node is None else None
    named = chosen.paths.node if chosen else None
    node = node or str(named or "node")
    found = shutil.which(node)
    if found is None and named:
        raise Unavailable(f"Cursor's runtime needs Node.js {NODE_MIN}, and {node} (paths.node in {chosen.file}) "
                          "isn't a program that you may run")
    if found is None:
        raise Unavailable(f"Cursor's runtime needs Node.js {NODE_MIN}, and there's no `{node}` on PATH")
    try:
        version = subprocess.run([found, "--version"], capture_output=True, text=True, timeout=30).stdout.strip()
        typescript = subprocess.run([found, "-p", "process.features.typescript"], capture_output=True, text=True,
                                    timeout=30).stdout.strip()
    except (OSError, subprocess.TimeoutExpired) as error:
        raise Unavailable(f"`{found}` didn't answer: {error}") from error
    if typescript not in ("strip", "transform"):
        raise Unavailable(f"Cursor's runtime needs Node.js {NODE_MIN}, which runs TypeScript as it is, and {found} "
                          f"({version or 'of an unknown version'}) doesn't")
    return found


def missing(host: Path) -> list[str]:
    """The packages that package-lock.json pins for the host and that aren't installed beside it: all
    but those for development, and of the optional ones, the one for this machine (the SDK's binaries)."""
    try:
        packages = json.loads((host / "package-lock.json").read_text(encoding="utf-8"))["packages"]
    except (OSError, ValueError, KeyError) as error:
        return [f"package-lock.json ({error})"]
    lacking = []
    for where, entry in packages.items():
        if not where or entry.get("dev") or (entry.get("optional") and not _for_this_machine(entry)):
            continue
        try:
            installed = json.loads((host / where / "package.json").read_text(encoding="utf-8")).get("version")
        except (OSError, ValueError):
            installed = None
        if installed != entry.get("version"):
            lacking.append(f"{where.removeprefix('node_modules/')}@{entry.get('version')}")
    return lacking


def _for_this_machine(entry: dict) -> bool:
    """Is a package that names its systems (`os`, `cpu`) one for this machine?"""
    system = {"darwin": "darwin", "linux": "linux", "win32": "win32"}.get(sys.platform, sys.platform)
    cpu = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "x64", "amd64": "x64"}.get(platform.machine().lower(), "")
    return "os" in entry and "cpu" in entry and system in entry["os"] and cpu in entry["cpu"]


async def ensure(host: Path, node: str) -> None:
    """Install what the host needs from its lockfile, if it isn't there. One wf process installs at
    a time. A timeout or a cancellation stops npm, and the wait for another process. Raises Unavailable."""
    if not missing(host):
        return
    npm = shutil.which("npm", path=os.pathsep.join([str(Path(node).parent), os.environ.get("PATH", "")]))
    if npm is None:
        raise Unavailable(f"Cursor's SDK isn't installed in {host}, and there's no npm to install it")
    try:
        lock = (host / ".install.lock").open("a")
    except OSError as error:
        raise Unavailable(f"can't install Cursor's SDK in {host}: {error}") from error
    with lock:
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:  # another wf process installs it
                await asyncio.sleep(0.2)
        if not missing(host):
            return
        try:
            process = await asyncio.create_subprocess_exec(
                npm, "ci", "--omit=dev", "--ignore-scripts", "--no-audit", "--no-fund", "--loglevel=error",
                cwd=host, env=env(), stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, start_new_session=True)
        except OSError as error:
            raise Unavailable(f"can't install Cursor's SDK in {host}: {error}") from error
        try:
            out, err = await asyncio.wait_for(process.communicate(), INSTALL_TIMEOUT)
        except BaseException as error:  # too slow, or cancelled: npm stops too
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(process.pid, signal.SIGKILL)
            await process.wait()
            if isinstance(error, TimeoutError):
                raise Unavailable(f"`npm ci` in {host} took longer than {INSTALL_TIMEOUT} seconds") from error
            raise
    left = missing(host)
    if process.returncode != 0 or left:
        said = short((err or out).decode(errors="replace") or ", ".join(left), 400)
        raise Unavailable(f"`npm ci --omit=dev` in {host} didn't install Cursor's SDK: {said}")


class Cursor(remote.RemoteHarness):
    name = "cursor"
    labels = LABELS
    fix = FIX
    file_tools = True

    def __init__(self, host: Path | None = None, node: str | None = None, request_timeout: float = 60,
                 acp: Callable[[], Harness] | None = None):
        super().__init__(request_timeout)
        self.host, self.node = Path(host or HOST), node
        self.acp = acp  # makes the harness for `edit` sessions (tests give a fake)
        self.run_dir = Path()
        self.home = Path()  # this run's Cursor folders
        self.saved = Path()  # the sessions that a later pass may continue
        self._prepared = False
        self._preparing = asyncio.Lock()
        self._acp: Harness | None = None
        self._acp_lock = asyncio.Lock()
        self._warned = False  # about the missing API key

    async def open(self, run_dir: Path) -> None:
        self.run_dir = Path(run_dir)
        self.home = workspaces.temp_root(self.run_dir.name) / "cursor-sdk"
        self.home.mkdir(parents=True, exist_ok=True)
        self.saved = workspaces.saved_sessions(self.run_dir, "cursor-sdk")

    async def close(self) -> None:
        if self._acp is not None:
            await self._acp.close()
            self._acp = None

    async def ready(self) -> None:
        """Node and the host's packages, checked and installed once per harness. Raises Unavailable."""
        async with self._preparing:
            if not self._prepared:
                self.node = await asyncio.to_thread(find_node, self.node)
                await ensure(self.host, self.node)
                self._prepared = True

    async def peer(self, key: str | None = None) -> remote.Peer:
        """A new Cursor host, with `key`, or else the key api_key() finds. Raises Unavailable."""
        await self.ready()
        key = key or api_key()
        try:
            return await remote.Peer.start(
                [self.node, str(self.host / ENTRY)], cwd=self.home, env=env(), log=self.home / "cursor-host.log",
                timeout=self.request_timeout, name="the Cursor host", client=CLIENT,
                credentials={"api_key": key} if key else None, tap=self.tap)
        except OSError as error:
            raise Unavailable(f"the Cursor host didn't start: {error}") from error
        except remote.RemoteError as error:
            raise Unavailable(str(error)) from error
        except (remote.PeerGone, TimeoutError) as error:
            raise Unavailable(f"the Cursor host didn't start: {error or 'no answer'}") from error

    async def check_model(self, model: str | None, access: str = "read-only") -> None:
        """On the SDK, or on ACP for the sessions that run there. Raises Unavailable."""
        if self._why_acp(access):
            await (await self._on_acp()).check_model(model, access)
        else:
            await super().check_model(model, access)

    async def session(self, spec: SessionSpec) -> remote.RemoteSession:
        why = self._why_acp(spec.access)
        if why:
            harness = await self._on_acp()
            if why == NO_KEY and not self._warned:
                self._warned = True
                spec.emit({"event": "warning", "message": f"{NO_KEY}. {FIX}"})
            transcript(spec.log, "runtime", runtime="acp", why=why)
            return await harness.session(spec)
        continuing = self.home / "continuing" / spec.fork_from if spec.fork_from else None
        saved = self.saved / spec.fork_from if spec.fork_from else None
        if continuing is not None and continuing.exists():
            raise Unavailable("the last attempt that continued this session failed, and Cursor can't fork it")
        if saved is not None and not saved.is_dir():
            raise Unavailable(f"there's no saved Cursor session {spec.fork_from}")
        state = saved or self.home / uuid.uuid4().hex[:12]
        state.mkdir(parents=True, exist_ok=True)
        args = {**remote.open_args(spec, self.name, self.file_tools), "state_dir": str(state),
                "labels": {"run": self.run_dir.name, "node": spec.where or spec.node},
                **({"start": {"kind": "load", "from": spec.fork_from}} if saved else {})}
        session = await self.start(spec, args, extra={"state": state})
        if continuing is not None:
            continuing.parent.mkdir(exist_ok=True)
            continuing.touch()
            session.extra["continuing"] = continuing
        transcript(spec.log, "session", id=session.id, model=spec.model, access=spec.access, continued=bool(saved),
                   effective=session.facts.get("effective"), sdk=session.facts.get("sdk"), state=str(state))
        return session

    def _why_acp(self, access: str) -> str | None:
        """Why a session at this access level runs through cursor-agent's ACP, or None when it
        runs on the SDK. Raises Unavailable when the API key can't be read."""
        if access in ON_ACP:
            return EDIT_ON_ACP
        return NO_KEY if api_key() is None else None

    async def _on_acp(self) -> Harness:
        """The ACP harness for `edit` sessions, and for every session when no API key is set, opened on first use."""
        async with self._acp_lock:
            if self._acp is None:
                from .cursor_acp import CursorAcp

                harness = (self.acp or CursorAcp)()
                await harness.open(self.run_dir)
                self._acp = harness
        return self._acp

    async def done(self, session: remote.RemoteSession) -> None:
        try:
            await session.peer.stop()
        finally:
            state, continuing = session.extra.get("state"), session.extra.get("continuing")
            if continuing is not None and session.succeeded:  # the continued attempt has its result
                continuing.unlink(missing_ok=True)
            if state is not None:
                self._keep(session, state)

    def _keep(self, session: remote.RemoteSession, state: Path) -> None:
        """Keep a session's state where a later pass finds it, if one may continue it. Else remove
        it, unless it is a saved session already, which the run's success or `wf discard` removes."""
        saved = self.saved
        if session.id and session.spec.continuable:
            if state != saved / session.id:
                with contextlib.suppress(OSError):
                    shutil.rmtree(saved / session.id, ignore_errors=True)
                    shutil.move(state, workspaces.make_private(saved) / session.id)
        elif state.parent != saved:
            shutil.rmtree(state, ignore_errors=True)


# ---------- wf runtimes cursor ----------


async def check(models: list[str] = (), key: str | None = None) -> dict:
    """Ask a new Cursor host whether the API key works, and what each model id maps to. Returns
    runtime.check's answer, with `maps`: each model id's {id, params}, or why Cursor can't run it.
    Raises Unavailable."""
    harness = Cursor()
    with tempfile.TemporaryDirectory(prefix="wf-cursor-check-") as folder:
        harness.home = Path(folder)
        peer = await harness.peer(key)
        try:
            try:
                found = await peer.request("runtime.check", {"runtime": "cursor"})
            except remote.RemoteError as error:
                raise Unavailable(harness.explain(error.error)) from error
            except (remote.PeerGone, TimeoutError) as error:
                raise Unavailable(f"the Cursor host didn't answer: {error or 'no answer'}") from error
            maps = {}
            for model in models:
                try:
                    maps[model] = (await peer.request("runtime.check", {"runtime": "cursor", "model": model})).get("model")
                except remote.RemoteError as error:
                    maps[model] = str(error)
            return {**found, "maps": maps}
        finally:
            await peer.stop()


def works(found: dict) -> str:
    """The line that says a key works, and whose it is."""
    account = found.get("account") or {}
    who = f"{account.get('api_key_name')} ({account.get('email')})" if account else "an account Cursor didn't name"
    return f"the key works: {who}, {len(found.get('models') or [])} models"


async def doctor(models: list[str]) -> tuple[list[str], bool]:
    """What `wf runtimes cursor doctor` says, line by line, and whether all is well."""
    try:
        node = await asyncio.to_thread(find_node)
        version = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=30).stdout.strip()
    except (Unavailable, OSError, subprocess.TimeoutExpired) as error:
        return [f"Node.js: {error}"], False
    lines = [f"Node.js: {version} ({node})"]
    try:
        await ensure(HOST, node)
    except Unavailable as error:
        return [*lines, f"Cursor's SDK: {error}"], False
    sdk = json.loads((HOST / "node_modules" / "@cursor" / "sdk" / "package.json").read_text(encoding="utf-8")).get("version")
    lines.append(f"Cursor's SDK: {sdk}, installed in {HOST}")
    try:
        key, where = key_source()
    except Unavailable as error:
        return [*lines, f"API key: {error}"], False
    if key is None:
        return [*lines, f"API key: none. {FIX}"], False
    lines.append(f"API key: {where}")
    ok = True
    try:
        found = await check(models, key)
        lines.append(f"Cursor: {works(found)}")
        for model, mapped in found["maps"].items():
            if isinstance(mapped, dict):
                params = ", ".join(f"{p['id']}={p['value']}" for p in mapped.get("params") or [])
                lines.append(f"Model {model}: {mapped.get('id')}" + (f" [{params}]" if params else ""))
            else:
                ok = False
                lines.append(f"Model {model}: {mapped}")
    except Unavailable as error:
        ok = False
        lines.append(f"Cursor: {error}")
    lines.append("cursor runs every session on cursor-agent's ACP (cursor.runtime is acp)" if runtime() == "acp" else
                 "cursor runs read-only and full sessions on the SDK, and edit sessions on cursor-agent's ACP")
    return lines, ok
