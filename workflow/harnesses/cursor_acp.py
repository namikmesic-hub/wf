"""Cursor through ACP: agent sessions through `cursor-agent acp`, the Agent Client Protocol over stdio.

wf ran every Cursor session this way before the Cursor host (cursor.py), and keeps it for one
release. cursor.runtime = "acp" in ~/.wf/config.toml (or WF_CURSOR_RUNTIME=acp) runs every Cursor
session here. The Cursor host's harness runs `edit` sessions here too (see cursor.py).

ACP is JSON-RPC 2.0, one message per line in both directions, and either side
can send requests. A session is: initialize, authenticate with `cursor_login`
(your local Cursor login, so no API key), session/new, then one session/prompt
per turn. While a turn runs, the agent sends session/update notifications
(message chunks, tool calls) and asks session/request_permission before
anything it may not do by itself. The reply to session/prompt, {stopReason},
ends the turn. A reminder is another session/prompt.

Each session is its own `cursor-agent --model <model> acp` process, which
takes a few seconds to start: paths.cursor_agent names the program, else it is
cursor-agent on PATH. The agent keeps one model per process (choosing
one in a session switches every session in the process), and `--model` is what
takes a Cursor model id with its effort in it, such as gpt-5.6-sol-max-fast.
The effort only applies when initialize says the client takes parameterized
models (`parameterizedModelPicker`); the session's transcript records the model
and parameters Cursor reports it uses. `--disable-project-configs` keeps the
repo's .cursor/cli.json from changing permissions. Its process group is stopped
with the session, so commands the agent started stop too.

Configuration: Cursor saves the model it was started with into its config
folder, with the parameters it last used for that model, and reads its
permissions, MCP servers and hooks from there. So every process gets a new
folder of its own as CURSOR_CONFIG_DIR (and CURSOR_DATA_DIR), in
~/.wf/temp/<run-id>/cursor-agent/, outside the repo, because Cursor also writes
your account's details there. It holds wf's cli-config.json (CONFIG): nothing
runs without asking, web search isn't accepted by itself, the sandbox is off,
the agent doesn't credit itself in commits, and DENY's rules for the access
level. The folder goes when the session closes. Your ~/.cursor settings don't
apply and aren't changed. A session a later pass may continue moves to the
run's saved sessions, as cursor-agent/<session-id> (workspaces.saved_sessions),
which outlive the run's success, so that `wf retry` and `wf continue` continue
it. Only the session's folder from acp-sessions/ moves there: Cursor's record of
the conversation. The config folder, with your account's details, still goes.
The sandbox is off at every level: with it on, a command wf allows still runs
outside it, so it would confine nothing that wf doesn't already decide.
CURSOR_API_KEY and CURSOR_AUTH_TOKEN are removed from the environment, so the
local login is used and the agent's commands can't read a key, and
NO_OPEN_BROWSER keeps a logged-out cursor-agent from opening a login page.

submit_result is a tool on an MCP server the harness runs (mcp_http.py), given
to session/new as the server `wf`. Each session gets its own URL, so a call
can only reach the attempt it belongs to.

wf tools (spec.tools) are served the same way: each tool gets a URL of its own
on that server, named after the tool (`linear`), with the operations its node
may use. A call comes back to the runner, which passes it on to the vendor
(tools/relay.py), so Cursor never sees the vendor's token. The transcript keeps
such a call's input and outcome, but not what the vendor returned. A node that
lists `tools` may call no other MCP server's tools, at any access level: its
own config folder already holds none of yours, and permit() refuses the rest.

Access levels: the node never stops to ask. Permission requests are answered
from its level with "allow once" or "reject", never "allow always", which
Cursor would save.
- read-only: Cursor's ask mode, meant for answering without changing anything,
  and a `Write(**)` deny rule in the config, so a write fails even if one is
  tried. Reads and wf's own tools are allowed; commands (ask mode still offers
  a shell), edits and other MCP servers' tools are refused. Like Claude Code,
  it gets the runner's `git` tool (git_tool.py) next to submit_result.
- edit: agent mode. Cursor writes inside the workspace without asking; a write
  outside it is refused, commands are allowed, other MCP servers' tools refused.
  Cursor also writes to temp folders and to Markdown files in ~/.cursor without
  asking, so deny rules block those writes.
- full: everything is allowed.
Web search and fetch follow `web` at every level.

The model must be one `cursor-agent --list-models` lists (read when the harness
opens); any other makes the session unavailable, so a fallback harness takes
over. `effort` isn't used: Cursor model ids carry their own.

How a turn ends: Cursor reports a failed request as a paragraph it adds at
the end of the turn's last message, with stopReason end_turn, not as an error.
The paragraph can come in several chunks, so the adapter reads the whole
message since the last tool call. A paragraph in the form Cursor writes an
error, "Error: [code] ..." or "Error: SomeError: ...", fails the turn, and a
sign-in, plan or payment notice, or an unauthenticated or resource_exhausted
error (a usage limit), makes it unavailable. A RetriableError is not a usage
limit, even "RetriableError: [resource_exhausted]": it only fails the turn, and
the node's retry starts RETRY_AFTER seconds later. Other text that starts with
"Error:" is the agent's own. Any stopReason but end_turn fails the turn.

Usage: ACP reports neither tokens nor cost, so Cursor turns count as zero.

A message from `wf steer` can't join a running turn: ACP has no request that
adds to one. So steer() takes none, and the runner sends the message as the
next session/prompt when this turn ends.

session: continue loads the previous pass's session (session/load) in a new
process with the same model, after moving the saved session into its config
folder. Cursor can't fork, so the session itself carries on. It's marked while
a continued attempt runs, and the mark goes only when a turn of that attempt
ends with a result. When the attempt fails, even after its result came in (a
timeout), the next one starts fresh instead of loading the failure.

Instruction files: cursor-agent reads the repo's rules, skills, subagents and
commands from the folder it starts in, not from session/new's cwd. It reads
.cursor/rules, .cursorrules, AGENTS.md, CLAUDE.md and CLAUDE.local.md there, in
each folder above it, and in folders below it. It also reads the skills in the
.cursor, .claude, .codex, .grok and .agents folders. So a session starts in the
workspace, unless the node sets `project_instructions: false`. Then it starts
in a new empty folder in the system's temp folder (_bare_folder), and
session/new still gives the workspace, where the agent works. The session is
unavailable instead when the temp folder is in a git repo (TMPDIR in a repo).
It is also unavailable when a folder above it holds such a file or skills
folder. Only your home folder's skills don't count: they load anywhere. The cost:
Cursor's semantic code search and lint diagnostics don't cover the workspace.

What can't be switched off: your Cursor account's User Rules, and the skills in
your home folder (such as ~/.claude/skills and ~/.agents/skills). The repo's
hooks (.cursor/hooks.json, .claude/settings.json) load from session/new's cwd,
even with `project_instructions: false`. Cursor runs a web fetch its server
marks as needing no approval, even with `web: false`.

wf is tested with one cursor-agent version (TESTED), because it reads the
messages Cursor writes, and warns when the installed one differs.
"""

