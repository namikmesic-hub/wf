"""schemas: field maps become JSON Schema; values are checked with field paths."""

from workflow import schemas

TYPES = {
    "task": {
        "title": {"type": "string"},
        "details": {"type": "string", "description": "What to build"},
    }
}


def where(problems):
    return [(p.where, p.message) for p in problems]


# ---------- field map -> JSON Schema ----------


def test_every_field_is_required_unless_optional():
    schema = schemas.object_schema(
        {
            "root_cause": {"type": "string"},
            "severity": {"enum": ["low", "high"]},
            "notes": {"type": "string", "optional": True},
        }
    )
    assert schema == {
        "type": "object",
        "properties": {
            "root_cause": {"type": "string"},
            "severity": {"enum": ["low", "high"]},
            "notes": {"type": "string"},
        },
        "required": ["root_cause", "severity"],
        "additionalProperties": False,
    }


def test_an_input_with_a_default_is_optional():
    schema = schemas.object_schema({"cmd": {"type": "string", "default": "pytest"}})
    assert schema["required"] == []
    assert schema["properties"]["cmd"] == {"type": "string", "default": "pytest"}


def test_refs_are_inlined_as_plain_object_schemas():
    schema = schemas.object_schema(
        {"tasks": {"type": "array", "maxItems": 5, "items": {"$ref": "task"}}}, TYPES
    )
    assert schema["properties"]["tasks"]["items"] == {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "details": {"type": "string", "description": "What to build"},
        },
        "required": ["title", "details"],
        "additionalProperties": False,
    }


def test_keys_next_to_a_ref_are_kept():
    schema = schemas.object_schema({"t": {"$ref": "task", "description": "The task"}}, TYPES)
    assert schema["properties"]["t"]["description"] == "The task"
    assert schema["properties"]["t"]["required"] == ["title", "details"]


# ---------- checking field maps ----------


def test_a_valid_field_map_has_no_problems():
    fm = {"tasks": {"type": "array", "items": {"$ref": "task"}}, "n": {"type": "integer", "minimum": 0}}
    assert schemas.field_map_problems(fm, TYPES, "nodes.plan.output") == []


def test_unsupported_keywords_are_errors():
    fm = {"x": {"type": "string", "format": "email"}, "y": {"anyOf": [{"type": "string"}]}}
    assert where(schemas.field_map_problems(fm, {}, "out")) == [
        ("out.x", "unsupported schema keyword `format`"),
        ("out.y", "unsupported schema keyword `anyOf`"),
    ]


def test_unsupported_keywords_are_found_in_nested_schemas():
    fm = {"x": {"type": "object", "properties": {"a": {"type": "array", "items": {"oneOf": []}}}}}
    assert where(schemas.field_map_problems(fm, {}, "out")) == [
        ("out.x.properties.a.items", "unsupported schema keyword `oneOf`"),
    ]


def test_optional_only_on_top_level_fields_and_default_only_in_inputs():
    fm = {
        "x": {"type": "object", "properties": {"a": {"type": "string", "optional": True}}},
        "y": {"type": "string", "default": "hi"},
    }
    assert where(schemas.field_map_problems(fm, {}, "out")) == [
        ("out.x.properties.a", "`optional` is only allowed on top-level fields"),
        ("out.y", "`default` is only allowed in start.input"),
    ]
    assert schemas.field_map_problems({"y": {"type": "string", "default": "hi"}}, {}, "in", inputs=True) == []


def test_a_default_must_match_its_field():
    fm = {"n": {"type": "integer", "default": "three"}}
    assert where(schemas.field_map_problems(fm, {}, "start.input", inputs=True)) == [
        ("start.input.n", "default doesn't match the field: 'three' is not of type 'integer'"),
    ]


def test_unknown_refs_and_bad_types_are_errors():
    fm = {"a": {"$ref": "nope"}, "b": {"type": "text"}, "c": {"type": "string", "minLength": "x"}}
    assert where(schemas.field_map_problems(fm, {}, "out")) == [
        ("out.a", "unknown type `nope` (types: none defined)"),
        ("out.b", "`text` isn't a JSON Schema type"),
        ("out.c", "invalid schema: 'x' is not of type 'integer'"),
    ]


