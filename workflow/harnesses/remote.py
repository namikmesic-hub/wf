"""Remote: a harness whose sessions run in another program, a peer that wf speaks the
wf-session protocol to.

A peer hosts one runtime's agent sessions. For Cursor it is the Cursor host (cursor_host/, in
TypeScript on Cursor's SDK), one child process per session, and the protocol runs over its stdin
and stdout. A later service that runs every runtime's sessions speaks the same protocol over a
socket, so this module doesn't change for it.

Frames are JSON objects, one per line, at most MAX_FRAME bytes, in both directions:
- hello and welcome: wf names the protocol it speaks (PROTOCOL) and gives the runtime's
  credentials. The peer answers with its own protocol and versions. Another protocol, or no
  answer, makes the harness unavailable.
- req and res: wf's requests, which the peer answers by id, in any order. An answer that fails
  carries an error with a class (below).
- ev: what happens in a session, numbered by `seq`, with the session's id and, in a turn, the turn's.
- call and ret: the peer asks wf to run one of the session's tools. wf always answers, with an
  error text when it can't run the tool, because the agent waits for the answer.
cursor_host/protocol/ has the frames as JSON Schema, and an example of each.

How the operations map onto base.py's interfaces:
- Harness.session() is session.open: the working folder, access level, web, model, the tools,
  and where to start (`fresh`, or `load` an earlier session for session: continue). The tools are
  submit_result, the runner's `git` at read-only, and the node's wf tools, each of kind `client`:
  wf keeps running them, and the peer only offers them to the agent and passes each call on.
  A harness with `file_tools` also gives a read-only session wf's file tools (files_tool.py),
  which read only the workspace and the files and folders the instruction names, and its peer then offers
  none of the runtime's own.
- Session.send() is turn.start, which the peer answers at once with the turn's id. Events follow:
  turn.text, turn.tool, turn.thinking, turn.request and turn.usage, and then turn.ended, which
  ends send().
- Session.steer() is turn.steer. `delivered`: the running turn has the message. `declined` is
  None here, so the runner sends the message as the next turn. `unconfirmed` is passed on, and so
  is no answer in STEER_TIMEOUT seconds. `queued`: the peer waits for the runtime's answer, and a
  steer.status event brings it. A message the runtime then declines goes back to the runner
  (given_back()), which sends it as the next turn, as it sends the ones that steer() returned None
  for. One that the runtime never answers for is unconfirmed when the session closes.
- Session.close() sends turn.cancel when a turn still runs, then session.close.

Error classes, in an answer or in turn.ended: unavailable, auth and usage_limit make the harness
unavailable for the attempt, so the runner goes to the node's next harness. An auth error's text
gets the harness's fix (RemoteHarness.fix). transient fails the attempt, and its retry waits the
seconds the error names (retry_after_s), or RETRY_AFTER. failed, internal and cancelled fail the
attempt, also when they come as a session opens (StartFailed). not_found, when the peer can't
load the session to continue, makes it start fresh. invalid is a bug in wf: opening a session
that way fails the job at once.

A frame longer than MAX_FRAME never goes: a tool's answer that big becomes an error text for the
agent, and a turn whose text is that big fails.

Usage: turn.ended carries the turn's usage object. Usage.tokens is its total_tokens, and
Usage.cost_usd its cost_usd, 0 when the runtime reports no dollars yet. The object's
input_tokens, cached_input_tokens (cache reads), cache_write_tokens and output_tokens are the
parts of total_tokens, as Cursor's SDK counts them. Its reasoning_tokens is a part of
output_tokens. Usage keeps all five, with cached_input_tokens as cache_read_tokens. turn.usage
events tell pending_usage() what a turn used so far, so a turn that a timeout cuts off still
counts. When close() cancels such a turn, what its turn.ended reports beyond that is
late_usage().

A turn's kind, for turn.started: the first is the instruction. The runner sends only reminders
(output_tool.REMINDER) and messages from `wf steer` after it, so the others are told apart by text.

What the agent does goes where each adapter puts it (base.py): an agent_tool progress event when
a tool call starts, the attempt's transcript (sent, text, tool, tool_result, turn, usage lines),
and the trace (spec.trace). For a wf tool the peer sends only the size of the answer, and so the
transcript and the trace keep only that. A block of the model's thinking (turn.thinking: its text
and the milliseconds it took) and the runtime's id for a request (turn.request) go to the trace only.
"""