import asyncio
import contextlib
import itertools
import json
import os
import re
import shutil
import signal
import tempfile
import uuid
from pathlib import Path

from .. import config, output_tool, workspaces
from ..runs import write_json
from . import git_tool, mcp_http
from .base import SessionSpec, TurnEnd, Unavailable, Usage, short, transcript

TESTED = "2026.10.18-ea085cb"
FLAGS = ["--sandbox", "disabled", "--disable-project-configs"]
SERVER = "wf"  # the MCP server name the agent sees
CONFIG = {
    "version": 1,
    "permissions": {"allow": [], "deny": []},
    "approvalMode": "allowlist",
    "autoAcceptWebSearch": False,
    "sandbox": {"mode": "disabled"},
    "attribution": {"attributeCommitsToAgent": False, "attributePRsToAgent": False},
}
WRITES_WITHOUT_ASKING = [  # outside the workspace: temp folders, and Markdown files in ~/.cursor
    "Write(/tmp/*)", "Write(/private/tmp/*)", "Write(/var/tmp/*)", "Write(/var/folders/*)",
    "Write(/private/var/folders/*)", "Write(~/.cursor/*)",
]
DENY = {"read-only": ["Write(**)"], "edit": WRITES_WITHOUT_ASKING, "full": []}  # Cursor's `*` matches any text
MODES = {"read-only": "ask", "edit": "agent", "full": "agent"}
SECRETS = ("CURSOR_API_KEY", "CURSOR_AUTH_TOKEN")
NOTICES = (  # Cursor's own messages when the account needs attention
    "Please sign in to continue", "Upgrade your plan to continue", "Add a payment method to continue",
    "Check your settings to continue",
)
ERROR = re.compile(r"\n\nError: (?:\[[a-z_]+\]|\w*Error:)(?: |$)")  # how Cursor reports a failed request (with a message or none)
UNAVAILABLE = (*NOTICES, "[unauthenticated]", "[resource_exhausted]")  # in a notice: it can't run here now
RETRIABLE = "Error: RetriableError:"  # by its name, an error that a retry can clear
RETRY_AFTER = 30  # seconds
AUTH_REQUIRED = -32000  # ACP's error code for "authenticate first"
LABELS = {"read": "read", "edit": "edit", "delete": "delete", "move": "move", "search": "search",
          "execute": "command", "fetch": "web fetch"}
