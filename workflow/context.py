"""Context: what the graph's nodes need that isn't saved in checkpoints.

LangGraph hands this object to every node as `runtime.context` (it's the
graph's context_schema, see graph.py). Each `wf run` or `wf retry` process
builds a fresh one: the parsed workflow, the run folder, a clock, live usage
counters, a semaphore per node that caps parallel items, and the cap on live
agent sessions across runs (capacity.py). None of it goes into a checkpoint,
which is why it can hold things like asyncio.Semaphore.

State (state.py) is the opposite: plain data that LangGraph saves after every
step, so it survives the process.

A sub-workflow (a workflow node) runs with a child context. That context has its own workflow,
topology, folder and `repo` workspace, and the path of node ids that leads to it (`prefix`). The
path names its worktree branches. The parts that the spec gives the whole run are the top-level
context's, which `root` reaches. These are the clock, the usage counters and the limits. A
sub-workflow's own `limits` don't count: `limits` is the file's, with the run's `--limit` values on
top (limits.py). In a follow-up (`wf continue`), the limits count from `budget_from`. The root also
has the agent cap, the attempts that wait for it, the open harnesses, and the sessions that
`wf steer` can reach (steer.py). It also has the git lock, the checkpointer and the trace file.
`trace` is the trace as this workflow writes it (trace.Scope). A sub-workflow's ids start with the
attempt that runs it (`trace_parent`). The models that `wf retry` gives (overrides.py) are for the
run's own nodes, not a sub-workflow's.
"""

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import harnesses, overrides, spec
from .harnesses import Harness, Usage
from .harnesses.scripted import Scripted
from .limits import check, since
from .runs import Run
from .scheduler import Topology
from .spec import Workflow
from .steer import Steering
from .trace import Scope, Trace


