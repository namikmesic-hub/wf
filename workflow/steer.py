"""Steer: a message from a person into the live agent session of a running node.

    wf steer <run-id> <node> [--item <index>] <message>
    wf steer <run-id> <node> [--item <index>] --file <file>     (- reads it from stdin)

A person who supervises a run can redirect an agent while it works, as they
would by typing into the agent's own terminal. The message goes to the agent as
its next user message. How soon depends on the harness (harnesses/base.py,
Session.steer):
- Codex adds it to the running turn (turn/steer), and the agent reads it at its
  next step: `delivered`.
- Claude Code keeps it and reads it at the agent's next step: `queued`, until
  Claude Code says that it read the message.
- Cursor can't add to a running turn: `queued`, and the runner sends it as the
  next turn when this one ends.
The messages that the runner keeps go out together as the next turn, when the
agent's turn ends without a result, before any reminder. They count as
delivered once that turn ends. When the attempt ends first (the agent submitted
its result, or the attempt failed or timed out), they are dropped.

Each message is recorded with its id, in events.jsonl (event `steer`) and in
the attempt's log (kind `steer`): `delivered`, `queued` or `unconfirmed` when
it arrives, then `delivered`, `dropped` or `unconfirmed` (with the reason) for
one that was queued. `unconfirmed` means the harness may have it, but wf can't
tell whether the agent read it. Then the turn that carried it failed or was cut
off, the attempt ended while its harness took the message, or Codex didn't
answer turn/steer. A message for a node with no live session gets the answer
`none`, with the reason, and changes nothing in the run.

A live session is one that an agent attempt has open: from when its harness
opened it until the attempt ends. The process that drives the run keeps them in
a Steering (RunContext.steering), by the path that events and dry-run scripts
use: implement, review[2], build[1]/implement.

The way in is files, as between all wf processes (status.json, the run's lock,
the agent slots). They are in ~/.wf/steer/<run-id>-<hash> (mailbox()), not in
the run folder. A run folder in the repo is inside the workspace of a `repo`
agent. An agent that writes only in its workspace mustn't be able to send
messages to the others. So `wf steer` is off (trusted()) when the mailbox is in
the run's repo or in any git work tree, where the agents of some run work. That
is so for the path as WF_HOME names it, and for the path that its links resolve
to. Both processes then use the resolved path, so a link that changes later
can't move the mailbox. An agent whose commands run as you without a sandbox
can run `wf steer` itself, as it can run any command of yours.

`wf steer` (send) writes <sent>-<id>.request there, with the time it sent the
message in the name. It adds none when MAX_IN_MAILBOX requests wait already.
While the graph streams, the driver (answering) looks every POLL_SECONDS. It
reads at most MAX_NAMES names, and takes the MAX_PER_LOOK oldest requests by
renaming them. It hands each message to its session, and answers in <id>.reply.
It removes its answers after 2 * ANSWER_SECONDS, when their senders have gone.
`wf steer` withdraws a request that nobody took within ANSWER_SECONDS, or when
it's stopped: it deletes the file. The rename and the delete can't both
succeed, so a message is taken once or never. The driver answers `none` to a
request older than ANSWER_SECONDS, because its sender has given up. Both
processes tell time by CLOCK_MONOTONIC, which a change of the wall clock leaves
alone. They add the boot it counts from, and a time from another boot is old.
On a system that doesn't say which boot it is in, `wf steer` is off.

Run events can only come from the job that runs the node (LangGraph's stream
writer reads the job's context). So the attempt starts a task of its own that
hands messages to its session (Live._serve). When the attempt ends, a message
being handed over finishes first, so its answer is true.
"""

import asyncio
import collections
import contextlib
import ctypes
import functools
import heapq
import itertools
import json
import os
import re
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from pathlib import Path

from . import spec, workspaces
from .harnesses.base import Session, SessionSpec, TurnEnd, steer_dropped, steered
from .runs import Run, write_json

