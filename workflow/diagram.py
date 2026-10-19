"""Diagram: a workflow as a Mermaid flowchart, for `wf graph`.

Solid arrows are `needs` edges; dotted arrows are routes, labelled with their
`when` and `max`. Shapes show the kind of node:

    ("agent")   [/"command"/]   {{"human"}}   [["workflow"]]

Mermaid ids get a prefix (n_<id>, wf_start, wf_end) because some words, such
as `end`, are Mermaid keywords.
"""

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
    lines = [node_id, " · ".join(details)] + ([node.description] if node.description else [])
    return _escape("<br/>".join(lines))


def _escape(text: str) -> str:
    """Make free text safe inside a quoted Mermaid label: quotes become #quot;, line breaks <br/>."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").strip("\n")
    return lines.replace('"', "#quot;").replace("\n", "<br/>")
