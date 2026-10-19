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
allowed_tools; the server lists no others.

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
  CLAUDE.md applies, your user-level settings and memory don't;
- your Claude subscription login. The Agent SDK passes its environment to
  Claude Code, and ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN there would take
  precedence over the login, so they're blanked for the Claude Code process
  only. Set WF_CLAUDE_AUTH=api-key to use those variables instead.

Cost: in a multi-turn session, each turn's ResultMessage.usage covers only
that turn, but total_cost_usd is the session's running total, so a turn costs
the difference between two totals. A forked session's total also includes
what the session it came from spent (spec.prior_cost_usd), which isn't counted
again. A turn cut off by a timeout never gets its ResultMessage: its tokens
are summed from the assistant messages seen so far (pending_usage), and its
dollar cost is lost.

A usage limit arrives as a RateLimitEvent with status "rejected"
("allowed_warning" only says it's getting close). That, a login error, or a
Claude Code that won't start makes the turn `unavailable`, which sends the
runner to the node's next harness.
"""

import asyncio
import os

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ClaudeSDKError,
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

from .. import output_tool
from ..output_tool import ResultTool
from . import git_tool
from .base import SessionSpec, TurnEnd, Unavailable, Usage, short, transcript

SERVER = "wf"
TOOL = f"mcp__{SERVER}__{output_tool.NAME}"
GIT = f"mcp__{SERVER}__{git_tool.NAME}"
SEARCH = ["Read", "Grep", "Glob"]
WEB = ["WebSearch", "WebFetch"]
API_KEY_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


class ClaudeCode:
    name = "claude-code"

    async def open(self, run_dir) -> None:
        pass  # nothing shared: every session is its own process

    async def session(self, spec: SessionSpec) -> "ClaudeSession":
        session = ClaudeSession(spec)
        await session.start()
        return session

    async def close(self) -> None:
        pass


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
    use_api_key = os.environ.get("WF_CLAUDE_AUTH") == "api-key"
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
        setting_sources=["project"],
        system_prompt={"type": "preset", "preset": "claude_code"},
        mcp_servers=servers,
        env={} if use_api_key else {name: "" for name in API_KEY_VARS},
        resume=spec.fork_from,
        fork_session=spec.fork_from is not None,
    )


def _submit_tool(result_tool: ResultTool):
    @tool(output_tool.NAME, output_tool.DESCRIPTION, result_tool.schema)
    async def submit_result(args: dict) -> dict:
        text, is_error = result_tool.submit(args)
        return {"content": [{"type": "text", "text": text}], "is_error": is_error}

    return submit_result


def _relayed(server, operation):
    """One operation of a wf tool, as an SDK MCP tool that passes each call on."""

    @tool(operation.name, operation.description, operation.input_schema, annotations=operation.annotations)
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
        self._seen: set[str] = set()  # message ids already counted in _turn

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
        return self._turn

    async def send(self, text: str) -> TurnEnd:
        log, emit = self.spec.log, self.spec.emit
        transcript(log, "sent", text=text)
        self._turn, self._seen = Usage(), set()
        await self._client.query(text)
        status, message, usage = "done", "", Usage()
        async for item in self._client.receive_response():
            if isinstance(item, SystemMessage):
                if item.subtype == "init":
                    self.id = item.data.get("session_id", self.id)
            elif isinstance(item, AssistantMessage):
                if item.usage and item.message_id not in self._seen:  # several messages can share one response
                    self._seen.add(item.message_id)
                    self._turn.tokens += sum(int(item.usage.get(k) or 0) for k in  # output is a placeholder here
                                             ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens"))
                for block in item.content:
                    if isinstance(block, TextBlock) and block.text.strip():
                        transcript(log, "text", text=block.text)
                    elif isinstance(block, ToolUseBlock):
                        transcript(log, "tool", name=block.name, input=block.input)
                        emit({"event": "agent_tool", "tool": _tool_name(block.name), "detail": _detail(block, self.spec.cwd)})
            elif isinstance(item, UserMessage) and isinstance(item.content, list):
                for block in item.content:
                    if isinstance(block, ToolResultBlock):
                        transcript(log, "tool_result", is_error=bool(block.is_error), content=short(block.content, 2000))
            elif isinstance(item, RateLimitEvent):
                info = item.rate_limit_info
                transcript(log, "rate_limit", status=info.status, resets_at=info.resets_at)
                if info.status == "rejected":
                    status, message = "unavailable", f"Claude usage limit reached (resets at {info.resets_at})"
            elif isinstance(item, ResultMessage):
                self.id = item.session_id or self.id
                usage, self._turn = self._usage(item), Usage()  # the result's own numbers replace the estimate
                transcript(log, "result", subtype=item.subtype, is_error=item.is_error, result=item.result,
                           tokens=usage.tokens, cost_usd=usage.cost_usd)
                if item.is_error and status == "done":
                    reason = item.result or ", ".join(item.errors or []) or item.subtype
                    status, message = ("unavailable" if _login_problem(item) else "failed"), f"Claude Code: {reason}"
        return TurnEnd(status, message, usage)

    def _usage(self, result: ResultMessage) -> Usage:
        counts = result.usage or {}
        tokens = sum(int(counts.get(key) or 0) for key in
                     ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens"))
        total = result.total_cost_usd or 0.0
        cost = max(total - self._cost_so_far, 0.0)  # a crashed session can report 0
        self._cost_so_far = max(self._cost_so_far, total)
        return Usage(tokens=tokens, cost_usd=cost)

    async def close(self) -> None:
        if self._client is not None:
            client, self._client = self._client, None
            try:
                await client.disconnect()
            except Exception as error:  # already gone, or teardown failed: keep a trace of it
                transcript(self.spec.log, "close_failed", error=f"{type(error).__name__}: {error}")


def _login_problem(result: ResultMessage) -> bool:
    text = f"{result.result or ''} {' '.join(result.errors or [])}".lower()
    return result.api_error_status in (401, 403, 429) or any(
        word in text for word in ("/login", "not logged in", "invalid api key", "authentication", "usage limit"))


def _tool_name(name: str) -> str:
    return name.removeprefix(f"mcp__{SERVER}__")


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
