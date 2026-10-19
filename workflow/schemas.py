"""Schemas: the YAML's field maps become JSON Schema, and values get checked against them.

A field map is the shorthand the workflow file uses wherever it declares data
(start.input, a node's output, the entries in `types`):

    output:
      root_cause: {type: string}
      notes: {type: string, optional: true}

Each key is a field and each value is a JSON Schema. object_schema() turns the
map into one object schema: every field is required unless it says
`optional: true`, extra fields aren't allowed, and every `$ref: <type>` is
replaced by a copy of that type. The result is plain and self-contained,
which matters because it becomes the input schema of the `submit_result` tool,
and Claude Code, Codex and Cursor each get the same schema.

Only the keywords in SUPPORTED are allowed. Every harness's tool calling
understands those, so one schema works everywhere.

Three users:
- validate.py: field_map_problems() and type_problems() check the file's
  schemas, and field_at() checks that `{{ plan.tasks }}` names a real field.
- the runner: check() validates agent results, human answers, workflow inputs
  and end outputs, and words each problem with its field path, so an agent
  can read the errors and fix its submit_result call. absent_as_none() then
  puts none in every optional field a result or input left out, since a
  template reads a missing optional field as none.
- templates and the graph: ANY stands for "a value we know nothing about".
"""

from collections.abc import Iterable, Mapping
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from .spec import Problem

SUPPORTED = frozenset({
    "type", "properties", "required", "additionalProperties", "items", "enum", "const",
    "description", "minimum", "maximum", "minLength", "maxLength", "minItems", "maxItems",
    "pattern", "$ref",
})
JSON_TYPES = frozenset({"string", "number", "integer", "boolean", "array", "object", "null"})

ANY: dict = {}  # a schema with no constraints: "could be anything"


class _Each:
    """Path step meaning "each element of this list", as in `{% for t in plan.tasks %}`."""

    def __repr__(self) -> str:
        return "EACH"


EACH = _Each()


class NoField(Exception):
    """field_at() was asked for a field the schema doesn't have."""


class _RefCycle(Exception):
    pass


# Outputs the spec fixes: an agent without `output`, and every command node
DEFAULT_OUTPUT = {"summary": {"type": "string"}}
COMMAND_OUTPUT = {
    "exit_code": {"type": "integer"},
    "stdout": {"type": "string", "description": "The last 20,000 characters of standard output"},
    "stderr": {"type": "string", "description": "The last 20,000 characters of standard error"},
    "log": {"type": "string", "description": "Path to the full output in the run folder"},
}


def node_output_schema(workflow, node_id: str) -> dict:
    """The schema of one result of an agent, command or human node (one item's, for for_each)."""
    node = workflow.nodes[node_id]
    if node.kind == "command":
        return object_schema(COMMAND_OUTPUT)
    if node.kind in ("agent", "human"):
        return object_schema(node.output or DEFAULT_OUTPUT, workflow.types)
    raise ValueError(f"`{node_id}` is a workflow node: its output is the sub-workflow's end output")


# ---------- field map -> JSON Schema ----------


def object_schema(field_map: Mapping[str, Mapping], types: Mapping[str, Mapping] | None = None) -> dict:
    """The JSON Schema for a field map, with every $ref inlined."""
    return _object(field_map, types or {}, ())


def _object(field_map, types, seen) -> dict:
    properties, required = {}, []
    for name, field in field_map.items():
        field = dict(field)
        optional = field.pop("optional", False)
        if not optional and "default" not in field:
            required.append(name)
        properties[name] = _inline(field, types, seen)
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


def _inline(schema, types, seen):
    """Replace {$ref: name} with that type's object schema. Other keys next to $ref are kept."""
    if not isinstance(schema, Mapping):
        return schema
    if "$ref" in schema:
        name = schema["$ref"]
        if name in seen:
            raise _RefCycle(name)
        if name not in types:
            return ANY  # an unknown type: reported where it's named (_keyword_problems)
        rest = {k: _inline(v, types, seen) for k, v in schema.items() if k != "$ref"}
        return {**_object(types[name], types, (*seen, name)), **rest}
    inlined = {}
    for key, value in schema.items():
        if key == "properties" and isinstance(value, Mapping):
            inlined[key] = {name: _inline(sub, types, seen) for name, sub in value.items()}
        elif key in ("items", "additionalProperties"):
            inlined[key] = _inline(value, types, seen)
        else:
            inlined[key] = value
    return inlined


