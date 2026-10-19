"""Capacity: one cap on the agent sessions live at once, across every wf process of the user.

`max_parallel` caps the items of one node in one run. Several runs started
together (eight reviews while another change is being written) can still open
more agent sessions at once than the machine, or an account's quota, holds.
`max_agents` caps them all:

    WF_MAX_AGENTS=4                       this process, and the background drivers it starts
    max_agents: 4   in ~/.wf/config.yaml  every wf process of the user (settings.py)

The environment variable wins over the file. With neither there is no cap, and
nothing here runs. Give every wf process the same cap; the file does that. A
process with a cap of N uses slots 0 to N-1, so with different caps the total
stays within the biggest.

An agent attempt takes one of the slots before its session opens and gives it
back when the attempt ends, so a retry or a fallback takes one again. A slot is
a file, ~/.wf/agent-slots/<n>, held with an exclusive fcntl.flock for as long
as the attempt runs, like a run's lock (runs.py): the operating system releases
it when the process dies, so a crashed or killed run never keeps one. The file
names its holder (pid, run, node, attempt); a free slot's file may still name
its last one.

An attempt that finds every slot taken waits, and tries again every
POLL_SECONDS. Waiting isn't failing: it uses no retry, and the node's timeout
starts once the attempt has its slot. A run limit reached during the wait ends
it, and the attempt doesn't start. nodes._agent_slot puts the wait in
events.jsonl and in status.json, for `wf status`.

Only agent attempts take slots. Command and human nodes don't, nor does a
workflow node (its own agent nodes do), nor a dry run, whose agents are scripted.
"""

import asyncio
import fcntl
import json
import os
from collections.abc import Callable
from pathlib import Path

from . import settings
from .workspaces import home

ENV = "WF_MAX_AGENTS"
POLL_SECONDS = 0.2


class CapError(Exception):
    """The cap is set, but not to a whole number of 1 or more."""


def slots_dir() -> Path:
    return home() / "agent-slots"


def configured() -> int | None:
    """The cap: WF_MAX_AGENTS, else `max_agents` in config.yaml, else None (no cap).
    Raises CapError, or settings.SettingsError when config.yaml is wrong."""
    raw = os.environ.get(ENV, "").strip()
    if raw:
        return _at_least_one(int(raw) if raw.isdigit() else raw, ENV)
    value = settings.get("max_agents")
    return None if value is None else _at_least_one(value, f"max_agents in {settings.path()}")


def _at_least_one(value: object, where: str) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
        return value
    raise CapError(f"{where} must be a whole number of 1 or more, not `{value}`")


class Slot:
    """One slot, held by this process until release()."""

    def __init__(self, fd: int):
        self.fd = fd

    def release(self) -> None:
        os.close(self.fd)  # closing releases the flock


def take(cap: int, holder: dict) -> Slot | None:
    """A slot among the first `cap` that is free right now, or None if they're all taken."""
    folder = slots_dir()
    folder.mkdir(parents=True, exist_ok=True)
    for number in range(cap):
        fd = os.open(folder / str(number), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            continue
        except BaseException:
            os.close(fd)
            raise
        try:
            os.ftruncate(fd, 0)
            os.pwrite(fd, (json.dumps(holder) + "\n").encode(), 0)
        except OSError:  # a full disk: the slot is held all the same, only the note is missing
            pass
        return Slot(fd)
    return None


async def wait(cap: int, holder: dict, stop: Callable[[], bool]) -> Slot | None:
    """The first slot that frees up, or None once stop() is true: it's asked before every try.
    A cancelled wait holds none."""
    while not stop():
        if (slot := take(cap, holder)) is not None:
            return slot
        await asyncio.sleep(POLL_SECONDS)
    return None
