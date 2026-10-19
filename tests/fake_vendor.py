"""A fake tool vendor for tests, served on 127.0.0.1 from a thread: OAuth (metadata, client
registration, authorization, tokens) and an MCP server at /mcp that wants a bearer token.

    vendor = FakeVendor(["list_issues", "get_user", "save_issue"]).start()
    monkeypatch.setitem(TOOLS, "linear", vendor.tool("linear"))

Every path asked for is in `paths`, every request's details in `log`, and every call that
reached an operation in `calls`. Access tokens
the MCP server takes are in `access`; refresh tokens the token endpoint takes are in
`refresh`, and each refresh replaces the one used (rotation, as some vendors do), unless
`rotate` is off. Settings change how it answers:
    issuer_path="/tenant"      the authorization server lives at a path, and has only OpenID
                               Connect's metadata, appended to the path
    page_size=2                tools/list answers in pages
    delays={"op": 1.5}         an operation takes that many seconds; token_delay for /token
    token_status=503           refreshes answer this status instead
    mcp_status=403             the MCP server answers this to every request
    reject_all=True            the MCP server takes no token at all
    deny=True                  the sign-in comes back with error=access_denied
    no_code=True               the sign-in comes back without a code
    iss="https://elsewhere"    the sign-in comes back with this `iss` ("" leaves it out)
    iss_supported=True         the metadata says the sign-in always has `iss`
    metadata_issuer="..."      the metadata names this issuer instead of its own
    resource_slash=True        the resource metadata names the issuer with a last slash, as Google's does
    hints={"op": {"readOnlyHint": True}}   an operation's MCP annotations
An operation called "screenshot" answers with an image.
"""

import base64
import hashlib
import itertools
import socket
import threading
import time
import urllib.parse
from dataclasses import replace

import anyio
import httpx2
import uvicorn
from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ListToolsResult, ToolAnnotations
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse
from starlette.routing import Route

from workflow.tools import keychain
from workflow.tools.catalog import TOOLS

PNG = b"\x89PNG\r\n\x1a\nfake"


class _PagedServer(MCPServer):
    page_size: int | None = None

    async def _handle_list_tools(self, ctx, params):
        tools = await self.list_tools()
        if not self.page_size:
            return ListToolsResult(tools=tools)
        start = int(params.cursor) if params and params.cursor else 0
        end = start + self.page_size
        return ListToolsResult(tools=tools[start:end], next_cursor=str(end) if end < len(tools) else None)


