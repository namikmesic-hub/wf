"""Templates: sandboxed Jinja for the workflow's text and expressions.

Two ways text gets evaluated (see "Templates and expressions" in the spec):
- render() for templates with {{ }} and {% %}: instruction, command, human, ...
- evaluate() for expressions without braces: `when` and `for_each`.

render_value() is render() with one extra rule: a template that is exactly one
{{ expression }} returns the expression's value, so `"{{ plan.tasks }}"` stays
a list instead of becoming the string "[...]". End outputs and `with` use it.

The environment (ENV) is set up so mistakes fail loudly:
- StrictUndefined: an unknown name, or a field of none, raises instead of
  rendering as "". A typo in a template is an error, not a blank in a prompt.
- ImmutableSandboxedEnvironment: templates can't reach Python internals
  (`x.__class__`) or change values (`list.append`). Agent output flows into
  templates, so treat it as untrusted.
- Mapping keys win over dict methods: `x.items` is the output field `items`,
  not dict.items.
- trim_blocks + lstrip_blocks: a {% tag %} on its own line leaves no blank line.
- Filters: `quote` (shell quoting, new) and `tojson` (plain JSON, without the
  HTML escaping Jinja's own version does, since nothing here is HTML).

watched() runs one of those three. It also returns each variable path that the
template used, with its value, for the run's trace (trace.py). The environment
notes every field and index a template reads (getattr and getitem). Jinja looks
up each name when a template starts, also a name that only an unused branch
reads. So the code generator wraps each use of a name, and only the uses that
run are noted. Elements of a list or map that was read are known by their path
too, so `t.title` in `{% for t in plan.tasks %}` reads plan.tasks[0].title. A
list or map that the template only reached into, or looped over, isn't listed
when the reads inside it are. One that it also used itself, in a test, a filter
or the output, is.

The second half of this module reads templates without running them.
references() walks Jinja's syntax tree and lists every outside name a
template uses, with its field path (`plan.tasks[0].title` reads plan ->
tasks -> 0 -> title) and some context: is it written into the text, through
`| quote`, and which `{% if x %}` checks surround it. validate.py turns those
into the spec's checks: unknown names, missing fields, nodes that might not
have run, unquoted agent output in shell commands.
"""

import contextvars
import itertools
import json
import shlex
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from jinja2 import StrictUndefined, TemplateSyntaxError, Undefined, nodes
from jinja2.compiler import CodeGenerator
from jinja2.idtracking import VAR_LOAD_RESOLVE
from jinja2.parser import Parser
from jinja2.sandbox import ImmutableSandboxedEnvironment

from .schemas import EACH


class TemplateError(Exception):
    """A template or expression that can't be parsed or evaluated. Raised by watched(), it
    also holds `reads`: what the template read before it failed."""

    reads: list = []


