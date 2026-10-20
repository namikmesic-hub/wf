"""Scheduler: the rules in the spec's "When a node runs", as pure functions.

Nothing here touches LangGraph, files or agents. state.Progress calls these
from the graph's `schedule` node; validate.py uses Topology to reason about
the graph before anything runs.

Topology is the graph: each node's `needs` and `routes`, in file order.
Two kinds of edges lead from a node to the next ones:
- needs edges: when `a` finishes, every node with `a` in its needs becomes
  pending (unless `a` has routes; then only its chosen route counts).
- route edges: when `a` finishes, the targets of its chosen route become pending.

The rules, in the order a run uses them:
- triggered(): what becomes pending when a node finishes
- choose_route(): which route a node with routes takes (`when`, `max`)
- add_pending(): pending nodes keep the order they became pending in
- settled() and starts(): which pending nodes can start now, including the
  tie-break when they're all waiting on each other

follow_up_targets() uses them to say where `wf continue` sends a follow-up
request in a workflow without `follow_up`: to the agent node with `edit` or
`full` access that a run starts first (first_starts). That follow-up keeps the
outputs of the nodes that start before that node (started_before).
"""

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import cached_property

from .spec import Route, Workflow

START, END = "start", "end"


@dataclass(frozen=True)
class Topology:
    order: tuple[str, ...]  # node ids in file order (not start or end)
    needs: Mapping[str, tuple[str, ...]]  # node -> the nodes it needs (may include start)
    routes: Mapping[str, tuple[Route, ...]]  # node -> its routes, in order

    @classmethod
    def of(cls, workflow: Workflow) -> "Topology":
        return cls(
            order=tuple(workflow.nodes),
            needs={n: tuple(node.needs) for n, node in workflow.nodes.items()},
            routes={n: tuple(node.routes) for n, node in workflow.nodes.items()},
        )

    @cached_property
    def dependents(self) -> dict[str, tuple[str, ...]]:
        """node (or start) -> the nodes that list it in `needs`, in file order."""
        found: dict[str, list[str]] = {n: [] for n in (START, *self.order)}
        for node in self.order:
            for need in self.needs[node]:
                found.setdefault(need, []).append(node)
        return {n: tuple(v) for n, v in found.items()}

    @cached_property
    def successors(self) -> dict[str, tuple[str, ...]]:
        """node (or start) -> every node one needs edge or route edge away, ignoring conditions."""
        found: dict[str, dict[str, None]] = {}
        for node in (START, *self.order):
            targets = dict.fromkeys(self.dependents.get(node, ()))
            for route in self.routes.get(node, ()):
                targets.update(dict.fromkeys(t for t in route.targets if t != END))
            found[node] = targets
        return {n: tuple(v) for n, v in found.items()}

    def reachable(self, sources: Iterable[str], avoiding: str | None = None) -> set[str]:
        """Every node some path from `sources` leads to (sources included), never entering `avoiding`."""
        seen = {s for s in sources if s != avoiding}
        todo = list(seen)
        while todo:
            for nxt in self.successors.get(todo.pop(), ()):
                if nxt not in seen and nxt != avoiding:
                    seen.add(nxt)
                    todo.append(nxt)
        return seen


# ---------- the rules ----------


def triggered(topo: Topology, node: str, route: int | None) -> list[str]:
    """The nodes that become pending when `node` finishes, having taken route number `route`.

    A node with routes triggers only its chosen route's targets (`end` triggers
    nothing: it ends this path, not the run). Any other node triggers every
    node that lists it in `needs`.
    """
    if topo.routes.get(node):
        if route is None:
            raise ValueError(f"`{node}` has routes: say which one was taken")
        return [t for t in topo.routes[node][route].targets if t != END]
    return list(topo.dependents.get(node, ()))