# ---------- checking the file's schemas ----------


def field_map_problems(field_map: Mapping, types: Mapping, where: str, *, inputs: bool = False) -> list[Problem]:
    """Problems in one field map. `inputs` allows `default` (start.input only)."""
    problems: list[Problem] = []
    for name, field in field_map.items():
        at = f"{where}.{name}"
        extra = {"optional", "default"} if inputs else {"optional"}
        found = _keyword_problems(field, types, at, extra)
        if "optional" in field and not isinstance(field["optional"], bool):
            found.append(Problem(at, "`optional` must be true or false"))
        if not found:
            found = _structure_problems(field, types, at)
        if not found and inputs and "default" in field:
            try:
                schema = _inline({k: v for k, v in field.items() if k not in ("optional", "default")}, types, ())
            except _RefCycle:
                schema = None  # reported once, by _cycle_problems
            for error in check(field["default"], schema) if schema is not None else []:
                found.append(Problem(at, f"default doesn't match the field: {error}"))
        problems += found
    return problems


def schema_problems(schema: Mapping, types: Mapping, where: str, extra: Iterable[str] = ()) -> list[Problem]:
    """Problems in one full JSON Schema (not a field map), e.g. an end output's schema."""
    return _keyword_problems(schema, types, where, set(extra)) or _structure_problems(
        {k: v for k, v in schema.items() if k not in set(extra)}, types, where
    )


def type_problems(types: Mapping[str, Mapping]) -> list[Problem]:
    """Problems in the `types` section, including types that refer to themselves."""
    problems = []
    for name, field_map in types.items():
        problems += field_map_problems(field_map, types, f"types.{name}")
    return problems + _cycle_problems(types)


def _keyword_problems(schema, types, where, extra=frozenset()) -> list[Problem]:
    if not isinstance(schema, Mapping):
        return [Problem(where, "expected a schema (a map of JSON Schema keywords)")]
    problems = []
    for key, value in schema.items():
        if key in extra:
            continue
        if key == "optional":
            problems.append(Problem(where, "`optional` is only allowed on top-level fields"))
        elif key == "default":
            problems.append(Problem(where, "`default` is only allowed in start.input"))
        elif key not in SUPPORTED:
            hint = ("; if it's the end of a description with a comma in it, quote the description: "
                    "in {a: b, c} YAML splits at the comma") if " " in str(key) else ""
            problems.append(Problem(where, f"unsupported schema keyword `{key}`{hint}"))
        elif key == "$ref":
            if not isinstance(value, str) or value not in types:
                known = ", ".join(types) or "none defined"
                problems.append(Problem(where, f"unknown type `{value}` (types: {known})"))
        elif key == "type":
            for name in [value] if isinstance(value, str) else value if isinstance(value, list) else [value]:
                if not isinstance(name, str) or name not in JSON_TYPES:
                    problems.append(Problem(where, f"`{name}` isn't a JSON Schema type"))
        elif key == "properties" and isinstance(value, Mapping):
            for name, sub in value.items():
                problems += _keyword_problems(sub, types, f"{where}.properties.{name}")
        elif key in ("items", "additionalProperties") and isinstance(value, Mapping):
            problems += _keyword_problems(value, types, f"{where}.{key}")
    return problems


def _structure_problems(field, types, where) -> list[Problem]:
    """Let jsonschema check everything else (e.g. `minLength: "x"`), on the inlined schema."""
    try:
        schema = _inline({k: v for k, v in field.items() if k not in ("optional", "default")}, types, ())
    except _RefCycle:
        return []  # reported once, by _cycle_problems
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as error:
        return [Problem(where, f"invalid schema: {error.message}")]
    return []


def _refs(schema) -> list[str]:
    """Every type name a schema (or field map) refers to."""
    found = []
    if isinstance(schema, Mapping):
        if isinstance(schema.get("$ref"), str):
            found.append(schema["$ref"])
        for value in schema.values():
            found += _refs(value)
    elif isinstance(schema, list):
        for value in schema:
            found += _refs(value)
    return found