class _CodeGenerator(CodeGenerator):
    """Jinja's code generator, with two changes for watched(). Each use of a name that the
    template gets from its variables goes through environment.used(name, whole, value), as
    it runs. A field or an index that the template uses itself goes through
    environment.whole(value). A use isn't whole when it only reaches inside the value (x in
    x.y), or loops over it. Then the reads inside it say what the template used."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._through: set[int] = set()  # the nodes whose value is only a way into it

    def visit_For(self, node: nodes.For, frame) -> None:
        self._through.add(id(node.iter))
        super().visit_For(node, frame)

    def visit_Getattr(self, node: nodes.Getattr, frame) -> None:
        self._through.add(id(node.node))
        self._whole(node, frame, super().visit_Getattr)

    def visit_Getitem(self, node: nodes.Getitem, frame) -> None:
        self._through.add(id(node.node))
        self._whole(node, frame, super().visit_Getitem)

    def _whole(self, node, frame, visit) -> None:
        if id(node) in self._through:
            visit(node, frame)
            return
        self.write("environment.whole(")
        visit(node, frame)
        self.write(")")

    def visit_Name(self, node: nodes.Name, frame) -> None:
        if node.ctx == "load":
            load = frame.symbols.find_load(frame.symbols.ref(node.name))
            if load is not None and load[0] == VAR_LOAD_RESOLVE:
                self.write(f"environment.used({node.name!r}, {id(node) not in self._through}, ")
                super().visit_Name(node, frame)
                self.write(")")
                return
        super().visit_Name(node, frame)


class _Environment(ImmutableSandboxedEnvironment):
    code_generator_class = _CodeGenerator

    def used(self, name: str, whole: bool, value: Any) -> Any:
        if (watch := _WATCH.get()) is not None and not isinstance(value, Undefined):
            watch.used(name, whole, value)
        return value

    def whole(self, value: Any) -> Any:
        if (watch := _WATCH.get()) is not None:
            watch.whole(value)
        return value

    def getattr(self, obj: Any, attribute: str) -> Any:
        # `x.items` should read the output field `items`, not the dict method
        if isinstance(obj, Mapping):
            try:
                value = obj[attribute]
            except (KeyError, TypeError):
                pass
            else:
                if (watch := _WATCH.get()) is not None:
                    watch.accessed(obj, attribute, value)
                return value
        return super().getattr(obj, attribute)

    def getitem(self, obj: Any, argument: Any) -> Any:
        value = super().getitem(obj, argument)
        watch = _WATCH.get()
        if watch is None or isinstance(value, Undefined):
            return value
        if isinstance(obj, Mapping) and isinstance(argument, str) and argument in obj:
            watch.accessed(obj, argument, value)
        elif isinstance(obj, (list, tuple)) and isinstance(argument, int) and not isinstance(argument, bool):
            watch.accessed(obj, argument % len(obj), value)  # x[-1] is read as x[n-1]
        return value


def _quote(value: Any) -> str:
    """Quote for the shell. A list becomes several quoted words; none becomes ''."""
    if isinstance(value, (list, tuple)):
        return " ".join(_quote(v) for v in value)
    return shlex.quote("" if value is None else str(value))


def _tojson(value: Any, indent: int | None = None) -> str:
    return json.dumps(value, ensure_ascii=False, indent=indent, default=str)


ENV = _Environment(
    undefined=StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
    keep_trailing_newline=True,
    autoescape=False,
)
ENV.filters["quote"] = _quote
ENV.filters["tojson"] = _tojson


# ---------- running templates ----------


def render(source: str, variables: Mapping[str, Any]) -> str:
    """Render a template to text."""
    try:
        template = ENV.from_string(source)
    except TemplateSyntaxError as error:
        raise TemplateError(f"line {error.lineno}: {error.message}") from error
    try:
        return template.render(variables)
    except Exception as error:
        raise TemplateError(_message(error)) from error


def evaluate(expression: str, variables: Mapping[str, Any]) -> Any:
    """Evaluate an expression (no braces), keeping its type."""
    _parse(expression, expression=True)  # for a clear syntax error
    compiled = ENV.compile_expression(expression, undefined_to_none=False)
    try:
        value = compiled(**variables)
        _reject_undefined(value)
    except Exception as error:
        raise TemplateError(_message(error)) from error
    return value


def render_value(source: str, variables: Mapping[str, Any]) -> Any:
    """Like render(), but a template that is one {{ expression }} keeps the value's type."""
    inner = single_expression(source)
    return evaluate(inner, variables) if inner is not None else render(source, variables)


def single_expression(source: str) -> str | None:
    """The expression inside a template made of exactly one {{ ... }}, else None."""
    text = source.strip()
    if not (text.startswith("{{") and text.endswith("}}")):
        return None
    try:
        tree = ENV.parse(text)
    except TemplateSyntaxError:
        return None
    if len(tree.body) != 1 or not isinstance(tree.body[0], nodes.Output):
        return None
    parts = tree.body[0].nodes
    if len(parts) != 1 or isinstance(parts[0], nodes.TemplateData):
        return None
    return text[2:-2].removeprefix("-").removesuffix("-").strip()


def _reject_undefined(value: Any) -> None:
    if isinstance(value, Undefined):
        str(value)  # StrictUndefined raises here, with Jinja's message
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_undefined(item)
    elif isinstance(value, dict):
        for item in value.values():
            _reject_undefined(item)


def _message(error: Exception) -> str:
    message = getattr(error, "message", None) or str(error)
    if isinstance(error, TemplateSyntaxError):
        return f"line {error.lineno}: {message}"
    return message if type(error).__module__.startswith("jinja2") else f"{type(error).__name__}: {message}"


# ---------- watching what a template reads ----------


@dataclass(frozen=True)
class Read:
    """A variable path a template read while it ran, like ("plan", "tasks", 0, "title"), and its value."""

    path: tuple
    value: Any


_CONTAINERS = (Mapping, list, tuple)


