"""ask: terminal questions, with typed answers fed in through `read`."""

import pytest

from workflow import ask, schemas


def asker(*lines: str):
    """read() that answers with these lines, and write() that records what was shown."""
    typed, shown = iter(lines), []

    def read(prompt: str) -> str:
        shown.append(prompt)
        return next(typed)

    return read, shown.append, shown


def answer(fields: dict, *lines: str):
    read, write, shown = asker(*lines)
    return ask.ask_fields(schemas.object_schema(fields), read=read, write=write), shown


@pytest.fixture(autouse=True)
def no_editor(monkeypatch):
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.delenv("EDITOR", raising=False)


def test_each_kind_of_field():
    fields = {
        "ok": {"type": "boolean"},
        "size": {"enum": ["small", "large"]},
        "again": {"enum": ["small", "large"]},
        "n": {"type": "integer"},
        "name": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
    }
    got, _ = answer(fields, "y", "2", "small", "3", "Ada", '["a", "b"]')
    assert got == {"ok": True, "size": "large", "again": "small", "n": 3, "name": "Ada", "tags": ["a", "b"]}


def test_answers_that_do_not_fit_are_asked_again():
    fields = {"ok": {"type": "boolean"}, "n": {"type": "integer", "minimum": 1}, "size": {"enum": ["s", "l"]}}
    got, shown = answer(fields, "maybe", "no", "x", "0", "2", "9", "1")
    assert got == {"ok": False, "n": 2, "size": "s"}
    assert "  answer yes or no" in shown
    assert "  enter a number" in shown
    assert "  0 is less than the minimum of 1" in shown
    assert "  pick a number from 1 to 2" in shown


def test_optional_fields_can_be_skipped():
    got, _ = answer({"note": {"type": "string", "optional": True}, "x": {"type": "object", "optional": True}}, "", "")
    assert got == {}


def test_bad_json_is_asked_again():
    got, shown = answer({"cfg": {"type": "object"}}, "{nope", '{"a": 1}')
    assert got == {"cfg": {"a": 1}}
    assert any("isn't valid JSON" in line for line in shown)


def test_the_editor_keeps_the_last_draft(tmp_path, monkeypatch):
    editor = tmp_path / "edit"
    # First run writes a wrong value; the second sees that draft and fixes it
    editor.write_text("#!/bin/sh\nif grep -q '\"x\"' \"$1\"; then echo '{\"a\": 1}' > \"$1\"; else echo '{\"a\": \"x\"}' > \"$1\"; fi\n")
    editor.chmod(0o755)
    monkeypatch.setenv("EDITOR", str(editor))
    fields = {"cfg": {"type": "object", "properties": {"a": {"type": "integer"}}, "required": ["a"]}}
    got, shown = answer(fields, "", "")
    assert got == {"cfg": {"a": 1}}
    assert "  a: 'x' is not of type 'integer'" in shown


def test_an_editor_that_cannot_run_falls_back_to_typing(monkeypatch):
    monkeypatch.setenv("EDITOR", "no-such-editor-anywhere")
    got, shown = answer({"cfg": {"type": "object"}}, "", '{"b": 2}')
    assert got == {"cfg": {"b": 2}}
    assert any(line.startswith("  can't run no-such-editor-anywhere") for line in shown)


def test_end_of_input_raises_no_answer():
    def closed(prompt: str) -> str:
        raise EOFError

    with pytest.raises(ask.NoAnswer):
        ask.ask_fields(schemas.object_schema({"ok": {"type": "boolean"}}), read=closed)


def test_enum_values_that_look_like_numbers_match_by_value_first():
    got, _ = answer({"pick": {"enum": [3, 1, 2]}, "label": {"enum": ["2", "1"]}}, "1", "1")
    assert got == {"pick": 1, "label": "1"}


def test_a_nullable_boolean_is_still_yes_or_no():
    got, _ = answer({"ok": {"type": ["boolean", "null"]}}, "yes")
    assert got == {"ok": True}


def test_a_required_text_answer_cannot_be_empty():
    got, shown = answer({"why": {"type": "string"}}, "", "because")
    assert got == {"why": "because"}
    assert "  an answer is required" in shown


def test_an_aborted_editor_asks_again_instead_of_skipping(tmp_path, monkeypatch):
    editor = tmp_path / "edit"
    # Fails the first time; writes JSON the second time
    editor.write_text(f"#!/bin/sh\nif [ -e {tmp_path}/once ]; then echo '[1]' > \"$1\"; else touch {tmp_path}/once; exit 1; fi\n")
    editor.chmod(0o755)
    monkeypatch.setenv("EDITOR", str(editor))
    got, shown = answer({"xs": {"type": "array", "optional": True}}, "", "")
    assert got == {"xs": [1]}
    assert any("nothing was saved" in line for line in shown)


def test_a_required_answer_that_may_be_text_or_a_number_cannot_be_empty():
    got, shown = answer({"v": {"type": ["string", "number"]}}, "", "7")
    assert got == {"v": 7}
    assert "  an answer is required" in shown
