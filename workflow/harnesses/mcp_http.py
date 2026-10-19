"""MCP over HTTP: the runner's own tools, for an agent that takes MCP servers by URL.

Cursor's agent can't call a function in the runner the way the Claude Agent SDK
and Codex's dynamic tools can: it connects to the MCP servers named when a
session starts. So the runner serves one on 127.0.0.1, on a free port. add()
gives each session its own URL, whose path holds an unguessable token, so a
call can only reach the tools of the session it was given to; remove() ends
the URL when the session closes.

It speaks the part of MCP's Streamable HTTP transport an agent uses:
- a POSTed request gets its reply as one JSON body, and a notification gets
  202 Accepted;
- GET, which would open a stream for messages the server starts, gets 405, as
  the transport allows: this server never starts any;
- initialize, ping, tools/list and tools/call. A tool's own failure comes back
  as a result with isError, so the agent reads it and can try again.
HTTP/1.1 with keep-alive, and bodies with Content-Length or chunked, which is
all an MCP client sends.
"""

import asyncio
import json
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

VERSION = "2025-06-18"  # offered when the client names no protocol version
MAX_BODY = 16 * 1024 * 1024
REASONS = {200: "OK", 202: "Accepted", 400: "Bad Request", 404: "Not Found", 405: "Method Not Allowed"}


@dataclass
class Tool:
    name: str
    description: str
    schema: dict
    call: Callable[[dict], Awaitable[tuple[str, bool]]]  # arguments -> (text for the agent, is_error)


class Server:
    def __init__(self):
        self.routes: dict[str, list[Tool]] = {}  # URL token -> that session's tools
        self.port = 0
        self._server: asyncio.Server | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    def add(self, tools: list[Tool]) -> str:
        """A new URL that serves these tools."""
        token = secrets.token_urlsafe(24)
        self.routes[token] = tools
        return f"http://127.0.0.1:{self.port}/mcp/{token}"

    def remove(self, url: str) -> None:
        self.routes.pop(url.rsplit("/", 1)[-1], None)

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            self._server.close_clients()  # an agent's keep-alive connection would hold wait_closed() open
            await self._server.wait_closed()
            self._server = None

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while request := await _read(reader):
                status, reply = await self._handle(*request)
                body = json.dumps(reply).encode() if reply is not None else b""
                head = f"HTTP/1.1 {status} {REASONS[status]}\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n"
                writer.write((head + ("Allow: POST\r\n" if status == 405 else "") + "\r\n").encode() + body)
                await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError, ValueError):
            pass  # the agent hung up or sent something that isn't HTTP: drop the connection
        finally:
            writer.close()

    async def _handle(self, method: str, path: str, body: bytes) -> tuple[int, dict | None]:
        tools = self.routes.get(path.removeprefix("/mcp/")) if path.startswith("/mcp/") else None
        if tools is None:
            return 404, None
        if method != "POST":
            return 405, None
        try:
            message = json.loads(body)
        except ValueError:
            return 400, _error(None, -32700, "the body isn't JSON")
        if not isinstance(message, dict):
            return 400, _error(None, -32600, "send one JSON-RPC message per request")
        if "method" not in message or "id" not in message:
            return 202, None  # a notification (or a reply, though this server asks nothing)
        return 200, await _answer(message, tools)


async def _answer(message: dict, tools: list[Tool]) -> dict:
    method, params, request_id = message["method"], message.get("params") or {}, message["id"]
    if method == "initialize":
        result = {"protocolVersion": params.get("protocolVersion") or VERSION, "capabilities": {"tools": {}},
                  "serverInfo": {"name": "wf", "version": "0.1.0"}}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": [{"name": t.name, "description": t.description, "inputSchema": t.schema} for t in tools]}
    elif method == "tools/call":
        tool = next((t for t in tools if t.name == params.get("name")), None)
        if tool is None:
            text, is_error = f"Error: there's no tool called `{params.get('name')}` here.", True
        else:
            try:
                text, is_error = await tool.call(params.get("arguments") or {})
            except Exception as error:  # never leave the agent without a reply
                text, is_error = f"Error: {type(error).__name__}: {error}", True
        result = {"content": [{"type": "text", "text": text}], "isError": is_error}
    else:
        return _error(request_id, -32601, f"wf doesn't handle {method}")
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


async def _read(reader: asyncio.StreamReader) -> tuple[str, str, bytes] | None:
    """One request: (method, path, body), or None when the client closed the connection."""
    line = b"\r\n"
    while line in (b"\r\n", b"\n"):  # a client may send a blank line between requests
        line = await reader.readline()
    if not line:
        return None
    method, path, _ = line.decode("latin-1").split(" ", 2)
    headers = {}
    while (header := await reader.readline()) not in (b"\r\n", b"\n", b""):
        name, _, value = header.decode("latin-1").partition(":")
        headers[name.strip().lower()] = value.strip()
    if "chunked" in headers.get("transfer-encoding", "").lower():
        body = b""
        while size := int((await reader.readline()).split(b";")[0], 16):
            if len(body) + size > MAX_BODY:
                raise ValueError("body too large")
            body += (await reader.readexactly(size + 2))[:-2]  # each chunk ends with CRLF
        while (await reader.readline()) not in (b"\r\n", b"\n", b""):
            pass  # trailer fields
    else:
        length = int(headers.get("content-length") or 0)
        if length > MAX_BODY:
            raise ValueError("body too large")
        body = await reader.readexactly(length)
    return method, path, body
