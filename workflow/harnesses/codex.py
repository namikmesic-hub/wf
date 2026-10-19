"""Codex: agent threads through `codex app-server`, JSON-RPC over stdio.

One app server process serves every Codex session in a stream of the graph:
`codex app-server --disable hooks --disable apps`, then `initialize` (with
experimentalApi, which dynamic tools need) and the `initialized`
notification. Messages are JSON objects, one per line, in both directions;
requests carry an id, notifications don't, and either side can send requests.

A session is a thread (thread/start), and a turn is turn/start followed by
notifications (item/started, item/completed, thread/tokenUsage/updated, ...)
until turn/completed. A reminder is another turn on the same thread.

Every setting is sent explicitly, because Codex fills any gap from your
~/.codex/config.toml: in testing, a thread started without a model silently
took the user's configured model and `xhigh` effort. So the model defaults to
the default in the app server's model/list, and the effort to that model's
default effort.

submit_result is a dynamic tool declared at thread/start. When the agent calls
it, the app server sends an `item/tool/call` request on the same connection
and waits for the reply with no timeout of its own. So _Connection replies to
every call, unknown tools and errors included; a missing reply would hang
the turn. Only the reply's text reaches the model, so problems are spelled
out there.

wf tools (spec.tools) are dynamic tools too: a namespace per tool, named after
it, holding the operations the node may use. A call arrives with that
`namespace` and is passed on to the vendor (tools/relay.py). Such a call can
take a while, so every request from the app server is answered in a task of
its own, and the connection keeps reading meanwhile. The transcript keeps such
a call's operation, arguments and outcome, but not what the vendor returned.

A node that lists `tools` gets no other MCP server. `--disable apps` already
keeps your ChatGPT connectors out of every thread. The thread's config also
switches off each MCP server your config.toml names, and each plugin, since
plugins bring MCP servers of their own: config/read says which there are.

Access levels map to Codex's sandbox: read-only, workspace-write and
danger-full-access, with approvalPolicy "never" so nothing stops to ask (any
approval request that still arrives is declined). Web search is "live" with
`web: true` and "disabled" otherwise (Codex's default, "cached", is never
left in place), and a workspace-write sandbox gets network access only with
`web: true`.

Usage: thread/tokenUsage/updated reports the thread's running total, so a
turn's tokens are the difference. A forked thread (session: continue) starts
with its source's total, which an earlier pass already counted: the first
report's total minus its `last` (the response just made) is that inherited
part, and counting starts from there. Codex reports no dollar cost.

Limits: an `error` notification with codexErrorInfo "usageLimitExceeded" (or
"unauthorized") that Codex won't retry makes the turn unavailable, which sends
the runner to the next harness. A plain "rateLimitExceeded" is retried by
Codex itself, so it's only noted.

Instruction files: a thread loads the repo's AGENTS.md files, from the git root
down to its cwd, and the skills in the repo's .agents/skills and .codex/skills.
With `project_instructions: false`, project_doc_max_bytes 0 keeps out the
AGENTS.md files, and each repo skill that skills/list names is switched off in
the thread's config.

What can't be switched off: your global ~/.codex/AGENTS.md always loads. The
skills in your home folder load too, with `project_instructions: false` as well.

The app server is experimental, so wf is tested against one Codex version
(TESTED) and warns when the installed one differs.
"""

import asyncio
import contextlib
import itertools
import json
import subprocess
import time
from pathlib import Path

from .. import output_tool
from .base import SessionSpec, TurnEnd, Unavailable, Usage, short, transcript

TESTED = "0.163.0"
COMMAND = ["codex", "app-server", "--disable", "hooks", "--disable", "apps"]
SANDBOX = {"read-only": "read-only", "edit": "workspace-write", "full": "danger-full-access"}
UNAVAILABLE = {"usageLimitExceeded", "unauthorized"}
DECLINE = {  # server requests that ask a person something: always no
    "item/commandExecution/requestApproval": {"decision": "decline"},
    "item/fileChange/requestApproval": {"decision": "decline"},
    "execCommandApproval": {"decision": "denied"},
    "applyPatchApproval": {"decision": "denied"},
    "mcpServer/elicitation/request": {"action": "decline"},
    "item/tool/requestUserInput": {"answers": {}},
}


