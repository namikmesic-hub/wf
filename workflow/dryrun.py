"""Dry runs: run a workflow with scripted agents, answers and commands, for free.

    wf run <workflow.yaml> --dry-run <script.yaml> [name=value ...]

A dry run exercises everything a workflow file decides (routes, loops,
for_each, templates, end outputs, retries, fallbacks, limits) without calling a
coding agent. The script says what each node produces, in order, one entry per
attempt:

    inputs: {task: Add a README}           # start inputs (name=value on the command line wins)
    agents:
      review:                              # by node id, or by path: build[1]/implement
        - {blocking_issues: [missing test]}    # the 1st attempt submits this
        - {blocking_issues: []}                # the next one, on the next pass
      implement:
        - {_files: {notes.txt: "hi"}, summary: done, notes: wrote notes.txt}
        - {_fail: crashed}                     # this attempt fails (retry applies)
        - {_unavailable: usage limit}          # the harness is out (fallback applies)
        - {summary: ok, notes: x, _tokens: 50000, _cost_usd: 1.2}   # pretend spending, for limits
    humans:
      approve:
        - {approved: true}
    commands:                              # optional: commands not mentioned here really run
      tests:
        - {exit_code: 1, stdout: "1 failed"}
        - {exit_code: 0, stdout: "all passed"}

Keys of agent entries starting with `_` steer the dry run; everything else is
the result, and is checked against the node's output schema before the run
starts, like answers and command results. A key is a node id, which matches
that node everywhere (every item, every sub-workflow with a node of that name),
or a path, which matches one place: build[1]/implement, or each[2] for an item
of a for_each node. Items running in parallel take a shared key's entries in
whatever order they ask.

A node the script mentions (by id or by any path) never really runs: when it
has no entry left for an attempt, the node fails at once (retrying can't help).
An agent node it doesn't mention fails the same way. A command it doesn't
mention really runs, and the run says which ones can before it starts.

Worktrees are real, so commands can look at what `_files` wrote. A dry run that
succeeds removes its worktrees and deletes its branches. The script is copied
into the run folder with a record of what's been used, so `wf retry`
continues a dry run where it stopped.
"""

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from . import schemas
from .spec import Workflow

KINDS = {"agents": "agent", "humans": "human", "commands": "command"}
STEERING = {"_files", "_tokens", "_cost_usd", "_fail", "_unavailable"}  # agent entries only
COMMAND_RESULT = {"type": "object", "properties": {
    "exit_code": {"type": "integer"}, "stdout": {"type": "string"}, "stderr": {"type": "string"}},
    "required": ["exit_code"], "additionalProperties": False}


class ScriptError(Exception):
    pass


class ScriptExhausted(Exception):
    """A node needs an entry the script doesn't have. Fails the node without retrying."""


