"""Tunables: the settings a run takes from its start inputs.

A route's `max`, a node's `timeout`, `retry` and `max_parallel` (in `defaults`
too), and each of `limits` can be a template instead of a literal, so a
workflow can take its loop cap or time limit as an input:

    routes: [{to: implement, max: "{{ start.max_rounds }}"}, {to: end}]
    timeout: "{{ start.review_timeout }}"

They decide how the run goes before any node runs, so their templates read only
`start` (validate.py says so), and resolve() renders them once, when the run
starts. Each value is checked as the field checks a literal (spec.TUNABLES),
and the workflow comes back with the values in place of the templates, so
nothing that runs sees a template.

The runner keeps the values in run.json; a sub-workflow resolves its own from
`with` when its node starts it, and keeps them in that node's pass folder. When
`wf resume` loads the workflow file again, apply() puts the same values back,
so a resumed run keeps the values it started with.
"""

import json
from typing import Any

from pydantic import TypeAdapter, ValidationError

from . import schemas, templates
from .spec import TUNABLES, Problem, Workflow, is_template

_LITERALS = {name: TypeAdapter(literal) for name, literal in TUNABLES.items()}


class TunableError(Exception):
    """A setting's template fails, or gives a value its field doesn't accept."""

    def __init__(self, problems: list[Problem]):
        super().__init__("; ".join(map(str, problems)))
        self.problems = problems


def templated(workflow: Workflow) -> list[tuple[str, str]]:
    """(where, template) of every setting written as a template, e.g. ("nodes.review.routes[1].max", "{{ ... }}")."""
    places = [("defaults", workflow.defaults), ("limits", workflow.limits)]
    for node_id, node in workflow.nodes.items():
        places.append((f"nodes.{node_id}", node))
        places += [(f"nodes.{node_id}.routes[{i}]", route) for i, route in enumerate(node.routes)]
    return [(f"{where}.{name}", getattr(model, name)) for where, model in places
            for name in type(model).model_fields if name in TUNABLES and is_template(getattr(model, name))]


def resolve(workflow: Workflow, inputs: dict) -> tuple[Workflow, dict[str, Any]]:
    """The workflow with each setting's template rendered with these inputs as `start`, and
    the values by where. Raises TunableError naming every setting that fails."""
    start = schemas.absent_as_none(inputs, schemas.object_schema(workflow.start.input, workflow.types))  # as nodes see it
    values, problems = {}, []
    for where, source in templated(workflow):
        try:
            values[where] = value(where, source, start)
        except TunableError as error:
            problems += error.problems
    if problems:
        raise TunableError(problems)
    return apply(workflow, values), values


def value(where: str, source: str, inputs: dict, *, defaults: bool = False) -> Any:
    """The value the setting at `where` gets from its template, checked as its field's literal.
    `defaults`: the inputs are start.input's defaults (for `wf validate`), which messages say."""
    try:
        rendered = templates.render_value(source, {"start": inputs})
    except templates.TemplateError as error:
        raise TunableError([Problem(where, str(error))]) from None
    if rendered is None:
        raise TunableError([Problem(where, f"{_given(source, defaults)} none, but this setting needs a value")])
    try:
        return _LITERALS[where.rsplit(".", 1)[1]].validate_python(rendered)
    except ValidationError as error:
        shown = json.dumps(rendered, ensure_ascii=False, default=str)
        raise TunableError([Problem(where, f"{_given(source, defaults)} {shown}: {error.errors()[0]['msg']}")]) from None


def apply(workflow: Workflow, values: dict[str, Any]) -> Workflow:
    """The workflow with each templated setting replaced by its value in `values` (by where)."""
    missing = [where for where, _ in templated(workflow) if where not in values]
    if missing:
        raise TunableError([Problem(where, "the run kept no value for this setting") for where in missing])
    if not values:
        return workflow

    def update(model, where: str):
        return model.model_copy(update={name: values[f"{where}.{name}"] for name in type(model).model_fields
                                        if f"{where}.{name}" in values})

    nodes = {}
    for node_id, node in workflow.nodes.items():
        at = f"nodes.{node_id}"
        routes = [update(route, f"{at}.routes[{i}]") for i, route in enumerate(node.routes)]
        nodes[node_id] = update(node, at).model_copy(update={"routes": routes})
    return workflow.model_copy(update={"defaults": update(workflow.defaults, "defaults"),
                                       "limits": update(workflow.limits, "limits"), "nodes": nodes})


def _given(source: str, defaults: bool) -> str:
    """What gave a setting its value, as messages say it, e.g. "input `max_rounds` gives"."""
    names = list(dict.fromkeys(ref.path[0] for ref in templates.references(source)
                               if ref.name == "start" and ref.path and isinstance(ref.path[0], str)))
    if not names:
        return f"`{source}` gives"
    listed = " and ".join(f"`{name}`" for name in names)
    if defaults:
        return f"the default of input {listed} gives" if len(names) == 1 else f"the defaults of inputs {listed} give"
    return f"input {listed} gives" if len(names) == 1 else f"inputs {listed} give"