import asyncio
import contextlib
import itertools
import json
import os
import signal
from collections.abc import Callable
from pathlib import Path

from .. import output_tool
from . import files_tool, git_tool
from .base import SessionSpec, StartFailed, TurnEnd, Unavailable, Usage, short, steer_dropped, steered, transcript

PROTOCOL = "wf-session/0"
MAX_FRAME = 1024 * 1024
STEER_TIMEOUT = 10  # seconds: inside the time `wf steer` waits for an answer (steer.py)
CLOSE_TIMEOUT = 5  # seconds for each step of closing a session
RETRY_AFTER = 30  # seconds before the retry after a `transient` error that names no wait
UNAVAILABLE = ("unavailable", "auth", "usage_limit", "not_found")
CLOSED = {"kind": "peer.closed"}  # in a session's events: the peer is gone


class RemoteError(Exception):
    """The peer refused a request: `error` holds its class, code and message."""

    def __init__(self, error: dict):
        self.error = error if isinstance(error, dict) else {}
        self.kind = self.error.get("class") or "internal"
        super().__init__(self.error.get("message") or self.kind)


class PeerGone(Exception):
    """The peer's process ended, or its pipes broke."""


class FrameTooBig(ValueError):
    """A frame longer than MAX_FRAME, which the peer would drop."""


