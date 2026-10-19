"""Overrides: another model for a node, or for one item of a for_each node, for the rest of a run.

A run keeps the copies of the workflow files that it started with (runs.py).
So when a model fails again and again (an outage, a used-up quota), each retry
of the run fails in the same way. These options of `wf retry` give a node, or
one item of a for_each node, another model, and the harness and effort with it:

    wf retry <run-id> --model plan=claude-sonnet-5-5
    wf retry <run-id> --harness 'review[2]=claude-code' --model 'review[2]=claude-opus-5-5'
    wf retry <run-id> --model 'review[2]=gemini-4-argon-high'

A target is an agent node of the run's own workflow, for all of its items, or
one item, `node[index]`. An item's override wins over the override of its node.
An override runs its target on one harness only: the harness that --harness
names, else the node's own harness. Without --harness, the node must name one
harness, not a list or a template. A setting that the override leaves out comes
from the workflow file, for that harness. Finished nodes and items don't run
again: the override applies to the attempts that are still to come.

check() runs before anything changes, as operate.check_answers does for answers.
It refuses a target that isn't an agent node of the run's workflow, or an item
that the node doesn't have. It also refuses a harness that the node may not use.
Then available() opens each harness as a run does. So it also refuses a harness
that isn't installed or signed in, and a model that Cursor doesn't list. Only
then does the runner keep the overrides in run.json (runs.Run.set_overrides). A later
override for the same target replaces the earlier one. Each override goes into
events.jsonl as an `override` event, with the last error of its target:

    {"event": "override", "node": "review", "index": 2, "harness": "claude-code",
     "model": "claude-opus-5-5", "effort": null, "last_error": "cursor is unavailable: ..."}

Each agent attempt records an `agent_model` event with the harness, model and
effort that it got (nodes._agent). The event also names the target of the
override, if the attempt has one. So the trace shows which model ran each
attempt, and why.
"""

import re
from pathlib import Path

from . import harnesses
from .spec import HARNESSES, Workflow

SETTINGS = ("harness", "model", "effort")
TARGET = re.compile(r"(?P<node>[a-z][a-z0-9_]*)(?:\[(?P<index>\d+)\])?")  # a node id (spec.NODE_ID), and an index


class OverrideError(Exception):
    pass


def parse(pairs: dict[str, list[str]]) -> dict[str, dict[str, str]]:
    """{"model": ["review[2]=x"], "harness": [...]} -> {"review[2]": {"model": "x"}}. Checks only the form."""
    found: dict[str, dict[str, str]] = {}
    for setting in SETTINGS:
        for pair in pairs.get(setting) or []:
            target, equals, value = (part.strip() for part in pair.partition("="))
            if not equals or not target or not value:
                raise OverrideError(f"--{setting}: expected NODE=VALUE or NODE[INDEX]=VALUE, got `{pair}`")
            if "/" in target:
                raise OverrideError(f"--{setting}: `{target}` is a step in a sub-workflow, and an override can only "
                                    "name a node of the run's own workflow, or one of its items")
            match = TARGET.fullmatch(target)
            if match is None:
                raise OverrideError(f"--{setting}: `{target}` isn't a node or an item: write NODE or NODE[INDEX], like review[2]")
            key = target_key(match["node"], None if match["index"] is None else int(match["index"]))
            if setting in found.setdefault(key, {}):
                raise OverrideError(f"--{setting} names {key} twice")
            found[key][setting] = value
    return found


def target_key(node: str, index: int | None) -> str:
    """review, or review[2] for one item."""
    return node if index is None else f"{node}[{index}]"


def split(key: str) -> tuple[str, int | None]:
    """review[2] -> ("review", 2)."""
    match = TARGET.fullmatch(key)
    return match["node"], None if match["index"] is None else int(match["index"])


def find(table: dict[str, dict], node: str, index: int | None) -> tuple[str, dict] | None:
    """The override for a job, as (its target, its settings): its item's, else its node's, else None."""
    for key in ([target_key(node, index)] if index is not None else []) + [node]:
        if key in table:
            return key, table[key]
    return None


# ---------- checks ----------


def check(given: dict[str, dict[str, str]], workflows: list[Workflow], values: dict) -> tuple[dict[str, dict], list[str]]:
    """The overrides, each with its harness filled in, and what is wrong with them.
    Here workflows holds the run's copies, its own workflow first, and values holds its state (state.RunState)."""
    workflow = workflows[0]
    found, problems = {}, []
    for key, settings in given.items():
        node_id, index = split(key)
        problem = _target(key, node_id, index, workflows, values)
        harness = None
        if problem is None:
            harness, problem = _harness(key, node_id, settings, workflow)
        if problem is not None:
            problems.append(problem)
            continue
        found[key] = {"harness": harness, "model": settings.get("model"), "effort": settings.get("effort")}
    return found, problems


