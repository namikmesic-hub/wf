"""Shape: a workflow's graph without its loops, as steps from start to end.

progress.py uses it to say how far a run is, and diagram.layout() to put the
boxes of `wf ui` in rows. Nothing here reads a run.

follow_up_targets() says where `wf continue` sends a follow-up request. A
workflow without `follow_up` sends it to the agent node with `edit` or `full`
access at the earliest step. Of two such nodes at the same step, the first in
the file takes it.

A walk from start, depth first and in file order, finds the route edges that
go back. Each of them closes a loop. Without them the graph has no cycles. A
node's step is the number of nodes on the longest path from start to it. The
straight path is the longest path from start to end, and its length is the
number of steps.

A loop repeats the nodes on the paths from the node it goes back to (`first`)
to the node whose route goes back (`last`). It can go back as many times as
the `max` of that route. When that route has no `max`, the smallest `max` of
another route inside the loop bounds it: validate puts a `max` on each loop.
"""

from dataclasses import dataclass

from .scheduler import END, START, Topology
from .spec import Workflow


@dataclass(frozen=True)
class Loop:
    first: str  # the step it goes back to
    last: str  # the step whose edge goes back
    route: int | None  # the index of that route in `last` (None: a needs edge goes back)
    max: int | None  # how many times it can go back
    body: frozenset[str]  # the steps it repeats


class Shape:
    def __init__(self, workflow: Workflow, topo: Topology | None = None):
        self.topo = topo = topo or Topology.of(workflow)
        self.order = topo.order
        self.forward: dict[str, list[str]] = {node: [] for node in (START, *topo.order)}
        self.edges: list[tuple[str, str, str, int | None]] = []  # (from, to, needs | route | loop, route index)
        back: list[tuple[str, int | None, str]] = []
        state: dict[str, int] = {}  # 1 while the walk is inside the node, 2 once it left it
        finished: list[str] = []

        def visit(node: str) -> None:
            state[node] = 1
            for index, target in _out(topo, node):
                if target == END:
                    self.edges.append((node, END, "route", index))
                elif state.get(target) == 1:
                    back.append((node, index, target))
                    self.edges.append((node, target, "loop", index))
                else:
                    self.edges.append((node, target, "needs" if index is None else "route", index))
                    if target not in self.forward[node]:
                        self.forward[node].append(target)
                    if target not in state:
                        visit(target)
            state[node] = 2
            finished.append(node)

        for node in (START, *topo.order):  # validate makes each node reachable from start: the others guard a damaged copy
            if node not in state:
                visit(node)
        self.sorted = [node for node in reversed(finished) if node != START]
        self.loops = [self._loop(*edge) for edge in back]
        unit = {node: 1.0 for node in self.order}
        self.step = {node: int(depth) for node, depth in self.depth(unit).items()}
        self.steps = max(self.step.values(), default=0)
        self.steps_after = {node: int(tail) for node, tail in self.tail(unit).items()}

    def depth(self, weight: dict[str, float]) -> dict[str, float]:
        """The weight of the heaviest path from start to each node, the node included."""
        found: dict[str, float] = {START: 0.0}
        for node in [START, *self.sorted]:
            if node not in found:  # not reached from start: only in a damaged copy
                found[node] = weight[node]
            for target in self.forward[node]:
                found[target] = max(found.get(target, 0.0), found[node] + weight[target])
        del found[START]
        return found

    def tail(self, weight: dict[str, float]) -> dict[str, float]:
        """The weight of the heaviest path from each node to end, the node left out."""
        found: dict[str, float] = {}
        for node in reversed(self.sorted):
            found[node] = max((weight[t] + found[t] for t in self.forward[node]), default=0.0)
        return found

    def span(self, loop: Loop, weight: dict[str, float]) -> float:
        """The weight of the heaviest path that a lap of the loop takes, from `first` to `last`."""
        found = {loop.first: weight[loop.first]}
        for node in self.sorted:
            if node in found:
                for target in (t for t in self.forward[node] if t in loop.body):
                    found[target] = max(found.get(target, 0.0), found[node] + weight[target])
        return found.get(loop.last, weight[loop.first])

    def after(self, node: str) -> set[str]:
        """The node, and each node that a path without loops leads to from it."""
        seen, todo = {node}, [node]
        while todo:
            for target in self.forward[todo.pop()]:
                if target not in seen:
                    seen.add(target)
                    todo.append(target)
        return seen

    def _loop(self, last: str, index: int | None, first: str) -> Loop:
        body = frozenset(n for n in self.after(first) if last in self.after(n))
        most = self.topo.routes[last][index].max if index is not None else None
        if most is None:
            most = min((route.max for node in self.order if node in body for route in self.topo.routes.get(node, ())
                        if route.max and set(route.targets) & body), default=None)
        return Loop(first, last, index, most, body)


def follow_up_targets(workflow: Workflow, topo: Topology | None = None) -> tuple[str, ...]:
    """The nodes where `wf continue` sends a follow-up request: those that `follow_up` names, or else the agent
    node with `edit` or `full` access at the earliest step. Empty when the workflow has neither."""
    if workflow.follow_up is not None:
        return workflow.follow_up.targets
    editors = [node_id for node_id, node in workflow.nodes.items()
               if node.kind == "agent" and workflow.setting(node_id, "access") in ("edit", "full")]
    if not editors:
        return ()
    step = Shape(workflow, topo).step
    return (min(editors, key=step.__getitem__),)  # the first in the file of those at the earliest step


def _out(topo: Topology, node: str) -> list[tuple[int | None, str]]:
    """The edges from a node, in file order: its needs edges (index None), then its routes. A route to a node
    that doesn't exist, in a file that validate refuses, leads nowhere."""
    edges = [(None, target) for target in topo.dependents.get(node, ())]
    return edges + [(i, target) for i, route in enumerate(topo.routes.get(node, ())) for target in route.targets
                    if target == END or target in topo.needs]
