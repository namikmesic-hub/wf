"""Claude Code: agent sessions through the Claude Agent SDK.

Each session is its own Claude Code process: a ClaudeSDKClient connected in
streaming mode, so the runner can send the instruction and then reminders as
further turns of the same conversation.

submit_result is an in-process MCP server (create_sdk_mcp_server): Claude
calls it like any tool, and the call arrives here as a Python function call.
Claude Code names MCP tools mcp__<server>__<tool>, so the agent sees
mcp__wf__submit_result.

wf tools (spec.tools) are in-process MCP servers too, one per tool, named after
it: the agent sees mcp__slack__slack_read_channel, and each call comes back
here to be passed on (tools/relay.py). Each operation the node may use is in
allowed_tools; the server lists no others. A node that lists `tools` gets no
other MCP server: strict_mcp_config leaves out the repo's and your own, and
ENABLE_CLAUDEAI_MCP_SERVERS=false your claude.ai connectors, which Claude Code
would otherwise load at `full` access. What a wf tool returns stays out of
the transcript, which notes only its size: it's other people's messages and
mail, and the run folder outlives the session.

Access levels (`options()`):
- read-only: Read, Grep and Glob, plus the runner's own `git` tool
  (git_tool.py) for diff, log, show, blame and status. No shell: even
  prefix-limited Bash rules can be talked into writing (`git diff
  --output=FILE`). permission_mode="dontAsk" refuses anything not in
  allowed_tools, instead of stopping to ask a person.
- edit: also Edit, Write, and any Bash command, still with "dontAsk".
- full: all of Claude Code's tools, with "bypassPermissions".
- web: WebSearch and WebFetch only when the node has `web: true`.

Every session also gets:
- the node's model and effort, explicitly, so your own Claude Code settings
  never decide them;
- setting_sources=["project"] and Claude Code's own system prompt: the repo's
  CLAUDE.md applies (or its AGENTS.md, when it has no CLAUDE.md), your
  user-level settings and memory don't. With `project_instructions: false`,
  setting_sources=[] leaves out the repo's side too: its CLAUDE.md and
  AGENTS.md files at every level, .claude/rules, .claude/settings.json and
  the skills in .claude/skills;
- your Claude subscription login. The Agent SDK passes its environment to
  Claude Code, and ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN there would take
  precedence over the login, so they're blanked for the Claude Code process
  only. claude.auth = "api-key" (WF_CLAUDE_AUTH) uses those variables instead.

Cost: in a multi-turn session, each turn's ResultMessage.usage covers only
that turn, but total_cost_usd is the session's running total, so a turn costs
the difference between two totals. A forked session's total also includes
what the session it came from spent (spec.prior_cost_usd), which isn't counted
again. A turn cut off by a timeout never gets its ResultMessage: its tokens
are summed from the assistant messages seen so far (pending_usage), and its
dollar cost is lost.

open() looks for the Claude Code program as the SDK does (its own copy, else
one on PATH or in a known folder), so a machine without it makes the harness
unavailable before a session starts. So does a program that you may not run.
A program that runs, but breaks, shows only when a session starts. The lookup
is the SDK's private _find_cli: with an SDK that lacks it, the first session
finds out instead, and a test fails.

A usage limit arrives as a RateLimitEvent with status "rejected"
("allowed_warning" only says it's getting close). That, a login error, or a
Claude Code that won't start makes the turn `unavailable`, which sends the
runner to the node's next harness.

Messages from `wf steer` (steer()): Claude Code keeps a user message that
comes while the agent works. The agent reads it at its next step, next to that
step's tool results. A message that comes after the agent's last step starts a
turn of its own once this one ends, with a result of its own. So every message
goes out with an id. With --replay-user-messages, Claude Code repeats each
message with its id when it reads it, and then a message from `wf steer` counts
as delivered. When a result comes while such a message is still unread, the
adapter reads on to that message's turn. But if the agent already submitted
its result, the session closes, and the message is dropped. A result that ends
the attempt (the agent submitted, or the turn failed) ends the turn at once,
also while a message is still being written: steer.py then cuts off that
write, and the message is unconfirmed. A message that Claude Code repeated
before the cut stays delivered. Until Claude Code has repeated a message in
this session, steer() takes none, and the runner sends it as the next turn.
"""