LOADED_FROM_ABOVE = (".git", "AGENTS.md", "CLAUDE.md", "CLAUDE.local.md", ".cursorrules", ".cursor/rules")  # .git: in a repo
SKILLS = tuple(f"{folder}/skills" for folder in (".cursor", ".claude", ".codex", ".grok", ".agents"))


class CursorAcp:
    name = "cursor"

    def __init__(self, command: list[str] | None = None, request_timeout: float = 60):
        self.command = command or [str(config.get().paths.cursor_agent or "cursor-agent")]
        self.request_timeout = request_timeout  # for starting a session; a turn has the node's timeout
        self.home = Path()  # this run's Cursor folders
        self.saved = Path()  # the sessions that a later pass may continue
        self.models: set[str] = set()
        self.tools: mcp_http.Server | None = None
        self.warning: str | None = None  # shown once, with the first session

    async def open(self, run_dir: Path) -> None:
        self.home = workspaces.temp_root(Path(run_dir).name) / "cursor-agent"
        self.home.mkdir(parents=True, exist_ok=True)
        self.saved = workspaces.saved_sessions(Path(run_dir), "cursor-agent")
        self.models = await self._list_models()
        installed = await self._version()
        if installed and installed != TESTED:
            self.warning = (f"cursor-agent {installed} is installed; wf is tested with {TESTED}, "
                            f"and reads Cursor's messages the way that version writes them")
        self.tools = mcp_http.Server()
        await self.tools.start()

    async def check_model(self, model: str | None, access: str = "read-only") -> None:
        if model is not None and model not in self.models:
            raise Unavailable(f"Cursor has no model `{model}`: `cursor-agent --list-models` lists the ones you can use")

    async def session(self, spec: SessionSpec) -> "CursorSession":
        await self.check_model(spec.model)
        if self.warning:
            spec.emit({"event": "warning", "message": self.warning})
            self.warning = None
        session = CursorSession(self, spec)
        await session.start()
        return session

    async def close(self) -> None:
        if self.tools is not None:
            await self.tools.close()
            self.tools = None

    def folder(self, access: str) -> Path:
        """A new config folder for one cursor-agent process, holding wf's cli-config.json."""
        folder = self.home / uuid.uuid4().hex[:12]
        folder.mkdir()
        write_json(folder / "cli-config.json", {**CONFIG, "permissions": {"allow": [], "deny": DENY[access]}})
        return folder

    def env(self, folder: Path) -> dict:
        kept = {name: value for name, value in os.environ.items() if name not in SECRETS}
        return {**kept, "CURSOR_CONFIG_DIR": str(folder), "CURSOR_DATA_DIR": str(folder), "NO_OPEN_BROWSER": "1"}

    async def _list_models(self) -> set[str]:
        """The model ids `cursor-agent --list-models` prints, one `<id> - <name>` per line."""
        code, text = await self._run("--list-models")
        models = {line.split(" - ", 1)[0].strip() for line in text.splitlines() if " - " in line}
        if code != 0 or not models:
            raise Unavailable(f"`cursor-agent --list-models` failed (is it logged in? `cursor-agent login`): {short(text, 300)}")
        return models

    async def _version(self) -> str | None:
        try:
            code, text = await self._run("--version")
        except Unavailable:
            return None
        return text.split()[-1] if code == 0 and text.split() else None

    async def _run(self, *args: str) -> tuple[int | None, str]:
        """Run cursor-agent with these arguments, in a config folder of its own: (exit code, output)."""
        folder = self.folder("read-only")
        try:
            try:
                process = await asyncio.create_subprocess_exec(
                    *self.command, *args, cwd=folder, env=self.env(folder),
                    stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            except OSError as error:
                raise Unavailable(f"cursor-agent didn't start: {error}") from error
            try:
                out, _ = await asyncio.wait_for(process.communicate(), self.request_timeout)
            except BaseException as error:  # timed out or cancelled: don't leave it running
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                await process.wait()
                if isinstance(error, TimeoutError):
                    raise Unavailable(f"`cursor-agent {' '.join(args)}` didn't answer") from error
                raise
        finally:
            shutil.rmtree(folder, ignore_errors=True)
        return process.returncode, re.sub(r"\x1b\[[0-9;]*m", "", out.decode(errors="replace"))


class CursorSession:
    def __init__(self, harness: CursorAcp, spec: SessionSpec):
        self.harness, self.spec = harness, spec
        self.id = ""
        self.folder: Path | None = None  # this process's config folder
        self.bare: Path | None = None  # an empty folder to start in, without the repo's instruction files
        self.rpc: _Connection | None = None
        self.url = ""  # this session's MCP server
        self.tool_urls: list[str] = []  # its wf tools' servers
        self.prompt: asyncio.Future | None = None  # the running session/prompt
        self.turn = False  # updates count only while a turn runs (session/load replays history)
        self.calls: dict[str, dict] = {}  # tool calls in progress, as their updates describe them
        self.text = ""  # the agent's message since the last tool call
        self.succeeded = False  # a turn ended with a result: the attempt has its result
        # Marks a continued session while an attempt that continued it has no result
        self.continuing = harness.home / "continuing" / spec.fork_from if spec.fork_from else None

    async def start(self) -> None:
        spec, harness = self.spec, self.harness
        saved = harness.saved / spec.fork_from if spec.fork_from else None
        if self.continuing is not None and self.continuing.exists():
            raise Unavailable("the last attempt that continued this session failed, and Cursor can't fork it")
        if saved is not None and not saved.is_dir():
            raise Unavailable(f"there's no saved Cursor session {spec.fork_from}")
        tools = [mcp_http.Tool(output_tool.NAME, output_tool.DESCRIPTION, spec.tool.schema, self._submit)]
        if spec.access == "read-only":
            tools.append(mcp_http.Tool(git_tool.NAME, git_tool.DESCRIPTION, git_tool.SCHEMA, self._git))
        self.folder = harness.folder(spec.access)
        self.url = harness.tools.add(tools)
        servers = [{"type": "http", "name": SERVER, "url": self.url, "headers": []}]
        for server in spec.tools:
            self.tool_urls.append(harness.tools.add([mcp_http.Tool(op.name, op.description, op.input_schema, _relay(server, op.name))
                                                     for op in server.operations]))
            servers.append({"type": "http", "name": server.name, "url": self.tool_urls[-1], "headers": []})
        command = [*harness.command, "--model", spec.model, *FLAGS, "acp"]
        try:
            if saved is not None:  # Cursor loads a session from its own config folder
                self.id = spec.fork_from  # set first: close() puts the session back even if loading fails
                (self.folder / "acp-sessions").mkdir()
                shutil.move(saved, self.folder / "acp-sessions" / self.id)
            if not spec.project_instructions:
                self.bare = _bare_folder()
            self.rpc = await _Connection.start(command, self.bare or spec.cwd, harness.env(self.folder),
                                               harness.home / "cursor-agent.log", harness.request_timeout, self)
            await self.rpc.request("initialize", {
                "protocolVersion": 1, "clientInfo": {"name": "wf", "version": "0.1.0"},
                "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False,
                                       "_meta": {"parameterizedModelPicker": True}}})
            await self.rpc.request("authenticate", {"methodId": "cursor_login"})
            if saved is not None:
                result = await self.rpc.request("session/load", {"sessionId": self.id, "cwd": str(spec.cwd),
                                                                  "mcpServers": servers})
                self.continuing.parent.mkdir(exist_ok=True)
                self.continuing.touch()
            else:
                result = await self.rpc.request("session/new", {"cwd": str(spec.cwd), "mcpServers": servers})
                self.id = result.get("sessionId") or ""
                if not self.id:
                    raise _RpcError(f"unexpected session/new reply {short(result, 200)}")
            await self.rpc.request("session/set_mode", {"sessionId": self.id, "modeId": MODES[spec.access]})
        except BaseException as error:  # failed or cancelled: don't leave cursor-agent running
            await self.close()
            if isinstance(error, (OSError, _RpcError, TimeoutError)):
                raise Unavailable(f"Cursor didn't start a session: {error if str(error) else 'no answer'}") from error
            raise
        transcript(spec.log, "session", id=self.id, model=spec.model, mode=MODES[spec.access],
                   continued=bool(spec.fork_from), config=str(self.folder), effective=_effective(result))

    def pending_usage(self) -> Usage:
        return Usage()

    async def steer(self, text: str, ident: str) -> None:
        return None

    async def send(self, text: str) -> TurnEnd:
        log = self.spec.log
        transcript(log, "sent", text=text)
        self.text = ""
        self.turn = True  # before the request goes out: its updates can arrive before call() returns
        try:
            self.prompt = await self.rpc.call("session/prompt", {"sessionId": self.id, "prompt": [{"type": "text", "text": text}]})
            result = await asyncio.shield(self.prompt)  # a timeout stops the wait; close() stops the turn
        except _RpcError as error:
            result = error
        finally:
            self.turn = False
            message = self.text
            self._flush()
        # The turn ended (a timeout never gets here): the runner takes a result now
        self.succeeded = self.succeeded or self.spec.tool.result is not None
        if isinstance(result, _RpcError):
            if self.rpc.dead:
                return TurnEnd("failed", f"cursor: {self.rpc.dead}")
            return TurnEnd("unavailable" if result.code == AUTH_REQUIRED else "failed", f"cursor: {result}")
        stop = result.get("stopReason")
        transcript(log, "turn", stop_reason=stop)
        failure = notice(message)
        if failure.startswith(RETRIABLE):
            return TurnEnd("failed", f"cursor: {failure}", retry_after=RETRY_AFTER)
        if any(sign in failure for sign in UNAVAILABLE):
            return TurnEnd("unavailable", f"cursor: {failure}")
        if failure:
            return TurnEnd("failed", f"cursor: {failure}")
        if stop != "end_turn":
            return TurnEnd("failed", f"cursor: the turn ended with stopReason {stop}")
        return TurnEnd("done")

    def update(self, params: dict) -> None:
        """A session/update notification."""
        update = params.get("update") or {}
        kind = update.get("sessionUpdate")
        if not self.turn:
            return
        if kind == "agent_message_chunk":
            self.text += (update.get("content") or {}).get("text") or ""
        elif kind in ("tool_call", "tool_call_update"):
            self._flush()
            self._tool_call(update)

    def permit(self, params: dict) -> dict:
        """The answer to session/request_permission: the node's access level decides."""
        asked = params.get("toolCall") or {}
        call = {**self.calls.get(asked.get("toolCallId"), {}), **{k: v for k, v in asked.items() if v}}
        allowed = allows(self.spec.access, self.spec.web, self.spec.cwd, call, {SERVER, *(t.name for t in self.spec.tools)},
                         only_these=self.spec.only_wf_tools)
        transcript(self.spec.log, "permission", tool_kind=call.get("kind"), title=call.get("title"), allowed=allowed)
        return choose(params.get("options") or [], allowed)

    def _tool_call(self, update: dict) -> None:
        call = self.calls.setdefault(update.get("toolCallId"), {})
        call.update({k: v for k, v in update.items() if v not in (None, {}, [])})
        status = call.get("status")
        if status == "in_progress" and not call.get("shown"):
            call["shown"] = True
            self.spec.emit({"event": "agent_tool", "tool": _label(call), "detail": _detail(call, self.spec.cwd)})
            self._trace_call(update.get("toolCallId"), call)  # now: a timeout may stop it before it completes
        elif "traced" in call and status not in ("completed", "failed"):
            self._trace_call(update.get("toolCallId"), call)  # an update can add the input after it started
        elif status in ("completed", "failed"):
            self.calls.pop(update.get("toolCallId"), None)
            output = call.get("rawOutput") or call.get("content") or ""
            relayed = (call.get("rawInput") or {}).get("providerIdentifier") in {server.name for server in self.spec.tools}
            kept = {"kept": False, "characters": len(json.dumps(output))} if relayed else {"output": short(output, 2000)}
            transcript(self.spec.log, "tool", title=call.get("title"), tool_kind=call.get("kind"), status=status,
                       input=call.get("rawInput"), **kept)
            self._trace_call(update.get("toolCallId"), call)
            self.spec.trace.tool_result(update.get("toolCallId"), output, status == "failed", kept=not relayed)

    def _trace_call(self, call_id, call: dict) -> None:
        """A tool call for the trace, as its updates have described it so far: again each time
        that they change what it is."""
        arguments = {"title": call.get("title"), "kind": call.get("kind"), "input": call.get("rawInput")}
        if call.get("traced") != arguments:
            call["traced"] = arguments
            self.spec.trace.tool_call(call_id, _label(call), arguments)

    def _flush(self) -> None:
        if self.text.strip():
            transcript(self.spec.log, "text", text=self.text)
            self.spec.trace.text(self.text)
        self.text = ""

    async def _submit(self, args: dict) -> tuple[str, bool]:
        text, is_error = self.spec.tool.submit(args)
        transcript(self.spec.log, "tool_result", tool=output_tool.NAME, is_error=is_error, content=text)
        return text, is_error

    async def _git(self, args: dict) -> tuple[str, bool]:
        return await asyncio.to_thread(git_tool.run, self.spec.cwd, args.get("args") or [])

    async def close(self) -> None:
        for call_id, call in self.calls.items():  # calls still open, as after a timeout: the trace gets them too
            self._trace_call(call_id, call)
        try:
            if self.rpc is not None:
                try:
                    if self.prompt is not None and not self.prompt.done() and not self.rpc.dead:  # a turn still running
                        with contextlib.suppress(Exception):
                            await self.rpc.notify("session/cancel", {"sessionId": self.id})
                            await asyncio.wait_for(self.prompt, 5)
                finally:
                    await self.rpc.stop()
        finally:  # even when cancelled: the URLs must stop reaching this attempt
            if self.harness.tools is not None:
                for url in filter(None, [self.url, *self.tool_urls]):
                    self.harness.tools.remove(url)
            if self.continuing is not None and self.succeeded:
                self.continuing.unlink(missing_ok=True)
            if self.folder is not None:
                self._keep_session()
                shutil.rmtree(self.folder, ignore_errors=True)
                self.folder = None
            if self.bare is not None:
                shutil.rmtree(self.bare, ignore_errors=True)
                self.bare = None

    def _keep_session(self) -> None:
        """Move the session out of the config folder, which goes, when a later pass may load it."""
        store = self.folder / "acp-sessions" / self.id
        if self.id and self.spec.continuable and store.is_dir():
            target = self.harness.saved / self.id
            shutil.rmtree(target, ignore_errors=True)
            shutil.move(store, workspaces.make_private(self.harness.saved) / self.id)


