"""Validate: the checks in the spec's Validation section, run before anything starts.

check(path) loads a workflow file and every sub-workflow it uses, and returns a
Report: errors (wf run refuses to start) and warnings (it starts, but something
looks risky). Its notes tell what isn't a problem: where `wf continue` sends a
follow-up request when the file has no `follow_up`. Nothing here runs a node.

The checks run in this order, because later ones lean on earlier ones:
1. structure: node ids, `needs` and route targets, reachability, loops
2. schemas: `types`, start.input, node outputs, end output schemas
3. sub-workflows: each is checked too (its problems are reported with its
   own file name), and its end output becomes the parent node's output schema
4. per node: templates and expressions, for_each, harness, tools, workspace, `with`
5. `branch` names, end outputs, then the warnings that need the whole graph

Templates are checked without running them. templates.references() lists every
name a template reads and its field path; _roots() says what each name holds,
as a JSON Schema; schemas.field_at() walks the path through it.

"Might not have run" is the subtle check. _Order works out which nodes are
certain to have finished before a node starts: its dominators (nodes on every
path of "this finishing made that pending" that leads to it), plus nodes those
make certain and that must finish before it can start. A template that reads
any other node without `{% if node %}` gets a warning.
"""

import difflib
import functools
import itertools
from dataclasses import dataclass, field, replace
from pathlib import Path

from . import harnesses, scheduler, schemas, spec, templates
from .scheduler import END, START, Topology
from .schemas import ANY, EACH, NoField
from .spec import HARNESSES, NODE_ID, RESERVED, Problem, SpecError, Workflow
from .templates import Ref, TemplateError
from .tools import keychain
from .tools.catalog import TOOLS

_WORKSPACE = schemas.object_schema(
    {"path": {"type": "string"}, "branch": {"type": ["string", "null"]}, "base": {"type": ["string", "null"]}}
)
_ERROR_ITEM = schemas.object_schema({"index": {"type": "integer"}, "item": {}, "error": {"type": "string"}})
_NAME = {"type": ["string", "null"]}  # in results.<node>: none with no agent attempt, and a model of none is the harness's default
_RUN = schemas.object_schema({
    "id": {"type": "string"}, "dir": {"type": "string"}, "cost_usd": {"type": "number"},
    "tokens": {"type": "integer"}, "elapsed_s": {"type": "number"}, "base": {"type": ["string", "null"]},
})
_SESSION = schemas.object_schema({"continued": {"type": "boolean"}})
_JINJA_GLOBALS = frozenset({"range", "dict", "lipsum", "cycler", "joiner", "namespace"})
_SAFE_IN_SHELL = frozenset({"integer", "number", "boolean", "null"})  # can't carry shell syntax


@dataclass
class Report:
    problems: list[Problem]
    workflow: Workflow | None = None  # the parsed top-level file, if it parsed
    files: list[Path] = field(default_factory=list)  # it and every sub-workflow it uses
    notes: list[str] = field(default_factory=list)  # information about a file without errors, not a problem

    @property
    def errors(self) -> list[Problem]:
        return [p for p in self.problems if p.level == "error"]

    @property
    def warnings(self) -> list[Problem]:
        return [p for p in self.problems if p.level == "warning"]


def check(path: Path | str) -> Report:
    """Check a workflow file and every sub-workflow it uses."""
    result = _check(Path(path).resolve(), (), frozenset(), {})
    problems = list(dict.fromkeys(result.problems))
    valid = result.workflow is not None and not any(p.level == "error" for p in problems)
    return Report(problems, result.workflow, list(dict.fromkeys(result.files)), _notes(result.workflow) if valid else [])


def _notes(workflow: Workflow) -> list[str]:
    """Where `wf continue` sends a follow-up request, for a workflow without `follow_up`."""
    if workflow.follow_up is not None:
        return []
    if targets := scheduler.follow_up_targets(workflow):
        return [f"`wf continue` sends a follow-up request to `{targets[0]}`, the first agent node with `edit` or "
                "`full` access. Name another node with `follow_up: {to: <node>}`"]
    return ["`wf continue` refuses the runs of this workflow, because it has no agent node with `edit` or `full` "
            "access. Name a node that takes a follow-up request with `follow_up: {to: <node>}`"]


@dataclass
class _Result:
    problems: list[Problem]
    workflow: Workflow | None = None
    end_schema: dict = field(default_factory=dict)  # what a parent node gets as output
    harnesses: set[str] = field(default_factory=set)  # every harness an agent here could run on
    files: list[Path] = field(default_factory=list)  # this file and its sub-workflows
    worktrees: bool = False  # does it (or a sub-workflow) create worktrees with `workspace: new`?
    branches: list["_Branch"] = field(default_factory=list)  # the names `branch` gives, here and in sub-workflows
    no_tokens: set[str] = field(default_factory=set)  # harnesses that may run an agent here without counting its tokens


def _check(path: Path, stack: tuple[Path, ...], tainted: frozenset[str], cache: dict) -> _Result:
    """`tainted`: start inputs the parent fills from node output, so commands must quote them."""
    key = (path, tainted)
    if key not in cache:
        try:
            workflow = spec.load(path)
        except SpecError as error:
            cache[key] = _Result(error.problems, files=[path])
        else:
            cache[key] = _Checker(workflow, path, (*stack, path), tainted, cache).run()
    return cache[key]