import asyncio
import os
import uuid
from dataclasses import replace

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ClaudeSDKError,
    CLINotFoundError,
    RateLimitEvent,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    create_sdk_mcp_server,
    tool,
)

from .. import config, output_tool
from ..output_tool import ResultTool
from . import git_tool
from .base import SessionSpec, TurnEnd, Unavailable, Usage, short, steer_dropped, steered, transcript

SERVER = "wf"
TOOL = f"mcp__{SERVER}__{output_tool.NAME}"
GIT = f"mcp__{SERVER}__{git_tool.NAME}"
SEARCH = ["Read", "Grep", "Glob"]
WEB = ["WebSearch", "WebFetch"]
API_KEY_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
CONNECTORS = "ENABLE_CLAUDEAI_MCP_SERVERS"  # false: Claude Code loads none of your claude.ai connectors
REPLAY = "replay-user-messages"  # Claude Code repeats each user message, with its id, when it reads it


class ClaudeCode:
    name = "claude-code"

    async def open(self, run_dir) -> None:
        find_program()  # nothing else is shared: every session is its own process

    async def check_model(self, model: str | None, access: str = "read-only") -> None:
        pass  # Claude Code lists no models: a session with a wrong one fails

    async def session(self, spec: SessionSpec) -> "ClaudeSession":
        session = ClaudeSession(spec)
        await session.start()
        return session

    async def close(self) -> None:
        pass


def find_program() -> None:
    """Raise Unavailable if the SDK can't find the Claude Code program, or finds one that can't run."""
    try:
        from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport

        transport = SubprocessCLITransport(prompt="", options=ClaudeAgentOptions())
        find = transport._find_cli
    except (ImportError, AttributeError, TypeError):  # another SDK version: the first session finds out
        return
    try:
        program = find()
    except CLINotFoundError as error:
        raise Unavailable(f"Claude Code isn't installed: {str(error).splitlines()[0]}") from error
    if not os.access(program, os.X_OK):  # the SDK takes a file it finds in a known folder as it is
        raise Unavailable(f"Claude Code can't run: {program} isn't a program that you may run")


def options(spec: SessionSpec) -> ClaudeAgentOptions:
    """The Claude Agent SDK options for one session."""
    web = WEB if spec.web else []
    if spec.access == "read-only":
        tools, allowed, mode, disallowed = [*SEARCH, *web], [*SEARCH, GIT, *web], "dontAsk", []
    elif spec.access == "edit":
        tools = [*SEARCH, "Edit", "Write", "Bash", *web]
        allowed, mode, disallowed = list(tools), "dontAsk", []
    else:  # full
        tools, allowed, mode, disallowed = None, [], "bypassPermissions", ([] if spec.web else list(WEB))
    use_api_key = config.get().claude.auth == "api-key"
    own_tools = [_submit_tool(spec.tool)] + ([_git_tool(spec.cwd)] if spec.access == "read-only" else [])
    servers = {SERVER: create_sdk_mcp_server(SERVER, tools=own_tools)}
    for server in spec.tools:
        servers[server.name] = create_sdk_mcp_server(server.name, tools=[_relayed(server, op) for op in server.operations])
        allowed += [f"mcp__{server.name}__{op.name}" for op in server.operations]
    budget = spec.budget_usd
    if budget is not None and spec.fork_from:
        budget += spec.prior_cost_usd  # the SDK caps the session's total, which includes what it was forked from
    return ClaudeAgentOptions(
        cwd=spec.cwd,
        model=spec.model,
        effort=spec.effort,
        max_turns=spec.max_turns,
        max_budget_usd=budget,
        tools=tools,
        allowed_tools=[*allowed, TOOL],
        disallowed_tools=disallowed,
        permission_mode=mode,
        setting_sources=["project"] if spec.project_instructions else [],
        system_prompt={"type": "preset", "preset": "claude_code"},
        mcp_servers=servers,
        strict_mcp_config=spec.only_wf_tools,
        env={**({} if use_api_key else {name: "" for name in API_KEY_VARS}),
             **({CONNECTORS: "false"} if spec.only_wf_tools else {})},
        resume=spec.fork_from,
        fork_session=spec.fork_from is not None,
        extra_args={REPLAY: None},
    )