@dataclass
class Script:
    entries: dict[str, dict[str, list]]  # kind -> key -> entries
    used_file: Path | None = None
    used: dict[str, int] = field(default_factory=dict)  # "kind:key" -> entries used so far
    inputs: dict = field(default_factory=dict)  # start inputs, used when the run starts

    @classmethod
    def load(cls, path: Path, workflows: list[Workflow]) -> "Script":
        """Read a script and check it against the workflow files it will run with."""
        try:
            data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        except (OSError, UnicodeDecodeError, yaml.YAMLError) as error:
            raise ScriptError(f"can't read {path}: {error}") from error
        if not isinstance(data, dict) or set(data) - {*KINDS, "inputs"}:
            raise ScriptError(f"a dry-run script has only these sections: inputs, {', '.join(KINDS)}")
        inputs = data.get("inputs") or {}
        if not isinstance(inputs, dict):
            raise ScriptError("inputs: expected a map from input name to value")
        problems, entries = [], {}
        for kind, wanted in KINDS.items():
            table = data.get(kind) or {}
            if not isinstance(table, dict):
                raise ScriptError(f"{kind}: expected a map from node to a list of entries")
            entries[kind] = {}
            for key, items in table.items():
                key = str(key)
                if not isinstance(items, list):
                    problems.append(f"{kind}.{key}: expected a list of entries")
                    continue
                node_id = leaf(key)
                nodes = [(w, node_id) for w in workflows if node_id in w.nodes]
                if not nodes:
                    problems.append(f"{kind}.{key}: no node `{node_id}` in this workflow or its sub-workflows")
                    continue
                for workflow, node_id in nodes:
                    if workflow.nodes[node_id].kind != wanted:
                        problems.append(f"{kind}.{key}: `{node_id}` is a {workflow.nodes[node_id].kind} node, not a {wanted} node")
                problems += [f"{kind}.{key}[{i}]: {p}" for i, item in enumerate(items) for p in _check(kind, item, nodes)]
                entries[kind][key] = items
        if problems:
            raise ScriptError("\n".join(problems))
        return cls(entries, inputs=inputs)

    @classmethod
    def resume(cls, folder: Path) -> "Script":
        """The script a dry run was started with, and what it has used."""
        try:
            script = cls(json.loads((folder / "script.json").read_text(encoding="utf-8")), folder / "used.json")
            if script.used_file.exists():
                script.used = json.loads(script.used_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:  # ValueError: not JSON, or not UTF-8
            raise ScriptError(f"can't read the dry run's script in {folder}: {error}") from error
        return script

    def keep(self, folder: Path) -> None:
        """Copy the checked script into the run folder (dry-run/), for wf retry."""
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "script.json").write_text(json.dumps(self.entries, indent=2), encoding="utf-8")
        self.used_file = folder / "used.json"

    def covers(self, kind: str, places: list[tuple[str, str]]) -> bool:
        """Are there entries left for all of these (path, node id) places at once?"""
        keys = Counter(self._key(kind, path, node_id) for path, node_id in places)
        return None not in keys and all(self.used.get(f"{kind}:{key}", 0) + need <= len(self.entries[kind][key])
                                        for key, need in keys.items())

    def take(self, kind: str, path: str, node_id: str) -> dict | None:
        """The next entry for this node (by path first, then by node id), or None if the script
        doesn't mention the node at all. Raises ScriptExhausted if it does, but has no entry
        left for this place."""
        key = self._key(kind, path, node_id)
        if key is None:
            mentioned = [k for k in self.entries.get(kind, {}) if leaf(k) == node_id]
            if mentioned:  # listed for other items or places: this one mustn't really run either
                raise ScriptExhausted(f"dry run: the script has no entry for {path or node_id}: "
                                      f"it lists `{node_id}` only as {', '.join(mentioned)}")
            return None
        count = self.used.get(f"{kind}:{key}", 0)
        if count >= len(self.entries[kind][key]):
            raise ScriptExhausted(f"dry run: the script has no result left for {path or node_id} "
                                  f"(its {count} entr{'y' if count == 1 else 'ies'} for `{key}` are used up)")
        self.used[f"{kind}:{key}"] = count + 1
        if self.used_file:
            temp = self.used_file.with_suffix(".tmp")
            temp.write_text(json.dumps(self.used), encoding="utf-8")
            temp.replace(self.used_file)
        return self.entries[kind][key][count]

    def unscripted_commands(self, workflows: list[Workflow]) -> list[str]:
        """Command nodes the script doesn't mention: in a dry run, these really run if reached.
        workflows[0] is the main one; others are named with their workflow."""
        mentioned = {leaf(key) for key in self.entries["commands"]}
        found = []
        for position, workflow in enumerate(workflows):
            where = "" if position == 0 else f" (in {workflow.name})"
            found += [f"{node_id}{where}" for node_id, node in sorted(workflow.nodes.items())
                      if node.kind == "command" and node_id not in mentioned]
        return found

    def _key(self, kind: str, path: str, node_id: str) -> str | None:
        table = self.entries.get(kind, {})
        return path if path in table else node_id if node_id in table else None


def leaf(key: str) -> str:
    """The node id a script key names: build[1]/implement -> implement, each[2] -> each."""
    return key.rsplit("/", 1)[-1].split("[", 1)[0]


def _check(kind: str, item, nodes: list[tuple[Workflow, str]]) -> list[str]:
    if not isinstance(item, dict):
        return ["expected a map"]
    if kind == "commands":
        return schemas.check(item, COMMAND_RESULT)
    steering = {k for k in item if k.startswith("_")}
    allowed = STEERING if kind == "agents" else set()
    problems = [f"`{k}` isn't a dry-run key" + (f" (use {', '.join(sorted(allowed))})" if allowed else
                                                 " (only agent entries take `_` keys)") for k in steering - allowed]
    for name, kind_of in (("_tokens", int), ("_cost_usd", (int, float))):
        value = item.get(name, 0)
        if isinstance(value, bool) or not isinstance(value, kind_of) or value < 0:
            problems.append(f"`{name}` must be a number, 0 or more" + (" (whole)" if kind_of is int else ""))
    if "_fail" in item or "_unavailable" in item:
        return problems
    files = item.get("_files", {})
    if not isinstance(files, dict) or any(not isinstance(v, str) for v in files.values()):
        problems.append("`_files` maps relative paths to file contents")
    elif any(Path(p).is_absolute() or ".." in Path(p).parts for p in files):
        problems.append("`_files` paths must stay inside the workspace")
    result = {k: v for k, v in item.items() if not k.startswith("_")}
    for workflow, node_id in nodes:
        try:
            schema = schemas.node_output_schema(workflow, node_id)
        except Exception as error:  # a broken contract: validate reports it; say why this entry wasn't checked
            problems.append(f"can't check against `{node_id}`'s output schema: {error}")
            continue
        problems += schemas.check(result, schema)
    return problems


def write_files(cwd: Path, files: dict[str, str]) -> None:
    """A scripted agent's `_files`, written into its workspace (paths were checked at load)."""
    for relative, content in files.items():
        target = Path(cwd) / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
