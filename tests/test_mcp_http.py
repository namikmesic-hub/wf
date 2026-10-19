"""mcp_http.Server: the runner's tools over MCP's Streamable HTTP, one URL per session."""

import asyncio
import json
import urllib.error
import urllib.request

from workflow.harnesses import mcp_http


def post(url: str, message: dict | bytes | None, method: str = "POST", **headers: str) -> tuple[int, dict | None]:
    body = message if isinstance(message, bytes) or message is None else json.dumps(message).encode()
    headers = {"Content-Type": "application/json", **{name.replace("_", "-"): value for name, value in headers.items()}}
    request = urllib.request.Request(url, body, headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=5) as reply:
            status, data = reply.status, reply.read()
    except urllib.error.HTTPError as error:
        status, data = error.code, error.read()
    return status, json.loads(data) if data else None


def echo(name: str) -> mcp_http.Tool:
    async def call(arguments: dict) -> tuple[str, bool]:
        return f"{name} got {json.dumps(arguments)}", False
    return mcp_http.Tool(name, f"the {name} tool", {"type": "object"}, call)


def serving(*tool_sets, then):
    """Start a server with a URL per tool set, run then(server, urls) in a thread, close it."""
    async def main():
        server = mcp_http.Server()
        await server.start()
        try:
            return await asyncio.to_thread(then, server, [server.add(tools) for tools in tool_sets])
        finally:
            await server.close()
    return asyncio.run(main())


def test_a_url_reaches_only_its_own_sessions_tools_until_it_is_removed():
    def calls(server, urls):
        a, b = urls
        listed = post(a, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        called = post(a, {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "a", "arguments": {"x": 1}}})
        other = post(b, {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "a", "arguments": {}}})
        guessed = post(a.rsplit("/", 1)[0] + "/not-a-token", {"jsonrpc": "2.0", "id": 4, "method": "tools/list"})
        server.remove(a)
        removed = post(a, {"jsonrpc": "2.0", "id": 5, "method": "tools/list"})
        return listed, called, other, guessed, removed

    listed, called, other, guessed, removed = serving([echo("a")], [echo("b")], then=calls)
    assert listed == (200, {"jsonrpc": "2.0", "id": 1, "result": {
        "tools": [{"name": "a", "description": "the a tool", "inputSchema": {"type": "object"}}]}})
    assert called[1]["result"] == {"content": [{"type": "text", "text": 'a got {"x": 1}'}], "isError": False}
    assert other[1]["result"] == {"content": [{"type": "text", "text": "Error: there's no tool called `a` here."}], "isError": True}
    assert (guessed[0], removed[0]) == (404, 404)


def test_the_handshake_notifications_and_an_event_stream_request():
    def calls(server, urls):
        (url,) = urls
        return (post(url, {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}}),
                post(url, {"jsonrpc": "2.0", "method": "notifications/initialized"}),
                post(url, None, method="GET"),
                post(url, {"jsonrpc": "2.0", "id": 1, "method": "resources/list"}))

    initialized, notified, stream, unknown = serving([echo("a")], then=calls)
    assert initialized[1]["result"]["protocolVersion"] == "2025-11-25"
    assert initialized[1]["result"]["capabilities"] == {"tools": {}}
    assert notified == (202, None)
    assert stream == (405, None)  # the server never starts messages of its own
    assert unknown[1]["error"]["code"] == -32601


def test_a_chunked_body_on_a_kept_alive_connection():
    async def main():
        server = mcp_http.Server()
        await server.start()
        url = server.add([echo("a")])
        path = "/" + url.split("/", 3)[3]
        reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
        replies = []
        for request_id in (1, 2):  # two requests on one connection
            body = json.dumps({"jsonrpc": "2.0", "id": request_id, "method": "ping"}).encode()
            chunks = b"".join(b"%x\r\n%s\r\n" % (len(part), part) for part in (body[:5], body[5:])) + b"0\r\n\r\n"
            writer.write(f"POST {path} HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n".encode() + chunks)
            await writer.drain()
            head = await reader.readuntil(b"\r\n\r\n")
            length = int(head.lower().split(b"content-length: ")[1].split(b"\r\n")[0])
            replies.append(json.loads(await reader.readexactly(length)))
        writer.close()
        await server.close()
        return replies

    assert asyncio.run(main()) == [{"jsonrpc": "2.0", "id": 1, "result": {}}, {"jsonrpc": "2.0", "id": 2, "result": {}}]


def test_a_body_that_is_not_json_gets_a_parse_error():
    status, reply = serving([echo("a")], then=lambda server, urls: post(urls[0], b"{not json"))
    assert (status, reply["error"]["code"]) == (400, -32700)


def test_a_browser_page_cannot_reach_the_tools():
    ping = {"jsonrpc": "2.0", "id": 1, "method": "ping"}

    def calls(server, urls):
        return (post(urls[0], ping, Origin="https://attacker.example"), post(urls[0], ping, Origin="null"),
                post(urls[0], ping, Origin=f"http://127.0.0.1:{server.port}"), post(urls[0], ping, Origin="http://localhost"))

    attacker, opaque, loopback, localhost = serving([echo("a")], then=calls)
    assert (attacker[0], opaque[0], loopback[0], localhost[0]) == (403, 403, 200, 200)


def test_an_unknown_protocol_version_gets_400():
    ping = {"jsonrpc": "2.0", "id": 1, "method": "ping"}

    def calls(server, urls):
        return (post(urls[0], ping, MCP_Protocol_Version="2099-01-01"), post(urls[0], ping, MCP_Protocol_Version="2025-11-25"),
                post(urls[0], {"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {"protocolVersion": "2099-01-01"}}))

    unknown, known, initialized = serving([echo("a")], then=calls)
    assert (unknown[0], known[0]) == (400, 200)
    assert initialized[1]["result"]["protocolVersion"] == mcp_http.VERSIONS[-1]  # what it offers instead


def test_a_body_over_the_limit_drops_the_connection_before_it_is_read():
    async def main():
        server = mcp_http.Server()
        await server.start()
        path = "/" + server.add([echo("a")]).split("/", 3)[3]
        closed = []
        for head in (f"Content-Length: {mcp_http.MAX_BODY + 1}", "Transfer-Encoding: chunked"):
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            chunk = b"%x\r\n" % (mcp_http.MAX_BODY + 1) if "chunked" in head else b""
            writer.write(f"POST {path} HTTP/1.1\r\nHost: x\r\n{head}\r\n\r\n".encode() + chunk)
            await writer.drain()
            closed.append(await asyncio.wait_for(reader.read(), 5))  # b"": no reply, the connection closed
            writer.close()
        await server.close()
        return closed

    assert asyncio.run(main()) == [b"", b""]