def _bare_folder() -> Path:
    """A new empty folder in the system's temp folder, for cursor-agent to start in. Cursor also reads
    instruction files from the folders above that one, and from the whole repo it is in. So a folder in a
    repo, or under such a file or skills folder, is refused. Your home folder's skills load anyway."""
    folder = Path(tempfile.mkdtemp(prefix="wf-cursor-")).resolve()
    home = Path.home().resolve()
    found = next((parent / name for parent in folder.parents
                  for name in LOADED_FROM_ABOVE + (() if parent == home else SKILLS) if (parent / name).exists()), None)
    if found is not None:
        shutil.rmtree(folder, ignore_errors=True)
        raise Unavailable(f"{found} is in a folder above the temp folder {folder.parent}, so Cursor could load instruction "
                          "files from there: set TMPDIR to a folder outside any repo, with none of them above it")
    return folder


def notice(message: str) -> str:
    """Cursor's notice that a request failed, the paragraph it adds at the end of the turn's last
    message: one of NOTICES, or an error in the form ERROR matches. "" when there's none."""
    text = message.rstrip()
    for account in NOTICES:
        if text.endswith("\n\n" + account):
            return account
    starts = [match.start() for match in ERROR.finditer(text)]
    return text[starts[-1] + 2:] if starts else ""