class Codex:
    name = "codex"

    def __init__(self, command: list[str] | None = None, request_timeout: float = 60):
        self.command = command or COMMAND
        self.request_timeout = request_timeout  # a wedged app server counts as unavailable after this
        self.rpc: _Connection | None = None
        self.models: list[dict] = []
        self.warning: str | None = None  # shown once, with the first session

    async def open(self, run_dir: Path) -> None:
        try:
            self.rpc = await _Connection.start(self.command, Path(run_dir) / "codex-app-server.log", self.request_timeout)
            await self.rpc.request("initialize", {"clientInfo": {"name": "wf", "version": "0.1.0"},
                                                  "capabilities": {"experimentalApi": True}})
            await self.rpc.notify("initialized")
            self.models = (await self.rpc.request("model/list", {})).get("data", [])
        except BaseException as error:  # failed or cancelled (Ctrl-C): don't leave the app server running
            await self.close()
            if isinstance(error, (OSError, _RpcError, TimeoutError)):
                raise Unavailable(f"the Codex app server didn't start: {error}") from error
            raise
        if self.command is COMMAND:
            installed = await asyncio.to_thread(_version)
            if installed and installed != TESTED:
                self.warning = f"codex {installed} is installed; wf is tested with {TESTED}, and the app server is experimental"

    async def session(self, spec: SessionSpec) -> "CodexSession":
        if self.rpc is None or self.rpc.dead:
            raise Unavailable(self.rpc.dead if self.rpc else "the Codex app server isn't running")
        if self.warning:
            spec.emit({"event": "warning", "message": self.warning})
            self.warning = None
        session = CodexSession(self, spec)
        await session.start()
        return session

    async def close(self) -> None:
        if self.rpc is not None:
            await self.rpc.stop()
            self.rpc = None

    def defaults(self, model: str | None) -> tuple[str | None, str | None]:
        """The model to use, and its default effort, from the app server's model list."""
        if model is None:
            model = next((m.get("id") for m in self.models if isinstance(m, dict) and m.get("isDefault")), None)
        effort = next((m.get("defaultReasoningEffort") for m in self.models if isinstance(m, dict) and m.get("id") == model), None)
        return model, effort


