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

The second half of this module reads templates without running them.
references() walks Jinja's syntax tree and lists every outside name a
template uses, with its field path (`plan.tasks[0].title` reads plan ->
tasks -> 0 -> title) and some context: is it written into the text, through
`| quote`, and which `{% if x %}` checks surround it. validate.py turns those
into the spec's checks: unknown names, missing fields, nodes that might not
have run, unquoted agent output in shell commands.
"""

import json
import shlex
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from jinja2 import StrictUndefined, TemplateSyntaxError, Undefined, nodes
from jinja2.parser import Parser
from jinja2.sandbox import ImmutableSandboxedEnvironment

from .schemas import EACH


class TemplateError(Exception):
    """A template or expression that can't be parsed or evaluated."""


class _Environment(ImmutableSandboxedEnvironment):
    def getattr(self, obj: Any, attribute: str) -> Any:
        # `x.items` should read the output field `items`, not the dict method
        if isinstance(obj, Mapping):
            try:
                return obj[attribute]
            except (KeyError, TypeError):
                pass
        return super().getattr(obj, attribute)


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


# ---------- reading templates without running them ----------


@dataclass(frozen=True)
class Ref:
    """One use of an outside name in a template.

    path: the fields read from it, e.g. ("tasks", 0, "title"); EACH stands for
      "an element of this list" (from a for loop). None means the template
      reads something derived from the name that we can't follow.
    output: the value is written into the text by {{ }}, not just tested or looped over.
    quoted: ...and that {{ }} ends with `| quote`.
    guarded: names that an enclosing check proves aren't none here, as in
      `{% if review %}` or `x.y if x else none`. "name.field" entries cover
      checks like `{% if workspace.implement %}`.
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


def fixed_text(source: str) -> str | tuple[str, str]:
    """What a template writes whatever its variables hold: all of its text if it has no {{ }} or
    {% %}, else the text before the first of them and after the last, as a pair."""
    parts = []  # each piece of text, or None where something is computed
    for statement in _parse(source, expression=False).body:
        if isinstance(statement, nodes.Output):
            parts += [n.data if isinstance(n, nodes.TemplateData) else None for n in statement.nodes]
        else:
            parts.append(None)
    if None not in parts:
        return "".join(parts)
    first, last = parts.index(None), len(parts) - parts[::-1].index(None)
    return "".join(parts[:first]), "".join(parts[last:])


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
# Filters whose result is always a number, so it can't carry text into a command
_NUMBERS = frozenset({"length", "count", "int", "float", "abs", "round", "sum", "wordcount", "filesizeformat"})
_UNSUPPORTED = {nodes.Include: "include", nodes.Import: "import", nodes.FromImport: "from ... import", nodes.Extends: "extends"}


@dataclass(frozen=True)
class _Local:
    """A name the template defines itself (loop variable, set, macro argument).

    If its value is a known path into an outside name (`t` in `for t in
    plan.tasks`), `name` and `path` say which. `sources` lists every outside
    name its value came from, so tainted data can still be followed.
    """

    name: str | None = None
    path: tuple = ()
    sources: tuple[str, ...] = ()


def _chain(node) -> tuple[nodes.Name, tuple, list] | None:
    """For a.b[0]['c'] -> (Name a, ("b", 0, "c"), dynamic index nodes); None if not that shape."""
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
            else:
                steps.append(EACH)
                dynamic.append(arg)
        elif isinstance(node, nodes.Name):
            return node, tuple(reversed(steps)), dynamic
        else:
            return None
        node = node.node


def _checks(test) -> tuple[frozenset, frozenset]:
    """Names proven not none (when the test is true, when it is false)."""
    chain = _chain(test)
    if chain:
        name, path, _ = chain
        tokens = {name.name} | ({f"{name.name}.{path[0]}"} if path and isinstance(path[0], str) else set())
        return frozenset(tokens), frozenset()
    if isinstance(test, nodes.Not):
        yes, no = _checks(test.node)
        return no, yes
    if isinstance(test, nodes.And):
        return _checks(test.left)[0] | _checks(test.right)[0], frozenset()
    if isinstance(test, nodes.Or):
        return frozenset(), _checks(test.left)[1] | _checks(test.right)[1]
    if isinstance(test, nodes.Test) and test.name == "none":
        return frozenset(), _checks(test.node)[0]
    if isinstance(test, nodes.Compare) and len(test.ops) == 1:
        op, right = test.ops[0].op, test.ops[0].expr
        if isinstance(right, nodes.Const) and right.value is None and op in ("eq", "ne"):
            tokens = _checks(test.expr)[0]
            return (tokens, frozenset()) if op == "ne" else (frozenset(), tokens)
        left = _chain(test.expr)
        if left and left[1]:  # `x.a == 1` can only be true if x isn't none
            return _checks(test.expr)[0], frozenset()
    return frozenset(), frozenset()


def _target_names(target) -> list[str]:
    return [n.name for n in ([target] if isinstance(target, nodes.Name) else target.find_all(nodes.Name))]


class _Walker:
    """Collects Refs. `scope` maps local names to _Local; `guards` is the current set of checked names."""

    def __init__(self):
        self.refs: list[Ref] = []

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
            source = self._local(node.iter, scope, each=True)
            for name in _target_names(node.target):
                inner[name] = source if isinstance(node.target, nodes.Name) else _Local(sources=source.sources)
            inner["loop"] = _Local()
            if node.test is not None:
                self.expr(node.test, inner, guards)
            self.stmts(node.body, inner, guards)
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
        yes, no = _checks(node.test)
        self.stmts(node.body, scope, guards | yes)
        ruled_out = no
        for branch in node.elif_:
            self.expr(branch.test, scope, guards | ruled_out)
            yes, no = _checks(branch.test)
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
            yes, no = _checks(node.test)
            self.expr(node.expr1, scope, guards | yes, output, quoted)
            if node.expr2 is not None:
                self.expr(node.expr2, scope, guards | no, output, quoted)
        elif isinstance(node, nodes.And):
            self.expr(node.left, scope, guards, output, quoted)
            self.expr(node.right, scope, guards | _checks(node.left)[0], output, quoted)
        elif isinstance(node, nodes.Or):
            self.expr(node.left, scope, guards, output, quoted)
            self.expr(node.right, scope, guards | _checks(node.left)[1], output, quoted)
        elif isinstance(node, nodes.Filter) and node.name in _NUMBERS:
            for child in node.iter_child_nodes():  # `x | length` writes a number, not x
                self.expr(child, scope, guards)
        else:
            for child in node.iter_child_nodes():
                self.expr(child, scope, guards, output, quoted)

    def _use(self, name: nodes.Name, path: tuple, scope, guards, output, quoted) -> None:
        local = scope.get(name.name)
        if local is None:
            self.refs.append(Ref(name.name, path, name.lineno, output, quoted, guards))
        elif local.name is not None:
            self.refs.append(Ref(local.name, local.path + path, name.lineno, output, quoted, guards))
        else:
            for source in local.sources:
                self.refs.append(Ref(source, None, name.lineno, output, quoted, guards))

    def _local(self, node, scope, each: bool = False) -> _Local:
        """What a local name assigned from `node` refers to."""
        unwrapped = node
        while isinstance(unwrapped, nodes.Filter) and unwrapped.name in _SAME_ELEMENTS and unwrapped.node is not None:
            unwrapped = unwrapped.node
        chain = _chain(unwrapped)
        if chain and not chain[2]:
            name, path, _ = chain
            suffix = (EACH,) if each else ()
            local = scope.get(name.name)
            if local is None:
                return _Local(name.name, path + suffix, (name.name,))
            if local.name is not None:
                return _Local(local.name, local.path + path + suffix, local.sources)
            return _Local(sources=local.sources)
        sources: list[str] = []
        for found in node.find_all(nodes.Name):
            if found.ctx != "load":
                continue
            local = scope.get(found.name)
            sources += [found.name] if local is None else list(local.sources)
        return _Local(sources=tuple(dict.fromkeys(sources)))