class _Checker:
    def __init__(self, workflow: Workflow, path: Path, stack, tainted, cache):
        self.wf = workflow
        self.nodes = workflow.nodes
        self.path = path
        self.stack = stack
        self.tainted = tainted
        self.cache = cache
        self.problems: list[Problem] = []
        self.subs: dict[str, _Result] = {}
        self.harnesses: set[str] = set()
        self.no_tokens: set[str] = set()  # harnesses that may run an agent here without counting its tokens
        self.cost_refs: list[str] = []  # where templates read run.cost_usd
        self.token_refs: list[str] = []  # where templates read run.tokens
        self.end_fields: dict[str, dict] = {}
        self.branches: list[_Branch] = []
        self._warned: set = set()
        self._tainting: set[str] = set()  # nodes whose `branch` _branch_tainted() is reading

    def error(self, where: str, message: str) -> None:
        self.problems.append(Problem(where, message, "error", self.path))

    def warn(self, where: str, message: str) -> None:
        self.problems.append(Problem(where, message, "warning", self.path))

    def add(self, problems: list[Problem]) -> None:
        self.problems += [replace(p, file=self.path) for p in problems]

    def run(self) -> _Result:
        self._ids()
        self.topo = self._edges()
        self._reachable()
        self._routes_and_loops()
        self._schemas()
        self.roots = self._roots()  # without sub-workflow outputs yet; enough to follow tainted data
        self._sub_workflows()
        self.roots = self._roots()
        self.order, self.follow_ups = (order := _Order(self.topo)), _FollowUps(self, order)
        if self.wf.defaults.tools is not None:  # once, whether or not an agent node inherits them
            self._tool_list("defaults.tools", self.wf.defaults.tools)
        self._secret_env()
        for node_id in self.nodes:
            self._node(node_id)
        self._branches()
        self._end()
        self._concurrent_edits()
        self._usage()
        end_schema = {
            "type": "object", "properties": self.end_fields,
            "required": list(self.end_fields), "additionalProperties": False,
        }
        files = [self.path] + [f for sub in self.subs.values() for f in sub.files]
        worktrees = any(self.wf.setting(n, "workspace") == "new" for n in self.nodes) or any(s.worktrees for s in self.subs.values())
        return _Result(self.problems, self.wf, end_schema, self.harnesses, files, worktrees, self.branches, self.no_tokens)

    # ---------- 1. structure ----------

    def _ids(self) -> None:
        for node_id in self.nodes:
            if not NODE_ID.match(node_id):
                self.error(f"nodes.{node_id}", "node ids start with a lowercase letter, followed by lowercase letters, digits or _")
            elif node_id in RESERVED:
                self.error(f"nodes.{node_id}", f"`{node_id}` is a reserved name")

    def _edges(self) -> Topology:
        """The graph, keeping only edges to nodes that exist."""
        needs = {}
        for node_id, node in self.nodes.items():
            kept = []
            for need in node.needs:
                where = f"nodes.{node_id}.needs"
                if need == START:
                    kept.append(need)
                elif need == END:
                    self.error(where, "`end` can't be in needs")
                elif need not in self.nodes:
                    self.error(where, f"unknown node `{need}`{_hint(need, self.nodes)}")
                elif need == node_id:
                    self.error(where, "a node can't need itself")
                else:
                    if self.nodes[need].routes:
                        self.error(where, f"`{need}` has routes, so it can't be in needs: point one of its routes at `{node_id}` instead")
                    kept.append(need)
            needs[node_id] = tuple(dict.fromkeys(kept))
        for cycle in _cycles(needs):
            self.error(f"nodes.{cycle[0]}.needs", f"needs form a cycle: {' → '.join(cycle)}")

        routes = {}
        for node_id, node in self.nodes.items():
            kept_routes = []
            for i, route in enumerate(node.routes):
                where = f"nodes.{node_id}.routes[{i}].to"
                targets = []
                for target in route.targets:
                    if target == START:
                        self.error(where, "`start` can't be a route target")
                    elif target == END and len(route.targets) > 1:
                        self.error(where, "`end` can't be part of a list of targets")
                    elif target != END and target not in self.nodes:
                        self.error(where, f"unknown node `{target}`{_hint(target, self.nodes)}")
                    else:
                        targets.append(target)
                kept_routes.append(route.model_copy(update={"to": targets or END}))
            routes[node_id] = tuple(kept_routes)
        return Topology(tuple(self.nodes), needs, routes)

    def _reachable(self) -> None:
        targets = {t for rules in self.topo.routes.values() for route in rules for t in route.targets}
        reached = self.topo.reachable([START])
        for node_id, node in self.nodes.items():
            if not node.needs and node_id not in targets:
                self.error(f"nodes.{node_id}", "this node never runs: give it `needs` (use [start] to run first) or point a route at it")
            elif node_id not in reached:
                self.error(f"nodes.{node_id}", "can't be reached from start")

    def _routes_and_loops(self) -> None:
        for node_id, node in self.nodes.items():
            if node.routes:
                where, last = f"nodes.{node_id}.routes[{len(node.routes) - 1}]", node.routes[-1]
                if last.when is not None:
                    self.error(where, "the last route can't have `when`, so there's always a way out")
                if last.max is not None:
                    self.error(where, "the last route can't have `max`, so there's always a way out")
        # A loop is fine if it passes through a route with `max`: drop those edges and look for cycles
        unlimited = {START: list(self.topo.dependents.get(START, ()))}
        for node_id in self.topo.order:
            unlimited[node_id] = list(self.topo.dependents.get(node_id, ()))
            for route in self.topo.routes[node_id]:
                if route.max is None:
                    unlimited[node_id] += [t for t in route.targets if t != END]
        for cycle in _cycles(unlimited):
            closing = [a for a, b in zip(cycle, cycle[1:]) if any(b in r.targets and r.max is None for r in self.topo.routes.get(a, ()))]
            if closing:  # cycles made only of needs were reported in _edges
                self.error(f"nodes.{closing[0]}.routes", f"loop {' → '.join(cycle)} has no route with `max`, so it could run forever")

    # ---------- 2. schemas ----------

    def _schemas(self) -> None:
        types = self.wf.types
        self.add(schemas.type_problems(types))
        self.add(schemas.field_map_problems(self.wf.start.input, types, "start.input", inputs=True))
        for node_id, node in self.nodes.items():
            if node.kind in ("agent", "human") and node.output is not None:
                self.add(schemas.field_map_problems(node.output, types, f"nodes.{node_id}.output"))
        for name, value in self.wf.end.output.items():
            if isinstance(value, dict):
                if "value" not in value:
                    self.error(f"end.output.{name}", "the full form needs `value`")
                self.add(schemas.schema_problems(value, types, f"end.output.{name}", extra={"value"}))

    def _roots(self) -> dict[str, dict]:
        """What each top-level template name holds, as a JSON Schema."""

        def one_result(node_id: str) -> dict:
            if self.nodes[node_id].kind == "workflow":
                return self.subs[node_id].end_schema if node_id in self.subs else ANY
            try:
                return schemas.node_output_schema(self.wf, node_id)
            except Exception:
                return ANY  # its schema is broken; that was reported in _schemas

        def per_pass(node_id: str, schema: dict) -> dict:
            return {"type": "array", "items": schema} if self.nodes[node_id].for_each is not None else schema

        def fields(properties: dict) -> dict:
            return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}

        outputs = {n: per_pass(n, one_result(n)) for n in self.nodes}
        try:
            start = schemas.object_schema(self.wf.start.input, self.wf.types)
        except Exception:
            start = ANY
        roots = {
            **outputs,
            "start": start,
            "history": fields({n: {"type": "array", "items": s} for n, s in outputs.items()}),
            "workspace": fields({n: per_pass(n, _WORKSPACE) for n in self.nodes}),
            "errors": fields({n: {"type": "array", "items": _ERROR_ITEM} for n in self.nodes}),
            "run": _RUN,
            "session": _SESSION, **({"follow_up": _FOLLOW_UP} if scheduler.follow_up_targets(self.wf, self.topo) else {}),
        }
        lists = [n for n, node in self.nodes.items() if node.for_each is not None]
        for _ in range(len(lists) + 1):  # a for_each list can read `results` too: each pass resolves one more step of that
            found = fields({n: {"type": "array", "maxItems": self.wf.setting(n, "max_items"),
                                "items": self._entry(n, one_result(n), roots)} for n in lists})
            if found == roots.get("results"):
                break
            roots["results"] = found
        return roots

    def _entry(self, node_id: str, output: dict, roots: dict) -> dict:
        """The schema of one item in `results.<node>`. With on_item_error: continue, a failed item's output is none."""
        if self.wf.setting(node_id, "on_item_error") == "continue" and output.get("type") == "object":
            output = {**output, "type": ["object", "null"]}
        return schemas.object_schema({"index": {"type": "integer"}, "item": self._item_schema(node_id, roots),
                                      "output": output, "harness": _NAME, "model": _NAME})

    # ---------- 3. sub-workflows ----------

    def _sub_workflows(self) -> None:
        for node_id, node in self.nodes.items():
            if node.kind != "workflow":
                continue
            where = f"nodes.{node_id}.workflow"
            sub_path = (self.path.parent / node.workflow).resolve()
            if sub_path in self.stack:
                chain = [p.name for p in self.stack[self.stack.index(sub_path):]] + [sub_path.name]
                self.error(where, f"workflow includes itself: {' → '.join(chain)}")
                continue
            if not sub_path.is_file():
                self.error(where, f"file not found: {node.workflow}")
                continue
            result = _check(sub_path, self.stack, self._tainted_inputs(node_id), self.cache)
            self.problems += result.problems
            self.harnesses |= result.harnesses
            self.no_tokens |= result.no_tokens
            self.subs[node_id] = result
            if result.worktrees and self.wf.setting(node_id, "workspace") == "new":
                # git can't have a branch wf/<run>/<node> and also wf/<run>/<node>/<sub node>
                self.error(f"nodes.{node_id}.workspace", f"`{node.workflow}` creates worktrees of its own, so this node "
                                                         "can't use `workspace: new`; give the sub-workflow's nodes their worktrees instead")

    def _tainted_inputs(self, node_id: str) -> frozenset[str]:
        """The sub-workflow inputs this node fills from node output."""
        found = set()
        for key, value in self.nodes[node_id].with_.items():
            if isinstance(value, str):
                try:
                    if any(self._tainted(ref, node_id) for ref in templates.references(value)):
                        found.add(key)
                except TemplateError:
                    pass
        return frozenset(found)

    # ---------- 4. one node ----------

    def _node(self, node_id: str) -> None:
        node = self.nodes[node_id]
        at = f"nodes.{node_id}"
        if node.for_each is not None:
            self._for_each(node_id)
        fields: list[tuple[str, str]] = []
        if node.kind == "agent":
            fields.append(("instruction", node.instruction))
        elif node.kind == "command":
            fields += [("command", node.command)] + [(f"env.{k}", v) for k, v in self.wf.setting(node_id, "env").items()]
        elif node.kind == "human":
            fields.append(("human", node.human))
        else:
            fields += [(f"with.{k}", v) for k, v in node.with_.items() if isinstance(v, str)]
        if node.base is not None:
            fields.append(("base", node.base))
        if node.branch is not None:
            fields.append(("branch", node.branch))
        for name, source in fields:
            refs = self._template(f"{at}.{name}", source, node_id, "node")
            if refs and name == "command":
                self._unquoted(f"{at}.command", refs, node_id)
            if refs and name == "instruction":
                self._web_to_full(f"{at}.instruction", refs, node_id)
        if node.kind == "agent":
            possible = self._harness(node_id)
            self._tools(node_id, possible)
            self._project_instructions(node_id, possible)
        if node.kind == "workflow":
            self._with(node_id)
        for i, route in enumerate(node.routes):
            if route.when is not None:
                self._template(f"{at}.routes[{i}].when", route.when, node_id, "when", expression=True)
        self._workspace(node_id)

    def _secret_env(self) -> None:
        """Each name in `secret_env` is one that `env` sets: in the node, or for defaults in any command node."""
        commands = [n for n, node in self.nodes.items() if node.kind == "command"]
        for name in self.wf.defaults.secret_env:
            if not any(name in self.wf.setting(n, "env") for n in commands):
                self.error("defaults.secret_env", f"`{name}` isn't set in any command node's `env`")
        for node_id in commands:
            env = self.wf.setting(node_id, "env")
            for name in self.nodes[node_id].secret_env:
                if name not in env:
                    self.error(f"nodes.{node_id}.secret_env", f"`{name}` isn't set in `env`{_hint(name, env)}")

    def _template(self, where: str, source: str, node_id: str | None, context: str, *, expression: bool = False):
        """Check one template or expression. context: node, for_each, when or end."""
        try:
            refs = templates.references(source, expression=expression)
        except TemplateError as error:
            self.error(where, str(error))
            return None
        for ref in refs:
            self._ref(where, ref, node_id, context)
        return refs

    def _ref(self, where: str, ref: Ref, node_id: str | None, context: str) -> None:
        name = ref.name
        if name in ("item", "index"):
            if node_id is None or self.nodes[node_id].for_each is None or context != "node":
                self.error(where, f"`{name}` is only defined inside a for_each node's templates (not in `when` or `for_each`)")
                return
            schema = self._item_schema(node_id) if name == "item" else {"type": "integer"}
        elif name == "session" and context == "end":
            self.error(where, "`session` isn't available in end.output, only while a node renders")
            return
        elif name in self.roots:
            schema = self.roots[name]
            listed = ref.path[0] if name == "results" and ref.path else None
            if listed in self.nodes and self.nodes[listed].for_each is None:
                self.error(where, f"`{listed}` isn't a for_each node, so `results.{listed}` doesn't exist: read `{listed}`")
                return
        elif name in _JINJA_GLOBALS:
            return
        else:
            self.error(where, f"unknown name `{name}`{_hint(name, self.roots)}")
            return
        if ref.path is None:
            return
        try:
            schemas.field_at(schema, ref.path)
        except NoField as error:
            self.error(where, f"`{_dotted(name, ref.path)}`: {error}")
            return
        if name == "run" and ref.path[:1] == ("cost_usd",):
            self.cost_refs.append(where)
        if name == "run" and ref.path[:1] == ("tokens",):
            self.token_refs.append(where)
        self._might_not_have_run(where, ref, node_id, context)
        self._failed_item(where, ref)

    def _might_not_have_run(self, where: str, ref: Ref, node_id: str | None, context: str) -> None:
        name, path = ref.name, ref.path
        if name in self.nodes or name == "follow_up":  # follow_up is none before `wf continue`, as a node that hasn't run
            target, reads_field = name, bool(path)
        elif name == "workspace" and path and path[0] in self.nodes:
            target, reads_field = path[0], len(path) > 1
            if target == node_id and context == "node":
                return  # a node's own workspace exists while its templates render
        elif name == "results" and path and path[0] in self.nodes:  # an empty list until the node runs
            target, reads_field = path[0], len(path) > 1 and path[1] is not EACH  # a loop over it then reads nothing
        else:
            return
        if not reads_field:
            return  # reading a node that hasn't run just gives none
        ran = self.order.at_end() if context == "end" else self.order.before(node_id) | ({node_id} if context == "when" else set())
        outputs = target if name == "results" else name  # a follow-up clears outputs and results, not workspaces
        cleared = target in ran and self.follow_ups.clears(outputs, target, node_id, context)
        why = " in a follow-up, which clears the outputs of the nodes that `follow_up.to` can lead to" if cleared else ""
        ran = set() if cleared else ran
        if (target in ran or target in ref.guarded or f"workspace.{target}" in ref.guarded or f"results.{target}" in ref.guarded
                or (where, target) in self._warned):
            return
        self._warned.add((where, target))
        self.warn(where, f"reads `{_dotted(name, path)}`, but {_not_yet(target)}{why}; check first with "
                         f"{{% if {target} %}}...{{% endif %}}, or in a single value `{_dotted(name, path)} if {target} else none`"
                         + (" (fails the run if it hasn't)" if context == "end" else ""))

    def _failed_item(self, where: str, ref: Ref) -> None:
        """A field of an entry's output that the template reads at a position of `results.<node>` itself,
        as results.build[0].output.done, from a node with on_item_error: continue, where no check of that
        output comes first: an item that failed has the output none, and the template then fails.
        A read through a name or a filter isn't checked."""
        path = ref.path or ()
        if ref.name != "results" or ref.local or len(path) < 4 or path[0] not in self.nodes or path[2] != "output":
            return
        entry = templates.guard(ref.name, path[:3])
        if self.wf.setting(path[0], "on_item_error") != "continue" or entry in ref.guarded or (where, entry) in self._warned:
            return
        self._warned.add((where, entry))
        self.warn(where, f"reads `{_dotted(ref.name, path)}`, but with `on_item_error: continue` an item of `{path[0]}` "
                         "that failed has the output none: check that the output is set first, with {% if ... %} "
                         "or `... if ... else none`")

    def _for_each(self, node_id: str) -> None:
        expression = self.nodes[node_id].for_each
        where = f"nodes.{node_id}.for_each"
        if self._template(where, expression, node_id, "for_each", expression=True) is None:
            return
        is_constant, value = templates.constant(expression)
        if is_constant:
            if not isinstance(value, list):
                self.error(where, "for_each must produce a list")
            return
        path = templates.pure_path(expression, expression=True)
        schema = self._resolve(path, node_id) if path else None
        kinds = schemas.types_of(schema) if schema else None
        if kinds is None:
            return
        if "array" not in kinds:
            self.error(where, f"for_each must produce a list, but `{_dotted(*path)}` is {_kinds(kinds, article=True)}")
        elif "maxItems" not in schema:
            limit = self.wf.setting(node_id, "max_items")
            self.warn(where, f"`{_dotted(*path)}` has no `maxItems`, so a list longer than {limit} fails the node at run time; "
                             "add maxItems to the schema that produces it, and the agent learns the limit")

    def _item_schema(self, node_id: str, roots: dict | None = None) -> dict:
        """The schema of the node's `item`, from what the template names hold (`roots`, else self.roots)."""
        expression = self.nodes[node_id].for_each
        is_constant, value = templates.constant(expression)
        if is_constant:
            simple = isinstance(value, list) and value and all(isinstance(v, (str, int, float, bool)) for v in value)
            return {"enum": value} if simple else ANY
        listed = templates.elements_of(expression)  # a plain path, also through filters that keep its elements
        path = (listed[0], listed[1][:-1]) if listed else None
        schema = self._resolve(path, None, roots) if path else None  # not node_id: for_each can't read its own `item`
        found = schemas.field_at(schema, [EACH]) if schema else ANY
        return found if isinstance(found, dict) else ANY  # a schema of true or false, as in `items: true`

    def _resolve(self, path: tuple[str, tuple], node_id: str | None = None, roots: dict | None = None) -> dict | None:
        """The schema at a (name, path), or None if it doesn't resolve."""
        name, steps = path
        if name == "item" and node_id and self.nodes[node_id].for_each is not None:
            root = self._item_schema(node_id)
        elif name == "index":
            root = {"type": "integer"}
        else:
            root = (self.roots if roots is None else roots).get(name)
        if root is None:
            return None
        try:
            return schemas.field_at(root, steps)
        except NoField:
            return None

    def _tainted(self, ref: Ref, node_id: str | None) -> bool:
        """Could this value carry text an agent (or a web page) wrote?"""
        name, path = ref.name, ref.path
        if name == "start":
            candidate = bool(self.tainted) if not path else path[0] in self.tainted
        elif name in self.nodes or name in ("history", "errors", "results", "follow_up"):  # a follow-up request is free text
            candidate = True
        elif name == "workspace" and path and path[0] in self.nodes:
            steps = [step for step in path[1:] if isinstance(step, str)]  # past a for_each node's item
            candidate = (not steps or steps[0] == "branch") and self._branch_tainted(path[0])
        elif name == "item" and node_id is not None and self.nodes[node_id].for_each is not None:
            try:
                refs = templates.references(self.nodes[node_id].for_each, expression=True)
            except TemplateError:
                return False
            candidate = any(self._tainted(r, None) for r in refs)
        else:
            return False
        if candidate and path is not None:
            schema = self._resolve((name, path), node_id)
            if schema is None:
                return False  # a field that doesn't exist: already an error
            kinds = schemas.types_of(schema)
            if kinds is not None and kinds <= _SAFE_IN_SHELL:
                return False  # validated as a number or boolean
        return candidate

    def _branch_tainted(self, node_id: str) -> bool:
        """Could the branch of this node's workspace carry text an agent wrote? Only if a `branch`
        names it from such text: wf names the others wf/<run-id>/..., and `repo` is your checkout."""
        seen = set()
        while (workspace := self.wf.setting(node_id, "workspace")) in self.nodes and node_id not in seen:
            seen.add(node_id)
            node_id = workspace  # it runs in that node's workspace
        source = self.nodes[node_id].branch if workspace == "new" else None
        if source is None or node_id in self._tainting:
            return False
        self._tainting.add(node_id)
        try:
            return any(self._tainted(ref, node_id) for ref in templates.references(source))
        except TemplateError:
            return False
        finally:
            self._tainting.discard(node_id)

    def _unquoted(self, where: str, refs: list[Ref], node_id: str) -> None:
        names = []
        for ref in refs:
            if ref.output and not ref.quoted and self._tainted(ref, node_id):
                names.append(f"{ref.name}.{ref.path[0]}" if ref.name in ("start", "workspace") and ref.path else ref.name)
        if names:
            listed = ", ".join(f"`{n}`" for n in dict.fromkeys(names))
            self.warn(where, f"puts output from {listed} into the shell unquoted; pass it through `env` and use \"$NAME\", or add `| quote`")

    def _web_to_full(self, where: str, refs: list[Ref], node_id: str) -> None:
        """An agent with `access: full` that reads what an agent with web pages or wf tools wrote."""
        if self.wf.setting(node_id, "access") != "full":
            return
        sources = []
        for ref in refs:
            target = ref.name if ref.name in self.nodes else (
                ref.path[0] if ref.name in ("history", "results") and ref.path and ref.path[0] in self.nodes else None)
            if target and self.nodes[target].kind == "agent":
                sources.append(target)
        for source in dict.fromkeys(sources):
            if self.wf.setting(source, "web"):
                self.warn(where, f"an agent with `access: full` reads output from `{source}`, which has `web: true`; "
                                 "web pages can carry instructions aimed at agents")
            if tools := [use.name for use in self.wf.setting(source, "tools") or ()]:
                self.warn(where, f"an agent with `access: full` reads output from `{source}`, which reads {', '.join(tools)}; "
                                 "messages, notes and mail that other people write can carry instructions aimed at agents")

    def _harness(self, node_id: str) -> set[str]:
        """Check the node's harness; returns the harnesses it can run on, as far as validate can tell."""
        node, at = self.nodes[node_id], f"nodes.{node_id}"
        harness = self.wf.setting(node_id, "harness")
        if harness is None:
            self.error(at, "no harness: set `harness` here or `defaults.harness`")
            return set()
        where = f"{at}.harness" if node.harness is not None else "defaults.harness"
        choices = ", ".join(HARNESSES)
        possible: set[str] = set()
        if isinstance(harness, list):
            for name in harness:
                if name in HARNESSES:
                    possible.add(name)
                else:
                    self.error(where, f"unknown harness `{name}` (choose from {choices})")
            for key in ("model", "effort"):
                if isinstance(getattr(node, key), str):
                    self.error(f"{at}.{key}", f"`harness` is a list, so `{key}` must be a map by harness, like {{claude-code: ..., codex: ...}}")
        elif "{{" in harness or "{%" in harness:
            self._template(where, harness, node_id, "node")
            if templates.pure_path(harness) == ("item", ()) and node.for_each is not None:
                is_constant, value = templates.constant(node.for_each)
                for name in value if is_constant and isinstance(value, list) else []:
                    if name in HARNESSES:
                        possible.add(name)
                    else:
                        self.error(f"{at}.for_each", f"unknown harness `{name}` (choose from {choices})")
        elif harness in HARNESSES:
            possible.add(harness)
        else:
            self.error(where, f"unknown harness `{harness}` (choose from {choices})")
        if "cursor" in possible and self.wf.harness_setting(node_id, "cursor", "model") is None:
            self.error(at, "this node can run on cursor, which needs a `model` (set `model` here, or defaults.harnesses.cursor.model)")
        if "cursor" in possible and self.wf.harness_setting(node_id, "cursor", "effort") is not None:
            self.warn(at, "cursor doesn't use `effort`: a Cursor model id carries its own, like gpt-5.6-sol-max-fast")
        self.harnesses |= possible
        self.no_tokens |= {name for name in possible if not harnesses.reports_tokens(name, self.wf.setting(node_id, "access"))}
        if not possible and isinstance(harness, str) and ("{{" in harness or "{%" in harness):
            possible = self._named_by(harness, node_id)
        self._model_and_effort(node_id, possible or set(HARNESSES))
        return possible

    def _named_by(self, harness: str, node_id: str) -> set[str]:
        """The harnesses a template like "{{ item.harness }}" can give, from its field's enum; none known otherwise."""
        path = templates.pure_path(harness)
        schema = self._resolve(path, node_id) if path else None
        if not schema:
            return set()
        names = [schema["const"]] if "const" in schema else schema.get("enum")
        return {name for name in names if name in HARNESSES} if isinstance(names, list) else set()

    def _model_and_effort(self, node_id: str, harnesses: set[str]) -> None:
        """model and effort are templates: check every value the node sets, and each default its harnesses read."""
        node, settings = self.nodes[node_id], set()
        for key in ("model", "effort"):
            own = getattr(node, key)
            if isinstance(own, dict):
                settings |= {(f"nodes.{node_id}.{key}.{name}", value) for name, value in own.items()}
            elif own is not None:
                settings.add((f"nodes.{node_id}.{key}", own))
            settings |= {self.wf.harness_setting_at(node_id, name, key) for name in harnesses}
        for where, value in sorted(settings, key=lambda setting: setting[0] or ""):
            if not isinstance(value, str):
                continue
            refs = self._template(where, value, node_id, "node") or []
            if any(ref.name == "session" for ref in refs):
                self.error(where, "`session` isn't available here: the model and effort are chosen before the session starts")

    def _tools(self, node_id: str, possible: set[str]) -> None:
        """The node's own `tools` (defaults.tools are checked once, in run()), and whether its agent
        could have them with a shell that reaches the Keychain, where every sign-in is stored."""
        node, at = self.nodes[node_id], f"nodes.{node_id}"
        if node.tools is not None:
            self._tool_list(f"{at}.tools", node.tools)
        uses, access = self.wf.setting(node_id, "tools") or [], self.wf.setting(node_id, "access")
        reaching = " and ".join(sorted(name for name in possible if harnesses.reaches_keychain(name, access)))
        if not uses or not reaching:
            return
        listed = ", ".join(use.name for use in uses)
        if node.tools_with_shell:
            self.warn(at, f"`tools_with_shell: true`: the agent of `{node_id}` has {listed}, and its commands on {reaching} "
                          f"at `{access}` access could read every stored sign-in")
        else:
            self.error(at, f"this node has {listed}, and can run on {reaching} at `{access}` access, where its commands could "
                           "read every stored sign-in: give the tools to a read-only node, or one on Codex at `edit`, "
                           "or set `tools_with_shell: true` to accept that")

    def _project_instructions(self, node_id: str, possible: set[str]) -> None:
        """`project_instructions: false` where a harness still loads some instruction file: once per
        harness, where the setting is. A harness a template picks could be any of them."""
        if self.wf.setting(node_id, "project_instructions"):
            return
        harness = self.wf.setting(node_id, "harness")
        if not possible and isinstance(harness, str) and ("{{" in harness or "{%" in harness):
            possible = set(HARNESSES)
        own = self.nodes[node_id].project_instructions is not None
        where = f"nodes.{node_id}.project_instructions" if own else "defaults.project_instructions"
        access = self.wf.setting(node_id, "access")
        for name in sorted(possible & harnesses.KEPT_INSTRUCTIONS.keys()):
            kept = harnesses.kept_instructions(name, access)
            if kept and (where, name) not in self._warned:
                self._warned.add((where, name))
                label = "cursor through cursor-agent" if name == "cursor" else name
                self.warn(where, f"on {label}, `project_instructions: false` doesn't keep out {kept}")

    def _tool_list(self, where: str, uses: list) -> None:
        seen = set()
        for i, use in enumerate(uses):
            if use.name not in TOOLS:
                self.error(f"{where}[{i}]", f"unknown tool `{use.name}` (choose from {', '.join(TOOLS)}){_hint(use.name, TOOLS)}")
            elif use.name in seen:
                self.error(f"{where}[{i}]", f"`{use.name}` is listed twice")
            else:
                seen.add(use.name)
                self._set_up(where, use.name, use.write)

    def _set_up(self, where: str, name: str, write: bool) -> None:
        try:
            stored = keychain.load(name)
        except keychain.Unavailable as error:
            self.warn(where, f"{error}, so wf can't tell whether `{name}` is set up")
            return
        if stored is None:
            self.warn(where, f"`{name}` isn't set up on this machine, so the node fails when it runs: run `wf tools {name} setup`")
        elif write and not keychain.can_write(stored):
            self.warn(where, f"`{name}` is set up to read only, so a node that writes with it fails when it runs: "
                             f"run `wf tools {name} setup --write`")

    def _workspace(self, node_id: str) -> None:
        workspace = self.wf.setting(node_id, "workspace")
        if workspace in ("repo", "new", "temp"):
            return
        where = f"nodes.{node_id}.workspace"
        if workspace not in self.nodes:
            self.error(where, f"unknown workspace `{workspace}`: use repo, new, temp or a node id")
        elif workspace == node_id:
            self.error(where, "a node can't use its own workspace; use `new` for a worktree of its own")
        elif self.nodes[workspace].for_each is not None:
            self.error(where, f"`{workspace}` is a for_each node, which has a workspace per item")
        elif workspace not in self.order.before(node_id):
            self.warn(where, f"`{workspace}` might not have run before this node, so its workspace might not exist yet")

    def _with(self, node_id: str) -> None:
        result = self.subs.get(node_id)
        if result is None or result.workflow is None:
            return
        sub, given = result.workflow, self.nodes[node_id].with_
        inputs, at = sub.start.input, f"nodes.{node_id}.with"
        for key, input_field in inputs.items():
            if not input_field.get("optional") and "default" not in input_field and key not in given:
                self.error(at, f"missing required input `{key}`")
        for key, value in given.items():
            if key not in inputs:
                self.error(f"{at}.{key}", f"{sub.name} has no input `{key}` (inputs: {', '.join(inputs) or 'none'})")
                continue
            try:
                wanted = schemas.types_of(schemas.object_schema({key: inputs[key]}, sub.types)["properties"][key])
            except Exception:
                continue
            got = self._value_types(value, node_id)
            if wanted and got and not wanted & got:
                self.error(f"{at}.{key}", f"`{key}` expects {_kinds(wanted)}, but this gives {_kinds(got)}")

    def _value_types(self, value, node_id: str) -> set[str] | None:
        """The JSON types a `with` value or end value can produce, if we can tell."""
        if not isinstance(value, str):
            return schemas.types_of({"const": value})
        path = templates.pure_path(value)
        if path:
            schema = self._resolve(path, node_id)
            return schemas.types_of(schema) if schema else None
        return None if templates.single_expression(value) is not None else {"string"}

    # ---------- 5. branch names, end outputs and whole-graph warnings ----------

    def _branches(self) -> None:
        """`branch` names a `workspace: new` worktree's branch: one per item, never `wf` or under
        wf/, and never one that git can't have beside another node's, in this file or in a
        sub-workflow it runs."""
        for node_id, node in self.nodes.items():
            if node.branch is None:
                continue
            where, workspace = f"nodes.{node_id}.branch", self.wf.setting(node_id, "workspace")
            if workspace != "new":
                self.error(where, f"`branch` names a `workspace: new` worktree's branch, but this node's workspace is `{workspace}`")
                continue
            try:
                refs, pattern = templates.references(node.branch), _folded(templates.pattern(node.branch))
            except TemplateError:
                continue  # reported with the node's other templates
            if node.for_each is not None and not any(r.name in ("item", "index") for r in refs):
                self.error(where, "a for_each node makes a worktree per item, so `branch` must use `item` or `index`")
            if any(r.name == "workspace" and r.path and r.path[0] == node_id for r in refs):
                self.error(where, "`branch` renders before this node's worktree exists, so it can't read its workspace")
            fixed = "".join(t for t in itertools.takewhile(lambda t: isinstance(t, str), pattern))
            if not pattern:
                self.error(where, "`branch` is empty: name a branch, or leave `branch` out for wf/<run-id>/...")
                continue
            if fixed == "wf" and len(fixed) == len(pattern) or fixed.startswith("wf/"):
                self.error(where, "wf and wf/... are where wf names branches itself (wf/<run-id>/...); pick another name")
            self.branches.append(_Branch(node_id, pattern, self._inputs_read(refs, node_id), where))
        for node_id, sub in self.subs.items():
            self.branches += self._lifted(node_id, sub)
        for i, b in enumerate(self.branches):
            for a in self.branches[:i]:
                if (a.via is None or a.via != b.via) and _clash(a.pattern, b.pattern):  # one sub-workflow's: checked there
                    self.error(b.where, f"`{a.label}` and `{b.label}` could name the same branch, or one under the other "
                                        "(fix/a and fix/a/b, which git can't have both of): make their fixed text differ, "
                                        "like fix/... and review/..., or name it on one node and run the other in its workspace")
                    break

    def _lifted(self, node_id: str, sub: _Result) -> list["_Branch"]:
        """The names a sub-workflow's `branch` fields give, as this file sees them: with what `with`
        passes in for {{ start.<input> }}, and reading this file's inputs through `with`."""
        node, found = self.nodes[node_id], []
        for b in sub.branches:
            pattern, reads, per_item = [], set(), False
            for token in b.pattern:
                given = node.with_.get(token.input) if isinstance(token, templates.Hole) else None
                if not isinstance(token, templates.Hole):
                    pattern.append(token)
                elif isinstance(given, str):
                    try:
                        pattern += templates.pattern(given)
                    except TemplateError:
                        pattern.append(templates.Hole())
                else:
                    pattern.append(templates.Hole())  # the sub-workflow's own input: no input of this file
            for name in b.reads:
                given = node.with_.get(name)
                try:
                    refs = templates.references(given) if isinstance(given, str) else []
                except TemplateError:
                    continue
                reads |= self._inputs_read(refs, node_id)
                per_item |= any(r.name in ("item", "index") for r in refs)
            if node.for_each is not None and not per_item:
                self.error(f"nodes.{node_id}.with", f"`{node_id}` runs {node.workflow} once per item, and its `{b.label}` "
                                                    "would name the same branch every time: give that node's `branch` "
                                                    "an input that `with` fills from `item` or `index`")
            found.append(_Branch(f"{node_id}/{b.label}", _folded(pattern), frozenset(reads), f"nodes.{node_id}", node_id))
        return found

    def _inputs_read(self, refs: list[Ref], node_id: str) -> frozenset[str]:
        """The start inputs these refs of a node read, and the ones its for_each list reads if they
        read `item` or `index`: the inputs a `branch` name depends on."""
        node = self.nodes[node_id]
        if node.for_each is not None and any(r.name in ("item", "index") for r in refs):
            try:
                refs = [*refs, *templates.references(node.for_each, expression=True)]
            except TemplateError:
                pass
        if any(r.name == "start" and not r.path for r in refs):
            return frozenset(self.wf.start.input)
        return frozenset(r.path[0] for r in refs if r.name == "start" and isinstance(r.path[0], str))

    def _end(self) -> None:
        for name, value in self.wf.end.output.items():
            where = f"end.output.{name}"
            if isinstance(value, dict):
                schema = {k: v for k, v in value.items() if k != "value"}
                try:
                    self.end_fields[name] = schemas.object_schema({name: schema}, self.wf.types)["properties"][name]
                except Exception:
                    self.end_fields[name] = ANY
                if isinstance(value.get("value"), str):
                    self._template(f"{where}.value", value["value"], None, "end")
                continue
            path = templates.pure_path(value)
            if path is None or not self._short_form_ok(*path):
                self.error(where, 'the short form must be a single field reference like "{{ node.field }}"; '
                                  "for anything else use the full form, {type: ..., value: ...}")
                self._template(where, value, None, "end")
                self.end_fields[name] = ANY
                continue
            self._template(where, value, None, "end")
            self.end_fields[name] = self._resolve(path) or ANY

    def _short_form_ok(self, name: str, steps: tuple) -> bool:
        if name in self.nodes:
            return bool(steps) and self.nodes[name].for_each is None
        return {"start": 1, "run": 1, "workspace": 2}.get(name, 99) <= len(steps)

    def _concurrent_edits(self) -> None:
        editors = [n for n, node in self.nodes.items()
                   if node.kind == "agent" and self.wf.setting(n, "access") in ("edit", "full")]

        def key(node_id: str, seen: tuple = ()) -> str | None:
            workspace = self.wf.setting(node_id, "workspace")
            if workspace in ("repo", "new", "temp"):
                return {"repo": "repo", "new": node_id, "temp": node_id}[workspace]  # temp is shared by name too
            if workspace in self.nodes and workspace not in seen and workspace != node_id:
                return key(workspace, (*seen, node_id))
            return None

        for i, b in enumerate(editors):
            shared = key(b)
            if shared is None:
                continue
            for a in editors[:i]:
                if key(a) == shared and not self._ordered(a, b):
                    label = "repo" if shared == "repo" else f"`{shared}`"
                    self.warn(f"nodes.{b}", f"`{a}` and `{b}` both edit the {label} workspace and could run at the same time")
            if (self.nodes[b].for_each is not None and self.wf.setting(b, "workspace") not in ("new", "temp")
                    and self.wf.setting(b, "max_parallel") > 1):  # new and temp give each item its own folder
                self.warn(f"nodes.{b}", "items run in parallel in the same workspace and each edits it; "
                                        "use `workspace: new` or `max_parallel: 1`")

    def _ordered(self, a: str, b: str) -> bool:
        """Is there a path from one to the other, so they can't overlap?"""
        return b in self.topo.reachable(self.topo.successors.get(a, ())) or a in self.topo.reachable(self.topo.successors.get(b, ()))

    def _usage(self) -> None:
        """Warn where a limit or a template counts what a harness here doesn't report."""
        silent = sorted(self.harnesses & {"codex", "cursor"})  # no dollars
        where = "limits.cost_usd" if self.wf.limits.cost_usd is not None else next(iter(self.cost_refs), None)
        if silent and where:
            verb = "doesn't" if len(silent) == 1 else "don't"
            hint = "limits.tokens counts Codex, " if "codex" in silent else ""
            self.warn(where, f"{' and '.join(silent)} {verb} report cost in dollars, so spending there isn't counted; "
                             f"{hint}limits.duration caps every harness")
        where = "limits.tokens" if self.wf.limits.tokens is not None else next(iter(self.token_refs), None)
        if "cursor" in self.no_tokens and where:
            self.warn(where, "cursor doesn't report tokens through cursor-agent's ACP, which runs its `edit` nodes "
                             "(and every node when cursor.runtime is acp, or with no Cursor API key set), "
                             "so that spending isn't counted; limits.duration caps every harness")


