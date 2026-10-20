"""Scheduler: the rules in the spec's "When a node runs", as pure functions.

Nothing here touches LangGraph, files or agents. state.Progress calls these
from the graph's `schedule` node; validate.py uses Topology to reason about
the graph before anything runs. Order says which nodes are certain to have
finished at a point of a run, for validate.py and for a follow-up.

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
`full` access that a run starts first (first_starts).
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


# ---------- what a run is certain to have done ----------


class Order:
    """Which nodes are certain to have finished at a given point of a run. validate.py warns about a template
    that reads another node without `{% if %}`, and a follow-up without `follow_up` keeps the outputs of the
    nodes certain to have finished before its node (state.Progress.follow_up)."""

    def __init__(self, topo: Topology):
        self.topo = topo
        self.route_sources: dict[str, list[str]] = {n: [] for n in topo.order}
        for source in topo.order:
            for route in topo.routes[source]:
                for target in route.targets:
                    if target != END and source not in self.route_sources[target]:
                        self.route_sources[target].append(source)
        # triggers: the nodes whose finishing can make a node pending
        triggers = {n: set(topo.needs[n]) | set(self.route_sources[n]) for n in topo.order}
        triggers[END] = {n for n in topo.order
                         if any(END in r.targets for r in topo.routes[n]) or not (topo.routes[n] or topo.dependents.get(n))}
        self.dominators = dominators([START, *topo.order, END], triggers, topo.reachable([START]) | {END})
        self.predecessors: dict[str, set[str]] = {n: set() for n in (START, *topo.order)}
        for node, successors in topo.successors.items():
            for nxt in successors:
                self.predecessors[nxt].add(node)
        self._before: dict[str, set[str]] = {}

    def before(self, node: str) -> set[str]:
        """Nodes certain to have finished before `node` starts."""
        if node not in self._before:
            known = self.dominators[node] - {node}
            self._before[node] = known | (self._ancestors(node) & self._certain(known, excluding=node))
        return self._before[node]

    def at_end(self) -> set[str]:
        """Nodes certain to have run by the time end.output is computed."""
        return self._certain(self.dominators[END] - {END}, excluding=None)

    def _ancestors(self, node: str) -> set[str]:
        seen, todo = set(), [node]
        while todo:
            for prev in self.predecessors.get(todo.pop(), ()):
                if prev not in seen and prev != node:
                    seen.add(prev)
                    todo.append(prev)
        return seen

    def _certain(self, known: set[str], excluding: str | None) -> set[str]:
        """`known` plus every node they're certain to make pending (a needs edge, or all of a node's routes)."""
        done, changed = set(known), True
        while changed:
            changed = False
            for node in self.topo.order:
                if node in done or node == excluding:
                    continue
                by_needs = any(need in done for need in self.topo.needs[node])
                by_routes = any(s in done and all(node in r.targets for r in self.topo.routes[s]) for s in self.route_sources[node])
                if by_needs or by_routes:
                    done.add(node)
                    changed = True
        return done


def dominators(nodes: list[str], triggers: dict[str, set[str]], reached: set[str]) -> dict[str, set[str]]:
    """Classic iterative dominators: d dominates n if every path from start to n passes through d."""
    everything = set(nodes)
    dominators = {n: set(everything) for n in nodes}
    dominators[START] = {START}
    changed = True
    while changed:
        changed = False
        for node in nodes[1:]:
            preds = [p for p in triggers.get(node, ()) if p in reached]
            new = {node} | (set.intersection(*(dominators[p] for p in preds)) if preds else set())
            if new != dominators[node]:
                dominators[node], changed = new, True
    return dominators


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
