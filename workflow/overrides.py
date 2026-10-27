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
names, else the node's own harness. That is the one harness that the node names.
A job is an item, or a node without for_each. For a target that is one job
still to run in the pass in progress, it can also be the harness that the
template gives the job. Finished nodes and items don't run again: the override
applies to the attempts still to come.

The functions job_harnesses() and job_setting() choose the harness, model and
effort of an agent job. Each attempt uses them (nodes._agent). So does check(),
for each job of the target still to run in the pass in progress, with the job's
own variables (job_variables). So the check sees the harness and model that the
next attempt gets, and skips a finished job, which won't run again.

A setting that the override leaves out comes from the workflow file, for the
harness of the override. But a model that the workflow gives for the node's own
harness is wrong for another harness. So when --harness changes the harness, the
override needs --model, unless the workflow gives a model for that harness. A
Cursor session gets no effort: a Cursor model id carries its own.

check() runs before anything changes, as operate.check_answers does for answers.
It refuses a target that isn't an agent node of the run's workflow, or an item
that the node doesn't have. It also refuses a harness that the node may not use.
After every other check, available() opens each harness as a run does, in a
folder outside the run. So it also refuses a harness that isn't installed or
signed in, and a model that Cursor doesn't list. It removes that folder, and
refuses when it can't, so the check leaves nothing behind. Only then does the runner keep
the overrides, with their events first (runner._keep). A later override for the
same target replaces the earlier one. Each override goes into events.jsonl as an
`override` event, with the last error of its target:

    {"event": "override", "node": "review", "index": 2, "harness": "claude-code",
     "model": "claude-opus-5-5", "effort": null, "last_error": "cursor is unavailable: ..."}

Each agent attempt records an `agent_model` event with the harness, model and
effort that it got (nodes._agent). The event also names the target of the
override, if the attempt has one. So the trace shows which model ran each
attempt, and why.
"""

import json
import os
import re
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import harnesses, scheduler, state, templates, workspaces
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


# ---------- what a job runs with ----------


class SettingError(Exception):
    """A model or an effort renders to something that isn't a non-empty string."""


def _render(field: str, source: str, variables: dict, call: Callable = templates.render, **facts) -> Any:
    """call(source, variables). An attempt gives job_harnesses() and job_setting() the trace's render
    instead (trace.Scope.render), which takes the same arguments and records it."""
    return call(source, variables)


def job_harnesses(workflow: Workflow, node_id: str, override: dict, variables: dict,
                  render: Callable = _render) -> list[str]:
    """The harnesses that an agent job tries, in order: the override's harness, else the node's list, or
    its one harness rendered with the job's variables. Raises templates.TemplateError."""
    harness = override.get("harness") or workflow.setting(node_id, "harness")
    return list(harness) if isinstance(harness, list) else [render("harness", harness, variables).strip()]


def job_setting(workflow: Workflow, node_id: str, harness: str, override: dict,
                variables: dict, render: Callable = _render) -> tuple[str | None, str | None]:
    """The model and effort of an agent job on one harness: the override's, else the workflow's, rendered
    with the job's variables. A Cursor model id carries its effort, so a Cursor session gets none.
    Raises templates.TemplateError, or SettingError."""
    model = override.get("model") or _rendered(workflow, node_id, harness, "model", variables, render)
    if harness == "cursor":
        return model, None
    return model, override.get("effort") or _rendered(workflow, node_id, harness, "effort", variables, render)


def _rendered(workflow: Workflow, node_id: str, harness: str, name: str, variables: dict,
              render: Callable) -> str | None:
    """`model` or `effort` for one harness, rendered: a template like "{{ item.model }}" gives each item its own."""
    where, source = workflow.harness_setting_at(node_id, harness, name)
    if source is None:
        return None
    try:
        value = render(name, source, variables, templates.render_value, harness=harness, set_in=where)
    except templates.TemplateError as error:
        raise templates.TemplateError(f"{name}: {error}") from error
    if not isinstance(value, str) or not value.strip():
        shown = "none" if value is None else json.dumps(value, ensure_ascii=False, default=str)
        raise SettingError(f"`{name}` of node `{node_id}` renders to {shown} for {harness}, but it must be a non-empty string")
    return value.strip()