class _Order:
    """Which nodes are certain to have finished at a given point of a run."""

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
        self.dominators = _dominators([START, *topo.order, END], triggers, topo.reachable([START]) | {END})
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


def _dominators(nodes: list[str], triggers: dict[str, set[str]], reached: set[str]) -> dict[str, set[str]]:
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


def _cycles(graph: dict[str, tuple | list]) -> list[list[str]]:
    """Cycles in a graph, each as [a, b, ..., a]; a node is reported in at most one."""
    found, reported, state = [], set(), {}

    def visit(node: str, path: list[str]) -> None:
        state[node] = "open"
        path.append(node)
        for nxt in graph.get(node, ()):
            if nxt not in graph:
                continue
            if state.get(nxt) == "open":
                cycle = path[path.index(nxt):] + [nxt]
                if not reported.intersection(cycle):
                    found.append(cycle)
                    reported.update(cycle)
            elif nxt not in state:
                visit(nxt, path)
        path.pop()
        state[node] = "done"

    for node in graph:
        if node not in state:
            visit(node, [])
    return found


@dataclass(frozen=True)
class _Branch:
    """A name a `branch` field gives, as one file sees it."""

    label: str  # the node's path, e.g. impl, or each/impl in the sub-workflow `each` runs
    pattern: tuple  # see _folded
    reads: frozenset[str]  # the start inputs of this file the name depends on
    where: str  # where a problem with it is reported
    via: str | None = None  # the workflow node it comes through, if it's a sub-workflow's