class CodexSession:
    def __init__(self, harness: Codex, spec: SessionSpec):
        self.harness, self.spec, self.rpc = harness, spec, harness.rpc
        self.id = ""
        self.events: asyncio.Queue = asyncio.Queue()  # notifications for this thread, from _Connection
        self.turn: str | None = None
        self._total = 0  # the thread's token count, as last reported
        self._turn_start = 0
        self._counted = False  # has a token count come in yet?

    async def start(self) -> None:
        spec = self.spec
        model, default_effort = self.harness.defaults(spec.model)
        effort = spec.effort or default_effort
        config = {"web_search": "live" if spec.web else "disabled"}
        if effort:
            config["model_reasoning_effort"] = effort
        if not model or not effort:  # don't let a gap pass silently: Codex fills it from your config
            missing = " and ".join(w for w, v in (("model", model), ("effort", effort)) if not v)
            spec.emit({"event": "warning", "message": f"codex: no {missing} set or known from model/list, "
                                                      f"so your ~/.codex/config.toml decides it"})
        if spec.access == "edit":
            config["sandbox_workspace_write"] = {"network_access": spec.web}
        if spec.only_wf_tools:
            config.update(await self._without_mcp_servers())
        if not spec.project_instructions:
            config.update(await self._without_repo_instructions())
        params = {
            "cwd": str(spec.cwd), "sandbox": SANDBOX[spec.access], "approvalPolicy": "never",
            **({"model": model} if model else {}),
            "ephemeral": not spec.continuable,  # saved only when a later pass may fork it
            "config": config,
            "dynamicTools": [{"type": "function", "name": output_tool.NAME, "description": output_tool.DESCRIPTION,
                              "inputSchema": spec.tool.schema}] + [_namespace(server) for server in spec.tools],
        }
        try:
            if spec.fork_from:
                result = await self.rpc.request("thread/fork", {"threadId": spec.fork_from, **params})
            else:
                result = await self.rpc.request("thread/start", params)
        except (_RpcError, TimeoutError) as error:  # TimeoutError: the app server stopped answering
            raise Unavailable(f"Codex didn't start a thread: {error if str(error) else 'no answer'}") from error
        thread_id = (result.get("thread") or {}).get("id") if isinstance(result, dict) else None
        if not thread_id:  # the protocol changed under us: try the next harness
            raise Unavailable(f"Codex didn't start a thread: unexpected reply {short(result, 200)}")
        self.id = thread_id
        self.rpc.threads[self.id] = self
        transcript(spec.log, "thread", id=self.id, model=result.get("model"), sandbox=params["sandbox"], config=config)

    async def _without_mcp_servers(self) -> dict:
        """Thread config that switches off the MCP servers and plugins Codex would load here.
        A server that the config doesn't name can't be switched off by name (Codex refuses the
        thread), which is why plugins go too: they bring theirs."""
        try:
            found = (await self.rpc.request("config/read", {"cwd": str(self.spec.cwd)})).get("config") or {}
        except (_RpcError, TimeoutError) as error:
            raise Unavailable(f"Codex didn't say which MCP servers it has, so wf can't keep them out: {error}") from error
        off = {"enabled": False}
        return {"mcp_servers": {name: off for name in found.get("mcp_servers") or {}},
                "plugins": {name: off for name in found.get("plugins") or {}}}

    async def _without_repo_instructions(self) -> dict:
        """Thread config that keeps out the repo's AGENTS.md files (and a fallback such as CLAUDE.md), and
        switches off each skill skills/list finds in the repo."""
        try:
            listed = await self.rpc.request("skills/list", {"cwds": [str(self.spec.cwd)], "forceReload": True})
        except (_RpcError, TimeoutError) as error:
            raise Unavailable(f"Codex didn't list its skills, so wf can't keep the repo's out: {error}") from error
        paths = [skill["path"] for entry in listed.get("data") or [] for skill in entry.get("skills") or []
                 if skill.get("scope") == "repo" and skill.get("path")]
        return {"project_doc_max_bytes": 0, "skills": {"config": [{"path": path, "enabled": False} for path in paths]}}

    def pending_usage(self) -> Usage:
        return Usage(tokens=self._total - self._turn_start) if self.turn else Usage()

    async def send(self, text: str) -> TurnEnd:
        log = self.spec.log
        transcript(log, "sent", text=text)
        self._turn_start = self._total
        try:
            result = await self.rpc.request("turn/start", {"threadId": self.id,
                                                           "input": [{"type": "text", "text": text, "text_elements": []}]})
        except TimeoutError:
            return TurnEnd("unavailable", "the Codex app server stopped answering")
        except _RpcError as error:
            if self.rpc.dead:
                return TurnEnd("unavailable", self.rpc.dead)
            return TurnEnd("failed", f"codex: turn/start: {error}")
        turn = (result.get("turn") or {}).get("id") if isinstance(result, dict) else None
        if not turn:
            return TurnEnd("unavailable", f"codex: unexpected turn/start reply {short(result, 200)}")
        self.turn = turn
        status, message = "done", ""
        while True:
            event = await self.events.get()
            method, params = event.get("method"), event.get("params") or {}
            if method == "closed":  # the app server died: it won't come back in this run
                status, message = "unavailable", self.rpc.dead or "the Codex app server exited"
                break
            if method == "item/started":
                self._progress(params.get("item") or {})
            elif method == "item/completed":
                self._record(params.get("item") or {})
            elif method == "thread/tokenUsage/updated":
                self._count(params.get("tokenUsage") or {})
            elif method == "error":
                error = params.get("error") or {}
                transcript(log, "error", error=error, will_retry=params.get("willRetry"))
                if not params.get("willRetry"):
                    kind = "unavailable" if _code(error) in UNAVAILABLE else "failed"
                    status, message = kind, error.get("message") or str(_code(error))
            elif method == "turn/completed" and (params.get("turn") or {}).get("id") == self.turn:
                turn = params["turn"]
                transcript(log, "turn", status=turn.get("status"), error=turn.get("error"))
                if turn.get("status") == "failed" and status == "done":
                    error = turn.get("error") or {}
                    status = "unavailable" if _code(error) in UNAVAILABLE else "failed"
                    message = error.get("message") or "the turn failed"
                elif turn.get("status") == "interrupted" and status == "done":
                    status, message = "failed", "the turn was interrupted"
                break
        self.turn = None
        if status == "failed" and not message.startswith("codex"):
            message = f"codex: {message}"
        return TurnEnd(status, message, Usage(tokens=self._total - self._turn_start))

    def _count(self, usage: dict) -> None:
        total = int((usage.get("total") or {}).get("totalTokens") or self._total)
        if not self._counted:  # the thread's tokens from before this session (a fork's source) aren't ours
            self._counted = True
            inherited = total - int((usage.get("last") or {}).get("totalTokens") or 0)
            self._turn_start += max(inherited, 0)
        self._total = total

    def _progress(self, item: dict) -> None:
        """A progress line for the things worth seeing: commands, edits, searches, tool calls."""
        kind = item.get("type")
        if kind == "commandExecution":
            command = item.get("command")
            self.spec.emit({"event": "agent_tool", "tool": "command",
                            "detail": short(" ".join(command) if isinstance(command, list) else command, 80)})
        elif kind == "fileChange":
            root = str(self.spec.cwd).rstrip("/") + "/"
            paths = [str(c.get("path", "")).removeprefix(root) for c in item.get("changes") or []]
            self.spec.emit({"event": "agent_tool", "tool": "edit", "detail": short(", ".join(paths), 80)})
        elif kind == "webSearch":
            self.spec.emit({"event": "agent_tool", "tool": "web search", "detail": short(item.get("query") or "", 80)})
        elif kind in ("mcpToolCall", "dynamicToolCall"):
            name = item.get("tool") or kind  # a wf tool's is linear.list_issues, as on the other harnesses
            self.spec.emit({"event": "agent_tool", "tool": f"{item['namespace']}.{name}" if item.get("namespace") else name,
                            "detail": ""})

    def _record(self, item: dict) -> None:
        kind = item.get("type")
        if kind == "agentMessage":
            transcript(self.spec.log, "text", text=item.get("text"))
        elif kind == "commandExecution":
            transcript(self.spec.log, "command", command=item.get("command"), exit_code=item.get("exitCode"),
                       output=short(item.get("aggregatedOutput") or "", 2000))
        elif kind == "dynamicToolCall" and item.get("namespace") in {server.name for server in self.spec.tools}:
            transcript(self.spec.log, "tool", item={key: value for key, value in item.items() if key != "contentItems"},
                       kept=False, characters=sum(len(c.get("text") or c.get("imageUrl") or "") for c in item.get("contentItems") or []))
        elif kind in ("fileChange", "webSearch", "mcpToolCall", "dynamicToolCall"):
            transcript(self.spec.log, "tool", item=item)

    async def close(self) -> None:
        try:
            if self.turn and not self.rpc.dead:  # a turn still running (a timeout): stop it
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(self.rpc.request("turn/interrupt", {"threadId": self.id, "turnId": self.turn}), 5)
        finally:  # even when cancelled: the connection must stop delivering to a closed session
            self.rpc.threads.pop(self.id, None)
            self.turn = None


