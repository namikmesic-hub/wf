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
A message that the runner keeps goes out when the agent's turn ends without a
result, before any reminder. When the attempt ends first (the agent submitted
its result, or the attempt failed or timed out), the message is dropped.

Each message is recorded with its id, in events.jsonl (event `steer`) and in
the attempt's log (kind `steer`): `delivered` or `queued` when it arrives, then
`delivered` or `dropped` (with the reason) for one that was queued. A message
for a node with no live session gets the answer `none`, with the reason, and
changes nothing in the run.

A live session is one that an agent attempt has open: from when its harness
opened it until the attempt ends. The process that drives the run keeps them in
a Steering (RunContext.steering), by the path that events and dry-run scripts
use: implement, review[2], build[1]/implement.

The way in is files in the run folder, as between all wf processes (status.json,
the run's lock, the agent slots). `wf steer` (send) writes steer/<id>.request.
While the graph streams, the driver (answering) looks every POLL_SECONDS. It
takes a request by renaming it, hands the message to the session, and answers
in steer/<id>.reply. `wf steer` withdraws a request that nobody took within
ANSWER_SECONDS: it deletes the file. The rename and the delete can't both
succeed, so a message is taken once or never. The driver answers `none` to a
request that is older than ANSWER_SECONDS, because its sender has given up.

Run events can only come from the job that runs the node (LangGraph's stream
writer reads the job's context). So the attempt starts a task of its own that
hands messages to its session (Live.serve).
"""

import asyncio
import json
import re
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from pathlib import Path

from . import spec
from .harnesses.base import Session, SessionSpec, steer_dropped, steered
from .runs import Run, write_json

FOLDER = "steer"
POLL_SECONDS = 0.2
ANSWER_SECONDS = 15.0
PART = re.compile(r"([a-z][a-z0-9_]*)(?:\[(\d+)\])?")  # a node id (spec.NODE_ID), and an item's index
NOT_RUNNING = {"waiting": "the run is waiting for a person", "limit": "the run is paused at a limit",
               "failed": "the run failed", "succeeded": "the run succeeded"}
STOPPED = "the run paused or ended before a session took the message"


class SteerError(Exception):
    """The message can't go: the node isn't an agent node of the run, or the driver never answered."""


# ---------- wf steer ----------


def target(run: Run, node: str, item: int | None) -> str:
    """The path of an agent node's sessions, as events name it: `node`, with `item` as the index of
    its last part, checked against the run's copies of its workflow files. Raises SteerError."""
    if item is not None and item < 0:
        raise SteerError("--item is an item's index: 0 or more")
    workflow = _load(run.workflow_file)
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
    folder = run.dir / FOLDER
    folder.mkdir(exist_ok=True)
    request, reply = folder / f"{ident}.request", folder / f"{ident}.reply"
    write_json(request, {"path": path, "text": text})
    answer = _await_answer(reply)
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
    deadline = time.monotonic() + ANSWER_SECONDS
    while time.monotonic() < deadline:
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
        serving = asyncio.create_task(live.serve())  # with this task's context, so its events reach the run
        self._live[spec.where] = live
        try:
            yield live
        finally:
            del self._live[spec.where]
            serving.cancel()
            await asyncio.gather(serving, return_exceptions=True)
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
    """One open agent session, and the messages that its harness couldn't take, for the next turns."""

    def __init__(self, spec: SessionSpec, attempt: int, harness: str, session: Session):
        self.spec, self.attempt, self.harness, self.session = spec, attempt, harness, session
        self._inbox: asyncio.Queue[tuple[Message, asyncio.Future]] = asyncio.Queue()
        self._handing: asyncio.Future | None = None  # the answer for the message being handed over
        self._held: list[Message] = []

    async def take(self, message: Message) -> dict:
        """Have the attempt's task hand the message over, and say how that went."""
        answer = asyncio.get_running_loop().create_future()
        self._inbox.put_nowait((message, answer))
        status = await answer
        if status is None:
            return {"status": "none", "reason": "the session ended before it took the message"}
        return {"status": status, "attempt": self.attempt, "harness": self.harness}

    async def serve(self) -> None:
        while True:
            message, self._handing = await self._inbox.get()
            try:
                status = await self.session.steer(message.text, message.id)
            except Exception:  # the session broke: its turn fails, and the message is dropped with it
                status = None
            if status is None:
                self._held.append(message)
            steered(self.spec, message.id, status or "queued", text=message.text)
            if not self._handing.done():
                self._handing.set_result(status or "queued")

    def next_turn(self) -> str | None:
        """The oldest message kept for the next turn, which counts as delivered now, or None."""
        if not self._held:
            return None
        message = self._held.pop(0)
        steered(self.spec, message.id, "delivered")
        return message.text

    def end(self) -> None:
        """The attempt is over: drop the messages kept for a next turn, and answer those not handed over."""
        for message in self._held:
            steer_dropped(self.spec, message.id)
        self._held.clear()
        waiting = [self._handing] + [answer for _, answer in self._drain()]
        for answer in waiting:
            if answer is not None and not answer.done():
                answer.set_result(None)

    def _drain(self) -> list[tuple[Message, asyncio.Future]]:
        found = []
        while not self._inbox.empty():
            found.append(self._inbox.get_nowait())
        return found


@asynccontextmanager
async def answering(run: Run, steering: Steering) -> AsyncIterator[None]:
    """While the graph streams: take the run's `wf steer` requests, and answer them."""
    box = _Mailbox(run.dir / FOLDER, steering)
    task = asyncio.create_task(box.serve())
    try:
        yield
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        for ident, _, _ in box.take():  # it came as the graph stopped
            box.reply(ident, {"status": "none", "reason": STOPPED})


class _Mailbox:
    def __init__(self, folder: Path, steering: Steering):
        self.folder, self.steering = folder, steering
        self.answering: set[asyncio.Task] = set()

    async def serve(self) -> None:
        try:
            while True:
                for ident, request, stale in self.take():
                    task = asyncio.create_task(self._answer(ident, request, stale))
                    self.answering.add(task)
                    task.add_done_callback(self.answering.discard)
                await asyncio.sleep(POLL_SECONDS)
        finally:
            for task in self.answering:
                task.cancel()
            await asyncio.gather(*self.answering, return_exceptions=True)

    async def _answer(self, ident: str, request: dict, stale: bool) -> None:
        answer = {"status": "none", "reason": STOPPED}
        try:
            if stale:
                answer["reason"] = f"it waited more than {ANSWER_SECONDS:g} seconds, so its sender has given up"
            elif not isinstance(request.get("path"), str) or not isinstance(request.get("text"), str):
                answer["reason"] = "the request isn't one that wf steer writes"
            else:
                answer = await self.steering.deliver(request["path"], Message(ident, request["text"]))
        finally:
            self.reply(ident, answer)

    def take(self) -> list[tuple[str, dict, bool]]:
        """The requests that nobody has taken yet, taken now: (id, request, whether it's stale). Also
        removes the files of requests whose `wf steer`, or driver, has gone."""
        if not self.folder.is_dir():
            return []
        taken = []
        for file in sorted(self.folder.glob("*.request")):
            claimed = file.with_suffix(".taken")
            try:
                stale = time.time() - file.stat().st_mtime > ANSWER_SECONDS
                file.rename(claimed)
            except FileNotFoundError:  # withdrawn
                continue
            try:
                request = json.loads(claimed.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                request = {}
            claimed.unlink(missing_ok=True)
            taken.append((file.stem, request if isinstance(request, dict) else {}, stale))
        for file in [*self.folder.glob("*.reply"), *self.folder.glob("*.taken")]:
            try:
                if time.time() - file.stat().st_mtime > 2 * ANSWER_SECONDS:
                    file.unlink()
            except FileNotFoundError:
                pass
        return taken

    def reply(self, ident: str, answer: dict) -> None:
        write_json(self.folder / f"{ident}.reply", answer)