def job_variables(values: dict, node_id: str, key: str) -> dict | None:
    """The template variables of a job of the node's pass in progress, as nodes.run_job makes them. The key is
    "-", or an item's index. A job with no workspace yet, as when making it failed, gets none of its own, so a
    template that reads it can't render. None when the node has no pass in progress."""
    active = (values.get("active") or {}).get(node_id)
    if active is None:
        return None
    own = ((active.get("failed") or {}).get(key) or {}).get("workspace") or (active.get("workspaces") or {}).get(key)
    workspace = {name: value for name, value in active["vars"]["workspace"].items() if name != node_id}
    item = {} if key == "-" else {"item": active["items"][int(key)], "index": int(key)}
    return {**active["vars"], **item, "workspace": workspace if own is None else {**workspace, node_id: own}}


# ---------- checks ----------


@dataclass
class Checked:
    found: dict[str, dict] = field(default_factory=dict)  # the overrides to keep, by target, each with its harness
    problems: list[str] = field(default_factory=list)
    models: dict[str, set[str]] = field(default_factory=dict)  # by target: the models that available() checks
    access: dict[str, str] = field(default_factory=dict)  # by target: its node's access level, which can decide the runtime


def check_start(given: dict[str, dict[str, str]], workflows: list[Workflow], inputs: dict) -> Checked:
    """Check overrides before a run starts, with items that its start input can define."""
    workflow = workflows[0]
    progress = state.Progress({}, workflow, scheduler.Topology.of(workflow), lambda event: None)
    progress.begin(inputs, {})
    variables = progress.vars()
    for key in given:
        node_id, _ = split(key)
        node = workflow.nodes.get(node_id)
        if node is None or node.kind != "agent":
            continue
        items = None
        if node.for_each is not None:
            try:
                items = templates.evaluate(node.for_each, variables)
            except templates.TemplateError:
                continue
            if not isinstance(items, (list, tuple)):
                continue
            items = list(items)
        progress.s["active"][node_id] = {"items": items, "vars": variables, "outputs": {}}
    return check(given, workflows, progress.s, before_start=True)


def check(given: dict[str, dict[str, str]], workflows: list[Workflow], values: dict,
          *, before_start: bool = False) -> Checked:
    """The overrides, each with its harness filled in, what is wrong with them, and the models to check.
    Here workflows holds the run's copies, its own workflow first, and values holds its state (state.RunState)."""
    workflow, checked = workflows[0], Checked()
    for key, settings in given.items():
        node_id, index = split(key)
        problem = _target(key, node_id, index, workflows, values)
        jobs = _jobs(values, node_id, index) if problem is None else {}
        harness = None
        if problem is None:
            harness, problem = _harness(key, node_id, index, settings, workflow, jobs)
        override = {"harness": harness, "model": settings.get("model"), "effort": settings.get("effort")}
        models = None
        if problem is None:
            models, problem = _models(key, node_id, override, workflow, jobs, before_start=before_start)
        if problem is not None:
            checked.problems.append(problem)
            continue
        checked.found[key], checked.models[key] = override, models
        checked.access[key] = workflow.setting(node_id, "access")
    return checked


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


def _jobs(values: dict, node_id: str, index: int | None) -> dict[str, dict]:
    """The target's jobs still to run in the node's pass in progress, with their variables, by key ("-" or an
    index). A finished job doesn't run again (state.Progress.retry), so its settings don't matter."""
    active = (values.get("active") or {}).get(node_id)
    if active is None:
        return {}
    if index is not None:
        keys = [str(index)]
    else:
        keys = ["-"] if active.get("items") is None else [str(i) for i in range(len(active["items"]))]
    return {key: job_variables(values, node_id, key) for key in keys if key not in active["outputs"]}