def _folded(pattern) -> tuple:
    """A templates.pattern() ready to compare: each run of fixed text stripped, since the name is
    stripped and can't hold whitespace, and casefolded on every machine, since git ignores case
    on macOS and Windows and a workflow file that validates here may run there."""
    found, text = [], ""
    for token in (*pattern, None):
        if isinstance(token, str):
            text += token
            continue
        found += text.strip().casefold()
        text = ""
        if token is not None:
            found.append(token)
    return tuple(found)


def _clash(a: tuple, b: tuple) -> bool:
    """Could two `branch` patterns (_folded) name the same branch, or one under the other, like
    fix/a and fix/a/b? A Hole stands for any text without a `/`: an input that holds one is
    caught when the run makes the branch."""
    return _meet(a, b) or _meet((*a, "/"), b, prefix=True) or _meet((*b, "/"), a, prefix=True)


def _meet(p: tuple, q: tuple, prefix: bool = False) -> bool:
    """Is there a name p can give that q can give too (with `prefix`: that a name q gives starts with)?"""

    @functools.cache
    def at(i: int, j: int) -> bool:
        if i == len(p):
            return prefix or all(isinstance(t, templates.Hole) for t in q[j:])
        if j == len(q):
            return all(isinstance(t, templates.Hole) for t in p[i:])
        x, y = p[i], q[j]
        if isinstance(x, templates.Hole):  # x writes nothing more, or y's next character too
            return at(i + 1, j) or (y != "/" and at(i, j + 1))
        if isinstance(y, templates.Hole):
            return at(i, j + 1) or (x != "/" and at(i + 1, j))
        return x == y and at(i + 1, j + 1)

    return at(0, 0)