class Peer:
    """A wf-session peer behind a child process's stdin and stdout. stderr goes to a log file."""

    def __init__(self, process: asyncio.subprocess.Process, stderr, timeout: float, name: str):
        self.process, self.stderr, self.timeout, self.name = process, stderr, timeout, name
        self.ids = itertools.count(1)
        self.pending: dict[str, asyncio.Future] = {}
        self.sessions: dict[str, RemoteSession] = {}
        self.early: dict[str, list[dict]] = {}  # events for a session that isn't attached yet
        self.answering: set[asyncio.Task] = set()  # tool calls in progress
        self.dead: str | None = None  # why the peer is gone, once it is
        self.welcome: dict = {}
        self.tap: Callable[[str, dict], None] | None = None  # sees every frame: "in" or "out" (tests)
        self._welcomed = asyncio.get_running_loop().create_future()
        self.reader = asyncio.ensure_future(self._read())

    @classmethod
    async def start(cls, command: list[str], *, cwd: Path, env: dict, log: Path, timeout: float, name: str,
                    client: dict, credentials: dict | None = None,
                    tap: Callable[[str, dict], None] | None = None) -> "Peer":
        """Start the peer and greet it. Raises OSError (it didn't start), PeerGone, RemoteError
        (another protocol) or TimeoutError."""
        log.parent.mkdir(parents=True, exist_ok=True)
        stderr = log.open("a", encoding="utf-8")
        try:
            process = await asyncio.create_subprocess_exec(
                *command, cwd=cwd, env=env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=stderr, limit=4 * MAX_FRAME,
                start_new_session=True,  # its own process group, so stopping it stops the commands it started
            )
        except OSError:
            stderr.close()
            raise
        peer = cls(process, stderr, timeout, name)
        peer.tap = tap
        try:
            await peer._write({"t": "hello", "protocol": PROTOCOL, "client": client,
                               **({"credentials": credentials} if credentials else {})})
            peer.welcome = await asyncio.wait_for(asyncio.shield(peer._welcomed), timeout)
            if peer.welcome is None:
                raise PeerGone(peer.dead)
            if peer.welcome.get("protocol") != PROTOCOL:
                raise RemoteError({"class": "unavailable", "code": "protocol.version",
                                   "message": f"{name} speaks {peer.welcome.get('protocol')}, and wf {PROTOCOL}"})
        except BaseException:
            await peer.stop()
            raise
        return peer

    async def request(self, op: str, args: dict, timeout: float | None = None) -> dict:
        """Send a request and wait for its answer. Raises RemoteError, PeerGone or TimeoutError."""
        if self.dead:
            raise PeerGone(self.dead)
        ident = f"r{next(self.ids)}"
        future = self.pending[ident] = asyncio.get_running_loop().create_future()
        session = {"session": args["session"]} if isinstance(args.get("session"), str) else {}
        try:
            await self._write({"t": "req", "id": ident, **session, "op": op, "args": args})
            return await asyncio.wait_for(future, timeout or self.timeout)
        except TimeoutError:  # an OSError too, but it means no answer, not a lost peer
            raise
        except OSError as error:
            raise PeerGone(self.dead or f"lost {self.name}: {error}") from error
        finally:
            self.pending.pop(ident, None)

    def attach(self, session: "RemoteSession") -> None:
        self.sessions[session.session] = session
        for event in self.early.pop(session.session, []):
            session.events.put_nowait(event)
        if self.dead:
            session.events.put_nowait(CLOSED)

    def detach(self, session: "RemoteSession") -> None:
        self.sessions.pop(session.session, None)

    async def _write(self, frame: dict) -> None:
        line = json.dumps(frame, ensure_ascii=False).encode()
        if len(line) > MAX_FRAME:
            raise FrameTooBig(f"a {frame.get('t')} frame of {len(line):,} bytes, more than the {MAX_FRAME:,} that {self.name} reads")
        if self.tap:
            self.tap("out", frame)
        self.process.stdin.write(line + b"\n")
        await self.process.stdin.drain()

    async def _read(self) -> None:
        try:
            while line := await self.process.stdout.readline():
                try:
                    frame = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(frame, dict):
                    continue
                if self.tap:
                    self.tap("in", frame)
                self._receive(frame)
        except (OSError, ValueError) as error:  # ValueError: a line longer than the reader's limit
            self.dead = f"lost {self.name}: {error}"
        except Exception as error:  # a bug in handling a frame: say so, rather than that the peer exited
            self.dead = f"wf failed to handle a frame from {self.name}: {type(error).__name__}: {error}"
        finally:
            self.dead = self.dead or f"{self.name} exited (its output is in {self.stderr.name})"
            if not self._welcomed.done():
                self._welcomed.set_result(None)  # no welcome: start() says why
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(PeerGone(self.dead))
            for session in self.sessions.values():
                session.events.put_nowait(CLOSED)

    def _receive(self, frame: dict) -> None:
        kind = frame.get("t")
        if kind == "welcome" and not self._welcomed.done():
            self._welcomed.set_result(frame)
        elif kind == "res":
            future = self.pending.get(str(frame.get("id")))
            if future is not None and not future.done():
                if frame.get("ok"):
                    future.set_result(frame.get("result") if isinstance(frame.get("result"), dict) else {})
                else:
                    future.set_exception(RemoteError(frame.get("error") or {}))
        elif kind == "ev":
            session = self.sessions.get(frame.get("session"))
            if session is not None:
                session.events.put_nowait(frame)
            elif isinstance(frame.get("session"), str):
                self.early.setdefault(frame["session"], []).append(frame)
        elif kind == "call":
            task = asyncio.ensure_future(self._answer(frame))
            self.answering.add(task)
            task.add_done_callback(self.answering.discard)

    async def _answer(self, frame: dict) -> None:
        """Run the tool a `call` asks for, and answer with `ret`. Every call gets its answer."""
        session = self.sessions.get(frame.get("session"))
        try:
            if session is None:
                content, is_error = [_text(f"Error: there's no session {frame.get('session')} here.")], True
            else:
                content, is_error = await session.call(frame)
        except Exception as error:
            content, is_error = [_text(f"Error: {type(error).__name__}: {error}")], True
        session = {"session": frame["session"]} if isinstance(frame.get("session"), str) else {}
        ret = {"t": "ret", "id": frame.get("id"), **session, "content": content, "isError": is_error}
        with contextlib.suppress(OSError):  # the peer is gone: the reader notices that
            try:
                await self._write(ret)
            except FrameTooBig as error:
                await self._write({**ret, "isError": True, "content": [_text(
                    f"Error: the answer was too big to pass on ({error}). Ask for less, for example a smaller part.")]})

    async def stop(self) -> None:
        """Close the peer's stdin, which asks it to stop, then end its process group."""
        if self.process.returncode is None:
            with contextlib.suppress(Exception):
                self.process.stdin.close()
            try:
                await asyncio.wait_for(self.process.wait(), CLOSE_TIMEOUT)
            except TimeoutError:
                _kill(self.process, signal.SIGTERM)
                try:
                    await asyncio.wait_for(self.process.wait(), CLOSE_TIMEOUT)
                except TimeoutError:
                    _kill(self.process, signal.SIGKILL)
                    await self.process.wait()
        _kill(self.process, signal.SIGKILL)  # whatever it started that's still running
        self.reader.cancel()
        for task in self.answering:
            task.cancel()
        # gather() hands back the reader's own cancellation as a value, but still lets a
        # cancellation of this task (Ctrl-C during teardown) through
        await asyncio.gather(self.reader, *self.answering, return_exceptions=True)
        self.stderr.close()


