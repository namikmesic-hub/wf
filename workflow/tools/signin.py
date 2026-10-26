"""Sign-in: getting a tool's secret once, and keeping it fresh.

`wf tools <name> setup` calls setup(), and keychain.py keeps what it returns. Every agent session
that uses the tool gets its token from access_token(), which refreshes an expired one first.

setup() is OAuth 2.1's authorization code flow with PKCE, as MCP's authorization spec has it:
1. Discovery: the MCP server's 401 names its protected resource metadata (RFC 9728), or wf
   tries the well-known places, and that metadata names the authorization server. Its own
   metadata (RFC 8414, or OpenID Connect's) gives the endpoints. The URLs are built in the order
   MCP's spec gives, by the MCP SDK's own helpers.
2. A client: Linear, Notion and Granola let wf register one (RFC 7591), so there's nothing to
   make first. Slack and Google don't: you make an OAuth client once (a Slack app, a Google
   Cloud OAuth client) and paste its id and secret.
3. Scopes: a tool's read scopes, unless setup is asked for writes (`--write`), for the vendors
   whose scopes tell reads from writes (Slack, Linear). Other vendors give one sign-in for both.
4. Your browser opens the vendor's sign-in page. wf waits for the redirect on
   http://localhost:<port>/callback and takes only one that brings back its `state`. Its `iss`
   must be the authorization server's issuer, exactly, before anything else in it counts (and
   it must be there when the server's metadata says it sends it). Then wf trades the code for
   tokens. The metadata's own `issuer` must be the one wf asked about, exactly (RFC 8414): the one
   the resource metadata named, or the catalog's, for a vendor that writes it two ways (Google).
With `--token`, setup takes a token you paste instead, for the vendors that give one out (a
Linear API key, a Notion MCP token, a Slack user token). Such a token has no refresh.

Why not the MCP SDK's OAuthClientProvider: it can't add the parameters Google needs for a
refresh token (`access_type=offline`), it rejects Slack's token answer (`token_type: user`),
and it refreshes within one process only. Its discovery helpers are used as they are.

Refresh: a stored access token that expires within a minute, or that the server just refused,
is refreshed with the refresh token when a session asks for one. Some vendors replace the
refresh token on every refresh and make the old one invalid, so:
- one refresh of a tool runs at a time, across wf processes too: a lock file per user, in the
  user's temporary folder, whatever WF_HOME says, since the Keychain item is the user's;
- a refresh runs in a task of its own, which finishes and saves the new tokens even when the
  session that asked is cancelled (a timeout), so the new refresh token isn't lost;
- new tokens the Keychain refuses to save are kept in memory, and saved again before the next
  refresh;
- a refresh the vendor refuses reads the Keychain again: a refresh outside the lock may have
  saved a newer refresh token there.
"""

import asyncio
import base64
import contextlib
import fcntl
import hashlib
import html
import os
import secrets
import tempfile
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx2

from . import keychain
from .catalog import Tool

TIMEOUT = 30  # seconds for each request to an authorization server
WAIT = 300  # seconds to finish signing in, in the browser
EARLY = 60  # refresh a token this many seconds before it expires

_refreshing: dict[str, asyncio.Task] = {}  # the refresh running for each tool, in this process
_unsaved: dict[str, dict] = {}  # refreshed sign-ins the Keychain refused to save


class NotSetUp(Exception):
    """There's no usable sign-in: `wf tools <name> setup` fixes it."""

    def __init__(self, tool: Tool, problem: str, fix: str = ""):
        self.problem, self.fix = problem, fix or f"run `wf tools {tool.name} setup`"
        super().__init__(f"{problem}: {self.fix}")


class Unreachable(Exception):
    """The vendor didn't answer as it should: trying again later may work."""


class SetupFailed(Exception):
    """Signing in didn't finish."""


@dataclass(frozen=True)
class AuthServer:
    """What discovery found: the endpoints, the resource to ask for, and the scopes."""

    issuer: str
    iss_required: bool  # the server says it puts `iss` in its redirect (RFC 9207)
    authorization_endpoint: str
    token_endpoint: str
    registration_endpoint: str | None
    resource: str
    scopes: tuple[str, ...]


def redirect_uri(port: int) -> str:
    return f"http://localhost:{port}/callback"