def choose_route(routes: Sequence[Route], taken: Mapping[int, int], is_true: Callable[[str], bool]) -> int:
    """The index of the route to take: the first whose `max` isn't used up and whose `when`
    is true (or missing). `taken` counts how often each route was taken in this run;
    `is_true` evaluates a `when`, and is only called for routes that are still possible."""
    for index, route in enumerate(routes):
        if used_up(route, taken.get(index, 0)):
            continue
        if route.when is None or is_true(route.when):
            return index
    raise ValueError("no route can be taken (validate makes sure the last route has no `when` and no `max`)")


def used_up(route: Route, taken: int) -> bool:
    """Has a route been taken as often as its `max` allows?"""
    return route.max is not None and taken >= route.max


def add_pending(topo: Topology, pending: Sequence[str], nodes: Iterable[str]) -> list[str]:
    """`pending` plus `nodes`, skipping nodes already pending. Order means "became
    pending first"; nodes that become pending at the same moment go in file order."""
    position = {node: i for i, node in enumerate(topo.order)}
    new = sorted(dict.fromkeys(n for n in nodes if n not in pending), key=position.__getitem__)
    return [*pending, *new]


def settled(topo: Topology, need: str, *, waiting: str, pending: Sequence[str], running: Iterable[str]) -> bool:
    """Is `need` settled for the pending node `waiting`?

    Settled: it isn't pending or running, and nothing else that is pending or
    running could still lead to it, along needs and route edges, ignoring
    conditions and leaving out paths through `waiting` itself.
    """
    active = set(pending) | set(running)
    if need in active:
        return False
    return need not in topo.reachable(active - {waiting}, avoiding=waiting)


def starts(topo: Topology, pending: Sequence[str], running: Iterable[str]) -> list[str]:
    """The pending nodes that start now, in pending order: those whose needs are all settled.

    If none can start and nothing is running, the pending nodes are all waiting on
    each other (only possible with routes that go backwards): the one that became
    pending first starts.
    """
    running = set(running)
    ready = [
        node for node in pending
        if node not in running
        and all(settled(topo, need, waiting=node, pending=pending, running=running) for need in topo.needs[node])
    ]
    if ready or running or not pending:
        return ready
    return [pending[0]]


# ---------- follow-ups ----------


def follow_up_targets(workflow: Workflow, topo: Topology | None = None) -> tuple[str, ...]:
    """The nodes where `wf continue` sends a follow-up request: those that `follow_up` names, or else the agent
    node with `edit` or `full` access that a run starts first (first_starts). Of two such nodes that start in the
    same step, the first in the file takes it. Empty when the workflow has neither."""
    if workflow.follow_up is not None:
        return workflow.follow_up.targets
    if START in workflow.nodes or END in workflow.nodes:
        return ()  # validate refuses a node with such a name
    editors = [node_id for node_id, node in workflow.nodes.items()
               if node.kind == "agent" and workflow.setting(node_id, "access") in ("edit", "full")]
    step = first_starts(topo or Topology.of(workflow)) if editors else {}
    started = [node_id for node_id in editors if node_id in step]  # start leads to it
    return (min(started, key=step.__getitem__),) if started else ()


def first_starts(topo: Topology) -> dict[str, int]:
    """The step in which a run first starts each node that start leads to, by the rules of starts() and
    add_pending(). Each job finishes in the step that starts it, and each route leads to all of its targets.
    Each node starts once, so the steps end for each graph, and they keep no stack."""
    found: dict[str, int] = {}
    pending, step = add_pending(topo, [], topo.successors[START]), 0
    while pending:
        step += 1
        started = starts(topo, pending, ())
        pending = [node for node in pending if node not in started]
        for node in started:
            found[node] = step
        for node in started:  # a route to a node that doesn't exist, in a file that validate refuses, leads nowhere
            new = [n for n in topo.successors[node] if n in topo.needs and n not in found]
            pending = add_pending(topo, pending, new)
    return found


def started_before(topo: Topology, node: str) -> set[str]:
    """The nodes that the run of first_starts() starts in an earlier step than `node`. That run finishes each
    of them before `node` starts, so on its first pass `node` finds their outputs."""
    step = first_starts(topo)
    return {other for other, at in step.items() if node in step and at < step[node]}