POLL_SECONDS = 0.2
ANSWER_SECONDS = 15.0
HANDOVER_SECONDS = 12.0  # the longest a harness takes a message: Codex waits up to 10 seconds for turn/steer
MAX_CHARACTERS = 100_000  # in one message
MAX_WAITING = 10  # messages that one session has not taken yet
MAX_PER_LOOK = 10  # requests the driver takes in one look at the mailbox: the rest wait for the next
MAX_IN_MAILBOX = 100  # requests that wait in a run's mailbox: wf steer adds no more
MAX_NAMES = 1_000  # names the driver reads in one look at the mailbox
PART = re.compile(r"([a-z][a-z0-9_]*)(?:\[(\d+)\])?")  # a node id (spec.NODE_ID), and an item's index
REQUEST = re.compile(r"(\d+\.\d+)-(.+)\.request")  # <sent>-<id>.request, as wf steer names a request
NOT_RUNNING = {"waiting": "the run is waiting for a person", "limit": "the run is paused at a limit",
               "failed": "the run failed", "succeeded": "the run succeeded"}
STOPPED = "the run paused or ended before a session took the message"
CUT_OFF = "the attempt ended while its harness took the message, so wf can't tell whether the agent read it"
EXPOSED = ("wf's own folder (WF_HOME) is in a repo, where an agent could write messages for the others, "
           "so wf steer is off. Move WF_HOME out of every repo")
NO_BOOT = ("this system doesn't say which boot it is in, so wf can't tell how old a message is, "
           "and wf steer is off")


class SteerError(Exception):
    """The message can't go: the node isn't an agent node of the run, or the driver never answered."""


def mailbox(run: Run) -> Path:
    """Where `wf steer` and the run's driver pass messages (workspaces.mailbox)."""
    return workspaces.mailbox(run.dir)


def trusted(run: Run) -> Path | None:
    """The run's mailbox, resolved, or None when an agent could write in it: when it is in the
    run's repo or in a git work tree, as WF_HOME names it or as its links resolve. Use the path
    that this returns, so that a link that changes later can't move the mailbox."""
    named = Path(os.path.abspath(mailbox(run)))
    resolved = named.resolve()
    repos = {Path(os.path.abspath(run.repo)), Path(run.repo).resolve()}
    for path in (named, resolved):
        if any(path.is_relative_to(repo) for repo in repos) or any(
                os.path.lexists(folder / ".git") for folder in (path, *path.parents)):
            return None
    return resolved


def now() -> float:
    """Seconds on a clock that every process on the machine shares, and that wall clock changes leave alone."""
    return time.clock_gettime(time.CLOCK_MONOTONIC)


@functools.cache
def boot() -> str:
    """This boot of the machine. CLOCK_MONOTONIC starts again at each boot, so a time from another
    boot means nothing. "" when the system doesn't say: then wf steer is off."""
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()  # Linux
    except OSError:
        return _sysctl("kern.bootsessionuuid")  # macOS


def _sysctl(name: str) -> str:
    """A text value of the kernel, from sysctlbyname() in the C library, so no program on PATH runs.
    "" when there is no such value, or no sysctlbyname()."""
    try:
        sysctlbyname = ctypes.CDLL(None).sysctlbyname
    except (OSError, AttributeError):
        return ""
    sysctlbyname.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p,
                             ctypes.c_size_t]
    size = ctypes.c_size_t(0)
    if sysctlbyname(name.encode(), None, ctypes.byref(size), None, 0) != 0 or not size.value:
        return ""
    value = ctypes.create_string_buffer(size.value)
    if sysctlbyname(name.encode(), value, ctypes.byref(size), None, 0) != 0:
        return ""
    return value.value.decode(errors="replace").strip()


def _older(sent: float | None, booted: object, seconds: float) -> bool:
    """Is a time that `wf steer` sent a message at, in the boot `booted`, older than `seconds`? A
    time in the future, missing, or from another boot or one that the system doesn't name counts
    as older."""
    return sent is None or not boot() or booted != boot() or not 0 <= now() - sent <= seconds