def _submit_tool(result_tool: ResultTool):
    @tool(output_tool.NAME, output_tool.DESCRIPTION, result_tool.schema)
    async def submit_result(args: dict) -> dict:
        text, is_error = result_tool.submit(args)
        return {"content": [{"type": "text", "text": text}], "is_error": is_error}

    return submit_result


def _relayed(server, operation):
    """One operation of a wf tool, as an SDK MCP tool that passes each call on."""

    schema = operation.input_schema
    if schema.get("type") == "object" and "properties" not in schema:  # the SDK would read its keys as parameters
        schema = {**schema, "properties": {}}

    @tool(operation.name, operation.description, schema, annotations=operation.annotations)
    async def relay(args: dict) -> dict:
        content, is_error = await server.call(operation.name, args)
        return {"content": content, "is_error": is_error}

    return relay


def _git_tool(cwd):
    @tool(git_tool.NAME, git_tool.DESCRIPTION, git_tool.SCHEMA)
    async def git(args: dict) -> dict:
        text, is_error = await asyncio.to_thread(git_tool.run, cwd, args.get("args") or [])
        return {"content": [{"type": "text", "text": text}], "is_error": is_error}

    return git


class ClaudeSession:
    def __init__(self, spec: SessionSpec):
        self.spec = spec
        self.id = ""
        self._client: ClaudeSDKClient | None = None
        self._cost_so_far = spec.prior_cost_usd if spec.fork_from else 0.0  # the running total, as last reported
        self._turn = Usage()  # tokens of the turn in progress, from its assistant messages
        self._spent = Usage()  # what the results of the send() in progress reported
        self._seen: set[str] = set()  # message ids already counted in _turn
        self._relayed: set[str] = set()  # ids of the calls to wf tools, whose results aren't kept
        self._tool_prefixes = tuple(f"mcp__{server.name}__" for server in spec.tools)
        self._working = False  # is a send() in progress?
        self._repeats = False  # has Claude Code repeated a message it read (REPLAY)?
        self._held: list[str] = []  # ids of messages from wf steer that Claude Code hasn't read yet
        self._writing: set[str] = set()  # ids of messages from wf steer being written now
        self._written = asyncio.Event()  # no message from wf steer is being written
        self._written.set()

    async def start(self) -> None:
        self._client = ClaudeSDKClient(options(self.spec))
        try:
            await self._client.connect()
        except BaseException as error:  # a failed or cancelled connect can leave the CLI running
            await self.close()
            if isinstance(error, (ClaudeSDKError, OSError)):
                raise Unavailable(f"Claude Code didn't start: {error}") from error
            raise

    def pending_usage(self) -> Usage:
        pending = replace(self._spent)
        pending.add(self._turn)
        return pending

    async def steer(self, text: str, ident: str) -> str | None:
        if not self._working or not self._repeats:
            return None
        self._held.append(ident)  # first: Claude Code can read it, and say so, before the write returns
        self._writing.add(ident)
        self._written.clear()
        try:
            await self._say(text, ident)
        except BaseException as error:
            if ident in self._held:  # not read: the write failed, or was cut off
                self._held.remove(ident)
                raise
            if isinstance(error, asyncio.CancelledError):  # cut off after Claude Code read it: still delivered
                asyncio.current_task().cancel()  # and the task still stops, at its next wait
            elif not isinstance(error, Exception):
                raise
        finally:
            self._writing.discard(ident)
            if not self._writing:
                self._written.set()
        return "queued" if ident in self._held else "delivered"  # read during the write: steer.py records it

    async def send(self, text: str) -> TurnEnd:
        transcript(self.spec.log, "sent", text=text)
        self._turn, self._seen, self._spent = Usage(), set(), Usage()
        prompt = str(uuid.uuid4())
        self._working = True
        try:
            await self._say(text, prompt)
            status, message = await self._read(prompt)
        finally:
            self._working = False
        usage, self._spent = self._spent, Usage()
        return TurnEnd(status, message, usage)

    async def _say(self, text: str, ident: str) -> None:
        """Write a user message with an id, which Claude Code repeats when it reads the message."""

        async def message():
            yield {"type": "user", "message": {"role": "user", "content": text}, "parent_tool_use_id": None, "uuid": ident}

        await self._client.query(message())

    async def _read(self, prompt: str) -> tuple[str, str]:
        """Claude Code's messages, up to the result that ends the turn: (status, message). Whether the
        agent submitted comes from the messages read so far, not from the ResultTool. The SDK answers
        tool calls as they come, before this reads up to them."""
        log, emit = self.spec.log, self.spec.emit
        status, message, submits, submitted = "done", "", set(), False
        async for item in self._client.receive_messages():
            if isinstance(item, SystemMessage):
                if item.subtype == "init":
                    self.id = item.data.get("session_id", self.id)
            elif isinstance(item, UserMessage) and item.uuid == prompt:
                self._repeats = True
            elif isinstance(item, UserMessage) and item.uuid in self._held:
                self._held.remove(item.uuid)
                if item.uuid not in self._writing:  # else steer() says it was delivered, and steer.py records that
                    steered(self.spec, item.uuid, "delivered")
            elif isinstance(item, AssistantMessage):
                if item.usage and item.message_id not in self._seen:  # several messages can share one response
                    self._seen.add(item.message_id)
                    self._turn.add(_tokens(item.usage))  # output is a placeholder here
                for block in item.content:
                    if isinstance(block, TextBlock) and block.text.strip():
                        transcript(log, "text", text=block.text)
                        self.spec.trace.text(block.text)
                    elif isinstance(block, ToolUseBlock):
                        if block.name.startswith(self._tool_prefixes):
                            self._relayed.add(block.id)
                        if block.name == TOOL:
                            submits.add(block.id)
                        transcript(log, "tool", name=block.name, input=block.input)
                        self.spec.trace.tool_call(block.id, _tool_name(block.name), block.input)
                        emit({"event": "agent_tool", "tool": _tool_name(block.name), "detail": _detail(block, self.spec.cwd)})
            elif isinstance(item, UserMessage) and isinstance(item.content, list):
                for block in item.content:
                    if isinstance(block, ToolResultBlock) and block.tool_use_id in submits and not block.is_error:
                        submitted = True
                    if isinstance(block, ToolResultBlock) and block.tool_use_id in self._relayed:
                        transcript(log, "tool_result", is_error=bool(block.is_error), kept=False,
                                   characters=len(str(block.content)))
                        self.spec.trace.tool_result(block.tool_use_id, block.content, bool(block.is_error), kept=False)
                    elif isinstance(block, ToolResultBlock):
                        transcript(log, "tool_result", is_error=bool(block.is_error), content=short(block.content, 2000))
                        self.spec.trace.tool_result(block.tool_use_id, block.content, bool(block.is_error))
            elif isinstance(item, RateLimitEvent):
                info = item.rate_limit_info
                transcript(log, "rate_limit", status=info.status, resets_at=info.resets_at)
                if info.status == "rejected":
                    status, message = "unavailable", f"Claude usage limit reached (resets at {info.resets_at})"
            elif isinstance(item, ResultMessage):
                self.id = item.session_id or self.id
                usage, self._turn = self._usage(item), Usage()  # the result's own numbers replace the estimate
                self._spent.add(usage)
                transcript(log, "result", subtype=item.subtype, is_error=item.is_error, result=item.result,
                           tokens=usage.tokens, cost_usd=usage.cost_usd)
                if item.is_error and status == "done":
                    reason = item.result or ", ".join(item.errors or []) or item.subtype
                    status, message = ("unavailable" if _login_problem(item) else "failed"), f"Claude Code: {reason}"
                if status != "done" or submitted:  # the attempt ends now: steer.py cuts off a write still going
                    break
                while self._writing:  # whether a message being written gets a turn of its own depends on the write
                    await self._written.wait()
                if not self._held:
                    break
                # a message from wf steer came after the agent's last step: its own turn follows
        return status, message

    def _usage(self, result: ResultMessage) -> Usage:
        usage = _tokens(result.usage or {})
        total = result.total_cost_usd or 0.0
        usage.cost_usd = max(total - self._cost_so_far, 0.0)  # a crashed session can report 0
        self._cost_so_far = max(self._cost_so_far, total)
        return usage

    async def close(self) -> None:
        for ident in self._held:
            steer_dropped(self.spec, ident)
        self._held.clear()
        if self._client is not None:
            client, self._client = self._client, None
            try:
                await client.disconnect()
            except Exception as error:  # already gone, or teardown failed: keep a trace of it
                transcript(self.spec.log, "close_failed", error=f"{type(error).__name__}: {error}")