class _Watch:
    def __init__(self, names: frozenset[str]):
        self.names = names  # the variables the template was given, not Jinja's own globals
        self.paths: dict[int, tuple] = {}  # id of a list or map in the variables -> its path
        self.kept: list = []  # holds those lists and maps, so no other object takes their id meanwhile
        self.found: dict[tuple, Any] = {}  # path -> value, in the order first read
        self.used_whole: set[tuple] = set()  # paths whose value the template used itself

    def used(self, name: str, whole: bool, value: Any) -> None:
        if name in self.names:
            self._read((name,), value)
            if whole:
                self.used_whole.add((name,))

    def whole(self, value: Any) -> None:
        if isinstance(value, _CONTAINERS) and (path := self.paths.get(id(value))) is not None:
            self.used_whole.add(path)  # the field or index just read, which registered its id

    def accessed(self, parent: Any, key: Any, value: Any) -> None:
        path = self.paths.get(id(parent))
        if path is not None:  # else a value the template made itself, such as a filter's result
            self._read((*path, key), value)

    def _read(self, path: tuple, value: Any) -> None:
        """Note the read, and the path of a list or map read, and of the lists and maps right
        inside it, which a {% for %} reaches without a getattr."""
        self.found.setdefault(path, value)
        if not isinstance(value, _CONTAINERS):
            return
        self.paths[id(value)] = path
        self.kept.append(value)
        for key, child in (value.items() if isinstance(value, Mapping) else enumerate(value)):
            if isinstance(child, _CONTAINERS):
                self.paths[id(child)] = (*path, key)
                self.kept.append(child)

    def reads(self) -> list[Read]:
        """What was read. A list or map that was only a way to a field inside it is left out,
        unless the template also used it itself, such as with `| length`."""
        paths = list(self.found)

        def passed_through(path: tuple) -> bool:
            return path not in self.used_whole and any(len(other) > len(path) and other[:len(path)] == path for other in paths)

        return [Read(path, value) for path, value in self.found.items()
                if not (isinstance(value, _CONTAINERS) and passed_through(path))]


_WATCH: contextvars.ContextVar[_Watch | None] = contextvars.ContextVar("wf_template_watch", default=None)


def watched(call: Callable[[str, Mapping[str, Any]], Any], source: str, variables: Mapping[str, Any]) -> tuple[Any, list[Read]]:
    """call(source, variables), where call is render, evaluate or render_value, and the variable
    paths it read with their values. A TemplateError it raises holds the reads so far."""
    watch = _Watch(frozenset(variables))
    token = _WATCH.set(watch)
    try:
        value = call(source, variables)
    except TemplateError as error:
        error.reads = watch.reads()
        raise
    finally:
        _WATCH.reset(token)
    return value, watch.reads()


def path_text(path: tuple) -> str:
    """("plan", "tasks", 0, "title") -> plan.tasks[0].title"""
    text = str(path[0])
    for step in path[1:]:
        if isinstance(step, int):
            text += f"[{step}]"
        elif isinstance(step, str) and step.isidentifier():
            text += f".{step}"
        else:
            text += f"[{json.dumps(step, ensure_ascii=False, default=str)}]"
    return text


# ---------- reading templates without running them ----------


@dataclass(frozen=True)
class Ref:
    """One use of an outside name in a template.

    path: the fields read from it, e.g. ("tasks", 0, "title"); EACH stands for
      "an element of this list" (from a for loop, or a filter such as map), and
      At("i") for the element at a position that the template computes, as in
      tasks[i]. None means the template reads something derived from the name
      that we can't follow.
    output: the value is written into the text by {{ }}, not just tested or looped over.
    quoted: ...and that {{ }} ends with `| quote`.
    guarded: names that an enclosing check proves aren't none here, as in
      `{% if review %}` or `x.y if x else none`. "name.field" entries cover
      checks like `{% if workspace.implement %}`. Each checked path is an entry
      too, as guard() writes it, also when the check reads a loop variable.
      A check through a loop variable covers only the reads through that same
      variable, or through a name set from it. A type test in selectattr or
      rejectattr adds the path before the last step of its field, because a
      none there can't make the test fail. A literal default adds each path
      before the field's last step, because Jinja then puts the default in
      place of each missing step.
    """

    name: str
    path: tuple | None
    line: int
    output: bool = False
    quoted: bool = False
    guarded: frozenset[str] = frozenset()


def references(source: str, *, expression: bool = False) -> list[Ref]:
    """Every outside name `source` uses, in order. Raises TemplateError if it doesn't parse."""
    tree = _parse(source, expression=expression)
    walker = _Walker()
    if expression:
        walker.expr(tree, {}, frozenset())
    else:
        walker.stmts(tree.body, {}, frozenset())
    return walker.refs


def pure_path(source: str, *, expression: bool = False) -> tuple[str, tuple] | None:
    """(name, path) if `source` is a single field reference like {{ a.b }} (or `a.b`), else None."""
    if not expression:
        source = single_expression(source)
        if source is None:
            return None
    try:
        node = _parse(source, expression=True)
    except TemplateError:
        return None
    chain = _chain(node)
    if chain is None or chain[2]:
        return None
    return chain[0].name, chain[1]


def constant(expression: str) -> tuple[bool, Any]:
    """(True, value) if the expression is a literal like ["a", "b"], else (False, None)."""
    try:
        node = _parse(expression, expression=True)
    except TemplateError:
        return False, None
    literal = (nodes.Const, nodes.List, nodes.Tuple, nodes.Dict, nodes.Pair)
    if not isinstance(node, literal) or any(not isinstance(n, literal) for n in node.find_all(nodes.Node)):
        return False, None
    return True, node.set_environment(ENV).as_const()