def _kill(process: asyncio.subprocess.Process, sig: int) -> None:
    """Signal the peer's process group."""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, sig)


class RemoteHarness:
    """A Harness whose sessions run behind wf-session peers. A subclass names the harness, says how
    to reach a peer (peer()), and what to do with it when a session is done (done())."""

    name = "remote"
    labels: dict[str, str] = {}  # the runtime's tool names, as progress shows them
    fix = ""  # what to do about an `auth` error
    file_tools = False  # read-only sessions read files through files_tool.py, not the runtime's tools
    tap: Callable[[str, dict], None] | None = None  # sees every frame to and from its peers (tests)

    def __init__(self, request_timeout: float = 60):
        self.request_timeout = request_timeout  # for each request: a turn has the node's timeout

    async def open(self, run_dir: Path) -> None:
        pass

    async def close(self) -> None:
        pass

    async def peer(self) -> Peer:
        """A peer for one session or check. Raises Unavailable."""
        raise NotImplementedError

    async def done(self, session: "RemoteSession") -> None:
        """After a session closed. A peer of its own stops here."""
        await session.peer.stop()

    async def check_model(self, model: str | None, access: str = "read-only") -> None:
        peer = await self.peer()
        try:
            await peer.request("runtime.check", {"runtime": self.name, "model": model, "effort": None})
        except RemoteError as error:
            raise Unavailable(self.explain(error.error)) from error
        except (PeerGone, TimeoutError) as error:
            raise Unavailable(f"{peer.name} didn't check the model: {error or 'no answer'}") from error
        finally:
            await peer.stop()

    async def session(self, spec: SessionSpec) -> "RemoteSession":
        return await self.start(spec, open_args(spec, self.name, self.file_tools))

    async def start(self, spec: SessionSpec, args: dict, extra: dict | None = None) -> "RemoteSession":
        """Open a session on a new peer with these session.open arguments. `extra` is the
        harness's own record of the session, for done()."""
        session = RemoteSession(self, await self.peer(), spec)
        session.extra = extra or {}
        try:
            await session.open(args)
        except BaseException as error:  # failed or cancelled: don't leave the peer running
            await session.close()
            if isinstance(error, RemoteError) and error.kind in UNAVAILABLE:
                raise Unavailable(self.explain(error.error)) from error
            if isinstance(error, RemoteError) and error.kind != "invalid":
                wait = float(error.error.get("retry_after_s") or RETRY_AFTER) if error.kind == "transient" else 0
                raise StartFailed(f"{self.name}: {self.explain(error.error)}", wait) from error
            if isinstance(error, (PeerGone, TimeoutError)):
                raise Unavailable(f"{session.peer.name} didn't start a session: {error or 'no answer'}") from error
            raise
        return session

    def explain(self, error: dict) -> str:
        """An error's text, and for `auth` what to do about it."""
        message = error.get("message") or error.get("class") or "error"
        return f"{message}. {self.fix}" if error.get("class") == "auth" and self.fix else message

    def ended(self, error: dict, usage: Usage) -> TurnEnd:
        """The TurnEnd for a turn that ended with this error."""
        kind, message = error.get("class"), f"{self.name}: {self.explain(error)}"
        if kind in UNAVAILABLE:
            return TurnEnd("unavailable", message, usage)
        if kind == "transient":
            return TurnEnd("failed", message, usage, retry_after=float(error.get("retry_after_s") or RETRY_AFTER))
        return TurnEnd("failed", message, usage)

    def label(self, data: dict) -> str:
        """A tool call's name, as progress and the trace show it. A wf tool's is linear.list_issues,
        as on the other harnesses."""
        tool, server = str(data.get("tool") or "tool"), data.get("server")
        return f"{server}.{tool}" if server else self.labels.get(tool, tool)


def open_args(spec: SessionSpec, runtime: str, file_tools: bool = False) -> dict:
    """session.open's arguments for a SessionSpec."""
    return {
        "runtime": runtime, "model": spec.model, "effort": spec.effort, "cwd": str(spec.cwd), "access": spec.access,
        "web": spec.web, "project_instructions": spec.project_instructions, "only_given_tools": spec.only_wf_tools,
        "max_turns": spec.max_turns, "budget_usd": spec.budget_usd, "tools": tools(spec, file_tools),
        "start": {"kind": "fresh"}, "retain": spec.continuable, "labels": {"node": spec.where or spec.node},
    }


