"""Sign-in: getting a tool's secret once, and keeping it fresh.

`wf tools <name> setup` calls setup(), and keychain.py keeps what it returns. Every agent session
that uses the tool gets its token from access_token(), which refreshes an expired one first.

setup() is OAuth 2.1's authorization code flow with PKCE, as MCP's authorization spec has it:
1. Discovery: the server's protected resource metadata (RFC 9728) names its authorization
   server, and that server's metadata (RFC 8414, or OpenID Connect's) gives the endpoints.
2. A client: Linear, Notion and Granola let wf register one (RFC 7591), so there's nothing to
   make first. Slack and Google don't: you make an OAuth client once (a Slack app, a Google
   Cloud OAuth client) and paste its id and secret.
3. Your browser opens the vendor's sign-in page. wf waits for the redirect on
   http://localhost:<port>/callback, checks `state` (and `iss`, when the server sends it), and
   trades the code for tokens.
With `--token`, setup takes a token you paste instead, for the vendors that give one out (a
Linear API key, a Notion MCP token, a Slack user token). Such a token has no refresh.

Refresh: a stored access token that expires within a minute, or that the server just refused,
is refreshed with the refresh token. Vendors that rotate refresh tokens make the old one
invalid, so two refreshes at once would break the sign-in: a lock file under ~/.wf/tools/
makes them take turns, across wf processes too, and whoever comes second finds the new token.
"""

import asyncio
import base64
import contextlib
import fcntl
import hashlib
import html
import secrets
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass

import httpx2

from .. import workspaces
from . import keychain
from .catalog import Tool

PORT = 47152  # where setup waits for the browser's redirect
TIMEOUT = 30  # seconds for each request to an authorization server
WAIT = 300  # seconds to finish signing in, in the browser
EARLY = 60  # refresh a token this many seconds before it expires


class NotSetUp(Exception):
    """There's no usable sign-in: `wf tools <name> setup` fixes it."""

    def __init__(self, tool: Tool, problem: str):
        self.problem, self.fix = problem, f"run `wf tools {tool.name} setup`"
        super().__init__(f"{problem}: {self.fix}")


class Unreachable(Exception):
    """The vendor didn't answer as it should: trying again later may work."""


class SetupFailed(Exception):
    """Signing in didn't finish."""


@dataclass(frozen=True)
class AuthServer:
    """What discovery found: the endpoints, the resource to ask for, and the scopes."""

    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    registration_endpoint: str | None
    resource: str
    scopes: tuple[str, ...]


def redirect_uri(port: int) -> str:
    return f"http://localhost:{port}/callback"


async def setup(tool: Tool, *, port: int, token: bool, say: Callable[[str], None], ask: Callable[[str], str],
                ask_secret: Callable[[str], str], open_url: Callable[[str], object]) -> dict:
    """Sign in to a tool. Returns what to keep in the Keychain. The CLI passes print, input,
    getpass and webbrowser.open; tests pass stand-ins."""
    today = time.strftime("%Y-%m-%d")
    if token:
        value = ask_secret(f"Paste the {tool.token}: ").strip()
        if not value:
            raise SetupFailed("no token was given")
        return {"method": "token", "access_token": value, "set_up": today}
    redirect = redirect_uri(port)
    async with _http() as http:
        server = await discover(http, tool)
        if tool.client == "yours":
            say(tool.client_help.format(redirect=redirect, scopes=" ".join(server.scopes)))
            client = {"client_id": ask("Client ID: ").strip(), "client_secret": ask_secret("Client secret: ").strip(),
                      "auth": "client_secret_post"}
            if not client["client_id"] or not client["client_secret"]:
                raise SetupFailed("both the client ID and the client secret are needed")
        else:
            client = await _register(http, tool, server, redirect)
        verifier, state = secrets.token_urlsafe(64), secrets.token_urlsafe(24)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        resource = {"resource": server.resource} if tool.resource else {}
        query = {"response_type": "code", "client_id": client["client_id"], "redirect_uri": redirect, "state": state,
                 "code_challenge": challenge, "code_challenge_method": "S256",
                 **({"scope": " ".join(server.scopes)} if server.scopes else {}), **resource, **tool.authorize}
        separator = "&" if "?" in server.authorization_endpoint else "?"
        url = server.authorization_endpoint + separator + urllib.parse.urlencode(query)
        async with _callback(port) as answer:
            say(f"Opening your browser to sign in to {tool.title}. If it doesn't open, go to:\n{url}")
            open_url(url)
            try:
                back = await asyncio.wait_for(answer, WAIT)
            except TimeoutError:
                raise SetupFailed(f"no sign-in came back within {WAIT // 60} minutes") from None
        if back.get("state") != state:
            raise SetupFailed("the sign-in came back with the wrong `state`, so it was ignored: try again")
        if back.get("error"):
            raise SetupFailed(f"{tool.title} didn't sign you in: {_described(back)}")
        if back.get("iss") and back["iss"].rstrip("/") != server.issuer.rstrip("/"):
            raise SetupFailed(f"the sign-in came back from {back['iss']}, not {server.issuer}, so it was ignored")
        if not back.get("code"):
            raise SetupFailed("the sign-in came back without a code: try again")
        form = {"grant_type": "authorization_code", "code": back["code"], "redirect_uri": redirect,
                "code_verifier": verifier, **resource}
        data, problem = _answer(await _token_request(http, server.token_endpoint, form, client))
    if problem:
        raise SetupFailed(f"{tool.title} didn't hand out a token: {problem}")
    return {"method": "oauth", **_tokens(data), "token_endpoint": server.token_endpoint, **client,
            "resource": server.resource if tool.resource else None, "set_up": today}


