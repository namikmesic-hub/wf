"""Diagram: a workflow as a Mermaid flowchart, for `wf graph`, and as rows of boxes, for `wf ui`.

Solid arrows are `needs` edges. Dotted arrows are routes, labelled with their `when` and `max`,
and the way in of a follow-up request (shape.follow_up_targets). Shapes show the kind of node:

    ("agent")   [/"command"/]   {{"human"}}   [["workflow"]]

Mermaid ids get a prefix (n_<id>, wf_start, wf_end) because some words, such
as `end`, are Mermaid keywords.

layout() puts each node in the row of its step (shape.py): start in row
0, end in the row after the last step. In a row, a node goes near the mean
place of the nodes before it, and file order breaks ties. The page draws the
boxes and the edges from that.
"""

from .shape import Shape, follow_up_targets
from .scheduler import END, START
from .spec import Workflow

_SHAPES = {"agent": ('("', '")'), "command": ('[/"', '"/]'), "human": ('{{"', '"}}'), "workflow": ('[["', '"]]')}


def mermaid(workflow: Workflow) -> str:
    lines = []
    if workflow.description:  # Mermaid's front matter is YAML, so the title is a quoted YAML string
        title = _escape(f"{workflow.name}: {workflow.description}").replace("\\", "\\\\")
        lines += ["---", f'title: "{title}"', "---"]
    lines += ["flowchart TD", '  wf_start(["start"])']
    for node_id, node in workflow.nodes.items():
        opening, closing = _SHAPES[node.kind]
        lines.append(f"  {_id(node_id)}{opening}{_label(workflow, node_id)}{closing}")
    lines.append('  wf_end(["end"])')
    if targets := follow_up_targets(workflow):
        lines.append('  wf_follow_up(["follow-up"])')
        lines += [f"  wf_follow_up -.-> {_id(target)}" for target in targets]
    for node_id, node in workflow.nodes.items():
        for need in node.needs:
            lines.append(f"  {_id(need)} --> {_id(node_id)}")
        for route in node.routes:
            text = " · ".join(([route.when] if route.when else []) + ([f"max {route.max}"] if route.max else []))
            arrow = f'-. "{_escape(text)}" .->' if text else "-.->"
            lines += [f"  {_id(node_id)} {arrow} {_id(target)}" for target in route.targets]
    return "\n".join(lines) + "\n"


def _id(node_id: str) -> str:
    return {"start": "wf_start", "end": "wf_end"}.get(node_id, f"n_{node_id}")


def _label(workflow: Workflow, node_id: str) -> str:
    node = workflow.nodes[node_id]
    lines = [node_id, " · ".join(_details(workflow, node_id))] + ([node.description] if node.description else [])
    return _escape("<br/>".join(lines))


def _details(workflow: Workflow, node_id: str) -> list[str]:
    """agent · codex · edit · workspace: new · for each plan.tasks"""
    node = workflow.nodes[node_id]
    details = [node.kind]
    if node.kind == "agent":
        if node.harness is not None:
            details.append(node.harness if isinstance(node.harness, str) else " → ".join(node.harness))
        if workflow.setting(node_id, "access") != "read-only":
            details.append(workflow.setting(node_id, "access"))
    if node.kind == "workflow":
        details.append(node.workflow)
    if workflow.setting(node_id, "workspace") != "repo":
        details.append(f"workspace: {workflow.setting(node_id, 'workspace')}")
    if node.for_each is not None:
        details.append(f"for each {node.for_each}")
    return details


def _escape(text: str) -> str:
    """Make free text safe inside a quoted Mermaid label: quotes become #quot;, line breaks <br/>."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").strip("\n")
    return lines.replace('"', "#quot;").replace("\n", "<br/>")


def layout(workflow: Workflow) -> dict:
    """The workflow as rows of boxes: {"rows", "nodes": [{id, kind, row, place, details, ...}], "edges"}.
    `place` is the box's place in its row, 0 in the middle. An edge's kind is needs, route or loop."""
    shape = Shape(workflow)
    rows: dict[int, list[str]] = {}
    for node in shape.sorted:
        rows.setdefault(shape.step[node], []).append(node)
    before: dict[str, list[str]] = {}
    for source, targets in shape.forward.items():
        for target in targets:
            before.setdefault(target, []).append(source)
    place, position = {START: 0.0}, {node: i for i, node in enumerate(shape.order)}
    for row in sorted(rows):
        nodes = sorted(rows[row], key=position.__getitem__)
        mean = {n: sum(place.get(b, 0.0) for b in before.get(n, [])) / max(len(before.get(n, [])), 1) for n in nodes}
        nodes.sort(key=mean.__getitem__)  # a stable sort: file order breaks ties
        for i, node in enumerate(nodes):
            place[node] = i - (len(nodes) - 1) / 2
    boxes = [{"id": START, "kind": "start", "row": 0, "place": 0.0, "details": []}]
    for node_id, node in workflow.nodes.items():
        boxes.append({"id": node_id, "kind": node.kind, "row": shape.step[node_id], "place": place[node_id],
                      "details": _details(workflow, node_id), "description": node.description or ""})
    boxes.append({"id": END, "kind": "end", "row": shape.steps + 1, "place": 0.0, "details": []})
    edges = []
    for source, target, kind, index in shape.edges:
        route = workflow.nodes[source].routes[index] if index is not None and source != START else None
        label = " · ".join(([route.when] if route and route.when else []) + ([f"max {route.max}"] if route and route.max else []))
        edges.append({"from": source, "to": target, "kind": kind, "route": index, "label": label})
    leads = {source for source, *_ in shape.edges}
    edges += [{"from": node, "to": END, "kind": "needs", "route": None, "label": ""} for node in shape.order if node not in leads]
    return {"rows": shape.steps + 2, "nodes": boxes, "edges": edges}