# ---------- wf steer ----------


def target(run: Run, node: str, item: int | None) -> str:
    """The path of an agent node's sessions, as events name it: `node`, with `item` as the index of
    its last part, checked against the run's copies of its workflow files. Raises SteerError."""
    if item is not None and item < 0:
        raise SteerError("--item is an item's index: 0 or more")
    try:
        main = run.workflow_file
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise SteerError(f"can't read the run's copy of its workflow: {error}") from error
    workflow = _load(main)
    parts, path = node.split("/"), []
    for position, part in enumerate(parts):
        match = PART.fullmatch(part)
        if match is None:
            raise SteerError(f"`{part}` isn't a node id, or a node id with an item's index such as review[2]")
        name, index, last = match[1], match[2], position == len(parts) - 1
        if last and item is not None:
            if index is not None:
                raise SteerError("give the item's index once: in the node, or with --item")
            index = str(item)
        found = workflow.nodes.get(name)
        if found is None:
            raise SteerError(f"workflow {workflow.name} has no node `{name}`")
        inside = f"{name}[<index>]/<node>" if found.for_each else f"{name}/<node>"
        if last and found.kind == "workflow":
            raise SteerError(f"`{name}` is a workflow node: name an agent node inside it, as {inside}")
        if last and found.kind != "agent":
            raise SteerError(f"`{name}` is a {found.kind} node: only agent nodes have sessions")
        if not last and found.kind != "workflow":
            raise SteerError(f"`{name}` isn't a workflow node, so it has no nodes inside")
        if found.for_each and index is None:
            raise SteerError(f"`{name}` runs a session for each item: name the item with --item <index>" if last
                             else f"`{name}` runs its sub-workflow for each item: name the item, as {inside}")
        if index is not None and not found.for_each:
            raise SteerError(f"`{name}` has no items: it has no for_each")
        path.append(name if index is None else f"{name}[{int(index)}]")
        if not last:
            workflow = _load((Path(workflow.path).parent / found.workflow).resolve())
    return "/".join(path)


def _load(path: Path) -> spec.Workflow:
    try:
        return spec.load(path)
    except (OSError, ValueError, spec.SpecError) as error:
        raise SteerError(f"can't read the run's copy of {Path(path).name}: {error}") from error


def send(run: Run, path: str, text: str) -> dict:
    """Hand a message for the session at `path` to the process that drives the run, and wait for its
    answer. The answer: {id, status: delivered | queued | unconfirmed | none, reason (for none and
    unconfirmed), attempt and harness (for all but none)}. Raises SteerError when the mailbox isn't
    safe to use, or the driver took the message but never answered."""
    ident = str(uuid.uuid4())
    status = run.status().get("status")
    if status != "running":
        why = NOT_RUNNING.get(status, f"the run's status is `{status}`")
        return {"id": ident, "status": "none", "reason": f"{why}, so no agent works in it"}
    if not run.driven():
        return {"id": ident, "status": "none", "reason": "no wf process drives the run (it crashed or was killed)"}
    folder = trusted(run)
    if folder is None:
        raise SteerError(EXPOSED)
    if not boot():
        raise SteerError(NO_BOOT)
    folder.mkdir(parents=True, exist_ok=True)
    if sum(name.endswith(".request") for name in _names(folder)) >= MAX_IN_MAILBOX:
        return {"id": ident, "status": "none", "reason": f"{MAX_IN_MAILBOX} messages already wait for the wf "
                                                         "process that drives the run"}
    sent = now()
    request, reply = folder / f"{sent:.6f}-{ident}.request", folder / f"{ident}.reply"
    write_json(request, {"path": path, "text": text, "boot": boot()})
    try:
        answer = _await_answer(reply)
    except BaseException:  # stopped (Ctrl-C): a request that nobody took yet goes with it
        request.unlink(missing_ok=True)
        raise
    if answer is None:
        try:
            request.unlink()  # withdrawn: nobody took it, and now nobody can
            return {"id": ident, "status": "none", "reason": "the wf process that drives the run didn't take the "
                                                             f"message within {ANSWER_SECONDS:g} seconds"}
        except FileNotFoundError:  # taken just now, so its answer is coming
            answer = _await_answer(reply)
    if answer is None:
        raise SteerError("the wf process that drives the run took the message but didn't answer. "
                         "Its events.jsonl says whether the message reached the agent")
    return {"id": ident, **answer}


