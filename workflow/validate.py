"""Validate: the checks in the spec's Validation section, run before anything starts.

check(path) loads a workflow file and every sub-workflow it uses, and returns a
Report: errors (wf run refuses to start) and warnings (it starts, but something
looks risky). Nothing here runs a node.

The checks run in this order, because later ones lean on earlier ones:
1. structure: node ids, `needs` and route targets, reachability, loops
2. schemas: `types`, start.input, node outputs, end output schemas
3. sub-workflows: each is checked too (its problems are reported with its
   own file name), and its end output becomes the parent node's output schema
4. per node: templates and expressions, for_each, harness, workspace, `with`
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
from dataclasses import dataclass, field, replace
from pathlib import Path

from . import schemas, spec, templates
from .scheduler import END, START, Topology
from .schemas import ANY, EACH, NoField
from .spec import HARNESSES, NODE_ID, RESERVED, Problem, SpecError, Workflow
from .templates import Ref, TemplateError

_WORKSPACE = schemas.object_schema(
    {"path": {"type": "string"}, "branch": {"type": ["string", "null"]}, "base": {"type": ["string", "null"]}}
)
_ERROR_ITEM = schemas.object_schema({"index": {"type": "integer"}, "item": {}, "error": {"type": "string"}})
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

    @property
    def errors(self) -> list[Problem]:
        return [p for p in self.problems if p.level == "error"]

    @property
    def warnings(self) -> list[Problem]:
        return [p for p in self.problems if p.level == "warning"]


def check(path: Path | str) -> Report:
    """Check a workflow file and every sub-workflow it uses."""
    result = _check(Path(path).resolve(), (), frozenset(), {})
    return Report(list(dict.fromkeys(result.problems)), result.workflow, list(dict.fromkeys(result.files)))


@dataclass
class _Result:
    problems: list[Problem]
    workflow: Workflow | None = None
    end_schema: dict = field(default_factory=dict)  # what a parent node gets as output
    harnesses: set[str] = field(default_factory=set)  # every harness an agent here could run on
    files: list[Path] = field(default_factory=list)  # this file and its sub-workflows
    worktrees: bool = False  # does it (or a sub-workflow) create worktrees with `workspace: new`?


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
        self.cost_refs: list[str] = []  # where templates read run.cost_usd
        self.end_fields: dict[str, dict] = {}
        self._warned: set = set()

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
        self.order = _Order(self.topo)
        for node_id in self.nodes:
            self._node(node_id)
        self._branches()
        self._end()
        self._concurrent_edits()
        self._cost()
        end_schema = {
            "type": "object", "properties": self.end_fields,
            "required": list(self.end_fields), "additionalProperties": False,
        }
        files = [self.path] + [f for sub in self.subs.values() for f in sub.files]
        worktrees = any(self.wf.setting(n, "workspace") == "new" for n in self.nodes) or any(s.worktrees for s in self.subs.values())
        return _Result(self.problems, self.wf, end_schema, self.harnesses, files, worktrees)

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
        return {
            **outputs,
            "start": start,
            "history": fields({n: {"type": "array", "items": s} for n, s in outputs.items()}),
            "workspace": fields({n: per_pass(n, _WORKSPACE) for n in self.nodes}),
            "errors": fields({n: {"type": "array", "items": _ERROR_ITEM} for n in self.nodes}),
            "run": _RUN,
            "session": _SESSION,
        }

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
            self._harness(node_id)
        if node.kind == "workflow":
            self._with(node_id)
        for i, route in enumerate(node.routes):
            if route.when is not None:
                self._template(f"{at}.routes[{i}].when", route.when, node_id, "when", expression=True)
        self._workspace(node_id)

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
        self._might_not_have_run(where, ref, node_id, context)

    def _might_not_have_run(self, where: str, ref: Ref, node_id: str | None, context: str) -> None:
        name, path = ref.name, ref.path
        if name in self.nodes:
            target, reads_field = name, bool(path)
        elif name == "workspace" and path and path[0] in self.nodes:
            target, reads_field = path[0], len(path) > 1
            if target == node_id and context == "node":
                return  # a node's own workspace exists while its templates render
        else:
            return
        if not reads_field:
            return  # reading a node that hasn't run just gives none
        if context == "end":
            ran = self.order.at_end()
        else:
            ran = self.order.before(node_id) | ({node_id} if context == "when" else set())
        if target in ran or target in ref.guarded or f"workspace.{target}" in ref.guarded or (where, target) in self._warned:
            return
        self._warned.add((where, target))
        self.warn(where, f"reads `{_dotted(name, path)}`, but `{target}` might not have run yet; check first with "
                         f"{{% if {target} %}}...{{% endif %}}, or in a single value `{_dotted(name, path)} if {target} else none`"
                         + (" (fails the run if it hasn't)" if context == "end" else ""))

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

    def _item_schema(self, node_id: str) -> dict:
        expression = self.nodes[node_id].for_each
        is_constant, value = templates.constant(expression)
        if is_constant:
            simple = isinstance(value, list) and value and all(isinstance(v, (str, int, float, bool)) for v in value)
            return {"enum": value} if simple else ANY
        path = templates.pure_path(expression, expression=True)
        schema = self._resolve(path, None) if path else None  # not node_id: for_each can't read its own `item`
        return schemas.field_at(schema, [EACH]) if schema else ANY

    def _resolve(self, path: tuple[str, tuple], node_id: str | None = None) -> dict | None:
        """The schema at a (name, path), or None if it doesn't resolve."""
        name, steps = path
        if name == "item" and node_id and self.nodes[node_id].for_each is not None:
            root = self._item_schema(node_id)
        elif name == "index":
            root = {"type": "integer"}
        else:
            root = self.roots.get(name)
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
        elif name in self.nodes or name in ("history", "errors"):
            candidate = True
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

    def _unquoted(self, where: str, refs: list[Ref], node_id: str) -> None:
        names = []
        for ref in refs:
            if ref.output and not ref.quoted and self._tainted(ref, node_id):
                names.append(f"start.{ref.path[0]}" if ref.name == "start" and ref.path else ref.name)
        if names:
            listed = ", ".join(f"`{n}`" for n in dict.fromkeys(names))
            self.warn(where, f"puts output from {listed} into the shell unquoted; pass it through `env` and use \"$NAME\", or add `| quote`")

    def _web_to_full(self, where: str, refs: list[Ref], node_id: str) -> None:
        if self.wf.setting(node_id, "access") != "full":
            return
        sources = []
        for ref in refs:
            target = ref.name if ref.name in self.nodes else (
                ref.path[0] if ref.name == "history" and ref.path and ref.path[0] in self.nodes else None)
            if target and self.nodes[target].kind == "agent" and self.wf.setting(target, "web"):
                sources.append(target)
        for source in dict.fromkeys(sources):
            self.warn(where, f"an agent with `access: full` reads output from `{source}`, which has `web: true`; "
                             "web pages can carry instructions aimed at agents")

    def _harness(self, node_id: str) -> None:
        node, at = self.nodes[node_id], f"nodes.{node_id}"
        harness = self.wf.setting(node_id, "harness")
        if harness is None:
            self.error(at, "no harness: set `harness` here or `defaults.harness`")
            return
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
        self.harnesses |= possible
        if not possible and isinstance(harness, str) and ("{{" in harness or "{%" in harness):
            possible = self._named_by(harness, node_id)
        self._model_and_effort(node_id, possible or set(HARNESSES))

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
        """`branch` names a `workspace: new` worktree's branch: one per item, and never one
        that another node's `branch` could name too."""
        named: list[tuple[str, str | tuple[str, str]]] = []  # (node id, the template's fixed text)
        for node_id, node in self.nodes.items():
            if node.branch is None:
                continue
            where, workspace = f"nodes.{node_id}.branch", self.wf.setting(node_id, "workspace")
            if workspace != "new":
                self.error(where, f"`branch` names a `workspace: new` worktree's branch, but this node's workspace is `{workspace}`")
                continue
            try:
                refs, fixed = templates.references(node.branch), templates.fixed_text(node.branch)
            except TemplateError:
                continue  # reported with the node's other templates
            if node.for_each is not None and not any(r.name in ("item", "index") for r in refs):
                self.error(where, "a for_each node makes a worktree per item, so `branch` must use `item` or `index`")
            if any(r.name == "workspace" and r.path and r.path[0] == node_id for r in refs):
                self.error(where, "`branch` renders before this node's worktree exists, so it can't read its workspace")
            fixed = fixed.strip() if isinstance(fixed, str) else (fixed[0].lstrip(), fixed[1].rstrip())  # names are stripped
            if (fixed if isinstance(fixed, str) else fixed[0]).startswith("wf/"):
                self.error(where, "wf/ is where wf names branches itself (wf/<run-id>/...); pick another name")
            for other, other_fixed in named:
                if _may_match(fixed, other_fixed):
                    self.error(where, f"`{other}` and `{node_id}` could name the same branch: make their fixed text differ, "
                                      "like fix/... and review/..., or name it on one node and run the other in its workspace")
                    break
            named.append((node_id, fixed))

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

    def _cost(self) -> None:
        silent = sorted(self.harnesses & {"codex", "cursor"})
        if not silent:
            return
        if self.wf.limits.cost_usd is not None:
            where = "limits.cost_usd"
        elif self.cost_refs:
            where = self.cost_refs[0]
        else:
            return
        verb = "doesn't" if len(silent) == 1 else "don't"
        self.warn(where, f"{' and '.join(silent)} {verb} report cost in dollars, so spending there isn't counted; "
                         "limits.tokens works for every harness")


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


def _may_match(a: str | tuple[str, str], b: str | tuple[str, str]) -> bool:
    """Could two templates with this fixed text (templates.fixed_text) render the same name?"""
    if isinstance(a, str) and isinstance(b, str):
        return a == b
    if isinstance(a, str):
        a, b = b, a
    if isinstance(b, str):  # a computes the text between its ends
        return b.startswith(a[0]) and b[len(a[0]):].endswith(a[1])
    return ((a[0].startswith(b[0]) or b[0].startswith(a[0]))
            and (a[1].endswith(b[1]) or b[1].endswith(a[1])))


def _hint(name: str, options) -> str:
    close = difflib.get_close_matches(name, list(options), n=1)
    return f" (did you mean `{close[0]}`?)" if close else ""


def _dotted(name: str, path: tuple | None) -> str:
    text = name
    for step in path or ():
        text += "[]" if step is EACH else f"[{step}]" if isinstance(step, int) else f".{step}"
    return text


def _kinds(kinds: set[str], article: bool = False) -> str:
    names = sorted(k for k in kinds if not (k == "number" and "integer" in kinds))
    text = " or ".join(names) or "nothing"
    return f"{'an' if text[0] in 'aeiou' else 'a'} {text}" if article else text