@dataclass(frozen=True)
class Hole:
    """Where pattern() can't tell what a template writes: a {{ }} or a {% %}. `input` is the
    start input when the hole is just {{ start.<input> }}, so a caller can put in what it holds."""

    input: str | None = None


def pattern(source: str) -> tuple[str | Hole, ...]:
    """What a template writes whatever its variables hold: each character of its fixed text,
    in order, with a Hole for each {{ }} or {% %}."""
    found: list[str | Hole] = []
    for statement in _parse(source, expression=False).body:
        if not isinstance(statement, nodes.Output):
            found.append(Hole())
            continue
        for node in statement.nodes:
            found += node.data if isinstance(node, nodes.TemplateData) else [Hole(_start_input(node))]
    return tuple(found)


def _start_input(node) -> str | None:
    """`k` for the expression start.k or start["k"], else None."""
    if not isinstance(getattr(node, "node", None), nodes.Name) or node.node.name != "start":
        return None
    if isinstance(node, nodes.Getattr):
        return node.attr
    if isinstance(node, nodes.Getitem) and isinstance(node.arg, nodes.Const) and isinstance(node.arg.value, str):
        return node.arg.value
    return None


def _parse(source: str, *, expression: bool):
    try:
        if not expression:
            return ENV.parse(source)
        parser = Parser(ENV, source, state="variable")
        node = parser.parse_expression()
        if not parser.stream.eos:
            raise TemplateSyntaxError("unexpected text after the expression", parser.stream.current.lineno)
        return node
    except TemplateSyntaxError as error:
        raise TemplateError(f"line {error.lineno}: {error.message}") from error


# Filters whose result holds the same elements as their input, so a loop over
# `build | selectattr("passed")` still loops over elements of `build`.
_SAME_ELEMENTS = frozenset({"selectattr", "rejectattr", "select", "reject", "sort", "unique", "list", "reverse"})
# Filters that give nothing for none, where the others fail
_NONE_IS_EMPTY = frozenset({"map", "select", "reject", "selectattr", "rejectattr"})
# Filters whose result is always a number, so it can't carry text into a command
_NUMBERS = frozenset({"length", "count", "int", "float", "abs", "round", "sum", "wordcount", "filesizeformat"})
# Filters that read a field of each element, and the positions of their arguments `attribute` and `default`
# for a template that doesn't name them. So sum("cost") reads the field cost.
_ATTRIBUTE_FILTERS = {"map": (None, None), "selectattr": (0, None), "rejectattr": (0, None), "sum": (0, None),
                      "groupby": (0, 1), "unique": (1, None), "min": (1, None), "max": (1, None), "sort": (2, None),
                      "join": (1, None)}
# Tests that check only the type or the identity of a value, so a field of none, which is undefined, can't make them fail
_TYPE_TESTS = frozenset({"defined", "undefined", "none", "boolean", "true", "false", "integer", "float", "number",
                         "string", "mapping", "sequence", "callable", "sameas", "escaped"})
# Variables that wf gives as an empty list or map, and never as none: a none check of them proves nothing
_NEVER_NONE = frozenset({"results", "history", "errors"})
_UNSUPPORTED = {nodes.Include: "include", nodes.Import: "import", nodes.FromImport: "from ... import", nodes.Extends: "extends"}


@dataclass(frozen=True)
class At:
    """A path step: the element at a position that the template computes, as in plan.tasks[index]."""

    text: str  # the expression of the position, as the template reads

    def __str__(self) -> str:
        return self.text


@dataclass(frozen=True)
class _Local:
    """A name the template defines itself (loop variable, set, macro argument).

    If its value is a known path into an outside name (`t` in `for t in
    plan.tasks`), `name` and `path` say which. `sources` lists every outside
    name its value came from, so tainted data can still be followed. `loops`
    names the for loops whose current element it is, or is inside of.
    A list through filters keeps the path of the list. `each` is the field that
    map(attribute=...) takes from each element: after `{% set outs =
    results.b | map(attribute="output") %}`, outs[0] is results.b[0].output.
    `kept` holds the fields that the filters keep set on each element, as
    paths from the element at a position of `path`.
    """

    name: str | None = None
    path: tuple = ()
    sources: tuple[str, ...] = ()
    loops: frozenset[int] = frozenset()
    each: tuple = ()
    kept: frozenset[tuple] = frozenset()