def _await_answer(reply: Path) -> dict | None:
    """The driver's answer, once it's in `reply`, or None after ANSWER_SECONDS."""
    deadline = now() + ANSWER_SECONDS
    while now() < deadline:
        try:
            answer = json.loads(reply.read_text(encoding="utf-8"))
        except FileNotFoundError:
            time.sleep(0.05)
            continue
        reply.unlink(missing_ok=True)
        return answer
    return None


# ---------- in the process that drives the run ----------


@dataclass
class Message:
    id: str
    text: str


class Steering:
    """The agent sessions this process has open, by path, and the messages for them."""

    def __init__(self):
        self._live: dict[str, Live] = {}
        self._opening: dict[str, tuple[int, str]] = {}  # path -> the attempt and harness opening a session

    @contextmanager
    def opening(self, path: str, attempt: int, harness: str) -> Iterator[None]:
        """While an attempt opens its session."""
        self._opening[path] = (attempt, harness)
        try:
            yield
        finally:
            self._opening.pop(path, None)

    @asynccontextmanager
    async def live(self, spec: SessionSpec, attempt: int, harness: str, session: Session) -> AsyncIterator["Live"]:
        """While an attempt's session is open. Enter it from the attempt's own task."""
        live = Live(spec, attempt, harness, session)
        live.start()
        self._live[spec.where] = live
        try:
            yield live
        finally:
            del self._live[spec.where]
            try:
                await live.stop()
            finally:
                live.end()

    async def deliver(self, path: str, message: Message) -> dict:
        live = self._live.get(path)
        if live is not None:
            return await live.take(message)
        if path in self._opening:
            attempt, harness = self._opening[path]
            return {"status": "none", "reason": f"attempt {attempt} is opening its {harness} session: "
                                                "send the message again in a moment"}
        return {"status": "none", "reason": "no agent session of it is open now"}


