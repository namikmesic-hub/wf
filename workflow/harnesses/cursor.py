"""Cursor: agent sessions through `cursor-agent acp`, the Agent Client Protocol over stdio.

ACP is JSON-RPC 2.0, one message per line in both directions, and either side
can send requests. A session is: initialize, authenticate with `cursor_login`
(your local Cursor login, so no API key), session/new, then one session/prompt
per turn. While a turn runs, the agent sends session/update notifications
(message chunks, tool calls) and asks session/request_permission before
anything it may not do by itself. The reply to session/prompt, {stopReason},
ends the turn. A reminder is another session/prompt.

Each session is its own `cursor-agent --model <model> --sandbox disabled acp`
process, which takes a few seconds to start. The agent keeps one model per
process (choosing one in a session switches every session in the process), and
`--model` is what takes a Cursor model id with its effort in it, such as
gpt-5.6-sol-max-fast. Its process group is stopped with the session, so
commands the agent started stop too.

Configuration: Cursor saves the model it was started with into its config
folder, and reads its permissions, MCP servers and hooks from there. So every
process gets CURSOR_CONFIG_DIR (and CURSOR_DATA_DIR) set to
<run folder>/cursor/<access>, which holds wf's cli-config.json (CONFIG):
nothing runs without asking, web search isn't accepted by itself, the sandbox
is off, and the agent doesn't credit itself in commits. Your ~/.cursor settings
don't apply and aren't changed. Sessions are saved in that folder too, so
`wf resume` can continue them; a session no later pass will continue is deleted
when it closes.
CURSOR_API_KEY and CURSOR_AUTH_TOKEN are removed from the environment, so the
local login is used and the agent's commands can't read a key, and
NO_OPEN_BROWSER keeps a logged-out cursor-agent from opening a login page.

submit_result is a tool on an MCP server the harness runs (mcp_http.py), given
to session/new as the server `wf`. Each session gets its own URL, so a call
can only reach the attempt it belongs to.

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
- full: everything is allowed.
Web search and fetch follow `web` at every level.

The model must be one `cursor-agent --list-models` lists (read when the harness
opens); any other makes the session unavailable, so a fallback harness takes
over. `effort` isn't used: Cursor model ids carry their own.

How a turn ends: Cursor reports a failed request as the turn's last message,
with stopReason end_turn, not as an error. So a last message "Error: ..."
fails the turn, and a sign-in, plan or payment notice, or an unauthenticated
or resource_exhausted error (a usage limit), makes it unavailable. Any
stopReason but end_turn fails it.

Usage: ACP reports neither tokens nor cost, so Cursor turns count as zero.

session: continue loads the previous pass's session (session/load) in a new
process with the same model. Cursor can't fork, so the session itself carries
on. It's marked while a continued attempt runs, and when that attempt fails,
the next one starts fresh instead of loading the failure.

What can't be switched off: the repo's own Cursor settings apply
(.cursor/cli.json can allow commands, and rules and AGENTS.md load), and
Cursor runs a web fetch its server marks as needing no approval, even with
`web: false`.

Tested with cursor-agent 2026.10.14-0f9edde.
"""

import asyncio
import contextlib
import itertools
import json
import os
import re
import shutil
import signal
from pathlib import Path

from .. import output_tool
from ..runs import write_json
from . import git_tool, mcp_http
from .base import SessionSpec, TurnEnd, Unavailable, Usage, short, transcript

COMMAND = ["cursor-agent"]
SERVER = "wf"  # the MCP server name the agent sees
CONFIG = {
    "version": 1,
    "permissions": {"allow": [], "deny": []},
    "approvalMode": "allowlist",
    "autoAcceptWebSearch": False,
    "sandbox": {"mode": "disabled"},
    "attribution": {"attributeCommitsToAgent": False, "attributePRsToAgent": False},
}
MODES = {"read-only": "ask", "edit": "agent", "full": "agent"}
SECRETS = ("CURSOR_API_KEY", "CURSOR_AUTH_TOKEN")
UNAVAILABLE = (  # in Cursor's last message: it can't run here now
    "Please sign in to continue", "Upgrade your plan to continue", "Add a payment method to continue",
    "Check your settings to continue", "[unauthenticated]", "[resource_exhausted]",
)
AUTH_REQUIRED = -32000  # ACP's error code for "authenticate first"
LABELS = {"read": "read", "edit": "edit", "delete": "delete", "move": "move", "search": "search",
          "execute": "command", "fetch": "web fetch"}