def _hint(name: str, options) -> str:
    close = difflib.get_close_matches(name, list(options), n=1)
    return f" (did you mean `{close[0]}`?)" if close else ""


def _dotted(name: str, path: tuple | None) -> str:
    text = name
    for step in path or ():
        text += "[]" if step is EACH else f"[{step}]" if isinstance(step, (int, templates.At)) else f".{step}"
    return text


def _kinds(kinds: set[str], article: bool = False) -> str:
    names = sorted(k for k in kinds if not (k == "number" and "integer" in kinds))
    text = " or ".join(names) or "nothing"
    return f"{'an' if text[0] in 'aeiou' else 'a'} {text}" if article else text


# ---------- follow-ups ----------


_FOLLOW_UP = schemas.object_schema({"request": {"type": "string"}, "number": {"type": "integer"}})


def _not_yet(target: str) -> str:
    """Why a template may find `target` none, for a warning."""
    if target == "follow_up":
        return "`follow_up` is none until `wf continue` gives the run a request"
    return f"`{target}` might not have run yet"


class _FollowUps:
    """The checks of a workflow's `follow_up` (`wf continue`). It names nodes of the workflow, and a node that
    a follow-up runs again reads `follow_up`. No template reads an output that a follow-up clears before it.
    `order` is the follow-up's _FollowUpOrder, or None without a good `follow_up`."""

    def __init__(self, checker: "_Checker", order: _Order):
        self.checker, self.order = checker, None
        if checker.wf.follow_up is None:
            return
        targets = checker.wf.follow_up.targets
        for target in targets:
            if target in (START, END):
                checker.error("follow_up.to", f"a follow-up starts at nodes of the workflow, not at `{target}`")
            elif target not in checker.nodes:
                checker.error("follow_up.to", f"unknown node `{target}`{_hint(target, checker.nodes)}")
        if not all(t in checker.nodes for t in targets):
            return
        lists = frozenset(n for n, node in checker.wf.nodes.items() if node.for_each is not None)
        self.order = _FollowUpOrder(checker.topo, targets, frozenset(order.at_end()), lists)
        if not any(_reads_follow_up(checker.wf, node) for node in self.order.ahead):
            checker.warn("follow_up", "no node that a follow-up runs again reads `follow_up`, so its request reaches "
                                      "no node; put {{ follow_up.request }} in the instruction of a node that "
                                      "`follow_up.to` starts, inside {% if follow_up %}...{% endif %}")

    def clears(self, name: str, target: str, node_id: str | None, context: str) -> bool:
        """Can a follow-up clear the output of `target` before the template of `node_id` reads it?"""
        order = self.order
        if order is None or name not in self.checker.nodes or (context != "end" and node_id not in order.ahead):
            return False
        ran = order.at_end() if context == "end" else order.before(node_id) | ({node_id} if context == "when" else set())
        return target not in ran


