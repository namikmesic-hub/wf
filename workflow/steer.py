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
the attempt's log (kind `steer`): `delivered` or `queued` when it arrives, then
`delivered`, `dropped` or `unconfirmed` (with the reason) for one that was
queued. `unconfirmed` means the harness may have it, but wf can't tell whether
the agent read it. Then the turn that carried it failed or was cut off, the
session ended in the hand-over, or Codex didn't answer turn/steer. A message for a
node with no live session gets the answer `none`, with the reason, and changes
nothing in the run.

A live session is one that an agent attempt has open: from when its harness
opened it until the attempt ends. The process that drives the run keeps them in
a Steering (RunContext.steering), by the path that events and dry-run scripts
use: implement, review[2], build[1]/implement.

The way in is files, as between all wf processes (status.json, the run's lock,
the agent slots). They are in ~/.wf/steer/<run-id>-<hash> (mailbox()), not in
the run folder. A run folder in the repo is inside the workspace of a `repo`
agent. An agent that writes only in its workspace mustn't be able to send
messages to the others. When WF_HOME puts ~/.wf in the repo, `wf steer` is off
for the run (exposed()). An agent whose commands run as you without a sandbox
can run `wf steer` itself, as it can run any command of yours.

`wf steer` (send) writes <id>.request there. While the graph streams, the driver
(answering) looks every POLL_SECONDS. It takes at most MAX_PER_LOOK requests by
renaming them, oldest first, hands each message to its session, and answers in
<id>.reply.
`wf steer` withdraws a request that nobody took within ANSWER_SECONDS, or when
it's stopped: it deletes the file. The rename and the delete can't both
succeed, so a message is taken once or never. The driver answers `none` to a
request older than ANSWER_SECONDS, because its sender has given up. Both
processes tell time by CLOCK_MONOTONIC, which a change of the wall clock leaves
alone. They add the boot it counts from, and a time from another boot is old.