def _relay(server, operation: str):
    """One operation of a wf tool, for mcp_http.Tool: the call goes to the vendor through tools/relay.py."""

    async def call(arguments: dict) -> tuple[list[dict], bool]:
        return await server.call(operation, arguments)

    return call


def _effective(result: dict) -> dict:
    """The model and model parameters a new or loaded session reports it uses."""
    options = result.get("configOptions") if isinstance(result, dict) else None
    return {o.get("id"): o.get("currentValue") for o in options or [] if isinstance(o, dict) and o.get("id") != "mode"}


def allows(access: str, web: bool, cwd: Path, call: dict, servers: set[str] = frozenset({SERVER}),
           only_these: bool = False) -> bool:
    """Is what a permission request asks for within the node's access level? `servers`: the MCP
    servers wf gave the session, wf's own and its tools'. `only_these`: the node lists `tools`,
    so no other server's tools, even at `full`."""
    kind = call.get("kind")
    if kind in ("fetch", "search"):  # web fetch and web search: searching files never asks
        return web
    if kind == "other" and only_these:
        return (call.get("rawInput") or {}).get("providerIdentifier") in servers
    if kind == "read" or access == "full":
        return True
    if kind == "other":  # an MCP tool: only on a server wf gave it
        return (call.get("rawInput") or {}).get("providerIdentifier") in servers
    if access == "edit" and kind == "execute":
        return True
    if access == "edit" and kind in ("edit", "delete", "move"):
        paths = _paths(call)
        root = Path(cwd).resolve()
        return bool(paths) and all((root / path).resolve().is_relative_to(root) for path in paths)
    return False