def _reads_follow_up(workflow: Workflow, node_id: str) -> bool:
    """Does a template or expression of the node read `follow_up`?"""
    node = workflow.nodes[node_id]
    texts = [(text, False) for text in (getattr(node, "instruction", None), getattr(node, "command", None),
                                        getattr(node, "human", None), node.base, node.branch) if isinstance(text, str)]
    texts += [(text, False) for text in workflow.setting(node_id, "env").values()] if node.kind == "command" else []
    texts += [(text, False) for text in node.with_.values() if isinstance(text, str)] if node.kind == "workflow" else []
    for name in ("harness", "model", "effort"):  # a string, a list of harnesses, or a map by harness
        value = getattr(node, name, None)
        texts += [(text, False) for text in (value.values() if isinstance(value, dict) else [value]) if isinstance(text, str)]
    texts += [(node.for_each, True)] if node.for_each is not None else []
    texts += [(route.when, True) for route in node.routes if route.when is not None]
    for text, expression in texts:
        try:
            if any(ref.name == "follow_up" for ref in templates.references(text, expression=expression)):
                return True
        except TemplateError:
            continue  # reported where the node is checked
    return False


class _FollowUpOrder(_Order):
    """_Order for a follow-up, which starts at the nodes in `entry` instead of at start. The nodes they can
    lead to (`ahead`) run again from none. Of the others, only `kept`, the nodes certain to have run by the
    end of the run, count as finished. Which nodes of `ahead` have finished at a point comes from stepping
    the scheduler through the follow-up (_follow_up_steps). The nodes in `lists` have `for_each`."""

    def __init__(self, topo: Topology, entry: tuple[str, ...], kept: frozenset[str], lists: frozenset[str]):
        super().__init__(topo)
        self.entry, self.ahead = set(entry), topo.reachable(entry)
        self.kept = kept - self.ahead
        triggers = {n: (set(topo.needs[n]) | set(self.route_sources[n])) & self.ahead for n in topo.order}
        for node in self.entry:
            triggers[node].add(START)
        triggers[END] = {n for n in self.ahead
                         if any(END in r.targets for r in topo.routes[n]) or not (topo.routes[n] or topo.dependents.get(n))}
        self.dominators = _dominators([START, *topo.order, END], triggers, self.ahead | {START, END})
        self.stepped = _follow_up_steps(topo, entry, lists)  # None: too many ways to step through

    def before(self, node: str) -> set[str]:
        if node not in self._before:
            if self.stepped is None:
                found = self.dominators[node] - {node, START}  # a follow-up's start ended long ago
            else:
                found = self.stepped.started.get(node, frozenset(self.topo.order))  # a node that never starts reads nothing
            self._before[node] = set(found) | self.kept
        return self._before[node]

    def at_end(self) -> set[str]:
        if self.stepped is None:
            return self._certain((self.dominators[END] - {END, START}) | self.entry, excluding=None) | self.kept
        return set(self.stepped.ended) | self.kept


