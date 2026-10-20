"""Harness interfaces: what every coding-agent adapter implements.

A Harness is one kind of coding agent: Claude Code, Codex or Cursor. The runner
opens each harness it needs on first use within a stream of the graph (one
asyncio.run in runner.py), asks it for a Session for every attempt, and closes
it when the stream ends. A Session is one agent conversation, and
send() runs one turn of it: the agent works until it stops and waits for the
next message.

The attempt loop (nodes.py) is written once, against these interfaces: send
the instruction, then up to two reminders, until submit_result has a result.
An adapter only translates: SessionSpec into its SDK's options, and the SDK's
messages into progress events, a transcript, and a TurnEnd.

Besides submit_result, a session can carry wf tools (SessionSpec.tools: Slack,
Linear, ...; see tools/relay.py). Each is a ToolServer with the operations its
node may use, and the adapter wraps them the way it wraps submit_result, so the
agent's calls come back to the runner, which passes them on to the vendor.
A node that lists `tools` gets those and no other MCP server or connector, at
every access level (SessionSpec.only_wf_tools), and each adapter switches off
what its harness would otherwise load. A node without `tools` gets what it got
before.

How a turn ended:
- done: the turn finished normally (the agent may or may not have called
  submit_result; the attempt loop checks the tool).
- unavailable: a usage limit, a login problem, or a harness that can't start.
  The runner moves on to the next harness in the node's list right away.
- failed: anything else. It counts against the node's `retry`. A turn that sets
  retry_after makes the runner wait that many seconds before the retry on the same
  harness. The harness sets it for an error that a retry can clear, but not at once.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol

from ..output_tool import ResultTool

if TYPE_CHECKING:  # tools/relay.py loads the MCP client, which only runs that use tools need
    from ..tools.relay import ToolServer


@dataclass
class Usage:
    tokens: int = 0  # input + output, cached input included, as the harness reports them
    cost_usd: float = 0.0  # 0 when the harness doesn't report dollars

    def add(self, other: "Usage") -> None:
        self.tokens += other.tokens
        self.cost_usd += other.cost_usd


@dataclass
class SessionSpec:
    """Everything an adapter needs to start one session."""

    cwd: Path
    access: Literal["read-only", "edit", "full"]
    web: bool
    model: str | None
    effort: str | None
    tool: ResultTool  # submit_result for this attempt
    log: Path  # the attempt's transcript, attempt-<n>.log
    emit: Callable[[dict], None]  # progress events; the runner adds node, attempt and harness
    max_turns: int | None = None
    budget_usd: float | None = None
    node: str = ""  # the node's id, and its full path (build[1]/implement) for nodes in sub-workflows
    where: str = ""
    fork_from: str | None = None  # an earlier session to continue from (session: continue)
    continuable: bool = False  # a later pass may continue this session, so the harness must keep it
    prior_cost_usd: float = 0.0  # what fork_from had spent: a forked session's totals include it
    tools: list["ToolServer"] = field(default_factory=list)  # wf tools the agent may call, besides submit_result
    only_wf_tools: bool = False  # the node lists `tools`: no other MCP servers or connectors, whatever the access


@dataclass
class TurnEnd:
    status: Literal["done", "unavailable", "failed"]
    message: str = ""
    usage: Usage = field(default_factory=Usage)
    retry_after: float = 0  # failed: the seconds to wait before the retry


class Session(Protocol):
    id: str

    async def send(self, text: str) -> TurnEnd:
        """One turn. Progress goes to spec.emit and the transcript as it happens."""

    def pending_usage(self) -> Usage:
        """What a turn that hasn't returned yet has used so far (zero between turns), so a turn
        cut off by a timeout still counts. Harnesses that can't tell return Usage()."""

    async def close(self) -> None:
        """End the session, interrupting a turn that's still running."""


class Harness(Protocol):
    name: str

    async def open(self, run_dir: Path) -> None:
        """Before the first session of a stream (e.g. start an app server)."""

    async def session(self, spec: SessionSpec) -> Session:
        """A new session. Raises Unavailable if the harness can't start one."""

    async def close(self) -> None: ...


class Unavailable(Exception):
    """The harness can't run: not installed, not logged in, or out of quota."""


def transcript(path: Path, kind: str, **data) -> None:
    """Add one line to an attempt's transcript (JSON lines: easy to read, easy to parse)."""
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps({"kind": kind, **data}, ensure_ascii=False, default=str) + "\n")


def short(text: object, limit: int = 100) -> str:
    """One line, at most `limit` characters, for progress output."""
    line = " ".join(str(text).split())
    return line if len(line) <= limit else line[: limit - 1] + "…"