class _RpcError(Exception):
    pass


class _Connection:
    """JSON-RPC over the app server's stdin and stdout: requests both ways, notifications."""

    def __init__(self, process: asyncio.subprocess.Process, stderr, timeout: float):
        self.process, self.stderr, self.timeout = process, stderr, timeout
        self.ids = itertools.count(1)
        self.pending: dict[int, asyncio.Future] = {}
        self.threads: dict[str, CodexSession] = {}
        self.answering: set[asyncio.Task] = set()  # replies to the app server's requests, in progress
        self.dead: str | None = None  # why the connection ended, once it has
        self.reader = asyncio.ensure_future(self._read())

    @classmethod
    async def start(cls, command: list[str], stderr_log: Path, timeout: float = 60) -> "_Connection":
        stderr = stderr_log.open("a", encoding="utf-8")
        try:
            process = await asyncio.create_subprocess_exec(
                *command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=stderr,
                limit=64 * 1024 * 1024,  # one JSON message per line, and some are large
            )
        except OSError:
            stderr.close()
            raise
        return cls(process, stderr, timeout)

    async def request(self, method: str, params: dict, timeout: float | None = None) -> dict:
        if self.dead:
            raise _RpcError(self.dead)
        request_id = next(self.ids)
        future = self.pending[request_id] = asyncio.get_running_loop().create_future()
        try:
            await self._write({"id": request_id, "method": method, "params": params})
            return await asyncio.wait_for(future, timeout or self.timeout)
        except OSError as error:  # its stdin is gone: the app server died after the check above
            raise _RpcError(self.dead or f"lost the Codex app server: {error}") from error
        finally:
            self.pending.pop(request_id, None)

    async def notify(self, method: str, params: dict | None = None) -> None:
        await self._write({"method": method, **({"params": params} if params is not None else {})})

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
                if "method" in message and "id" in message:
                    task = asyncio.ensure_future(self._answer(message))
                    self.answering.add(task)
                    task.add_done_callback(self.answering.discard)
                elif "method" in message:
                    session = self.threads.get((message.get("params") or {}).get("threadId"))
                    if session is not None:
                        session.events.put_nowait(message)
                elif message.get("id") in self.pending:
                    future = self.pending[message["id"]]
                    if not future.done():
                        if "error" in message:
                            future.set_exception(_RpcError((message["error"] or {}).get("message", "error")))
                        else:
                            future.set_result(message.get("result") or {})
        except (OSError, ValueError) as error:
            self.dead = f"lost the Codex app server: {error}"
        finally:
            self.dead = self.dead or "the Codex app server exited"
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(_RpcError(self.dead))
            for session in self.threads.values():
                session.events.put_nowait({"method": "closed"})

    async def _answer(self, message: dict) -> None:
        """Reply to a request from the app server. Every request gets a reply."""
        method, params, request_id = message["method"], message.get("params") or {}, message["id"]
        try:
            if method == "item/tool/call":
                result = await self._tool_call(params)
            elif method in DECLINE:
                result = DECLINE[method]
            elif method == "currentTime/read":
                result = {"currentTimeAt": int(time.time())}
            else:
                result = None
        except Exception as error:  # never leave the app server waiting
            result = _tool_reply(f"Error: {type(error).__name__}: {error}", False)
        reply = {"result": result} if result is not None else {
            "error": {"code": -32601, "message": f"wf doesn't handle {method}"}}
        with contextlib.suppress(OSError):  # the app server is gone: the reader notices that
            await self._write({"id": request_id, **reply})

    async def _tool_call(self, params: dict) -> dict:
        session = self.threads.get(params.get("threadId"))
        server = next((t for t in session.spec.tools if t.name == params.get("namespace")), None) if session else None
        if server is not None:
            content, is_error = await server.call(params.get("tool"), params.get("arguments"))
            return {"success": not is_error, "contentItems": [_content_item(block) for block in content]}
        if session is None or params.get("namespace") or params.get("tool") != output_tool.NAME:
            return _tool_reply(f"Error: there's no tool called `{params.get('tool')}` here. "
                               f"Call `{output_tool.NAME}` with your result.", False)
        text, is_error = session.spec.tool.submit(params.get("arguments"))  # (item/started already showed the call)
        transcript(session.spec.log, "tool_result", tool=output_tool.NAME, is_error=is_error, content=text)
        return _tool_reply(text, not is_error)

    async def stop(self) -> None:
        if self.process.returncode is None:
            with contextlib.suppress(Exception):
                self.process.stdin.close()
            with contextlib.suppress(ProcessLookupError):
                self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 5)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    self.process.kill()
                await self.process.wait()
        self.reader.cancel()
        for task in self.answering:
            task.cancel()
        # gather() hands back the reader's own cancellation as a value, but still lets a
        # cancellation of this task (Ctrl-C during teardown) through
        await asyncio.gather(self.reader, *self.answering, return_exceptions=True)
        self.stderr.close()


def _tool_reply(text: str, success: bool) -> dict:
    return {"success": success, "contentItems": [{"type": "inputText", "text": text}]}


def _namespace(server) -> dict:
    """A wf tool as a namespace of dynamic tools: the operations its node may use."""
    return {"type": "namespace", "name": server.name, "description": server.description,
            "tools": [{"type": "function", "name": op.name, "description": op.description, "inputSchema": op.input_schema}
                      for op in server.operations]}


def _content_item(block: dict) -> dict:
    if block.get("type") == "image":
        return {"type": "inputImage", "imageUrl": f"data:{block['mimeType']};base64,{block['data']}"}
    return {"type": "inputText", "text": block.get("text", "")}


def _code(error: dict):
    info = error.get("codexErrorInfo")
    return info if isinstance(info, str) else next(iter(info), None) if isinstance(info, dict) else None


def _version() -> str | None:
    try:
        out = subprocess.run(["codex", "--version"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.strip().split()[-1] if out.strip() else None
