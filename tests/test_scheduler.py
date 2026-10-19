"""scheduler: the rules in "When a node runs", one table per rule.

Graphs are written as YAML node maps; every node is a plain command, since
the rules only look at `needs` and `routes`.
"""

from textwrap import dedent

import pytest
import yaml

from workflow import scheduler
from workflow.scheduler import Topology
from workflow.spec import Workflow


def graph(text: str) -> Topology:
    nodes = yaml.safe_load(dedent(text))
    for node in nodes.values():
        node.setdefault("command", "x")
    return Topology.of(Workflow.model_validate({"version": 1, "name": "t", "start": {}, "nodes": nodes, "end": {}}))


LOOP = graph("""
    explore: {needs: [start]}
    implement: {needs: [explore]}
    tests: {needs: [implement]}
    review:
      needs: [tests]
      routes: [{when: "ok", to: end}, {to: implement, max: 3}, {to: end}]
    open_pr: {needs: [implement]}
""")

BRANCHES = graph("""
    classify: {needs: [start], routes: [{when: "bug", to: bugfix}, {to: feature}]}
    bugfix: {}
    feature: {}
    open_pr: {needs: [bugfix, feature]}
""")

DIAMOND = graph("""
    a: {needs: [start]}
    b: {needs: [start]}
    join: {needs: [a, b]}
""")


# ---------- what becomes pending when a node finishes ----------


@pytest.mark.parametrize(
    "topo, node, route, expected",
    [
        (DIAMOND, "start", None, ["a", "b"]),  # start finishes at once; its dependents become pending
        (DIAMOND, "a", None, ["join"]),  # no routes: every node that needs it
        (BRANCHES, "classify", 0, ["bugfix"]),  # routes: only the chosen route's targets
        (BRANCHES, "classify", 1, ["feature"]),
        (LOOP, "review", 1, ["implement"]),
        (LOOP, "review", 0, []),  # to: end ends this path
        (LOOP, "implement", None, ["tests", "open_pr"]),
    ],
)
def test_triggered(topo, node, route, expected):
    assert scheduler.triggered(topo, node, route) == expected


def test_a_route_to_a_list_makes_every_target_pending():
    topo = graph("""
        fan: {needs: [start], routes: [{to: [b, a]}]}
        a: {}
        b: {}
    """)
    assert scheduler.triggered(topo, "fan", 0) == ["b", "a"]


def test_pending_keeps_order_and_never_holds_a_node_twice():
    assert scheduler.add_pending(LOOP, ["open_pr"], ["tests", "open_pr"]) == ["open_pr", "tests"]
    assert scheduler.add_pending(LOOP, [], ["open_pr", "tests"]) == ["tests", "open_pr"]  # same moment: file order


# ---------- choosing a route ----------


def routes(text: str):
    return graph(f"n: {{needs: [start], routes: {text}}}").routes["n"]


@pytest.mark.parametrize(
    "rules, taken, true, expected",
    [
        ('[{when: a, to: end}, {to: end}]', {}, {"a"}, 0),  # first true `when` wins
        ('[{when: a, to: end}, {to: end}]', {}, set(), 1),  # no `when`: always true
        ('[{when: a, to: end}, {when: b, to: end}, {to: end}]', {}, {"b"}, 1),
        ('[{to: n, max: 2}, {to: end}]', {0: 1}, set(), 0),  # max not used up yet
        ('[{to: n, max: 2}, {to: end}]', {0: 2}, set(), 1),  # used up: skipped
        ('[{when: a, to: n, max: 1}, {to: end}]', {0: 1}, {"a"}, 1),  # true but used up
    ],
)
def test_choose_route(rules, taken, true, expected):
    assert scheduler.choose_route(routes(rules), taken, lambda when: when in true) == expected


def test_choose_route_stops_evaluating_at_the_first_match():
    asked = []
    scheduler.choose_route(routes('[{when: a, to: end}, {when: b, to: end}, {to: end}]'), {}, lambda w: asked.append(w) or True)
    assert asked == ["a"]


def test_a_used_up_route_is_not_evaluated():
    asked = []
    scheduler.choose_route(routes('[{when: a, to: n, max: 1}, {to: end}]'), {0: 1}, lambda w: asked.append(w) or True)
    assert asked == []


# ---------- settled needs and which pending nodes start ----------


@pytest.mark.parametrize(
    "name, topo, pending, running, expected",
    [
        ("first nodes start", DIAMOND, ["a", "b"], [], ["a", "b"]),
        ("a join waits for a running branch", DIAMOND, ["join"], ["b"], []),
        ("a join waits for a pending branch", DIAMOND, ["join", "b"], [], ["b"]),
        ("a join runs once both are done", DIAMOND, ["join"], [], ["join"]),
        ("a skipped branch doesn't block a join", BRANCHES, ["open_pr"], [], ["open_pr"]),
        ("a join waits while a loop can come back", LOOP, ["tests", "open_pr"], [], ["tests"]),
        ("... still waiting during review", LOOP, ["open_pr", "review"], [], ["review"]),
        ("... and while the loop reruns implement", LOOP, ["open_pr", "implement"], [], ["implement"]),
        ("... and runs once the loop ends", LOOP, ["open_pr"], [], ["open_pr"]),
        ("a running node doesn't start again", LOOP, ["implement"], ["implement"], []),
        ("nothing pending, nothing starts", LOOP, [], ["tests"], []),
    ],
)
def test_starts(name, topo, pending, running, expected):
    assert scheduler.starts(topo, pending, running) == expected, name


def test_paths_through_the_waiting_node_are_ignored():
    topo = graph("""
        x: {needs: [start]}
        n: {needs: [x], routes: [{when: again, to: x, max: 1}, {to: end}]}
        p: {needs: [start], routes: [{to: n}]}
        other: {needs: [x]}
    """)
    # p can reach x, but only through n: for n that doesn't count, for `other` it does
    assert scheduler.settled(topo, "x", waiting="n", pending=["n", "p"], running=[])
    assert not scheduler.settled(topo, "x", waiting="other", pending=["other", "p"], running=[])


DEADLOCK = graph("""
    p: {needs: [start]}
    q: {needs: [start]}
    a: {needs: [p], routes: [{when: again, to: q, max: 1}, {to: end}]}
    b: {needs: [q], routes: [{when: again, to: p, max: 1}, {to: end}]}
""")


@pytest.mark.parametrize(
    "pending, running, expected",
    [
        (["b", "a"], [], ["b"]),  # all waiting on each other: the one pending first starts
        (["a", "b"], [], ["a"]),
        (["a", "b"], ["p"], []),  # something is still running: wait for it instead
    ],
)
def test_deadlock_tie_break(pending, running, expected):
    assert scheduler.starts(DEADLOCK, pending, running) == expected