def _tokens(counts: dict) -> Usage:
    """The tokens in the usage of a result or a message. Claude Code counts the cached input apart
    from input_tokens, and the thinking in output_tokens. output_tokens_details gives the thinking
    when the SDK has it."""
    def count(key: str) -> int:
        return int(counts.get(key) or 0)

    details = counts.get("output_tokens_details")
    thinking = details.get("thinking_tokens") if isinstance(details, dict) else None
    usage = Usage(input_tokens=count("input_tokens"), output_tokens=count("output_tokens"),
                  cache_read_tokens=count("cache_read_input_tokens"), cache_write_tokens=count("cache_creation_input_tokens"),
                  reasoning_tokens=int(thinking or 0))
    usage.tokens = usage.input_tokens + usage.output_tokens + usage.cache_read_tokens + usage.cache_write_tokens
    return usage


def _login_problem(result: ResultMessage) -> bool:
    text = f"{result.result or ''} {' '.join(result.errors or [])}".lower()
    return result.api_error_status in (401, 403, 429) or any(
        word in text for word in ("/login", "not logged in", "invalid api key", "authentication", "usage limit"))


def _tool_name(name: str) -> str:
    """For progress lines: mcp__wf__submit_result is submit_result, and a wf tool's
    mcp__linear__list_issues is linear.list_issues, as on the other harnesses."""
    if name.startswith(f"mcp__{SERVER}__") or not name.startswith("mcp__"):
        return name.removeprefix(f"mcp__{SERVER}__")
    server, _, operation = name.removeprefix("mcp__").partition("__")
    return f"{server}.{operation}"


def _detail(block: ToolUseBlock, cwd) -> str:
    """A few words about a tool call, for the progress line. Paths are shown relative to the workspace."""
    data = block.input or {}
    if isinstance(data.get("args"), list):  # the runner's git tool: ["diff", "main...HEAD"]
        return short(" ".join(str(part) for part in data["args"]), 80)
    for key in ("file_path", "path", "pattern", "command", "query", "url", "description"):
        if data.get(key):
            text = str(data[key])
            if key in ("file_path", "path"):
                text = text.removeprefix(str(cwd).rstrip("/") + "/")
            return short(text, 80)
    return ""