@dataclass
class RunContext:
    workflow: Workflow
    run: Run
    home: dict  # the workflow's `repo` workspace: {path, branch, base}
    topo: Topology | None = None  # filled in from `workflow`
    parent: "RunContext | None" = None  # set for a sub-workflow
    prefix: tuple[str, ...] = ()  # node ids and item indexes leading to this sub-workflow
    where: str = ""  # the same as a readable path, e.g. "build[1]/" ("" at the top)
    saver: object = None  # the checkpointer, set by the runner for each stream
    script: object = None  # a dryrun.Script during a dry run
    limits: spec.Limits | None = None  # the run's limits, if not just the workflow file's (--limit)
    usage: dict = field(default_factory=lambda: {"cost_usd": 0.0, "tokens": 0})  # spent so far, kept live
    budget_from: dict | None = None  # in a follow-up: the usage and time when it began (limits.since)
    max_agents: int | None = None  # the cap on live agent sessions across runs (capacity.py), if any
    trace_parent: str = "run"  # in the trace, the attempt that runs this sub-workflow
    secret_outputs: frozenset[str] = frozenset()  # its end outputs that feed a secret of the parent
    overrides: dict = field(default_factory=dict)  # models from `wf retry --model`, by node or item (overrides.py)
    _clock_start: float = field(default_factory=time.monotonic)
    _elapsed_before: float = 0.0
    _slots: dict = field(default_factory=dict)
    _harnesses: dict = field(default_factory=dict)  # name -> open Harness
    _opening: dict = field(default_factory=dict)  # name -> asyncio.Lock, so a harness opens once
    _git: asyncio.Lock | None = None  # one git operation at a time (see workspaces.py)
    _workflows: dict = field(default_factory=dict)  # sub-workflow files, parsed once
    _agents_waiting: dict = field(default_factory=dict)  # attempts waiting for an agent slot, for status.json
    _trace_file: Trace | None = None  # the run's trace, on the top-level context
    _trace: Scope | None = None
    _steering: Steering = field(default_factory=Steering)  # the open agent sessions, for wf steer

    def __post_init__(self):
        if self.topo is None:
            self.topo = Topology.of(self.workflow)

    @property
    def root(self) -> "RunContext":
        return self.parent.root if self.parent else self

    @property
    def nested(self) -> bool:
        return self.parent is not None

    @property
    def run_info(self) -> dict:
        """What the state keeps about the run (the `run` template variable, minus usage)."""
        return {"id": self.run.id, "dir": str(self.root.run.dir), "base": self.home["base"],
                "branch": self.home["branch"], "repo": self.home["path"]}

    def child(self, workflow: Workflow, folder: Path, home: dict, path: list[str], label: str, attempt: str,
              secret_outputs: frozenset[str] = frozenset()) -> "RunContext":
        """The context for a sub-workflow run in `folder`, working in `home`, for node `label`,
        by the attempt whose trace id is `attempt`. Its end outputs in `secret_outputs` feed a
        secret here."""
        return RunContext(workflow=workflow, run=Run(self.run.id, folder, self.run.repo), home=home,
                          parent=self, prefix=(*self.prefix, *path), where=f"{self.where}{label}/", saver=self.saver,
                          trace_parent=attempt, secret_outputs=secret_outputs)

    @property
    def trace(self) -> Scope:
        """The run's trace, as this workflow writes it."""
        if self._trace is None:
            root = self.root
            if root._trace_file is None:
                root._trace_file = Trace(root.run.dir)
            self._trace = Scope(root._trace_file, self.workflow, self.load, self.trace_parent, self.where, self.secret_outputs)
        return self._trace

    def override(self, node: str, index: int | None) -> tuple[str, dict] | None:
        """The override for a job of this workflow, as (its target, its settings), or None."""
        return None if self.nested else overrides.find(self.overrides, node, index)

    def load(self, path: Path) -> Workflow:
        """A sub-workflow file, parsed once per process."""
        cache = self.root._workflows
        if path not in cache:
            cache[path] = spec.load(path)
        return cache[path]

    # ---------- time ----------

    def restart_clock(self, elapsed_before: float) -> None:
        """Count running time from now, on top of what earlier processes ran. Time spent
        paused (waiting for `wf retry`, or for answers at the terminal) doesn't count."""
        root = self.root  # the clock is the whole run's (see elapsed())
        root._elapsed_before = elapsed_before
        root._clock_start = time.monotonic()

    def elapsed(self) -> float:
        root = self.root
        return root._elapsed_before + (time.monotonic() - root._clock_start)

    # ---------- limits ----------

    def add_usage(self, usage: Usage) -> None:
        self.root.usage["cost_usd"] += usage.cost_usd
        self.root.usage["tokens"] += usage.tokens

    def limit_check(self) -> tuple[str, str] | None:
        """The run's first limit reached, as (name, in words), or None (always the top-level run's)."""
        root = self.root
        return check(root.limits or root.workflow.limits, *since(root.usage, self.elapsed(), root.budget_from))

    def limit_reached(self) -> str | None:
        """The same, in words only (for messages)."""
        found = self.limit_check()
        return found[1] if found else None

    # ---------- parallel items ----------

    def new_event_loop(self) -> None:
        """Forget asyncio objects from an earlier asyncio.run(): a semaphore that was
        waited on is bound to the loop it was waited on in, and can't be reused."""
        root = self.root  # harnesses, the git lock and the open sessions live on the top-level context
        self._slots.clear()
        root._opening.clear()
        root._harnesses.clear()
        root._git = None
        root._steering = Steering()

    def git_lock(self) -> asyncio.Lock:
        root = self.root
        if root._git is None:
            root._git = asyncio.Lock()
        return root._git

    def slots(self, node: str) -> asyncio.Semaphore:
        """At most max_parallel of a node's items run at the same time."""
        if node not in self._slots:
            self._slots[node] = asyncio.Semaphore(self.workflow.setting(node, "max_parallel"))
        return self._slots[node]

    # ---------- the agent cap ----------

    def note_slot_wait(self, key: str, entry: dict | None) -> None:
        """Note in status.json, for `wf status`, that an attempt waits for an agent slot
        (`entry`: {node, attempt, since}), or with None, that it no longer does."""
        root = self.root  # status.json is the top-level run's
        if entry is None:
            root._agents_waiting.pop(key, None)
        else:
            root._agents_waiting[key] = entry
        waiting = list(root._agents_waiting.values())
        root.run.set_status("running", **({"agents_waiting": waiting} if waiting else {}))

    # ---------- wf steer ----------

    @property
    def steering(self) -> Steering:
        """The run's open agent sessions, which `wf steer` messages reach."""
        return self.root._steering

    # ---------- harnesses ----------

    async def harness(self, name: str) -> Harness:
        """The harness called `name`, opened on first use, shared by the whole run.
        Raises harnesses.Unavailable."""
        root = self.root
        async with root._opening.setdefault(name, asyncio.Lock()):
            if name not in root._harnesses:
                harness = Scripted(name, root.script) if root.script else harnesses.create(name)  # a dry run plays its script
                await harness.open(root.run.dir)
                root._harnesses[name] = harness
        return root._harnesses[name]

    async def close_harnesses(self) -> None:
        """Close every open harness (at the end of each asyncio.run, whose loop they belong to)."""
        root = self.root  # harnesses are opened (and so kept) on the top-level context
        try:
            for harness in root._harnesses.values():
                try:
                    await harness.close()
                except Exception:
                    pass  # closing is best effort
        finally:  # even when cancelled: these objects belong to a loop that's ending
            root._harnesses.clear()

    # ---------- a stream that starts ----------

    def resume(self, values: dict) -> None:
        """Before a stream, set the clock, the usage and a follow-up's start as the saved state has them.
        Then a job that runs again before the first `schedule` step sees the limits as that step does."""
        self.restart_clock(values.get("elapsed_s", 0.0))
        root = self.root
        root.usage = dict(values.get("usage") or {"cost_usd": 0.0, "tokens": 0})
        root.budget_from = (values.get("follow_up") or {}).get("from")