def _harness(key: str, node_id: str, index: int | None, settings: dict[str, str], workflow: Workflow,
             jobs: dict[str, dict]) -> tuple[str | None, str | None]:
    """(the harness the override runs its target on, None), or (None, why it can't)."""
    own = _own_harness(workflow, node_id, index, jobs)
    harness = settings.get("harness") or own
    if harness is None:
        how = "from a list" if isinstance(workflow.setting(node_id, "harness"), list) else "with a template"
        return None, f"{key}: `{node_id}` picks its harness {how}, so name one: --harness {key}=NAME"
    if harness not in HARNESSES:
        return None, f"{key}: unknown harness `{harness}` (choose from {', '.join(HARNESSES)})"
    where, model = workflow.harness_setting_at(node_id, harness, "model")
    if "model" not in settings and harness != own and model is not None and not _for_harness(where, harness):
        return None, (f"{key}: the workflow gives `{node_id}` the model `{model}` for its own harness, and none "
                      f"for {harness}: add --model {key}=ID")
    if harness == "cursor":
        if "effort" in settings:
            return None, f"{key}: cursor has no effort setting: a Cursor model id carries its own, like gpt-5.6-sol-max-fast"
        if "model" not in settings and model is None:
            return None, f"{key}: cursor needs a model, and the workflow gives `{node_id}` none for it: add --model {key}=ID"
    access, node = workflow.setting(node_id, "access"), workflow.nodes[node_id]
    if workflow.setting(node_id, "tools") and harnesses.reaches_keychain(harness, access) and not node.tools_with_shell:
        return None, (f"{key}: `{node_id}` has tools, and on {harness} at `{access}` access its commands could read "
                      "every stored sign-in, but the node doesn't set `tools_with_shell: true`")
    return harness, None


def _own_harness(workflow: Workflow, node_id: str, index: int | None, jobs: dict[str, dict]) -> str | None:
    """The one harness that the node names. When the target is one job still to run in the pass in progress, also
    the harness that the node's template gives that job, as nodes._agent renders it. None for a list, or when wf
    can't tell."""
    own = workflow.setting(node_id, "harness")
    if isinstance(own, list):
        return None
    if "{{" not in own and "{%" not in own:
        return own
    one_job = index is not None or workflow.nodes[node_id].for_each is None  # a node's items can differ
    if not one_job or len(jobs) != 1:
        return None
    try:
        return job_harnesses(workflow, node_id, {}, next(iter(jobs.values())))[0]
    except templates.TemplateError:
        return None


def _for_harness(where: str | None, harness: str) -> bool:
    """Does the setting at `where` (Workflow.harness_setting_at) belong to this harness alone?"""
    return where is not None and (where.endswith(f".{harness}") or where.startswith(f"defaults.harnesses.{harness}."))


def _models(key: str, node_id: str, override: dict, workflow: Workflow,
            jobs: dict[str, dict], *, before_start: bool = False) -> tuple[set[str] | None, str | None]:
    """The models of the target's jobs on the override's harness, as far as wf knows them now, or why one
    of them can't run. A job still to run in the pass in progress renders its settings as its next attempt
    will. Else wf takes the override's model, or the workflow's model when it isn't a template."""
    harness = override["harness"]
    if jobs:
        models = set()
        for job, variables in jobs.items():
            try:
                if before_start:
                    model = _start_model(workflow, node_id, override, variables)
                else:
                    model, _ = job_setting(workflow, node_id, harness, override, variables)
            except SettingError as error:
                return None, f"{key}: {error}"
            except templates.TemplateError as error:
                if node_id in variables["workspace"]:
                    return None, f"{key}: {error}"
                name = f"`{node_id}`" if job == "-" else f"item {job} of `{node_id}`"
                return None, (f"{key}: {error}. {name} has no workspace yet, so a template can't read it: "
                              f"give the setting with --model {key}=ID or --effort {key}=LEVEL")
            if model is not None:
                models.add(model)
        return models, None
    model = override["model"] or workflow.harness_setting(node_id, harness, "model")
    return ({model.strip()} if model is not None and "{{" not in model and "{%" not in model else set()), None