async def setup(tool: Tool, *, port: int, token: bool, write: bool, say: Callable[[str], None],
                ask: Callable[[str], str], ask_secret: Callable[[str], str], open_url: Callable[[str], object]) -> dict:
    """Sign in to a tool. Returns what to keep in the Keychain. The CLI passes print, input,
    getpass and webbrowser.open; tests pass stand-ins. `write` asks for the write scopes too."""
    today = time.strftime("%Y-%m-%d")
    if token:
        value = ask_secret(f"Paste the {tool.token}: ").strip()
        if not value:
            raise SetupFailed("no token was given")
        return {"method": "token", "access_token": value, "set_up": today}
    redirect = redirect_uri(port)
    async with _http() as http:
        server = await discover(http, tool, write)
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
        async with _callback(port, state) as answer:
            say(f"Opening your browser to sign in to {tool.title}. If it doesn't open, go to:\n{url}")
            open_url(url)
            try:
                back = await asyncio.wait_for(answer, WAIT)
            except TimeoutError:
                raise SetupFailed(f"no sign-in came back within {WAIT} seconds") from None
        if "iss" in back and back["iss"] != server.issuer:  # before reading anything else in it
            raise SetupFailed(f"the sign-in came back from {back['iss']}, not {server.issuer}, so it was ignored")
        if "iss" not in back and server.iss_required:
            raise SetupFailed(f"the sign-in came back without `iss`, which {tool.title} says it sends, so it was ignored")
        if back.get("error"):
            raise SetupFailed(f"{tool.title} didn't sign you in: {_described(back)}")
        if not back.get("code"):
            raise SetupFailed("the sign-in came back without a code: try again")
        form = {"grant_type": "authorization_code", "code": back["code"], "redirect_uri": redirect,
                "code_verifier": verifier, **resource}
        data, problem = _answer(await _token_request(http, server.token_endpoint, form, client))
    if problem:
        raise SetupFailed(f"{tool.title} didn't hand out a token: {problem}")
    return {"method": "oauth", **_tokens(data), "token_endpoint": server.token_endpoint, **client,
            "resource": server.resource if tool.resource else None, "set_up": today,
            **({"write": write} if tool.write_scopes else {})}