async def discover(http: httpx2.AsyncClient, tool: Tool) -> AuthServer:
    """Find the tool's authorization server, starting from its MCP server's URL."""
    resource = await _first_json(http, _well_known(tool.url, "oauth-protected-resource"), f"{tool.title}'s resource metadata")
    issuers = resource.get("authorization_servers") or []
    if not issuers or not isinstance(issuers[0], str):
        raise Unreachable(f"{tool.title}'s resource metadata names no authorization server")
    issuer = issuers[0].rstrip("/")
    meta = await _first_json(http, _well_known(issuer, "oauth-authorization-server") + _well_known(issuer, "openid-configuration"),
                             f"{tool.title}'s authorization server metadata")
    if not meta.get("authorization_endpoint") or not meta.get("token_endpoint"):
        raise Unreachable(f"{tool.title}'s authorization server doesn't list its endpoints")
    scopes = tool.scopes or tuple(resource.get("scopes_supported") or ())
    if not tool.scopes and "offline_access" in (meta.get("scopes_supported") or ()) and "offline_access" not in scopes:
        scopes += ("offline_access",)  # how some servers hand out a refresh token
    return AuthServer(issuer=meta.get("issuer") or issuer, authorization_endpoint=meta["authorization_endpoint"],
                      token_endpoint=meta["token_endpoint"], registration_endpoint=meta.get("registration_endpoint"),
                      resource=resource.get("resource") or tool.url, scopes=scopes)


async def access_token(tool: Tool, refused: str | None = None) -> str:
    """A token for the tool's server: the stored one, refreshed first if it has expired, or if
    it's the one the server just `refused`. Raises NotSetUp, Unreachable or keychain.Unavailable."""
    stored = await _load(tool)
    if not _stale(stored, refused):
        return stored["access_token"]
    if not stored.get("refresh_token"):
        what = "refused the stored token" if stored["access_token"] == refused else "sign-in has expired"
        raise NotSetUp(tool, f"{tool.title} {what}")
    async with _taking_turns(tool.name):
        stored = await _load(tool)
        if not _stale(stored, refused):
            return stored["access_token"]  # another session refreshed it meanwhile
        stored = await _refresh(tool, stored)
        await asyncio.to_thread(keychain.save, tool.name, stored)
    return stored["access_token"]


async def _load(tool: Tool) -> dict:
    stored = await asyncio.to_thread(keychain.load, tool.name)
    if stored is None:
        raise NotSetUp(tool, f"{tool.title} isn't set up")
    return stored


def _stale(stored: dict, refused: str | None) -> bool:
    expires = stored.get("expires_at")
    return stored["access_token"] == refused or (expires is not None and expires - EARLY <= time.time())


async def _refresh(tool: Tool, stored: dict) -> dict:
    form = {"grant_type": "refresh_token", "refresh_token": stored["refresh_token"]}
    if stored.get("resource"):
        form["resource"] = stored["resource"]
    async with _http() as http:
        response = await _token_request(http, stored["token_endpoint"], form, stored)
    data, problem = _answer(response)
    if problem and response.status_code < 500 and response.status_code != 429:  # refused, not just busy
        raise NotSetUp(tool, f"{tool.title} didn't refresh the sign-in ({problem})")
    if problem:
        raise Unreachable(f"refreshing {tool.title}'s sign-in failed: {problem}")
    tokens = _tokens(data)
    return {**stored, **tokens, "refresh_token": tokens.get("refresh_token") or stored["refresh_token"]}


@contextlib.asynccontextmanager
async def _taking_turns(name: str):
    """One refresh of a tool's sign-in at a time, across processes: an exclusive lock on a file."""
    folder = workspaces.home() / "tools"
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / f"{name}.lock").open("a") as file:  # closing it releases the lock
        deadline = time.monotonic() + 2 * TIMEOUT
        while True:
            try:
                fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise Unreachable(f"another wf process has been refreshing the {name} sign-in for a minute") from None
                await asyncio.sleep(0.05)
        yield


async def _register(http: httpx2.AsyncClient, tool: Tool, server: AuthServer, redirect: str) -> dict:
    """Register wf as a public OAuth client of the tool's authorization server (RFC 7591)."""
    if not server.registration_endpoint:
        raise SetupFailed(f"{tool.title}'s authorization server doesn't let wf register as a client")
    response = await _send(http, "POST", server.registration_endpoint, json={
        "client_name": "wf", "redirect_uris": [redirect], "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"], "token_endpoint_auth_method": "none",
    })
    data, problem = _answer(response)
    if problem or not data.get("client_id"):
        raise SetupFailed(f"{tool.title} didn't register wf as a client: {problem or 'no client_id in the answer'}")
    secret = data.get("client_secret") or ""
    return {"client_id": data["client_id"], "client_secret": secret,
            "auth": data.get("token_endpoint_auth_method") or ("client_secret_post" if secret else "none")}


