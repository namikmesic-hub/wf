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

A session also gets the repo's own agent instruction files (CLAUDE.md,
AGENTS.md, Cursor's rules), as the harness loads them, unless the node sets
`project_instructions: false` (SessionSpec.project_instructions). Then each
adapter keeps out every such file its harness lets it. Its docstring names
what stays, and `wf validate` warns about that (harnesses.KEPT_INSTRUCTIONS).

Each adapter also reports to SessionSpec.trace, a Recorder, what the agent did:
its text, and each tool call with its full arguments and answer. These go into
the run's trace (trace.py). What a wf tool answers isn't kept there either.
Cursor's SDK also reports the model's thinking, with the time it took, and an
id for each request.

While a session is open, a person can send it a message with `wf steer`
(steer.py). steer() offers the message to the turn that's running. A harness
that can't take it now returns None, and the runner sends it as the next turn.
Every message is recorded with steered(): in the transcript and as an event.

How a turn ended:
- done: the turn finished normally (the agent may or may not have called
  submit_result; the attempt loop checks the tool).
- unavailable: a usage limit, a login problem, or a harness that can't start.
  The runner moves on to the next harness in the node's list right away.
- failed: anything else. It counts against the node's `retry`. A turn that sets
  retry_after makes the runner wait that many seconds before the retry on the same
  harness. The harness sets it for an error that a retry can clear, but not at once.
"""

import contextlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol

from ..output_tool import ResultTool

if TYPE_CHECKING:  # tools/relay.py loads the MCP client, which only runs that use tools need
    from ..tools.relay import ToolServer


@dataclass
class Usage:
    """What a turn or an attempt used. input_tokens, cache_read_tokens, cache_write_tokens and
    output_tokens are the parts of tokens. A count that the harness doesn't report is 0."""

    tokens: int = 0  # input + output, cached input included, as the harness reports them
    cost_usd: float = 0.0  # 0 when the harness doesn't report dollars
    input_tokens: int = 0  # the input that the prompt cache didn't read or write
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0  # the part of output_tokens that the model used to reason

    def add(self, other: "Usage") -> None:
        for count in fields(self):
            setattr(self, count.name, getattr(self, count.name) + getattr(other, count.name))

    def beyond(self, before: "Usage") -> "Usage":
        """What this has used after `before`, count by count, and none where `before` has more."""
        return Usage(**{count.name: max(getattr(self, count.name) - getattr(before, count.name), count.default)
                        for count in fields(self)})


class Recorder:
    """Where an adapter reports what the agent does in a turn. The runner gives each attempt a
    trace.Attempt, which writes the run's trace. This one keeps nothing."""

    def text(self, text: str) -> None:
        """Text the agent wrote."""

    def tool_call(self, call: str, tool: str, arguments: Any) -> None:
        """The agent called `tool`. `call` is the harness's id for the call. An adapter reports a
        call when it starts, and again if it learns more of its arguments later."""

    def tool_result(self, call: str, result: Any, is_error: bool = False, kept: bool = True,
                    characters: int | None = None) -> None:
        """The answer to a call. kept=False for a wf tool's answer (other people's messages and
        mail): the trace notes only its size, which is `characters` when the harness gives only that."""

    def thinking(self, text: str, duration_ms: int | None) -> None:
        """A block of the model's thinking, and the milliseconds it took (None: the turn ended first)."""

    def request(self, request_id: str) -> None:
        """The runtime's id for a request of the turn, by which the runtime's own records know it."""


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
    project_instructions: bool = True  # False: the repo's CLAUDE.md, AGENTS.md and rules stay out of the session
    read_roots: list[Path] = field(default_factory=list)  # the files and folders the instruction names, which the agent may read too
    trace: Recorder = field(default_factory=Recorder)  # the agent's text and tool calls, for the run's trace
    watchdog: Any = None


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
        cut off by a timeout still counts. Harnesses that can't tell return Usage(). A harness
        that learns more of it only as close() ends the turn has late_usage() too, which the runner
        reads after close()."""

    async def steer(self, text: str, ident: str) -> Literal["delivered", "queued", "unconfirmed"] | None:
        """A message from `wf steer`, with its id. "delivered": the running turn has it now.
        "queued": the harness holds it for the agent's next step, and records with steered() what
        became of it, or steer_dropped() when the session ends first. "unconfirmed": the harness
        sent it on but got no answer, so it may have it, and the runner doesn't send it again. None:
        the harness can't take it now, so the runner sends it in the next turn. A harness whose
        runtime can decline a queued message later has given_back() too: the runner takes those
        messages for its next turn, as if steer() had returned None."""

    async def close(self) -> None:
        """End the session, interrupting a turn that's still running."""


class Harness(Protocol):
    name: str

    async def open(self, run_dir: Path) -> None:
        """Before the first session of a stream (e.g. start an app server)."""

    async def check_model(self, model: str | None, access: str = "read-only") -> None:
        """After open(): raise Unavailable if the harness knows that it can't run `model` for a node
        at this access level. With no model, whether the harness can run at all."""

    async def session(self, spec: SessionSpec) -> Session:
        """A new session. Raises Unavailable if the harness can't start one, or StartFailed when a
        retry may."""

    async def close(self) -> None: ...


class Unavailable(Exception):
    """The harness can't run: not installed, not logged in, or out of quota."""


class StartFailed(Exception):
    """The harness couldn't start a session now, for a reason that a retry can clear. The retry on
    the same harness waits `retry_after` seconds."""

    def __init__(self, message: str, retry_after: float = 0):
        super().__init__(message)
        self.retry_after = retry_after


def transcript(path: Path, kind: str, **data) -> None:
    """Add one line to an attempt's transcript (JSON lines: easy to read, easy to parse)."""
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps({"kind": kind, **data}, ensure_ascii=False, default=str) + "\n")


def steered(spec: SessionSpec, ident: str, status: str, **more) -> None:
    """Record what became of a message from `wf steer`: a `steer` line in the transcript, and a
    `steer` event. A line that can't be written (a full disk) is lost alone: the event still goes,
    and neither the message nor the session fails."""
    with contextlib.suppress(OSError):
        transcript(spec.log, "steer", id=ident, status=status, **more)
    spec.emit({"event": "steer", "id": ident, "status": status, **more})


def steer_dropped(spec: SessionSpec, ident: str) -> None:
    """A message from `wf steer` that the agent never read, because its session ended first."""
    reason = ("the agent submitted its result before it read the message" if spec.tool.result is not None
              else "the session ended before the agent read the message")
    steered(spec, ident, "dropped", reason=reason)


def short(text: object, limit: int = 100) -> str:
    """One line, at most `limit` characters, for progress output."""
    line = " ".join(str(text).split())
    return line if len(line) <= limit else line[: limit - 1] + "…"
