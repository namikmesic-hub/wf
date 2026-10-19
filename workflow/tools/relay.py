"""Relay: a tool's server, attached to one agent session through wf.

wf is the vendor server's only MCP client. The agent sees the tool the way it sees
submit_result, as a server wf hosts in its own process: an SDK MCP server for Claude Code, a
namespace of dynamic tools for Codex, an MCP server on 127.0.0.1 for Cursor (mcp_http.py).
Every call comes back to wf, which passes it on with the stored sign-in. That keeps two promises on every harness alike:
- The secret stays in wf's process. Handing the vendor's URL and token to the harness instead
  would put the token on Claude Code's command line (the Agent SDK passes MCP servers there),
  where any command the agent runs could read it with `ps`.
- Read-only holds in one place. The agent is told only about the operations its node may use,
  and wf refuses any other name. Each harness's own filtering couldn't do it everywhere:
  Claude Code's `full` access allows every tool, and ACP (Cursor's protocol) has no filter.

attach() connects to each tool a node uses, for one attempt. A connection runs in a task of
its own, which enters and leaves the MCP client (anyio wants both in the same task), while
calls come from whichever task the harness runs them in.
"""

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import Implementation

from . import keychain, signin
from .catalog import TOOLS
from .signin import NotSetUp, Unreachable

CALL_TIMEOUT = 120  # seconds for one call to a tool's server
CLOSE_TIMEOUT = 10  # seconds to say goodbye to it
PAGES = 20  # at most this many pages of operations
CLIENT = Implementation(name="wf", version="0.1.0")


@dataclass(frozen=True)
class Operation:
    name: str
    description: str
    input_schema: dict
    annotations: object = None  # the server's hints (readOnlyHint, ...), passed on to Claude Code


class ToolServer:
    """One tool as an agent session sees it: the operations its node may use, and call().
    This is what each harness adapter gets in SessionSpec.tools and wraps in its own tool type."""

    def __init__(self, connection: "_Connection", write: bool):
        tool = connection.tool
        self.name, self.title = tool.name, tool.title
        self.description = f"{tool.title}, through wf ({'reads and writes' if write else 'read-only'})"
        self.operations = [op for op in connection.operations if write or op.name in tool.reads]
        self._allowed = {op.name for op in self.operations}
        self._connection = connection

    async def call(self, name: str, arguments: object) -> tuple[list[dict], bool]:
        """Run one operation. Returns (content, is_error): MCP text and image blocks."""
        if name not in self._allowed:
            return [_text(f"Error: `{name}` isn't one of the {self.title} operations this step may use.")], True
        try:
            result = await self._connection.client.call_tool(
                name, arguments if isinstance(arguments, dict) else {}, read_timeout_seconds=CALL_TIMEOUT)
        except Exception as error:
            return [_text(f"Error: {self.title} didn't answer: {_why(error)}")], True
        return _content(result), bool(result.is_error)


@contextlib.asynccontextmanager
async def attach(uses: list) -> AsyncIterator[list[ToolServer]]:
    """Connect to the tools a node uses (spec.ToolUse entries), for one attempt. Raises NotSetUp
    or keychain.Unavailable when one can't be used as it is, Unreachable when its server failed."""
    connections = [_Connection(TOOLS[use.name]) for use in uses]
    try:
        opened = await asyncio.gather(*(c.open() for c in connections), return_exceptions=True)
        failures = [result for result in opened if isinstance(result, BaseException)]
        if failures:  # one that setup fixes says more than one that retrying might
            raise next((f for f in failures if not isinstance(f, Unreachable)), failures[0])
        yield [ToolServer(connection, use.write) for connection, use in zip(connections, uses)]
    finally:
        await asyncio.gather(*(c.close() for c in connections), return_exceptions=True)