class Live:
    """One open agent session, and the messages that its harness couldn't take, for the next turn."""

    def __init__(self, spec: SessionSpec, attempt: int, harness: str, session: Session):
        self.spec, self.attempt, self.harness, self.session = spec, attempt, harness, session
        self._inbox: asyncio.Queue[tuple[Message, asyncio.Future] | None] = asyncio.Queue()
        self._handing: asyncio.Future | None = None  # the answer for the message being handed over
        self._idle = asyncio.Event()  # no message is waiting or being handed over
        self._idle.set()
        self._held: list[Message] = []  # for the next turn
        self._sending: list[Message] = []  # in the turn that is running
        self._serving: asyncio.Task | None = None

    def start(self) -> None:
        self._serving = asyncio.create_task(self._serve())  # with this task's context, so its events reach the run

    async def take(self, message: Message) -> dict:
        """Have the attempt's task hand the message over, and say how that went."""
        handing = self._handing is not None and not self._handing.done()
        if self._inbox.qsize() + len(self._held) + handing >= MAX_WAITING:
            return {"status": "none", "reason": f"{MAX_WAITING} messages already wait for this session"}
        answer = asyncio.get_running_loop().create_future()
        self._idle.clear()
        self._inbox.put_nowait((message, answer))
        status = await answer
        if status is None:
            return {"status": "none", "reason": "the session ended before it took the message"}
        return {"status": status, "attempt": self.attempt, "harness": self.harness,
                **({"reason": CUT_OFF} if status == "unconfirmed" else {})}

    async def _serve(self) -> None:
        while (item := await self._inbox.get()) is not None:
            message, self._handing = item
            try:
                await self._hand_over(message, self._handing)
            except Exception:  # only recording it can fail here, after its answer: serve the next one
                pass
            finally:
                if not self._handing.done():  # stopped while its harness took it: the harness may have it
                    self._handing.set_result("unconfirmed")
                    steered(self.spec, message.id, "unconfirmed", text=message.text, reason=CUT_OFF)
            if self._inbox.empty():
                self._idle.set()

    async def _hand_over(self, message: Message, answer: asyncio.Future) -> None:
        try:
            status = await self.session.steer(message.text, message.id)
        except Exception:  # the session broke: its turn fails, and the message is dropped with it
            status = None
        if status is None:
            self._held.append(message)
        answer.set_result(status or "queued")
        steered(self.spec, message.id, status or "queued", text=message.text)

    async def next_turn(self) -> str | None:
        """The text of the messages kept for the next turn, or None. A message being handed over goes
        first: if the harness can't take it, it's in this turn too."""
        while not self._idle.is_set():  # another message can come between the wake-up and this check
            await self._idle.wait()
        if not self._held:
            return None
        self._sending, self._held = self._held, []
        return "\n\n".join(message.text for message in self._sending)

    def sent(self, end: TurnEnd) -> None:
        """The turn that carried the kept messages has ended: they reached the agent, unless it failed."""
        sending, self._sending = self._sending, []
        for message in sending:
            if end.status == "done":
                steered(self.spec, message.id, "delivered")
            else:
                steered(self.spec, message.id, "unconfirmed", reason="the turn that carried it failed, so wf can't "
                                                                     f"tell whether the agent read it: {end.message}")

    async def stop(self) -> None:
        """The attempt is ending. A message being handed over finishes, and the ones after it get `none`.
        One that is still being handed over after HANDOVER_SECONDS is `unconfirmed`."""
        while not self._inbox.empty():
            if (item := self._inbox.get_nowait()) is not None and not item[1].done():
                item[1].set_result(None)
        self._inbox.put_nowait(None)
        with contextlib.suppress(TimeoutError):  # too slow: it's cancelled (_serve)
            await asyncio.wait_for(self._serving, HANDOVER_SECONDS)

    def end(self) -> None:
        """Drop the messages kept for a next turn. Those of a turn that didn't end are `unconfirmed`."""
        sending, held, self._sending, self._held = self._sending, self._held, [], []
        for message in sending:
            steered(self.spec, message.id, "unconfirmed", reason="the attempt ended during the turn that carried "
                                                                 "it, so wf can't tell whether the agent read it")
        for message in held:
            steer_dropped(self.spec, message.id)


@asynccontextmanager
async def answering(run: Run, steering: Steering) -> AsyncIterator[None]:
    """While the graph streams: take the run's `wf steer` requests, and answer them. Not when an
    agent could write in the mailbox (trusted), or the system doesn't name its boot: `wf steer`
    refuses then, and what is in the mailbox didn't come from it."""
    folder = trusted(run)
    if folder is None or not boot():
        yield
        return
    box = _Mailbox(folder, steering)
    box.tidy_up()
    task = asyncio.create_task(box.serve())
    try:
        yield
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        box.refuse_the_rest()