def _cycle_problems(types) -> list[Problem]:
    problems, done = [], set()

    def visit(name, path):
        if name in path:
            cycle = path[path.index(name):] + [name]
            if not done.intersection(cycle):
                problems.append(Problem(f"types.{cycle[0]}", f"type `{cycle[0]}` refers to itself: {' → '.join(cycle)}"))
            done.update(cycle)
            return
        if name in done or name not in types:
            return
        for ref in dict.fromkeys(_refs(types[name])):
            visit(ref, [*path, name])
        done.add(name)

    for name in types:
        visit(name, [])
    return problems


# ---------- checking values ----------


def check(value: Any, schema: Mapping, *, none_is_absent: bool = False) -> list[str]:
    """Every way `value` breaks `schema`, as "field.path: problem" lines (no path for the top level).

    none_is_absent: none in a field the schema doesn't require counts as left out. For values
    templates made, which carry none where an optional field was left out (absent_as_none).
    """
    if none_is_absent:
        value = _without_none(value, schema)
    errors = sorted(
        Draft202012Validator(schema).iter_errors(value),
        key=lambda e: [(0, p) if isinstance(p, int) else (1, p) for p in e.absolute_path],
    )
    return [f"{_path(e.absolute_path)}: {e.message}" if e.absolute_path else e.message for e in errors]


def _path(parts) -> str:
    text = ""
    for part in parts:
        text += f"[{part}]" if isinstance(part, int) else f".{part}" if text else str(part)
    return text


def absent_as_none(value: Any, schema: Mapping) -> Any:
    """`value` with none in every optional field it leaves out, at any depth, so templates read none there."""
    if not isinstance(schema, Mapping):
        return value
    properties, items = schema.get("properties"), schema.get("items")
    if isinstance(value, dict) and isinstance(properties, Mapping):
        filled = {k: absent_as_none(v, properties.get(k, ANY)) for k, v in value.items()}
        return filled | {k: None for k in properties if k not in value and k not in schema.get("required", ())}
    if isinstance(value, list) and isinstance(items, Mapping):
        return [absent_as_none(v, items) for v in value]
    return value


def _without_none(value: Any, schema: Mapping) -> Any:
    """`value` without the none of fields `schema` declares but doesn't require, at any depth."""
    if not isinstance(schema, Mapping):
        return value
    properties, items = schema.get("properties"), schema.get("items")
    if isinstance(value, dict) and isinstance(properties, Mapping):
        required = schema.get("required", ())
        return {k: _without_none(v, properties.get(k, ANY)) for k, v in value.items()
                if v is not None or k not in properties or k in required}
    if isinstance(value, list) and isinstance(items, Mapping):
        return [_without_none(v, items) for v in value]
    return value


# ---------- looking up fields ----------


def field_at(schema: Mapping, path: Iterable) -> Mapping:
    """The schema of the value at `path` (field names, list indexes or EACH).

    Raises NoField only when the schema rules the field out: an object with
    additionalProperties false that doesn't list it. Anywhere it can't tell
    (a string method like `.upper`, a free-form object, ANY) it returns ANY.
    """
    for step in path:
        if not isinstance(schema, Mapping) or not schema:  # e.g. `items: true`
            return ANY
        if isinstance(step, str):
            properties = schema.get("properties")
            if properties is not None and step in properties:
                schema = properties[step]
            elif properties is not None and schema.get("additionalProperties") is False:
                raise NoField(f"no field `{step}` (fields: {', '.join(properties) or 'none'})")
            else:
                return ANY
        else:  # a list index or EACH
            schema = schema.get("items", ANY) if _is_array(schema) else ANY
    return schema


def types_of(schema: Mapping) -> set[str] | None:
    """The JSON types a schema allows, or None if it doesn't say. `integer` implies `number`."""
    kind, enum = schema.get("type"), schema.get("enum")
    if isinstance(kind, str):
        names = {kind}
    elif isinstance(kind, list) and all(isinstance(k, str) for k in kind):
        names = set(kind)
    elif kind is not None:
        return None  # malformed: validation reports it
    elif "const" in schema:
        names = {json_type(schema["const"])}
    elif isinstance(enum, list) and enum:
        names = {json_type(v) for v in enum}
    else:
        return None
    if "integer" in names:
        names.add("number")
    return names


def json_type(value: Any) -> str:
    """The JSON Schema type name of a Python value."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (list, tuple)):
        return "array"
    return "object"


def _is_array(schema: Mapping) -> bool:
    kind = schema.get("type")
    return kind == "array" or (isinstance(kind, list) and "array" in kind) or "items" in schema