class FakeVendor:
    def __init__(self, operations=("list_issues", "get_user", "save_issue"), *, register: bool = True,
                 expires_in: int | None = 3600, clients: dict | None = None, **settings):
        self.register = register  # does it let clients register themselves?
        self.expires_in = expires_in
        self.clients = dict(clients or {})  # client id -> secret ("" for a public client)
        self.settings = {"issuer_path": "", "page_size": None, "delays": {}, "token_delay": 0, "token_status": None,
                         "mcp_status": None, "reject_all": False, "deny": False, "no_code": False, "iss": None,
                         "iss_supported": False, "metadata_issuer": None, "resource_slash": False, "rotate": True,
                         "hints": {},
                         **settings}
        self.access: set[str] = set()
        self.refresh: set[str] = set()
        self.codes: dict[str, dict] = {}
        self.log: list[dict] = []
        self.paths: list[str] = []
        self.calls: list[tuple[str, dict]] = []
        self.errors: dict[str, str] = {}  # operation -> what it fails with
        self._numbers = itertools.count(1)
        self.mcp = _PagedServer("fake-vendor")
        self.mcp.page_size = self.settings["page_size"]
        for name in operations:
            self.add_operation(name)

    # ---------- running ----------

    def start(self) -> "FakeVendor":
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        self.port = sock.getsockname()[1]
        mcp_app = self.mcp.streamable_http_app()
        well_known = (f"{self.settings['issuer_path']}/.well-known/openid-configuration" if self.settings["issuer_path"]
                      else "/.well-known/oauth-authorization-server")
        oauth_app = Starlette(routes=[
            Route("/.well-known/oauth-protected-resource/mcp", self._resource),
            Route(well_known, self._server),
            Route("/register", self._register, methods=["POST"]),
            Route("/authorize", self._authorize),
            Route("/token", self._token, methods=["POST"]),
        ])

        async def app(scope, receive, send):
            if scope["type"] == "http":
                self.paths.append(scope["path"])
            if scope["type"] == "http" and not scope["path"].startswith("/mcp"):
                return await oauth_app(scope, receive, send)
            if scope["type"] == "http":
                authorization = dict(scope["headers"]).get(b"authorization", b"").decode()
                self.log.append({"path": scope["path"], "method": scope["method"], "authorization": authorization})
                status = self.settings["mcp_status"]
                if status is None and (self.settings["reject_all"] or authorization.removeprefix("Bearer ") not in self.access):
                    status = 401
                if status is not None:
                    header = f'Bearer resource_metadata="{self.base}/.well-known/oauth-protected-resource/mcp"'
                    response = JSONResponse({"error": "invalid_token"}, status_code=status, headers={"WWW-Authenticate": header})
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

    @property
    def issuer(self) -> str:
        return self.base + self.settings["issuer_path"]

    def tool(self, name: str = "linear", **changes):
        """The catalog's tool, pointed at this vendor (its issuer too, for a tool that pins one)."""
        pinned = {"issuer": self.issuer} if TOOLS[name].issuer else {}
        return replace(TOOLS[name], **{"url": f"{self.base}/mcp", **pinned, **changes})

    # ---------- what tests set up ----------

    def add_operation(self, name: str) -> None:
        delay = self.settings["delays"].get(name, 0)

        async def operation(query: str = ""):
            self.calls.append((name, {"query": query}))
            await anyio.sleep(delay)
            if name in self.errors:
                raise ToolError(self.errors[name])
            if name == "screenshot":
                return Image(data=PNG, format="png")
            return f"{name} answered {query!r}"

        hints = self.settings["hints"].get(name)
        self.mcp.add_tool(operation, name=name, description=f"The {name} operation",
                          annotations=ToolAnnotations(**hints) if hints else None)

    def issue(self, refresh: bool = True) -> dict:
        """A new access token, and a refresh token for it."""
        number = next(self._numbers)
        access = f"access-{number}"
        self.access.add(access)
        tokens = {"access_token": access, "token_type": "Bearer",
                  **({"expires_in": self.expires_in} if self.expires_in is not None else {})}
        if refresh:
            tokens["refresh_token"] = f"refresh-{number}"
            self.refresh.add(tokens["refresh_token"])
        return tokens

    def requests(self, path: str) -> list[dict]:
        return [entry for entry in self.log if entry["path"] == path]

    # ---------- OAuth ----------

    async def _resource(self, request: Request):
        named = self.issuer + ("/" if self.settings["resource_slash"] else "")
        return JSONResponse({"resource": f"{self.base}/mcp", "authorization_servers": [named],
                             "scopes_supported": ["read", "write"]})

    async def _server(self, request: Request):
        return JSONResponse({"issuer": self.settings["metadata_issuer"] or self.issuer,
                             "authorization_response_iss_parameter_supported": self.settings["iss_supported"],
                             "authorization_endpoint": f"{self.base}/authorize",
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
        iss = self.issuer if self.settings["iss"] is None else self.settings["iss"]
        back = {"state": query.get("state", ""), **({"iss": iss} if iss else {})}
        if self.settings["deny"]:
            back.update(error="access_denied", error_description="you said no")
        elif not self.settings["no_code"]:
            back["code"] = code
        return RedirectResponse(f"{query['redirect_uri']}?{urllib.parse.urlencode(back)}", status_code=302)

    async def _token(self, request: Request):
        form = dict(await request.form())
        self.log.append({"path": "/token", "form": form})
        await anyio.sleep(self.settings["token_delay"])
        if self.clients.get(form.get("client_id"), None) != form.get("client_secret", ""):
            return JSONResponse({"error": "invalid_client"}, status_code=401)
        if form.get("grant_type") == "authorization_code":
            asked = self.codes.pop(form.get("code"), None)
            challenge = base64.urlsafe_b64encode(hashlib.sha256(form.get("code_verifier", "").encode()).digest()).rstrip(b"=").decode()
            if asked is None or asked["code_challenge"] != challenge or asked["redirect_uri"] != form.get("redirect_uri"):
                return JSONResponse({"error": "invalid_grant", "error_description": "bad code"}, status_code=400)
            return JSONResponse(self.issue())
        if self.settings["token_status"] is not None:
            return JSONResponse({"error": "temporarily_unavailable"}, status_code=self.settings["token_status"])
        if form.get("grant_type") == "refresh_token" and form.get("refresh_token") in self.refresh:
            if self.settings["rotate"]:
                self.refresh.discard(form["refresh_token"])
            return JSONResponse(self.issue(refresh=self.settings["rotate"]))
        return JSONResponse({"error": "invalid_grant", "error_description": "unknown refresh token"}, status_code=400)


def sign_in(vendor: FakeVendor, name: str = "linear", **changes) -> dict:
    """Keep a sign-in to the vendor in the keyring, as setup does: tokens it takes, a client it knows."""
    vendor.clients["client-0"] = ""
    tokens = vendor.issue()
    stored = {"method": "oauth", "access_token": tokens["access_token"], "refresh_token": tokens["refresh_token"],
              "expires_at": int(time.time()) + 3600, "token_endpoint": f"{vendor.base}/token", "client_id": "client-0",
              "client_secret": "", "auth": "none", "resource": f"{vendor.base}/mcp", "set_up": "2026-10-19", **changes}
    vendor.access.add(stored["access_token"])
    keychain.save(name, stored)
    return stored


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


def png_base64() -> str:
    return base64.b64encode(PNG).decode()