class _Mailbox:
    def __init__(self, folder: Path, steering: Steering):
        self.folder, self.steering = folder, steering
        self.answering: set[asyncio.Task] = set()
        self.answers: collections.deque[tuple[float, Path]] = collections.deque()  # (written, file), oldest first

    async def serve(self) -> None:
        try:
            while True:
                for ident, request, problem in self.take():
                    task = asyncio.create_task(self._answer(ident, request, problem))
                    self.answering.add(task)
                    task.add_done_callback(self.answering.discard)
                self.forget_answers()
                await asyncio.sleep(POLL_SECONDS)
        finally:
            for task in self.answering:
                task.cancel()
            await asyncio.gather(*self.answering, return_exceptions=True)

    async def _answer(self, ident: str, request: dict, problem: str | None) -> None:
        answer = {"status": "none", "reason": problem or STOPPED}
        try:
            if problem is None:
                answer = await self.steering.deliver(request["path"], Message(ident, request["text"]))
        finally:
            self.reply(ident, answer)

    def tidy_up(self) -> None:
        """Remove the requests that an earlier driver of the run took, and never read: it died. Its
        answers go after 2 * ANSWER_SECONDS from now, as this driver's own do."""
        for name in _names(self.folder):
            if name.endswith(".taken"):
                (self.folder / name).unlink(missing_ok=True)
            elif name.endswith(".reply"):
                self.answers.append((now(), self.folder / name))

    def take(self) -> list[tuple[str, dict, str | None]]:
        """At most MAX_PER_LOOK requests that nobody has taken yet, taken now, oldest first: (id,
        request, why it can't go, or None)."""
        taken = []
        for ident, sent, claimed in self._claim(MAX_PER_LOOK):
            try:
                small = claimed.stat().st_size <= 8 * MAX_CHARACTERS
                request = json.loads(claimed.read_text(encoding="utf-8")) if small else None
            except (OSError, ValueError):
                request = {}
            claimed.unlink(missing_ok=True)
            taken.append((ident, request if isinstance(request, dict) else {}, _problem(request, sent)))
        return taken

    def refuse_the_rest(self) -> None:
        """Answer the requests that came as the graph stopped: no session takes them now."""
        for ident, _, claimed in self._claim(MAX_NAMES):
            claimed.unlink(missing_ok=True)
            self.reply(ident, {"status": "none", "reason": STOPPED})

    def _claim(self, most: int) -> list[tuple[str, float | None, Path]]:
        """The `most` oldest requests, taken by renaming them: (id, when wf steer sent it, the renamed
        file). A request whose name doesn't say when it was sent goes first, to be refused."""
        requests = (name for name in _names(self.folder) if name.endswith(".request"))
        claimed = []
        for name in heapq.nsmallest(most, requests, key=lambda name: _sent(name)[0] or 0.0):
            file = self.folder / name
            try:
                file.rename(file.with_suffix(".taken"))
            except OSError:  # withdrawn
                continue
            sent, ident = _sent(name)
            claimed.append((ident, sent, file.with_suffix(".taken")))
        return claimed

    def reply(self, ident: str, answer: dict) -> None:
        with contextlib.suppress(OSError):  # wf steer then says that the driver didn't answer
            write_json(self.folder / f"{ident}.reply", answer)
        self.answers.append((now(), self.folder / f"{ident}.reply"))

    def forget_answers(self) -> None:
        """Remove the answers whose `wf steer` has gone: it reads its answer at once, or stops
        waiting after ANSWER_SECONDS."""
        while self.answers and now() - self.answers[0][0] > 2 * ANSWER_SECONDS:
            self.answers.popleft()[1].unlink(missing_ok=True)


def _names(folder: Path) -> list[str]:
    """At most MAX_NAMES names in the mailbox, in the order that the system lists them."""
    try:
        with os.scandir(folder) as entries:
            return [entry.name for entry in itertools.islice(entries, MAX_NAMES)]
    except OSError:
        return []


def _sent(name: str) -> tuple[float | None, str]:
    """When `wf steer` sent a request, and its id, from the request's name: <sent>-<id>.request."""
    match = REQUEST.fullmatch(name)
    return (float(match[1]), match[2]) if match else (None, name.removesuffix(".request"))


def _problem(request, sent: float | None) -> str | None:
    """Why a request can't go to a session, or None."""
    if request is None:
        return f"the message is longer than {MAX_CHARACTERS:,} characters"
    if sent is None or not isinstance(request, dict) or not all(isinstance(request.get(k), str) for k in
                                                                ("path", "text", "boot")):
        return "the request isn't one that wf steer writes"
    if len(request["text"]) > MAX_CHARACTERS:
        return f"the message is longer than {MAX_CHARACTERS:,} characters"
    if _older(sent, request["boot"], ANSWER_SECONDS):
        return f"it waited more than {ANSWER_SECONDS:g} seconds, so its sender has given up"
    return None