class _Connection:
    def __init__(self, tool):
        self.tool = tool
        self.auth = _Bearer(tool)
        self.client: Client | None = None
        self.operations: list[Operation] = []
        self._stop = asyncio.Event()
        self._ready: asyncio.Future | None = None
        self._task: asyncio.Task | None = None

    async def open(self) -> None:
        self._ready = asyncio.get_running_loop().create_future()
        self._task = asyncio.create_task(self._serve(), name=f"wf tool {self.tool.name}")
        await self._ready

    async def close(self) -> None:
        if self._task is None:
            return
        self._stop.set()
        if self.client is None:
            self._task.cancel()  # still connecting: nobody will use it
        await asyncio.wait({self._task}, timeout=CLOSE_TIMEOUT)
        if not self._task.done():
            self._task.cancel()
            await asyncio.wait({self._task}, timeout=1)
        if self._ready.done() and not self._ready.cancelled():
            self._ready.exception()  # seen, even if open() was cancelled before it got there

    async def _serve(self) -> None:
        try:
            timeout = httpx2.Timeout(signin.TIMEOUT, read=CALL_TIMEOUT)
            async with (httpx2.AsyncClient(auth=self.auth, timeout=timeout) as http,
                        Client(streamable_http_client(self.tool.url, http_client=http), mode="legacy",
                               read_timeout_seconds=CALL_TIMEOUT, client_info=CLIENT) as client):
                self.operations = await _operations(client)
                self.client = client
                if not self._ready.done():  # open() may have been cancelled meanwhile
                    self._ready.set_result(None)
                await self._stop.wait()
        except Exception as error:  # after it was ready, later calls report the lost connection themselves
            if not self._ready.done():
                self._ready.set_exception(self._explain(error))

    def _explain(self, error: BaseException) -> Exception:
        tool = self.tool
        if self.auth.problem is not None:
            return self.auth.problem
        if self.auth.status in (401, 403):
            return NotSetUp(tool, f"{tool.title} refused the stored sign-in (HTTP {self.auth.status})")
        status = f"HTTP {self.auth.status}, " if self.auth.status else ""
        return Unreachable(f"can't use {tool.title}'s server at {tool.url}: {status}{_why(error)}")


class _Bearer(httpx2.Auth):
    """Adds the stored token to every request. On a 401 it refreshes the token once and tries again."""

    def __init__(self, tool):
        self.tool = tool
        self.token: str | None = None
        self.status: int | None = None  # the last error status the server answered a message with
        self.problem: Exception | None = None  # why there's no token to send

    async def async_auth_flow(self, request):
        if self.token is None:
            self.token = await self._token()
        request.headers["Authorization"] = f"Bearer {self.token}"
        response = yield request
        if response.status_code == 401:
            self.token = await self._token(refused=self.token)
            request.headers["Authorization"] = f"Bearer {self.token}"
            response = yield request
        if response.status_code >= 400 and request.method == "POST":  # a GET is only the optional event stream
            self.status = response.status_code

    async def _token(self, refused: str | None = None) -> str:
        try:
            return await signin.access_token(self.tool, refused)
        except (NotSetUp, Unreachable, keychain.Unavailable) as error:
            self.problem = error
            raise


async def _operations(client: Client) -> list[Operation]:
    found, cursor = [], None
    for _ in range(PAGES):
        page = await client.list_tools(cursor=cursor)
        found += [Operation(t.name, t.description or "", t.input_schema, t.annotations) for t in page.tools]
        cursor = page.next_cursor
        if not cursor:
            break
    return found


def _content(result) -> list[dict]:
    """A tool result as text and image blocks, which every harness can show the agent."""
    blocks = []
    for block in result.content:
        item = block.model_dump(mode="json", by_alias=True, exclude_none=True)
        kind = item.get("type")
        if kind == "text":
            blocks.append(_text(item.get("text", "")))
        elif kind == "image":
            blocks.append({"type": "image", "data": item["data"], "mimeType": item["mimeType"]})
        elif kind == "resource_link":
            blocks.append(_text(" ".join(str(item[key]) for key in ("name", "uri") if item.get(key))))
        elif kind == "resource" and "text" in (item.get("resource") or {}):
            blocks.append(_text(item["resource"]["text"]))
        else:
            blocks.append(_text(f"[{kind} content left out]"))
    if not blocks and result.structured_content is not None:
        blocks.append(_text(json.dumps(result.structured_content, ensure_ascii=False)))
    return blocks or [_text("(no content)")]


def _text(text: str) -> dict:
    return {"type": "text", "text": text}


def _why(error: BaseException) -> str:
    while isinstance(error, BaseExceptionGroup) and error.exceptions:
        error = error.exceptions[0]
    return f"{type(error).__name__}: {error}" if str(error) else type(error).__name__