def _chain(node) -> tuple[nodes.Name, tuple, list] | None:
    """For a.b[0]['c'][i] -> (Name a, ("b", 0, "c", At("i")), dynamic index nodes); None if not that shape."""
    steps, dynamic = [], []
    while True:
        if isinstance(node, nodes.Getattr):
            steps.append(node.attr)
        elif isinstance(node, nodes.Getitem):
            arg = node.arg
            if isinstance(arg, nodes.Slice):
                pass  # x[1:] is still the list
            elif isinstance(arg, nodes.Const) and isinstance(arg.value, (str, int)) and not isinstance(arg.value, bool):
                steps.append(arg.value)
            elif isinstance(arg, nodes.Neg) and isinstance(arg.node, nodes.Const) and type(arg.node.value) is int:
                steps.append(-arg.node.value)  # x[-1]
            else:
                steps.append(At(_text(arg)))
                dynamic.append(arg)
        elif isinstance(node, nodes.Name):
            return node, tuple(reversed(steps)), dynamic
        else:
            return None
        node = node.node


_OPERATORS = {nodes.Add: "+", nodes.Sub: "-", nodes.Mul: "*", nodes.Div: "/", nodes.FloorDiv: "//", nodes.Mod: "%"}


def _text(node) -> str:
    """An expression as one text, for the position in an At step: index, start.pos | int or loop.index0 + 1."""
    if isinstance(node, nodes.Const):
        return repr(node.value)
    if isinstance(node, nodes.Neg):
        return f"-{_text(node.node)}"
    if type(node) in _OPERATORS:
        return f"{_text(node.left)} {_OPERATORS[type(node)]} {_text(node.right)}"
    if isinstance(node, (nodes.Filter, nodes.Call)):
        args = ", ".join([_text(arg) for arg in node.args] + [f"{kw.key}={_text(kw.value)}" for kw in node.kwargs])
        if isinstance(node, nodes.Call):
            return f"{_text(node.node)}({args})"
        return f"{_text(node.node)} | {node.name}" + (f"({args})" if args else "")
    chain = _chain(node)
    return guard(chain[0].name, chain[1]) if chain else repr(node)


def guard(name: str, path: tuple) -> str:
    """How Ref.guarded names a checked path: results.build[0].output, results.build[index].output,
    or results.build[*].output for an element of a loop."""
    text = name
    for step in path:
        text += "[*]" if step is EACH else f"[{step}]" if isinstance(step, (int, At)) else f".{step}"
    return text


def _checks(test, scope: dict | None = None) -> tuple[frozenset, frozenset]:
    """Names proven not none (when the test is true, when it is false). The checked path is one of them,
    as guard() writes it, with a loop variable in `scope` read as the path it stands for. A path through
    the element of a loop is bound to that loop, as (its loops, the path), so that it counts only for the
    reads through the same element (_plain)."""
    chain = _chain(test)
    if chain:
        name, path, _ = chain
        tokens = {name.name} | ({f"{name.name}.{path[0]}"} if path and isinstance(path[0], str) else set())
        local = (scope or {}).get(name.name)
        if local is None:
            tokens.add(guard(name.name, path))
        elif local.name is not None:
            tokens.add(_bound(local, guard(local.name, _through(local, path))))
        return frozenset(tokens), frozenset()
    if isinstance(test, nodes.Not):
        yes, no = _checks(test.node, scope)
        return no, yes
    if isinstance(test, nodes.And):
        return _checks(test.left, scope)[0] | _checks(test.right, scope)[0], frozenset()
    if isinstance(test, nodes.Or):
        return frozenset(), _checks(test.left, scope)[1] | _checks(test.right, scope)[1]
    if isinstance(test, nodes.Test) and test.name == "none":
        return frozenset(), _not_none(test.node, scope)
    if isinstance(test, nodes.Compare) and len(test.ops) == 1:
        op, right = test.ops[0].op, test.ops[0].expr
        if isinstance(right, nodes.Const) and right.value is None and op in ("eq", "ne"):
            tokens = _not_none(test.expr, scope)
            return (tokens, frozenset()) if op == "ne" else (frozenset(), tokens)
        left = _chain(test.expr)
        if left and left[1]:  # `x.a == 1` can only be true if x isn't none
            return _checks(test.expr, scope)[0], frozenset()
    return frozenset(), frozenset()


def _not_none(node, scope: dict | None) -> frozenset:
    """What `node is not none` proves: what _checks() gives for `node`, but nothing for a list or map
    of _NEVER_NONE, such as results.build, which is an empty list before its node runs."""
    chain = _chain(node)
    if chain and chain[0].name in _NEVER_NONE and chain[0].name not in (scope or {}) and len(chain[1]) <= 1:
        return frozenset()
    return _checks(node, scope)[0]


def _bound(local: _Local, checked: str):
    """The guard token of a path checked through `local`: the path itself, or (its loops, the path)."""
    return (local.loops, checked) if local.loops else checked