def choose(options: list[dict], allowed: bool) -> dict:
    """Pick "allow once" or "reject once". A request with several options to allow (a question
    for the user, with its answers as options) is never answered: its "reject" skips it."""
    wanted = [o for o in options if o.get("kind") == ("allow_once" if allowed else "reject_once")]
    rejects = [o for o in options if o.get("kind") == "reject_once"]
    choice = wanted[0] if len(wanted) == 1 else rejects[0] if rejects else None
    return {"outcome": {"outcome": "selected", "optionId": choice["optionId"]}} if choice else {"outcome": {"outcome": "cancelled"}}


def _paths(call: dict) -> list[str]:
    """The files an edit request names: in its diff, its locations, or its input."""
    found = [c.get("path") for c in call.get("content") or [] if isinstance(c, dict) and c.get("type") == "diff"]
    found += [place.get("path") for place in call.get("locations") or [] if isinstance(place, dict)]
    found.append((call.get("rawInput") or {}).get("path"))
    return [path for path in found if isinstance(path, str) and path]


def _label(call: dict) -> str:
    data = call.get("rawInput") or {}
    tool, server = data.get("toolName"), data.get("providerIdentifier")  # an MCP tool: submit_result, git, ...
    if tool and server not in (None, SERVER):
        return f"{server}.{tool}"  # a wf tool's is linear.list_issues, as on the other harnesses
    return tool or LABELS.get(call.get("kind"), call.get("kind") or "tool")