def test_types_that_refer_to_themselves_are_errors():
    types = {
        "a": {"b": {"$ref": "b"}},
        "b": {"list": {"type": "array", "items": {"$ref": "a"}}},
        "leaf": {"x": {"type": "string"}},
    }
    assert where(schemas.type_problems(types)) == [
        ("types.a", "type `a` refers to itself: a → b → a"),
    ]


# ---------- checking values ----------


def test_check_reports_each_problem_with_its_field_path():
    schema = schemas.object_schema(
        {"tasks": {"type": "array", "items": {"$ref": "task"}}, "n": {"type": "integer"}}, TYPES
    )
    errors = schemas.check({"tasks": [{"title": 1, "details": "d"}, {"title": "t"}], "x": 1}, schema)
    assert errors == [
        "'n' is a required property",
        "Additional properties are not allowed ('x' was unexpected)",
        "tasks[0].title: 1 is not of type 'string'",
        "tasks[1]: 'details' is a required property",
    ]
    assert schemas.check({"tasks": [], "n": 2}, schema) == []


NOTED = {"task": {"title": {"type": "string"}, "notes": {"type": "string", "optional": True}}}


def test_optional_fields_left_out_become_none_at_any_depth():
    schema = schemas.object_schema(
        {"tasks": {"type": "array", "items": {"$ref": "task"}}, "lead": {"$ref": "task", "optional": True}}, NOTED
    )
    value = {"tasks": [{"title": "a"}, {"title": "b", "notes": "n"}]}
    assert schemas.absent_as_none(value, schema) == {
        "tasks": [{"title": "a", "notes": None}, {"title": "b", "notes": "n"}], "lead": None,
    }
    assert value == {"tasks": [{"title": "a"}, {"title": "b", "notes": "n"}]}  # a copy: the value itself is kept


def test_none_counts_as_left_out_only_in_fields_that_are_not_required():
    schema = schemas.object_schema({"tasks": {"type": "array", "items": {"$ref": "task"}}}, NOTED)
    filled = {"tasks": [{"title": "a", "notes": None}]}
    assert schemas.check(filled, schema) == ["tasks[0].notes: None is not of type 'string'"]
    assert schemas.check(filled, schema, none_is_absent=True) == []
    assert schemas.check({"tasks": [{"title": None}]}, schema, none_is_absent=True) == [
        "tasks[0].title: None is not of type 'string'",
    ]
    assert schemas.check({"tasks": [{"title": "a", "other": None}]}, schema, none_is_absent=True) == [
        "tasks[0]: Additional properties are not allowed ('other' was unexpected)",
    ]


# ---------- looking up fields (for template checks) ----------


def test_field_at_walks_objects_and_lists():
    schema = schemas.object_schema({"tasks": {"type": "array", "items": {"$ref": "task"}}}, TYPES)
    assert schemas.field_at(schema, ["tasks", schemas.EACH, "title"]) == {"type": "string"}
    assert schemas.field_at(schema, ["tasks", 0, "details"])["type"] == "string"


def test_field_at_reports_missing_fields():
    schema = schemas.object_schema({"plan": {"type": "string"}})
    try:
        schemas.field_at(schema, ["plna"])
    except schemas.NoField as error:
        assert str(error) == "no field `plna` (fields: plan)"
    else:
        raise AssertionError("expected NoField")


def test_field_at_gives_up_quietly_where_it_cant_know():
    schema = schemas.object_schema({"s": {"type": "string"}, "free": {"type": "object"}})
    assert schemas.field_at(schema, ["s", "upper"]) == schemas.ANY  # a string method, not a field
    assert schemas.field_at(schema, ["free", "anything"]) == schemas.ANY
    assert schemas.field_at(schemas.ANY, ["a", "b"]) == schemas.ANY


def test_a_description_split_at_a_comma_gets_a_hint():
    fm = {"x": {"type": "string", "description": "The report", "as written": None}}  # {description: The report, as written}
    ((_, message),) = where(schemas.field_map_problems(fm, {}, "out"))
    assert message.startswith("unsupported schema keyword `as written`; if it's the end of a description with a comma")