def _through(local: _Local, path: tuple) -> tuple:
    """The outside path that `path` reads through `local`. In a list that map(attribute=...) gives,
    the first step picks the element, and the field that map takes comes after it."""
    return (*local.path, path[0], *local.each, *path[1:]) if local.each and path else (*local.path, *path)


def _plain(guards: frozenset, loops: frozenset = frozenset()) -> frozenset[str]:
    """The guards of a read through a local in `loops`: each plain path, and each path that a check
    through the elements of those loops gave. A check of another loop's element doesn't count."""
    return frozenset(g if isinstance(g, str) else g[1] for g in guards if isinstance(g, str) or g[0] <= loops)


def _field_path(text: str) -> tuple:
    """A field as an attribute argument names it, "output.items.0", as a path: ("output", "items", 0)."""
    return tuple(int(part) if part.isdecimal() else part for part in text.split("."))


def _keeps(node) -> tuple | None:
    """The field that select, reject, selectattr or rejectattr keeps set on each element it lets through,
    as a path (() for the element itself), else None. Its test decides, as Jinja runs it on none:
    `selectattr("output")`, `rejectattr("output", "none")` and `selectattr("output", "ne", none)` let no
    output of none through."""
    if not isinstance(node, nodes.Filter) or node.name not in _NONE_IS_EMPTY - {"map"} or node.kwargs or node.dyn_args or node.dyn_kwargs:
        return None
    if not all(isinstance(arg, nodes.Const) for arg in node.args):
        return None
    values = [arg.value for arg in node.args]
    if node.name.endswith("attr"):
        if not values or not isinstance(values[0], str) or not values[0]:
            return None
        field, test = _field_path(values[0]), values[1:]
    else:
        field, test = (), values
    if test and not isinstance(test[0], str):
        return None
    try:
        passes = bool(ENV.call_test(test[0], None, test[1:])) if test else False  # without a test, none is false
    except Exception:  # an unknown test, or one that fails on none
        return None
    return field if passes == node.name.startswith("reject") else None


def _mapped(node) -> tuple | None:
    """The field that `map(attribute=...)` takes from each element, as a path, else None."""
    if not isinstance(node, nodes.Filter) or node.name != "map" or node.node is None or node.args:
        return None
    value = _argument(node, "attribute", None)
    return _field_path(value.value) if isinstance(value, nodes.Const) and isinstance(value.value, str) and value.value else None


def _unwrap(node) -> tuple[Any, list]:
    """The list under the filters that keep its elements (_SAME_ELEMENTS) or take a field of each
    (map(attribute=...)), and those filters, from the list out."""
    filters = []
    while isinstance(node, nodes.Filter) and node.node is not None and (node.name in _SAME_ELEMENTS or _mapped(node)):
        filters.append(node)
        node = node.node
    return node, filters[::-1]


def elements_of(expression: str) -> tuple[str, tuple, frozenset] | None:
    """For an expression that gives the elements of a plain path, also through filters that keep them,
    as `results.build | selectattr("output") | list` does: (name, the path of an element, the paths
    that the filters keep set on each element). None for any other expression."""
    try:
        inner, filters = _unwrap(_parse(expression, expression=True))
    except TemplateError:
        return None
    chain = _chain(inner)
    if chain is None or chain[2] or any(f.name == "map" for f in filters):
        return None
    element = (*chain[1], EACH)
    return chain[0].name, element, frozenset((*element, *field) for f in filters if (field := _keeps(f)) is not None)


def _filter_fields(node) -> list[tuple]:
    """The fields that a filter reads from each element, as paths: [("output", "stdout")] for
    `map(attribute="output.stdout")`. sort takes several, with commas: sort(attribute="a,b")."""
    if not isinstance(node, nodes.Filter) or node.name not in _ATTRIBUTE_FILTERS or node.node is None:
        return []
    if node.name == "map" and node.args:  # map("upper") runs a filter, and reads no field
        return []
    value = _argument(node, "attribute", _ATTRIBUTE_FILTERS[node.name][0])
    if not (isinstance(value, nodes.Const) and isinstance(value.value, str)):
        return []
    names = value.value.split(",") if node.name == "sort" else [value.value]
    return [_field_path(name) for name in names if name]


def _defaulted(node: nodes.Filter) -> bool:
    """Whether the filter gives a literal default other than none. Jinja puts it in place of each missing
    step of the field, so that a missing field can't make the filter fail. Jinja ignores a default of none."""
    default = _argument(node, "default", _ATTRIBUTE_FILTERS[node.name][1])
    return isinstance(default, (nodes.List, nodes.Tuple, nodes.Dict)) or (isinstance(default, nodes.Const) and default.value is not None)


def _type_tested(node: nodes.Filter) -> bool:
    """Whether selectattr or rejectattr runs a test of _TYPE_TESTS on its field. The test takes the
    undefined value of a missing last step, but Jinja can't read a step after a missing one."""
    test = node.args[1] if node.name in ("selectattr", "rejectattr") and len(node.args) > 1 else None
    return isinstance(test, nodes.Const) and test.value in _TYPE_TESTS


