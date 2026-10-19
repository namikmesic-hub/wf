"""A scripted fake harness for tests: each send() takes the next turn from a list."""

import asyncio
from pathlib import Path

from workflow import nodes
from workflow.harnesses.base import TurnEnd, Usage


class Fake:
    def __init__(self, name: str, *turns, fork_fails: bool = False):
        self.name, self.turns, self.fork_fails = name, list(turns), fork_fails
        self.sent, self.specs, self.replies, self.closed = [], [], [], 0

    def factory(self):
        return _FakeHarness(self)


class _FakeHarness:
    def __init__(self, fake: Fake):
        self.fake, self.name = fake, fake.name

    async def open(self, run_dir):
        pass

    async def session(self, spec):
        self.fake.specs.append(spec)
        if spec.fork_from and self.fake.fork_fails:
            from workflow.harnesses import Unavailable
            raise Unavailable(f"no session {spec.fork_from}")
        return _FakeSession(self.fake, spec)

    async def close(self):
        pass


class _FakeSession:
    def __init__(self, fake: Fake, spec):
        self.fake, self.spec, self.id = fake, spec, f"session-{len(fake.specs)}"
        self.pending = Usage()

    async def send(self, text: str) -> TurnEnd:
        self.fake.sent.append(text)
        return await self.fake.turns.pop(0)(self)

    def pending_usage(self) -> Usage:
        return self.pending

    async def close(self):
        self.fake.closed += 1


def submit(*results, tokens=100, cost=0.01):
    """A turn that reads notes.txt, then calls submit_result with each result in turn."""
    async def turn(session):
        session.spec.emit({"event": "agent_tool", "tool": "Read", "detail": "notes.txt"})
        session.spec.trace.tool_call("read-1", "Read", {"file_path": "notes.txt"})
        session.spec.trace.tool_result("read-1", "the notes")
        for result in results:
            session.fake.replies.append(session.spec.tool.submit(result))
        return TurnEnd("done", usage=Usage(tokens, cost))
    return turn


def talk(tokens=100, cost=0.01):
    """A turn that only writes text."""
    async def turn(session):
        session.spec.trace.text("Still looking.")
        return TurnEnd("done", usage=Usage(tokens, cost))
    return turn


def ends(status, message, cost=0.0, retry_after=0):
    async def turn(session):
        return TurnEnd(status, message, Usage(0, cost), retry_after)
    return turn


def hang(tokens=0):
    """A turn that never ends, having used `tokens` so far."""
    async def turn(session):
        session.pending = Usage(tokens, 0.0)
        await asyncio.sleep(60)
    return turn


def edits(files: dict, *results, check=None):
    """A turn that writes files in the session's folder, then submits. `check(cwd)` runs first."""
    async def turn(session):
        cwd = Path(session.spec.cwd)
        if check:
            check(cwd)
        for name, text in files.items():
            (cwd / name).write_text(text)
        for result in results:
            session.fake.replies.append(session.spec.tool.submit(result))
        return TurnEnd("done", usage=Usage(10, 0.0))
    return turn


def record_pauses(monkeypatch) -> list[float]:
    """The waits the runner makes before a retry, in seconds. Each wait still runs."""
    pauses, real_pause = [], nodes._pause

    async def pause(ctx, wait):
        pauses.append(wait)
        await real_pause(ctx, wait)

    monkeypatch.setattr(nodes, "_pause", pause)
    return pauses