def _detail(call: dict, cwd: Path) -> str:
    """A few words about a tool call, for the progress line. Paths are shown relative to the workspace."""
    data = call.get("rawInput") or {}
    arguments = data.get("args")  # an MCP tool's arguments
    if isinstance(arguments, dict) and isinstance(arguments.get("args"), list):  # the runner's git tool: ["diff", "main...HEAD"]
        return short(" ".join(str(part) for part in arguments["args"]), 80)
    for key in ("path", "command", "pattern", "query", "url"):
        if data.get(key):
            return short(str(data[key]).removeprefix(str(cwd).rstrip("/") + "/"), 80)
    return "" if data.get("toolName") else short(call.get("title") or "", 80)


class _RpcError(Exception):
    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


class _Connection:
    """JSON-RPC over one cursor-agent's stdin and stdout: requests both ways, notifications."""

    def __init__(self, process: asyncio.subprocess.Process, stderr, timeout: float, session: CursorSession):
        self.process, self.stderr, self.timeout, self.session = process, stderr, timeout, session
        self.ids = itertools.count(1)
        self.pending: dict[int, asyncio.Future] = {}
        self.dead: str | None = None  # why the connection ended, once it has
        self.reader = asyncio.ensure_future(self._read())

    @classmethod
    async def start(cls, command: list[str], cwd: Path, env: dict, stderr_log: Path, timeout: float,
                    session: CursorSession) -> "_Connection":
        stderr_log.parent.mkdir(parents=True, exist_ok=True)
        stderr = stderr_log.open("a", encoding="utf-8")
        try:
            process = await asyncio.create_subprocess_exec(
                *command, cwd=cwd, env=env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=stderr,
                limit=64 * 1024 * 1024,  # one JSON message per line, and some are large
                start_new_session=True,  # its own process group, so closing stops the commands it started
            )
        except OSError:
            stderr.close()
            raise
        return cls(process, stderr, timeout, session)

    async def call(self, method: str, params: dict) -> asyncio.Future:
        """Send a request. The future gets its result, or an _RpcError."""
        if self.dead:
            raise _RpcError(self.dead)
        request_id = next(self.ids)
        future = self.pending[request_id] = asyncio.get_running_loop().create_future()
        try:
            await self._write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        except OSError as error:  # its stdin is gone: cursor-agent died after the check above
            self.pending.pop(request_id, None)
            raise _RpcError(self.dead or f"lost cursor-agent: {error}") from error
        return future

    async def request(self, method: str, params: dict) -> dict:
        return await asyncio.wait_for(await self.call(method, params), self.timeout)

    async def notify(self, method: str, params: dict) -> None:
        await self._write({"jsonrpc": "2.0", "method": method, "params": params})

    async def _write(self, message: dict) -> None:
        self.process.stdin.write((json.dumps(message) + "\n").encode())
        await self.process.stdin.drain()

    async def _read(self) -> None:
        try:
            while line := await self.process.stdout.readline():
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(message, dict):
                    continue
                if "method" in message and "id" in message:
                    await self._answer(message)
                elif message.get("method") == "session/update":
                    self.session.update(message.get("params") or {})
                elif isinstance(message.get("id"), int) and (future := self.pending.pop(message["id"], None)):
                    if future.done():
                        continue
                    if "error" in message:
                        error = message["error"] or {}
                        detail = (error.get("data") or {}).get("message") if isinstance(error.get("data"), dict) else None
                        future.set_exception(_RpcError(": ".join(filter(None, [error.get("message"), detail])) or "error",
                                                       error.get("code")))
                    else:
                        future.set_result(message.get("result") or {})
        except (OSError, ValueError) as error:
            self.dead = f"lost cursor-agent: {error}"
        except Exception as error:  # a bug in handling a message: say so, rather than that cursor-agent exited
            self.dead = f"wf failed to handle a message from cursor-agent: {type(error).__name__}: {error}"
        finally:
            self.dead = self.dead or f"cursor-agent exited (its output is in {self.stderr.name})"
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(_RpcError(self.dead))

    async def _answer(self, message: dict) -> None:
        """Reply to a request from the agent. Every request gets a reply."""
        method, params = message["method"], message.get("params") or {}
        try:
            if method == "session/request_permission":
                reply = {"result": self.session.permit(params)}
            else:  # files and terminals (declared unsupported), questions, plans
                reply = {"error": {"code": -32601, "message": f"wf doesn't handle {method}"}}
        except Exception as error:  # never leave the agent waiting; it takes a failed request as "no"
            reply = {"error": {"code": -32603, "message": f"{type(error).__name__}: {error}"}}
        await self._write({"jsonrpc": "2.0", "id": message["id"], **reply})

    async def stop(self) -> None:
        if self.process.returncode is None:
            with contextlib.suppress(Exception):
                self.process.stdin.close()
            _kill(self.process, signal.SIGTERM)
            try:
                await asyncio.wait_for(self.process.wait(), 5)
            except TimeoutError:
                _kill(self.process, signal.SIGKILL)
                await self.process.wait()
        _kill(self.process, signal.SIGKILL)  # whatever it started that's still running
        self.reader.cancel()
        # gather() hands back the reader's own cancellation as a value, but still lets a
        # cancellation of this task (Ctrl-C during teardown) through
        await asyncio.gather(self.reader, return_exceptions=True)
        self.stderr.close()


def _kill(process: asyncio.subprocess.Process, sig: int) -> None:
    """Signal cursor-agent's process group."""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, sig)