def tools(spec: SessionSpec, file_tools: bool = False) -> list[dict]:
    """The session's tools, which wf runs: submit_result, git (and with `file_tools` the file
    tools) at read-only, and the node's wf tools."""
    given = [{"kind": "client", "name": output_tool.NAME, "description": output_tool.DESCRIPTION,
              "input_schema": spec.tool.schema}]
    if spec.access == "read-only":
        given.append({"kind": "client", "name": git_tool.NAME, "description": git_tool.DESCRIPTION,
                      "input_schema": git_tool.SCHEMA})
        given += [{"kind": "client", "name": name, "description": files_tool.description(name),
                   "input_schema": files_tool.schema(name)} for name in (files_tool.NAMES if file_tools else ())]
    for server in spec.tools:
        for op in server.operations:
            hints = op.annotations.model_dump(exclude_none=True) if hasattr(op.annotations, "model_dump") else op.annotations
            given.append({"kind": "client", "server": server.name, "name": op.name, "description": op.description,
                          "input_schema": op.input_schema, "private": True,
                          **({"annotations": hints} if isinstance(hints, dict) and hints else {})})
    return given


def usage(data: object) -> Usage:
    """wf's Usage for the protocol's usage object."""
    data = data if isinstance(data, dict) else {}

    def count(key: str) -> int:
        return int(data.get(key) or 0)

    return Usage(tokens=count("total_tokens"), cost_usd=float(data.get("cost_usd") or 0),
                 input_tokens=count("input_tokens"), output_tokens=count("output_tokens"),
                 cache_read_tokens=count("cached_input_tokens"), cache_write_tokens=count("cache_write_tokens"),
                 reasoning_tokens=count("reasoning_tokens"))