def _argument(node: nodes.Filter, keyword: str, position: int | None):
    """The argument of a filter that the template names `keyword`, or else gives at `position`. None if neither."""
    given = {pair.key: pair.value for pair in node.kwargs}
    if keyword in given:
        return given[keyword]
    return node.args[position] if position is not None and position < len(node.args) else None


def _target_names(target) -> list[str]:
    return [n.name for n in ([target] if isinstance(target, nodes.Name) else target.find_all(nodes.Name))]


class _Walker:
    """Collects Refs. `scope` maps local names to _Local; `guards` is the current set of checked names."""

    def __init__(self):
        self.refs: list[Ref] = []
        self.loops = itertools.count(1)  # a key for each for loop, which binds the checks of its element

    # ----- statements -----

    def stmts(self, body, scope: dict, guards: frozenset) -> None:
        for node in body:
            self.stmt(node, scope, guards)

    def stmt(self, node, scope: dict, guards: frozenset) -> None:
        for kind, word in _UNSUPPORTED.items():
            if isinstance(node, kind):
                raise TemplateError(f"line {node.lineno}: {{% {word} %}} isn't supported in workflow templates")
        if isinstance(node, nodes.Output):
            for part in node.nodes:
                if not isinstance(part, nodes.TemplateData):
                    quoted = isinstance(part, nodes.Filter) and part.name == "quote"
                    self.expr(part, scope, guards, output=True, quoted=quoted)
        elif isinstance(node, nodes.If):
            self._if(node, scope, guards)
        elif isinstance(node, nodes.For):
            self.expr(node.iter, scope, guards)
            inner = dict(scope)
            source, kept = self._elements(node.iter, scope, next(self.loops))
            for name in _target_names(node.target):
                inner[name] = source if isinstance(node.target, nodes.Name) else _Local(sources=source.sources)
            inner["loop"] = _Local()
            kept |= guards
            if node.test is not None:
                self.expr(node.test, inner, kept)
                kept |= _checks(node.test, inner)[0]  # the body runs only for the elements that pass the test
            self.stmts(node.body, inner, kept)
            self.stmts(node.else_, scope, guards)
        elif isinstance(node, nodes.Assign):
            self.expr(node.node, scope, guards)
            value = self._local(node.node, scope)
            for name in _target_names(node.target):
                scope[name] = value if isinstance(node.target, nodes.Name) else _Local(sources=value.sources)
        elif isinstance(node, nodes.AssignBlock):
            self.stmts(node.body, dict(scope), guards)
            for name in _target_names(node.target):
                scope[name] = _Local()
        elif isinstance(node, nodes.With):
            inner = dict(scope)
            for target, value in zip(node.targets, node.values):
                self.expr(value, scope, guards)
                for name in _target_names(target):
                    inner[name] = self._local(value, scope)
            self.stmts(node.body, inner, guards)
        elif isinstance(node, (nodes.Macro, nodes.CallBlock)):
            if isinstance(node, nodes.CallBlock):
                self.expr(node.call, scope, guards)
            else:
                scope[node.name] = _Local()
            for default in node.defaults:
                self.expr(default, scope, guards)
            inner = dict(scope) | {a.name: _Local() for a in node.args}
            inner |= {name: _Local() for name in ("caller", "varargs", "kwargs")}
            self.stmts(node.body, inner, guards)
        else:
            for child in node.iter_child_nodes():
                if isinstance(child, nodes.Expr):
                    self.expr(child, scope, guards)
                else:
                    self.stmt(child, scope, guards)

    def _if(self, node, scope, guards) -> None:
        self.expr(node.test, scope, guards)
        yes, no = _checks(node.test, scope)
        self.stmts(node.body, scope, guards | yes)
        ruled_out = no
        for branch in node.elif_:
            self.expr(branch.test, scope, guards | ruled_out)
            yes, no = _checks(branch.test, scope)
            self.stmts(branch.body, scope, guards | ruled_out | yes)
            ruled_out |= no
        self.stmts(node.else_, scope, guards | ruled_out)

    # ----- expressions -----

    def expr(self, node, scope, guards, output: bool = False, quoted: bool = False) -> None:
        chain = _chain(node)
        if chain:
            name, path, dynamic = chain
            self._use(name, path, scope, guards, output, quoted)
            for arg in dynamic:
                self.expr(arg, scope, guards)
        elif isinstance(node, nodes.Call) and isinstance(node.node, nodes.Getattr):
            # x.y.upper() reads x.y; `upper` is a method, not a field
            self.expr(node.node.node, scope, guards, output, quoted)
            for child in [*node.args, *node.kwargs, node.dyn_args, node.dyn_kwargs]:
                if child is not None:
                    self.expr(child, scope, guards)
        elif isinstance(node, nodes.CondExpr):
            self.expr(node.test, scope, guards)
            yes, no = _checks(node.test, scope)
            self.expr(node.expr1, scope, guards | yes, output, quoted)
            if node.expr2 is not None:
                self.expr(node.expr2, scope, guards | no, output, quoted)
        elif isinstance(node, nodes.And):
            self.expr(node.left, scope, guards, output, quoted)
            self.expr(node.right, scope, guards | _checks(node.left, scope)[0], output, quoted)
        elif isinstance(node, nodes.Or):
            self.expr(node.left, scope, guards, output, quoted)
            self.expr(node.right, scope, guards | _checks(node.left, scope)[1], output, quoted)
        elif isinstance(node, nodes.Filter) and node.name in _NUMBERS:
            for child in node.iter_child_nodes():  # `x | length` writes a number, not x
                self.expr(child, scope, guards)
            self._attribute(node, scope, guards)
        else:
            for child in node.iter_child_nodes():
                self.expr(child, scope, guards, output, quoted)
            self._attribute(node, scope, guards)

    def _attribute(self, node, scope, guards) -> None:
        """`x | map(attribute="output.stdout")` reads that field of each element of x, as a loop over x
        does. Where a missing field can't make the filter fail, the read is guarded: at each step but
        the last after a literal default, and at the step before the last after a type test. Over none,
        only map, select, reject, selectattr and rejectattr read nothing, so only they need no check of x."""
        fields = _filter_fields(node)
        source, kept = self._elements(node.node, scope) if fields else (_Local(), frozenset())
        if source.name is None:
            return
        listed, filters = _unwrap(node.node)
        if (filters[0] if filters else node).name in _NONE_IS_EMPTY:
            kept |= _checks(listed, scope)[0]
        if _defaulted(node):
            kept |= {_bound(source, guard(source.name, source.path + field[:k])) for field in fields for k in range(len(field))}
        elif _type_tested(node):
            kept |= {_bound(source, guard(source.name, source.path + field[:-1])) for field in fields}
        for field in fields:
            self.refs.append(Ref(source.name, source.path + field, node.lineno, guarded=_plain(guards | kept, source.loops)))

    def _elements(self, node, scope, loop: int | None = None) -> tuple[_Local, frozenset]:
        """What each element of the list `node` is, and the guards of the fields that its filters keep set
        on it. Through map(attribute=...), each element is that field: each element of
        `results.b | map(attribute="output")` is results.b[*].output. The guards of a for loop's
        element are bound to that `loop`."""
        listed = self._local(node, scope)
        if listed.name is None:
            return listed, frozenset()
        loops = listed.loops | ({loop} if loop is not None else set())
        element = _Local(listed.name, _through(listed, (EACH,)), listed.sources, loops)
        return element, frozenset(_bound(element, guard(listed.name, (*listed.path, EACH, *field))) for field in listed.kept)

    def _use(self, name: nodes.Name, path: tuple, scope, guards, output, quoted) -> None:
        local = scope.get(name.name)
        if local is None:
            self.refs.append(Ref(name.name, path, name.lineno, output, quoted, _plain(guards)))
        elif local.name is not None:
            kept = {_bound(local, guard(local.name, (*local.path, path[0], *field))) for field in local.kept} if path else set()
            self.refs.append(Ref(local.name, _through(local, path), name.lineno, output, quoted, _plain(guards | kept, local.loops)))
        else:
            for source in local.sources:
                self.refs.append(Ref(source, None, name.lineno, output, quoted, _plain(guards)))

    def _local(self, node, scope) -> _Local:
        """What a local name assigned from `node` refers to. Through the filters that keep the elements
        of a list or take a field of each, it is still that list (see _Local)."""
        inner, filters = _unwrap(node)
        chain = _chain(inner)
        if chain and not chain[2]:
            name, path, _ = chain
            local = scope.get(name.name)
            if local is None:
                value = _Local(name.name, path, (name.name,))
            elif local.name is not None:
                value = replace(local, path=_through(local, path), each=(), kept=frozenset()) if path else local
            else:
                return _Local(sources=local.sources)
            for applied in filters:
                if (field := _mapped(applied)) is not None:
                    value = replace(value, each=value.each + field)
                elif (field := _keeps(applied)) is not None:
                    value = replace(value, kept=value.kept | {value.each + field})
            return value
        sources: list[str] = []
        for found in node.find_all(nodes.Name):
            if found.ctx != "load":
                continue
            local = scope.get(found.name)
            sources += [found.name] if local is None else list(local.sources)
        return _Local(sources=tuple(dict.fromkeys(sources)))