@dataclass(frozen=True)
class _Stepped:
    started: dict[str, frozenset[str]]  # node -> the nodes that finished in the follow-up before each of its starts
    ended: frozenset[str]  # the nodes that finished in the follow-up before each end of it


_STEPS_MOST = 100_000


def _follow_up_steps(topo: Topology, entry: tuple[str, ...], lists: frozenset[str]) -> _Stepped | None:
    """Step the scheduler through a follow-up in each way it can go, as graph.schedule and state.Progress do.
    None when that takes more than _STEPS_MOST steps, as with more than about ten nodes that run at once.

    A step records the results of the jobs that the step before started, in their order. Then it starts
    what can start (Progress.starts_now). A job can also fail or stop at a limit. Then `wf retry` runs it
    again, before the new jobs (Progress.retry). So each step finishes some of the running nodes, and the
    others go on. A route can be taken while its `max` isn't used up. A `for_each` list can be empty, and
    then its node finishes as it starts.

    A state is (pending, running, taken, ready), as the scheduler sees it. `ready` holds the nodes that
    starts_now has yet to start, or is None between steps. Each state is stepped once. The nodes that
    finished on every way to a state are found again for each new way, until they don't change."""
    first = (tuple(scheduler.add_pending(topo, [], entry)), (), frozenset(), ())
    ways: dict[tuple, list] = {}  # state -> where it can go: what finished and started on the way, and the state after
    finished = {first: frozenset()}  # state -> the nodes that finished on every way to it
    todo, count = [first], 0
    while todo:
        state = todo.pop()
        if state not in ways:
            ways[state] = []
            for way in _ways(topo, lists, *state):
                ways[state].append(way)
                count += 1
                if count > _STEPS_MOST:
                    return None
        for events, after in ways[state]:
            new = finished[state] | {node for kind, node in events if kind == "finished"}
            if after not in finished or not finished[after] <= new:
                finished[after] = new if after not in finished else finished[after] & new
                todo.append(after)
    started: dict[str, frozenset[str]] = {}
    ended = None
    for state, steps in ways.items():
        if state[3] is None and not state[0] and not state[1]:
            ended = finished[state] if ended is None else ended & finished[state]
        for events, _ in steps:
            done = set(finished[state])
            for kind, node in events:
                if kind == "finished":
                    done.add(node)
                else:
                    started[node] = started[node] & done if node in started else frozenset(done)
    return _Stepped(started, ended or frozenset())