class RemoteSession:
    def __init__(self, harness: RemoteHarness, peer: Peer, spec: SessionSpec):
        self.harness, self.peer, self.spec = harness, peer, spec
        self.id = ""  # the runtime's id for the session, which session: continue loads
        self.session = ""  # the peer's id for it
        self.facts: dict = {}  # session.open's answer
        self.events: asyncio.Queue = asyncio.Queue()
        self.turn: str | None = None  # the turn that runs
        self.turns = 0
        self.text = ""  # the agent's text since the last tool call
        self.traced: dict[str, object] = {}  # call -> the arguments the trace has for it
        self.answered: dict[str, int] = {}  # call -> the size of a wf tool's answer
        self.pending = Usage()
        self.succeeded = False  # a turn ended with a result: the attempt has its result
        self.closed = False
        self.late = Usage()  # what a turn that close() cancelled reported beyond pending_usage()
        self.steering: dict[str, str] = {}  # `wf steer` messages the runtime hasn't answered for: id -> text
        self.given: list[tuple[str, str]] = []  # those it declined later, for the runner's next turn
        self.extra: dict = {}  # the harness's own record of the session

    async def open(self, args: dict) -> None:
        self.facts = await self.peer.request("session.open", args)
        self.session, self.id = str(self.facts.get("session") or ""), str(self.facts.get("runtime_session_id") or "")
        self.peer.attach(self)

    def pending_usage(self) -> Usage:
        return self.pending if self.turn else Usage()

    def late_usage(self) -> Usage:
        """What a turn that close() cancelled reported, beyond what pending_usage() had."""
        return self.late

    async def send(self, text: str) -> TurnEnd:
        spec, name = self.spec, self.harness.name
        kind = "instruction" if self.turns == 0 else "reminder" if text == output_tool.REMINDER else "steer"
        self.turns += 1
        transcript(spec.log, "sent", text=text)
        self.text, self.pending = "", Usage()
        try:
            answer = await self.peer.request("turn.start", {"session": self.session, "kind": kind, "text": text})
        except RemoteError as error:
            return self.harness.ended(error.error, Usage())
        except FrameTooBig as error:
            return TurnEnd("failed", f"{name}: the turn's text is too big to send: {error}")
        except (PeerGone, TimeoutError) as error:
            return TurnEnd("failed", f"{name}: {error or f'{self.peer.name} did not start the turn'}")
        self.turn = answer.get("turn")
        try:
            while True:
                event = await self.events.get()
                if event is CLOSED:
                    self.turn = None
                    return TurnEnd("failed", f"{name}: {self.peer.dead}", self.pending)
                kind, data = event.get("kind"), event.get("data") or {}
                if kind == "turn.text":
                    self.text += str(data.get("text") or "")
                elif kind == "turn.tool":
                    self._flush()
                    self._tool(data)
                elif kind in ("turn.thinking", "turn.request"):
                    self._note(kind, data)
                elif kind == "turn.usage":
                    self.pending = usage(data.get("usage"))
                elif kind == "steer.status":
                    self._steer_status(data)
                elif kind == "turn.ended" and event.get("turn") == self.turn:
                    self.turn = None
                    return self._ended(data)
        finally:
            self._flush()

    def _take_events(self) -> None:
        """Read what the runtime answered between turns for a `wf steer` message. Other events stay, in order."""
        kept = []
        while not self.events.empty():
            event = self.events.get_nowait()
            if event is not CLOSED and event.get("kind") == "steer.status":
                self._steer_status(event.get("data") or {})
            else:
                kept.append(event)
        for event in kept:
            self.events.put_nowait(event)

    def _steer_status(self, data: dict) -> None:
        """The runtime's late answer for a queued `wf steer` message."""
        ident = str(data.get("id") or "")
        text = self.steering.pop(ident, None)
        if text is None:
            return
        if data.get("status") == "delivered":
            steered(self.spec, ident, "delivered")
        else:
            self.given.append((ident, text))

    def given_back(self) -> list[tuple[str, str]]:
        """The `wf steer` messages that the runtime declined after steer() said `queued`, as (id, text),
        for the runner to send as the next turn (steer.Live.next_turn)."""
        self._take_events()
        given, self.given = self.given, []
        return given

    def _ended(self, data: dict) -> TurnEnd:
        error, used = data.get("error") or {}, usage(data.get("usage"))
        transcript(self.spec.log, "turn", status=data.get("status"), usage=data.get("usage"),
                   **({"error": error} if error else {}))
        self.succeeded = self.succeeded or self.spec.tool.result is not None
        if data.get("status") == "done":
            return TurnEnd("done", usage=used)
        if data.get("status") == "cancelled":
            return TurnEnd("failed", f"{self.harness.name}: the turn was cancelled", used)
        return self.harness.ended(error, used)

    def _tool(self, data: dict) -> None:
        spec, call, status = self.spec, str(data.get("call") or ""), data.get("status")
        label = self.harness.label(data)
        if status == "started":
            spec.emit({"event": "agent_tool", "tool": label, "detail": detail(data, spec.cwd)})
        self._trace_call(call, label, data)
        if status not in ("completed", "failed"):
            return
        self.traced.pop(call, None)
        kept = data.get("kept") is not False
        size = self.answered.pop(call, None) if not kept else None
        size = size if size is not None else int(data.get("size") or 0)
        outcome = {"output": short(data.get("result") or "", 2000)} if kept else {"kept": False, "characters": size}
        transcript(spec.log, "tool", tool=label, status=status, input=data.get("args"), **outcome)
        if kept:
            spec.trace.tool_result(call, data.get("result"), status == "failed")
        else:
            spec.trace.tool_result(call, None, status == "failed", kept=False, characters=size)

    def _note(self, kind: str, data: dict) -> None:
        """A block of the model's thinking, or the runtime's id for a request: for the trace only."""
        self._flush()
        if kind == "turn.thinking":
            took = data.get("duration_ms")
            self.spec.trace.thinking(str(data.get("text") or ""), took if isinstance(took, int) else None)
        elif data.get("request_id"):
            self.spec.trace.request(str(data["request_id"]))

    def _trace_call(self, call: str, label: str, data: dict) -> None:
        """A tool call for the trace, again each time that the peer tells more of its arguments."""
        arguments = data.get("args")
        if call not in self.traced or self.traced[call] != arguments:
            self.traced[call] = arguments
            self.spec.trace.tool_call(call, label, arguments)

    def _flush(self) -> None:
        if self.text.strip():
            transcript(self.spec.log, "text", text=self.text)
            self.spec.trace.text(self.text)
        self.text = ""

    async def call(self, frame: dict) -> tuple[list[dict], bool]:
        """Run the tool a `call` frame asks for: (content, is_error)."""
        spec, tool, server, args = self.spec, frame.get("tool"), frame.get("server"), frame.get("args")
        if server is None and tool == output_tool.NAME:
            text, is_error = spec.tool.submit(args)
            transcript(spec.log, "tool_result", tool=output_tool.NAME, is_error=is_error, content=text)
            return [_text(text)], is_error
        if server is None and tool == git_tool.NAME and spec.access == "read-only":
            given = args.get("args") if isinstance(args, dict) else None
            text, is_error = await asyncio.to_thread(git_tool.run, spec.cwd, given or [])
            return [_text(text)], is_error
        if server is None and tool in files_tool.NAMES and spec.access == "read-only" and self.harness.file_tools:
            text, is_error = await files_tool.run_async(str(tool), spec.cwd, spec.read_roots, args)
            return [_text(text)], is_error
        found = next((s for s in spec.tools if s.name == server), None)
        if found is None:
            return [_text(f"Error: there's no tool called `{tool}` here. Call `{output_tool.NAME}` with your result.")], True
        content, is_error = await found.call(str(tool), args)
        if frame.get("callId"):
            self.answered[str(frame["callId"])] = len(json.dumps(content, default=str))
        return content, is_error

    async def steer(self, text: str, ident: str) -> str | None:
        if not self.turn or self.peer.dead:
            return None
        self.steering[ident] = text  # before the answer: its steer.status may come first
        try:
            answer = await self.peer.request("turn.steer", {"session": self.session, "id": ident, "text": text},
                                             timeout=STEER_TIMEOUT)
        except TimeoutError:  # sent, but no answer: the agent may have it, so it isn't sent again
            self.steering.pop(ident, None)
            return "unconfirmed"
        except (RemoteError, PeerGone, FrameTooBig):
            self.steering.pop(ident, None)
            return None
        status = answer.get("status")
        if status != "queued":  # queued: a steer.status event brings the runtime's answer
            self.steering.pop(ident, None)
        return status if status in ("delivered", "queued", "unconfirmed") else None

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            if self.session and not self.peer.dead:
                if self.turn:  # a turn still runs, as after a timeout: stop it
                    with contextlib.suppress(Exception):
                        await self.peer.request("turn.cancel", {"session": self.session}, timeout=CLOSE_TIMEOUT)
                        await asyncio.wait_for(self._turn_end(), CLOSE_TIMEOUT)
                with contextlib.suppress(Exception):
                    await self.peer.request("session.close", {"session": self.session, "reason": "the attempt ended"},
                                            timeout=CLOSE_TIMEOUT)
        finally:  # even when cancelled: the peer must stop
            self._flush()
            self._take_events()
            for ident, _ in self.given:  # the runner didn't take them for another turn
                steer_dropped(self.spec, ident)
            for ident in self.steering:
                steered(self.spec, ident, "unconfirmed", reason=f"{self.harness.name} never said whether the agent read it")
            self.given, self.steering = [], {}
            self.peer.detach(self)
            await self.harness.done(self)

    async def _turn_end(self) -> None:
        """Read the running turn's events until it ends, so that its tool calls reach the trace, and
        what it reported beyond pending_usage() counts as late_usage()."""
        counted = self.pending
        while True:
            event = await self.events.get()
            if event is CLOSED:
                return
            kind, data = event.get("kind"), event.get("data") or {}
            if kind == "turn.tool":
                self._flush()
                self._tool(data)
            elif kind == "turn.text":
                self.text += str(data.get("text") or "")
            elif kind in ("turn.thinking", "turn.request"):
                self._note(kind, data)
            elif kind == "steer.status":
                self._steer_status(data)
            elif kind == "turn.ended" and event.get("turn") == self.turn:
                transcript(self.spec.log, "turn", status=data.get("status"), usage=data.get("usage"))
                self.late = usage(data.get("usage")).beyond(counted)
                self.turn = None
                return


def detail(data: dict, cwd: Path) -> str:
    """A few words about a tool call, for the progress line. Paths are shown relative to the workspace."""
    args = data.get("args")
    if not isinstance(args, dict):
        return ""
    if isinstance(args.get("args"), list):  # the runner's git tool: ["diff", "main...HEAD"]
        return short(" ".join(str(part) for part in args["args"]), 80)
    for key in ("path", "command", "pattern", "globPattern", "query", "searchTerm", "url"):
        if args.get(key):
            return short(str(args[key]).removeprefix(str(cwd).rstrip("/") + "/"), 80)
    return ""


def _text(text: str) -> dict:
    return {"type": "text", "text": text}