def _target(key: str, node_id: str, index: int | None, workflows: list[Workflow], values: dict) -> str | None:
    workflow = workflows[0]
    node = workflow.nodes.get(node_id)
    if node is None:
        inside = next((w.name for w in workflows[1:] if node_id in w.nodes), None)
        if inside:
            return (f"{key}: `{node_id}` is a step of the sub-workflow {inside}, and an override can only name "
                    f"a node of {workflow.name}, or one of its items")
        agents = ", ".join(n for n, spec in workflow.nodes.items() if spec.kind == "agent") or "none"
        return f"{key}: {workflow.name} has no node `{node_id}` (its agent nodes: {agents})"
    if node.kind != "agent":
        return f"{key}: `{node_id}` is a {node.kind} node, and only an agent node has a model"
    if index is None:
        return None
    if node.for_each is None:
        return f"{key}: `{node_id}` isn't a for_each node, so it has no items: name the node alone"
    count = _items(values, node_id)
    if count is None:
        return f"{key}: `{node_id}` hasn't started yet, so it has no items: name the node to change all of them"
    if index >= count:
        return f"{key}: `{node_id}` has " + (f"only items 0 to {count - 1}" if count else "no items")
    return None


def _items(values: dict, node: str) -> int | None:
    """How many items the node has: in its pass in progress, else in its last pass, else None."""
    active = (values.get("active") or {}).get(node)
    if active is not None and active.get("items") is not None:
        return len(active["items"])
    done = (values.get("outputs") or {}).get(node)  # the items that worked, and with on_item_error: continue, the rest
    return len(done) + len((values.get("errors") or {}).get(node) or []) if isinstance(done, list) else None


def _harness(key: str, node_id: str, settings: dict[str, str], workflow: Workflow) -> tuple[str | None, str | None]:
    """(the harness the override runs its target on, None), or (None, why it can't)."""
    own = workflow.setting(node_id, "harness")
    harness = settings.get("harness")
    if harness is None:
        if isinstance(own, list) or "{{" in own or "{%" in own:
            how = "from a list" if isinstance(own, list) else "with a template"
            return None, f"{key}: `{node_id}` picks its harness {how}, so name one: --harness {key}=NAME"
        harness = own
    if harness not in HARNESSES:
        return None, f"{key}: unknown harness `{harness}` (choose from {', '.join(HARNESSES)})"
    if harness == "cursor":
        if "effort" in settings:
            return None, f"{key}: cursor has no effort setting: a Cursor model id carries its own, like gpt-5.6-sol-max-fast"
        if settings.get("model") is None and workflow.harness_setting(node_id, "cursor", "model") is None:
            return None, f"{key}: cursor needs a model, and the workflow gives `{node_id}` none for it: add --model {key}=ID"
    access, node = workflow.setting(node_id, "access"), workflow.nodes[node_id]
    if workflow.setting(node_id, "tools") and harnesses.reaches_keychain(harness, access) and not node.tools_with_shell:
        return None, (f"{key}: `{node_id}` has tools, and on {harness} at `{access}` access its commands could read "
                      "every stored sign-in, but the node doesn't set `tools_with_shell: true`")
    return harness, None


async def available(found: dict[str, dict], run_dir: Path) -> list[str]:
    """Open each harness that the overrides name, as a run does, and ask it about each model they give.
    Returns what can't run on this machine: a harness that isn't installed or signed in, or a model it doesn't have."""
    problems = []
    by_harness: dict[str, list[str]] = {}
    for key, override in found.items():
        by_harness.setdefault(override["harness"], []).append(key)
    for name, keys in by_harness.items():
        harness = None
        try:
            harness = harnesses.create(name)
            await harness.open(run_dir)
            for key in keys:
                if (model := found[key]["model"]) is not None:
                    try:
                        harness.check_model(model)
                    except harnesses.Unavailable as error:
                        problems.append(f"{key}: {error}")
        except (harnesses.Unavailable, OSError) as error:
            problems += [f"{key}: {name} can't run on this machine: {error}" for key in keys]
        finally:
            if harness is not None:
                await harness.close()
    return problems


# ---------- records ----------


def last_error(values: dict, node: str, index: int | None) -> str | None:
    """Why the target failed last, from the run's state, or None: the reason that its `override` event gives."""
    if index is None:
        failure = values.get("failure") or {}
        return failure.get("error") if failure.get("node") == node else None
    failed = ((values.get("active") or {}).get(node) or {}).get("failed") or {}
    if str(index) in failed:
        return failed[str(index)]["error"]
    return next((e["error"] for e in (values.get("errors") or {}).get(node) or [] if e.get("index") == index), None)


def describe(key: str, override: dict) -> str:
    """`review[2]: claude-code, model claude-opus-5-5, the workflow's effort`, for messages."""
    parts = [override["harness"], f"model {override['model']}" if override["model"] else "the workflow's model"]
    if override["harness"] != "cursor":  # a Cursor model id carries its effort
        parts.append(f"effort {override['effort']}" if override["effort"] else "the workflow's effort")
    return f"{key}: {', '.join(parts)}"