def _ways(topo: Topology, lists: frozenset[str], pending: tuple, running: tuple, taken: frozenset, ready: tuple | None):
    """Where a state can go: (("finished" or "started", node), ...) in order, and the state after."""
    if ready is None:  # between steps: the results of some of the running jobs come in (Progress.record)
        for size in range(1, len(running) + 1):
            for done in itertools.combinations(running, size):
                going_on = tuple(n for n in running if n not in done)
                for events, now_pending, now_taken in _finishing(topo, pending, taken, done):
                    yield events, (now_pending, going_on, now_taken, ())
    elif not ready:  # Progress.starts_now asks what starts now, and the step ends when nothing does
        yield (), (pending, running, taken, tuple(scheduler.starts(topo, list(pending), list(running))) or None)
    else:
        node, rest = ready[0], ready[1:]
        left = tuple(n for n in pending if n != node)
        yield (("started", node),), (left, (*running, node), taken, rest)
        if node in lists:  # an empty list: the node finishes as it starts
            for now_pending, now_taken in _finish(topo, node, left, taken):
                yield (("started", node), ("finished", node)), (now_pending, running, now_taken, rest)


def _finishing(topo: Topology, pending: tuple, taken: frozenset, nodes: tuple) -> list:
    """Each way `nodes` can finish in turn (Progress.record): what happened, the pending nodes, the routes taken."""
    found = [((), pending, taken)]
    for node in nodes:
        found = [((*events, ("finished", node)), now_pending, now_taken)
                 for events, before, before_taken in found for now_pending, now_taken in _finish(topo, node, before, before_taken)]
    return found


def _finish(topo: Topology, node: str, pending: tuple, taken: frozenset):
    """Each way `node` can finish (Progress._complete): with each route it can take, when it has routes."""
    routes = topo.routes.get(node)
    if not routes:
        yield tuple(scheduler.add_pending(topo, list(pending), scheduler.triggered(topo, node, None))), taken
        return
    counts = dict(taken)
    for index, route in enumerate(routes):
        count = counts.get((node, index), 0)
        if scheduler.used_up(route, count):
            continue
        yield (tuple(scheduler.add_pending(topo, list(pending), scheduler.triggered(topo, node, index))),
               frozenset({**counts, (node, index): count + 1}.items()) if route.max is not None else taken)
        if route.when is None:  # the routes after it are never checked
            return