async def discover(http: httpx2.AsyncClient, tool: Tool, write: bool = False) -> AuthServer:
    """Find the tool's authorization server, starting from its MCP server's URL."""
    from mcp.client.auth.utils import (  # the MCP SDK, which only setup needs here
        build_oauth_authorization_server_metadata_discovery_urls,
        build_protected_resource_metadata_discovery_urls,
        extract_resource_metadata_from_www_auth,
        extract_scope_from_www_auth,
    )

    challenge = await _send(http, "POST", tool.url, headers={"Accept": "application/json, text/event-stream"},
                            json={"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}})
    named = extract_resource_metadata_from_www_auth(challenge) if challenge.status_code == 401 else None
    resource = await _first_json(http, build_protected_resource_metadata_discovery_urls(named, tool.url),
                                 f"{tool.title}'s resource metadata")
    issuers = resource.get("authorization_servers") or []
    if not issuers or not isinstance(issuers[0], str):
        raise Unreachable(f"{tool.title}'s resource metadata names no authorization server")
    issuer = tool.issuer or issuers[0]
    meta = await _first_json(http, build_oauth_authorization_server_metadata_discovery_urls(issuer, tool.url),
                             f"{tool.title}'s authorization server metadata")
    if not meta.get("authorization_endpoint") or not meta.get("token_endpoint"):
        raise Unreachable(f"{tool.title}'s authorization server doesn't list its endpoints")
    if meta.get("issuer") != issuer:  # RFC 8414: identical, or the metadata isn't this server's
        raise Unreachable(f"{tool.title}'s authorization server metadata names {meta.get('issuer')}, not {issuer}, "
                          "so wf doesn't use it")
    if tool.scopes:
        scopes = tool.scopes + (tool.write_scopes if write else ())
    else:  # the server's choice: what its 401 asks for, else what its metadata lists
        asked = extract_scope_from_www_auth(challenge) if challenge.status_code == 401 else None
        scopes = tuple(asked.split()) if asked else tuple(resource.get("scopes_supported") or ())
        if "offline_access" in (meta.get("scopes_supported") or ()) and "offline_access" not in scopes:
            scopes += ("offline_access",)  # how some servers hand out a refresh token
    return AuthServer(issuer=meta["issuer"], iss_required=meta.get("authorization_response_iss_parameter_supported") is True,
                      authorization_endpoint=meta["authorization_endpoint"],
                      token_endpoint=meta["token_endpoint"], registration_endpoint=meta.get("registration_endpoint"),
                      resource=resource.get("resource") or tool.url, scopes=scopes)


async def access_token(tool: Tool, refused: str | None = None) -> str:
    """A token for the tool's server: the stored one, refreshed first if it has expired, or if
    it's the one the server just `refused`. Raises NotSetUp, Unreachable or keychain.Unavailable."""
    stored = await load(tool)
    if not _stale(stored, refused):
        return stored["access_token"]
    if not stored.get("refresh_token"):
        what = "refused the stored token" if stored["access_token"] == refused else "sign-in has expired"
        raise NotSetUp(tool, f"{tool.title} {what}")
    task = _refreshing.get(tool.name)
    if task is None or task.done():
        task = _refreshing[tool.name] = asyncio.ensure_future(_refresh_in_turn(tool, refused))
        task.add_done_callback(lambda done: done.cancelled() or done.exception())  # seen, even with no one waiting
    return await asyncio.shield(task)  # a cancelled session stops waiting; the refresh goes on


async def load(tool: Tool) -> dict:
    """The tool's sign-in: one the Keychain refused to save, saved now if it can be, else the stored one."""
    pending = _unsaved.get(tool.name)
    if pending is not None:
        with contextlib.suppress(keychain.Unavailable):
            await asyncio.to_thread(keychain.save, tool.name, pending)
            _unsaved.pop(tool.name, None)
        return pending
    stored = await asyncio.to_thread(keychain.load, tool.name)
    if stored is None:
        raise NotSetUp(tool, f"{tool.title} isn't set up")
    return stored


def _stale(stored: dict, refused: str | None) -> bool:
    expires = stored.get("expires_at")
    return stored["access_token"] == refused or (expires is not None and expires - EARLY <= time.time())


async def _refresh_in_turn(tool: Tool, refused: str | None) -> str:
    async with _taking_turns(tool.name):
        stored = await load(tool)
        if not _stale(stored, refused):
            return stored["access_token"]  # refreshed meanwhile, by another session or process
        try:
            fresh = await _refresh(tool, stored)
        except NotSetUp:
            latest = await load(tool)
            if latest.get("refresh_token") != stored.get("refresh_token") and not _stale(latest, refused):
                return latest["access_token"]  # a refresh outside the lock saved a newer one
            raise
        try:
            await asyncio.to_thread(keychain.save, tool.name, fresh)
        except keychain.Unavailable:
            _unsaved[tool.name] = fresh  # the old refresh token may be dead already: keep the new one
        return fresh["access_token"]


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


def lock_folder() -> Path:
    """Where the refresh locks are: one folder per user, since the Keychain item is the user's."""
    return Path(tempfile.gettempdir()) / f"wf-tools-{os.getuid()}"


@contextlib.asynccontextmanager
async def _taking_turns(name: str):
    """One refresh of a tool's sign-in at a time, across processes: an exclusive lock on a file."""
    folder = lock_folder()
    folder.mkdir(mode=0o700, exist_ok=True)
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


def _http() -> httpx2.AsyncClient:
    return httpx2.AsyncClient(timeout=TIMEOUT, follow_redirects=False)


async def _send(http: httpx2.AsyncClient, method: str, url: str, **kwargs) -> httpx2.Response:
    try:
        return await http.request(method, url, **kwargs)
    except httpx2.HTTPError as error:
        raise Unreachable(f"can't reach {url}: {error or type(error).__name__}") from error


@contextlib.asynccontextmanager
async def _callback(port: int, state: str):
    """Listen on localhost:<port> for the browser's redirect. Yields a future of the query of the
    first one that brings back `state`: any other request to the port gets an error page, and the
    wait goes on."""
    answer = asyncio.get_running_loop().create_future()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request = (await asyncio.wait_for(reader.readline(), 10)).decode("latin-1").split()
            while (await asyncio.wait_for(reader.readline(), 10)).strip():
                pass  # the headers
            target = urllib.parse.urlsplit(request[1]) if len(request) > 1 else None
            query = dict(urllib.parse.parse_qsl(target.query)) if target is not None else {}
            if target is None or target.path != "/callback":
                status, page = "404 Not Found", "Not found."
            elif not secrets.compare_digest(query.get("state", ""), state):
                status, page = "400 Bad Request", "This isn't the sign-in wf is waiting for."
            else:
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
                          "port with --port, or with signin_port under [tools] in ~/.wf/config.toml (a Slack app or "
                          "Google client then needs that redirect URL too)") from error
    async with server:
        yield answer