Run events can only come from the job that runs the node (LangGraph's stream
writer reads the job's context). So the attempt starts a task of its own that
hands messages to its session (Live._serve). When the attempt ends, a message
being handed over finishes first, so its answer is true.
"""

import asyncio
import contextlib
import functools
import json
import re
import subprocess
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
MAX_PER_LOOK = 10  # requests the driver reads in one look at the mailbox: the rest wait for the next
PART = re.compile(r"([a-z][a-z0-9_]*)(?:\[(\d+)\])?")  # a node id (spec.NODE_ID), and an item's index
NOT_RUNNING = {"waiting": "the run is waiting for a person", "limit": "the run is paused at a limit",
               "failed": "the run failed", "succeeded": "the run succeeded"}
STOPPED = "the run paused or ended before a session took the message"
EXPOSED = ("wf's own folder (WF_HOME) is inside the repo, where an agent could write messages for the others, "
           "so wf steer is off for this run")


class SteerError(Exception):
    """The message can't go: the node isn't an agent node of the run, or the driver never answered."""


def mailbox(run: Run) -> Path:
    """Where `wf steer` and the run's driver pass messages (workspaces.mailbox)."""
    return workspaces.mailbox(run.dir)


def exposed(run: Run) -> bool:
    """Is the mailbox inside the run's repo, where a `repo` agent could write? (WF_HOME can put it there.)"""
    return mailbox(run).resolve().is_relative_to(Path(run.repo).resolve())


def now() -> float:
    """Seconds on a clock that every process on the machine shares, and that wall clock changes leave alone."""
    return time.clock_gettime(time.CLOCK_MONOTONIC)


@functools.cache
def boot() -> str:
    """This boot of the machine. CLOCK_MONOTONIC starts again at each boot, so a time from another
    boot means nothing. "" when the system doesn't say."""
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()  # Linux
    except OSError:
        pass
    try:
        return subprocess.run(["sysctl", "-n", "kern.bootsessionuuid"], capture_output=True, text=True,  # macOS
                              timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _older(record: dict, key: str, seconds: float) -> bool:
    """Is the time at `key` in a record of `wf steer` or the driver older than `seconds`? A time in
    the future, from another boot, or missing counts as older."""
    stamp = record.get(key)
    return not isinstance(stamp, float) or record.get("boot") != boot() or not 0 <= now() - stamp <= seconds


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
    answer. The answer: {id, status: delivered | queued | none, reason (for none), attempt and
    harness (for the others)}. Raises SteerError when the driver took the message but never answered."""
    ident = str(uuid.uuid4())
    status = run.status().get("status")
    if status != "running":
        why = NOT_RUNNING.get(status, f"the run's status is `{status}`")
        return {"id": ident, "status": "none", "reason": f"{why}, so no agent works in it"}
    if not run.driven():
        return {"id": ident, "status": "none", "reason": "no wf process drives the run (it crashed or was killed)"}
    if exposed(run):
        raise SteerError(EXPOSED)
    folder = mailbox(run)
    folder.mkdir(parents=True, exist_ok=True)
    request, reply = folder / f"{ident}.request", folder / f"{ident}.reply"
    write_json(request, {"path": path, "text": text, "sent": now(), "boot": boot()})
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
        return {k: v for k, v in answer.items() if k not in ("written", "boot")}
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
        return {"status": status, "attempt": self.attempt, "harness": self.harness}

    async def _serve(self) -> None:
        while (item := await self._inbox.get()) is not None:
            message, self._handing = item
            try:
                await self._hand_over(message, self._handing)
            except Exception:  # only recording it can fail here, after its answer: serve the next one
                pass
            finally:
                if not self._handing.done():  # stopped while its harness took it: the harness may have it
                    self._handing.set_result("queued")
                    self._record(message.id, "queued", text=message.text)
                    self._record(message.id, "unconfirmed", reason="the session ended while its harness took the "
                                                                   "message, so wf can't tell whether the agent read it")
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
        self._record(message.id, status or "queued", text=message.text)

    def _record(self, ident: str, status: str, **more) -> None:
        with contextlib.suppress(OSError):  # a full disk loses the record, not the message or the attempt
            steered(self.spec, ident, status, **more)

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
                self._record(message.id, "delivered")
            else:
                self._record(message.id, "unconfirmed", reason="the turn that carried it failed, so wf can't tell "
                                                               f"whether the agent read it: {end.message}")

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
            self._record(message.id, "unconfirmed", reason="the attempt ended during the turn that carried it, "
                                                           "so wf can't tell whether the agent read it")
        for message in held:
            with contextlib.suppress(OSError):
                steer_dropped(self.spec, message.id)


@asynccontextmanager
async def answering(run: Run, steering: Steering) -> AsyncIterator[None]:
    """While the graph streams: take the run's `wf steer` requests, and answer them. Not when the
    mailbox is inside the repo (exposed): `wf steer` refuses then, and anything there is an agent's."""
    if exposed(run):
        yield
        return
    box = _Mailbox(mailbox(run), steering)
    box.forget_taken()
    task = asyncio.create_task(box.serve())
    try:
        yield
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        while taken := box.take():  # they came as the graph stopped
            for ident, _, _ in taken:
                box.reply(ident, {"status": "none", "reason": STOPPED})


class _Mailbox:
    def __init__(self, folder: Path, steering: Steering):
        self.folder, self.steering = folder, steering
        self.answering: set[asyncio.Task] = set()

    async def serve(self) -> None:
        try:
            while True:
                for ident, request, problem in self.take():
                    task = asyncio.create_task(self._answer(ident, request, problem))
                    self.answering.add(task)
                    task.add_done_callback(self.answering.discard)
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

    def forget_taken(self) -> None:
        """Remove the requests that an earlier driver of the run took, and never read: it died."""
        if self.folder.is_dir():
            for file in self.folder.glob("*.taken"):
                file.unlink(missing_ok=True)

    def take(self) -> list[tuple[str, dict, str | None]]:
        """At most MAX_PER_LOOK requests that nobody has taken yet, taken now, oldest first: (id,
        request, why it can't go, or None). Also removes the answers whose `wf steer` has gone."""
        if not self.folder.is_dir():
            return []
        taken = []
        for file in sorted(self.folder.glob("*.request"), key=_mtime)[:MAX_PER_LOOK]:  # a guess at the oldest
            claimed = file.with_suffix(".taken")
            try:
                size = file.stat().st_size
                file.rename(claimed)
            except FileNotFoundError:  # withdrawn
                continue
            try:
                request = json.loads(claimed.read_text(encoding="utf-8")) if size <= 8 * MAX_CHARACTERS else None
            except (OSError, ValueError):
                request = {}
            claimed.unlink(missing_ok=True)
            taken.append((file.stem, request if isinstance(request, dict) else {}, _problem(request)))
        for file in self.folder.glob("*.reply"):
            try:
                if _older(json.loads(file.read_text(encoding="utf-8")), "written", 2 * ANSWER_SECONDS):
                    file.unlink()
            except (OSError, ValueError):  # just answered, or gone
                pass
        return sorted(taken, key=lambda found: found[1].get("sent") if isinstance(found[1].get("sent"), float) else 0.0)

    def reply(self, ident: str, answer: dict) -> None:
        write_json(self.folder / f"{ident}.reply", {**answer, "written": now(), "boot": boot()})


def _mtime(file: Path) -> float:
    try:
        return file.stat().st_mtime
    except FileNotFoundError:
        return 0.0


def _problem(request) -> str | None:
    """Why a request can't go to a session, or None."""
    if request is None:
        return f"the message is longer than {MAX_CHARACTERS:,} characters"
    if not isinstance(request, dict) or not all(isinstance(request.get(k), t) for k, t in
                                                (("path", str), ("text", str), ("sent", float))):
        return "the request isn't one that wf steer writes"
    if len(request["text"]) > MAX_CHARACTERS:
        return f"the message is longer than {MAX_CHARACTERS:,} characters"
    if _older(request, "sent", ANSWER_SECONDS):
        return f"it waited more than {ANSWER_SECONDS:g} seconds, so its sender has given up"
    return None
