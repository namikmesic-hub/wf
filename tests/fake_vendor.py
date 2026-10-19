"""A fake tool vendor for tests, served on 127.0.0.1 from a thread: OAuth (metadata, client
registration, authorization, tokens) and an MCP server at /mcp that wants a bearer token.

    vendor = FakeVendor(["list_issues", "get_user", "save_issue"]).start()
    monkeypatch.setitem(TOOLS, "linear", vendor.tool("linear"))

Every request is in `log`, every call that reached an operation in `calls`. Access tokens
the MCP server takes are in `access`; refresh tokens the token endpoint takes are in
`refresh`, and each refresh replaces the one used (rotation, as Linear and Notion do).
"""

import base64
import hashlib
import itertools
import socket
import threading
import time
import urllib.parse
from dataclasses import replace

import httpx2
import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse
from starlette.routing import Route

from workflow.tools.catalog import TOOLS


class FakeVendor:
    def __init__(self, operations=("list_issues", "get_user", "save_issue"), *, register: bool = True,
                 expires_in: int | None = 3600, clients: dict | None = None):
        self.register = register  # does it let clients register themselves?
        self.expires_in = expires_in
        self.clients = dict(clients or {})  # client id -> secret ("" for a public client)
        self.access: set[str] = set()
        self.refresh: set[str] = set()
        self.codes: dict[str, dict] = {}
        self.log: list[dict] = []
        self.calls: list[tuple[str, dict]] = []
        self.errors: dict[str, str] = {}  # operation -> what it fails with
        self._numbers = itertools.count(1)
        self.mcp = MCPServer("fake-vendor")
        for name in operations:
            self.add_operation(name)

    # ---------- running ----------

    def start(self) -> "FakeVendor":
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        self.port = sock.getsockname()[1]
        mcp_app = self.mcp.streamable_http_app()
        oauth_app = Starlette(routes=[
            Route("/.well-known/oauth-protected-resource/mcp", self._resource),
            Route("/.well-known/oauth-authorization-server", self._server),
            Route("/register", self._register, methods=["POST"]),
            Route("/authorize", self._authorize),
            Route("/token", self._token, methods=["POST"]),
        ])

        async def app(scope, receive, send):
            if scope["type"] == "http" and not scope["path"].startswith("/mcp"):
                return await oauth_app(scope, receive, send)
            if scope["type"] == "http":
                authorization = dict(scope["headers"]).get(b"authorization", b"").decode()
                self.log.append({"path": scope["path"], "method": scope["method"], "authorization": authorization})
                if authorization.removeprefix("Bearer ") not in self.access:
                    response = JSONResponse({"error": "invalid_token"}, status_code=401,
                                            headers={"WWW-Authenticate": "Bearer"})
                    return await response(scope, receive, send)
            return await mcp_app(scope, receive, send)  # the lifespan too

        self.server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="on"))
        self.thread = threading.Thread(target=self.server.run, kwargs={"sockets": [sock]}, daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("the fake vendor didn't start")
            time.sleep(0.02)
        return self

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(5)

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def tool(self, name: str = "linear", **changes):
        """The catalog's tool, pointed at this vendor."""
        return replace(TOOLS[name], **{"url": f"{self.base}/mcp", **changes})

    # ---------- what tests set up ----------

    def add_operation(self, name: str) -> None:
        def operation(query: str = "") -> str:
            self.calls.append((name, {"query": query}))
            if name in self.errors:
                raise ToolError(self.errors[name])
            return f"{name} answered {query!r}"

        self.mcp.add_tool(operation, name=name, description=f"The {name} operation")

    def issue(self) -> dict:
        """A new access token, and a refresh token for it."""
        number = next(self._numbers)
        access, refresh = f"access-{number}", f"refresh-{number}"
        self.access.add(access)
        self.refresh.add(refresh)
        return {"access_token": access, "token_type": "Bearer", "refresh_token": refresh,
                **({"expires_in": self.expires_in} if self.expires_in is not None else {})}

    def requests(self, path: str) -> list[dict]:
        return [entry for entry in self.log if entry["path"] == path]

    # ---------- OAuth ----------

    async def _resource(self, request: Request):
        return JSONResponse({"resource": f"{self.base}/mcp", "authorization_servers": [self.base],
                             "scopes_supported": ["read", "write"]})

    async def _server(self, request: Request):
        return JSONResponse({"issuer": self.base, "authorization_endpoint": f"{self.base}/authorize",
                             "token_endpoint": f"{self.base}/token",
                             **({"registration_endpoint": f"{self.base}/register"} if self.register else {})})

    async def _register(self, request: Request):
        body = await request.json()
        self.log.append({"path": "/register", "json": body})
        client = f"client-{next(self._numbers)}"
        self.clients[client] = ""
        return JSONResponse({"client_id": client, "redirect_uris": body["redirect_uris"],
                             "token_endpoint_auth_method": "none"}, status_code=201)

    async def _authorize(self, request: Request):
        query = dict(request.query_params)
        self.log.append({"path": "/authorize", "query": query})
        code = f"code-{next(self._numbers)}"
        self.codes[code] = query
        back = {"code": code, "state": query.get("state", ""), "iss": self.base}
        return RedirectResponse(f"{query['redirect_uri']}?{urllib.parse.urlencode(back)}", status_code=302)

    async def _token(self, request: Request):
        form = dict(await request.form())
        self.log.append({"path": "/token", "form": form})
        if self.clients.get(form.get("client_id"), None) != form.get("client_secret", ""):
            return JSONResponse({"error": "invalid_client"}, status_code=401)
        if form.get("grant_type") == "authorization_code":
            asked = self.codes.pop(form.get("code"), None)
            challenge = base64.urlsafe_b64encode(hashlib.sha256(form.get("code_verifier", "").encode()).digest()).rstrip(b"=").decode()
            if asked is None or asked["code_challenge"] != challenge or asked["redirect_uri"] != form.get("redirect_uri"):
                return JSONResponse({"error": "invalid_grant", "error_description": "bad code"}, status_code=400)
            return JSONResponse(self.issue())
        if form.get("grant_type") == "refresh_token" and form.get("refresh_token") in self.refresh:
            self.refresh.discard(form["refresh_token"])
            return JSONResponse(self.issue())
        return JSONResponse({"error": "invalid_grant", "error_description": "unknown refresh token"}, status_code=400)


def browser(change=None):
    """open_url for setup: signs in the way a person in a browser would, following the vendor's
    redirect back to wf's callback, in a thread. `change(url)` can alter the URL first."""

    def open_url(url: str) -> None:
        def visit():
            with httpx2.Client(follow_redirects=True, timeout=10) as http:
                http.get(change(url) if change else url)

        threading.Thread(target=visit, daemon=True).start()

    return open_url


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