class Cursor:
    name = "cursor"

    def __init__(self, command: list[str] | None = None, request_timeout: float = 60):
        self.command = command or COMMAND
        self.request_timeout = request_timeout  # for starting a session; a turn has the node's timeout
        self.home = Path()
        self.models: set[str] = set()
        self.tools: mcp_http.Server | None = None
        self._configured: set[str] = set()

    async def open(self, run_dir: Path) -> None:
        self.home = Path(run_dir).resolve() / "cursor"  # absolute: cursor-agent runs in the workspace
        self.models = await self._list_models()
        self.tools = mcp_http.Server()
        await self.tools.start()

    async def session(self, spec: SessionSpec) -> "CursorSession":
        if spec.model not in self.models:
            raise Unavailable(f"Cursor has no model `{spec.model}`; `cursor-agent --list-models` lists the ones you can use")
        session = CursorSession(self, spec)
        await session.start()
        return session

    async def close(self) -> None:
        if self.tools is not None:
            await self.tools.close()
            self.tools = None

    def config(self, access: str) -> Path:
        """The config folder for sessions at this access level, holding wf's cli-config.json."""
        folder = self.home / access
        if access not in self._configured:
            folder.mkdir(parents=True, exist_ok=True)
            deny = ["Write(**)"] if access == "read-only" else []
            write_json(folder / "cli-config.json", {**CONFIG, "permissions": {"allow": [], "deny": deny}})
            self._configured.add(access)
        return folder

    def env(self, config: Path) -> dict:
        kept = {name: value for name, value in os.environ.items() if name not in SECRETS}
        return {**kept, "CURSOR_CONFIG_DIR": str(config), "CURSOR_DATA_DIR": str(config), "NO_OPEN_BROWSER": "1"}

    async def _list_models(self) -> set[str]:
        """The model ids `cursor-agent --list-models` prints, one `<id> - <name>` per line."""
        config = self.config("read-only")
        try:
            process = await asyncio.create_subprocess_exec(
                *self.command, "--list-models", cwd=config, env=self.env(config),
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
                raise Unavailable("`cursor-agent --list-models` didn't answer") from error
            raise
        text = re.sub(r"\x1b\[[0-9;]*m", "", out.decode(errors="replace"))
        models = {line.split(" - ", 1)[0].strip() for line in text.splitlines() if " - " in line}
        if process.returncode != 0 or not models:
            raise Unavailable(f"`cursor-agent --list-models` failed (is it logged in? `cursor-agent login`): {short(text, 300)}")
        return models


class CursorSession:
    def __init__(self, harness: Cursor, spec: SessionSpec):
        self.harness, self.spec = harness, spec
        self.id = ""
        self.config = harness.config(spec.access)
        self.rpc: _Connection | None = None
        self.url = ""  # this session's MCP server
        self.prompt: asyncio.Future | None = None  # the running session/prompt
        self.turn = False  # updates count only while a turn runs (session/load replays history)
        self.calls: dict[str, dict] = {}  # tool calls in progress, as their updates describe them
        self.text = ""  # the agent's message so far, for the transcript
        self.last_chunk = ""  # the turn's last message chunk, unless a tool call came after it
        # Marks a continued session while an attempt that continued it hasn't got a result
        self.continuing = self.config / "wf-continuing" / spec.fork_from if spec.fork_from else None

    async def start(self) -> None:
        spec, harness = self.spec, self.harness
        if self.continuing is not None and self.continuing.exists():
            raise Unavailable("the last attempt that continued this session failed, and Cursor can't fork it")
        tools = [mcp_http.Tool(output_tool.NAME, output_tool.DESCRIPTION, spec.tool.schema, self._submit)]
        if spec.access == "read-only":
            tools.append(mcp_http.Tool(git_tool.NAME, git_tool.DESCRIPTION, git_tool.SCHEMA, self._git))
        self.url = harness.tools.add(tools)
        command = [*harness.command, "--model", spec.model, "--sandbox", "disabled", "acp"]
        servers = [{"type": "http", "name": SERVER, "url": self.url, "headers": []}]
        try:
            self.rpc = await _Connection.start(command, spec.cwd, harness.env(self.config), harness.home / "cursor-agent.log",
                                               harness.request_timeout, self)
            await self.rpc.request("initialize", {
                "protocolVersion": 1, "clientInfo": {"name": "wf", "version": "0.1.0"},
                "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False}})
            await self.rpc.request("authenticate", {"methodId": "cursor_login"})
            if spec.fork_from:
                await self.rpc.request("session/load", {"sessionId": spec.fork_from, "cwd": str(spec.cwd), "mcpServers": servers})
                self.id = spec.fork_from
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
                   continued=bool(spec.fork_from), config=str(self.config))

    def pending_usage(self) -> Usage:
        return Usage()

    async def send(self, text: str) -> TurnEnd:
        log = self.spec.log
        transcript(log, "sent", text=text)
        self.text = self.last_chunk = ""
        self.turn = True  # before the request goes out: its updates can arrive before call() returns
        try:
            self.prompt = await self.rpc.call("session/prompt", {"sessionId": self.id, "prompt": [{"type": "text", "text": text}]})
            result = await asyncio.shield(self.prompt)  # a timeout stops the wait; close() stops the turn
        except _RpcError as error:
            if self.rpc.dead:
                return TurnEnd("failed", f"cursor: {self.rpc.dead}")
            return TurnEnd("unavailable" if error.code == AUTH_REQUIRED else "failed", f"cursor: {error}")
        finally:
            self.turn = False
            self._flush()
        stop = result.get("stopReason")
        transcript(log, "turn", stop_reason=stop)
        notice = self.last_chunk.strip() if self.last_chunk.startswith("\n\n") else ""
        if any(sign in notice for sign in UNAVAILABLE):
            return TurnEnd("unavailable", f"cursor: {notice}")
        if notice.startswith("Error:"):
            return TurnEnd("failed", f"cursor: {notice}")
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
            chunk = (update.get("content") or {}).get("text") or ""
            self.text += chunk
            self.last_chunk = chunk
        elif kind in ("tool_call", "tool_call_update"):
            self._flush()
            self.last_chunk = ""
            self._tool_call(update)

    def permit(self, params: dict) -> dict:
        """The answer to session/request_permission: the node's access level decides."""
        asked = params.get("toolCall") or {}
        call = {**self.calls.get(asked.get("toolCallId"), {}), **{k: v for k, v in asked.items() if v}}
        allowed = allows(self.spec.access, self.spec.web, self.spec.cwd, call)
        transcript(self.spec.log, "permission", tool_kind=call.get("kind"), title=call.get("title"), allowed=allowed)
        return choose(params.get("options") or [], allowed)

    def _tool_call(self, update: dict) -> None:
        call = self.calls.setdefault(update.get("toolCallId"), {})
        call.update({k: v for k, v in update.items() if v not in (None, {}, [])})
        status = call.get("status")
        if status == "in_progress" and not call.get("shown"):
            call["shown"] = True
            self.spec.emit({"event": "agent_tool", "tool": _label(call), "detail": _detail(call, self.spec.cwd)})
        elif status in ("completed", "failed"):
            self.calls.pop(update.get("toolCallId"), None)
            transcript(self.spec.log, "tool", title=call.get("title"), tool_kind=call.get("kind"), status=status,
                       input=call.get("rawInput"), output=short(call.get("rawOutput") or call.get("content") or "", 2000))

    def _flush(self) -> None:
        if self.text.strip():
            transcript(self.spec.log, "text", text=self.text)
        self.text = ""

    async def _submit(self, args: dict) -> tuple[str, bool]:
        text, is_error = self.spec.tool.submit(args)
        transcript(self.spec.log, "tool_result", tool=output_tool.NAME, is_error=is_error, content=text)
        return text, is_error

    async def _git(self, args: dict) -> tuple[str, bool]:
        return await asyncio.to_thread(git_tool.run, self.spec.cwd, args.get("args") or [])

    async def close(self) -> None:
        try:
            if self.rpc is not None:
                try:
                    if self.prompt is not None and not self.prompt.done() and not self.rpc.dead:  # a turn still running
                        with contextlib.suppress(Exception):
                            await self.rpc.notify("session/cancel", {"sessionId": self.id})
                            await asyncio.wait_for(self.prompt, 5)
                finally:
                    await self.rpc.stop()
        finally:  # even when cancelled: the URL must stop reaching this attempt
            if self.url and self.harness.tools is not None:
                self.harness.tools.remove(self.url)
            if self.continuing is not None and self.spec.tool.result is not None:
                self.continuing.unlink(missing_ok=True)
            if self.id and not self.spec.continuable:  # no later pass will load it
                shutil.rmtree(self.config / "acp-sessions" / self.id, ignore_errors=True)


def allows(access: str, web: bool, cwd: Path, call: dict) -> bool:
    """Is what a permission request asks for within the node's access level?"""
    kind = call.get("kind")
    if kind in ("fetch", "search"):  # web fetch and web search: searching files never asks
        return web
    if kind == "read" or access == "full":
        return True
    if kind == "other":  # an MCP tool: only wf's own
        return (call.get("rawInput") or {}).get("providerIdentifier") == SERVER
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
    tool = (call.get("rawInput") or {}).get("toolName")  # an MCP tool: submit_result, git
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