def _start_model(workflow: Workflow, node_id: str, override: dict, variables: dict) -> str | None:
    """Check each setting whose inputs exist, and leave the others for the node's pass."""
    later = set(workflow.nodes) | {"run", "workspace", "history", "results", "errors"}
    harness, known = override["harness"], {}
    for name in (("model",) if harness == "cursor" else ("model", "effort")):
        if override.get(name) is not None:
            known[name] = override[name]
            continue
        reads = []

        def render(field, source, variables, call=templates.render, **facts):
            try:
                value, used = templates.watched(call, source, variables)
            except templates.TemplateError as error:
                reads.extend(error.reads)
                raise
            reads.extend(used)
            return value

        try:
            value = _rendered(workflow, node_id, harness, name, variables, render)
        except (templates.TemplateError, SettingError):
            if any(read.path[0] in later for read in reads):
                continue
            raise
        if not any(read.path[0] in later for read in reads):
            known[name] = value
    return known.get("model")


async def available(checked: Checked) -> list[str]:
    """Open each harness that the overrides name, as a run does, and ask it about the models of each target.
    Returns what can't run on this machine: a harness that isn't installed or signed in, or a model it doesn't have.
    The harnesses open in a folder of their own, so the run's folder stays as it was. That folder is removed
    after, with the folder that Cursor makes by its name, and one that stays is a problem too."""
    by_harness: dict[str, list[str]] = {}
    for key, override in checked.found.items():
        by_harness.setdefault(override["harness"], []).append(key)
    try:
        folder = Path(tempfile.mkdtemp(prefix="wf-check-"))
    except OSError as error:
        return [f"wf can't make a folder to open the harnesses in: {error.strerror or error}"]
    problems = []
    try:
        for name, keys in by_harness.items():
            problems += await _open(name, keys, checked.models, checked.access, folder)
    finally:
        problems += [problem for made in (folder, workspaces.temp_root(folder.name)) if (problem := _remove(made))]
    return problems


def _remove(folder: Path) -> str | None:
    """Remove a folder that opening the harnesses made, if it is there. Returns why it stays, or None."""
    try:
        shutil.rmtree(folder)
    except OSError as error:
        if os.path.lexists(folder):
            return f"wf can't remove {folder}, which the harness check made: {error.strerror or error}"
    return None


async def _open(name: str, keys: list[str], models: dict[str, set[str]], access: dict[str, str],
                folder: Path) -> list[str]:
    """Open the harness, and check each target's models on it at the target's access level. A target
    whose model wf can't tell yet is checked with none, which asks whether the harness can run at all."""
    problems, harness = [], None
    try:
        harness = harnesses.create(name)
        await harness.open(folder)
        for key in keys:
            for model in sorted(models[key]) or [None]:
                try:
                    await harness.check_model(model, access.get(key, "read-only"))
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
    """Why the target failed last, from the run's state, or None: the reason that its `override` event gives.
    For a node, that is its failed job in the pass in progress, or its first failed item. Else it is the run's
    failure at the node, or the first item that its last pass went on without."""
    failed = ((values.get("active") or {}).get(node) or {}).get("failed") or {}
    errors = (values.get("errors") or {}).get(node) or []  # items that on_item_error: continue left behind
    if index is not None:
        if str(index) in failed:
            return failed[str(index)]["error"]
        return next((e["error"] for e in errors if e.get("index") == index), None)
    if "-" in failed:
        return failed["-"]["error"]
    if failed:
        first = min(failed, key=int)
        return f"item {first}: {failed[first]['error']}"
    failure = values.get("failure") or {}
    if failure.get("node") == node:
        return failure.get("error")
    return f"item {errors[0]['index']}: {errors[0]['error']}" if errors else None


def describe(key: str, override: dict) -> str:
    """`review[2]: claude-code, model claude-opus-5-5, the workflow's effort`, for messages."""
    parts = [override["harness"], f"model {override['model']}" if override["model"] else "the workflow's model"]
    if override["harness"] != "cursor":  # a Cursor model id carries its effort
        parts.append(f"effort {override['effort']}" if override["effort"] else "the workflow's effort")
    return f"{key}: {', '.join(parts)}"