async def _token_request(http: httpx2.AsyncClient, url: str, form: dict, client: dict) -> httpx2.Response:
    """POST to a token endpoint, authenticating as the client the way it registered."""
    form, auth = {**form, "client_id": client["client_id"]}, None
    if client.get("client_secret"):
        if client.get("auth") == "client_secret_basic":
            auth = httpx2.BasicAuth(form.pop("client_id"), client["client_secret"])
        else:
            form["client_secret"] = client["client_secret"]
    return await _send(http, "POST", url, data=form, headers={"Accept": "application/json"},
                       **({"auth": auth} if auth else {}))


def _answer(response: httpx2.Response) -> tuple[dict, str | None]:
    """The JSON an OAuth endpoint answered, and what went wrong, if anything. Slack says
    `ok: false` with HTTP 200, so the body decides as much as the status."""
    try:
        data = response.json()
    except ValueError:
        data = None
    if not isinstance(data, dict):
        return {}, f"HTTP {response.status_code}, without a JSON answer"
    if response.status_code >= 400 or data.get("error") or data.get("ok") is False:
        return data, _described(data) if data.get("error") else f"HTTP {response.status_code}"
    return data, None


def _described(data: dict) -> str:
    """An OAuth error in words, without anything else from the answer (it can echo secrets)."""
    error, detail = str(data.get("error") or "error"), data.get("error_description")
    return f"{error}: {detail}" if detail else error


def _tokens(data: dict) -> dict:
    if not data.get("access_token"):
        raise Unreachable("the token endpoint answered without an access_token")
    tokens = {"access_token": data["access_token"]}
    if data.get("refresh_token"):
        tokens["refresh_token"] = data["refresh_token"]
    try:
        tokens["expires_at"] = int(time.time() + float(data["expires_in"]))
    except (KeyError, TypeError, ValueError):
        tokens["expires_at"] = None  # it doesn't say: used until the server refuses it
    return tokens


async def _first_json(http: httpx2.AsyncClient, urls: list[str], what: str) -> dict:
    problem = ""
    for url in dict.fromkeys(urls):
        try:
            response = await _send(http, "GET", url, headers={"Accept": "application/json"})
        except Unreachable as error:
            problem = str(error)
            continue
        if response.status_code == 200:
            try:
                data = response.json()
            except ValueError:
                data = None
            if isinstance(data, dict):
                return data
        problem = f"{url} answered HTTP {response.status_code}"
    raise Unreachable(f"can't read {what}: {problem}")


def _well_known(url: str, suffix: str) -> list[str]:
    """Where a server keeps its metadata: /.well-known/<suffix> with the URL's path after it, then without."""
    parts = urllib.parse.urlsplit(url)
    origin, path = f"{parts.scheme}://{parts.netloc}", parts.path.rstrip("/")
    return ([f"{origin}/.well-known/{suffix}{path}"] if path else []) + [f"{origin}/.well-known/{suffix}"]


def _http() -> httpx2.AsyncClient:
    return httpx2.AsyncClient(timeout=TIMEOUT, follow_redirects=False)


async def _send(http: httpx2.AsyncClient, method: str, url: str, **kwargs) -> httpx2.Response:
    try:
        return await http.request(method, url, **kwargs)
    except httpx2.HTTPError as error:
        raise Unreachable(f"can't reach {url}: {error or type(error).__name__}") from error


@contextlib.asynccontextmanager
async def _callback(port: int):
    """Listen on localhost:<port> for the browser's redirect. Yields a future of its query."""
    answer = asyncio.get_running_loop().create_future()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request = (await asyncio.wait_for(reader.readline(), 10)).decode("latin-1").split()
            while (await asyncio.wait_for(reader.readline(), 10)).strip():
                pass  # the headers
            target = urllib.parse.urlsplit(request[1]) if len(request) > 1 else None
            if target is None or target.path != "/callback":
                status, page = "404 Not Found", "Not found."
            else:
                query = dict(urllib.parse.parse_qsl(target.query))
                if not answer.done():
                    answer.set_result(query)
                page = "Signed in: you can close this tab." if query.get("code") else "Signing in didn't finish: see the terminal."
                status = "200 OK"
            body = f"<!doctype html><title>wf</title><p>{html.escape(page)}</p>".encode()
            writer.write(f"HTTP/1.1 {status}\r\nContent-Type: text/html; charset=utf-8\r\n"
                         f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)
            await writer.drain()
        except (OSError, TimeoutError, UnicodeError):
            pass
        finally:
            writer.close()

    try:
        server = await asyncio.start_server(handle, "localhost", port)
    except OSError as error:
        raise SetupFailed(f"can't listen on localhost:{port} for the sign-in ({error.strerror or error}): choose another "
                          "port with --port (a Slack app or Google client then needs that redirect URL too)") from error
    async with server:
        yield answer
