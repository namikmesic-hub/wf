"""output_tool: submit_result checks results, explains problems, and keeps the first valid one."""

from workflow import output_tool, schemas

SCHEMA = schemas.object_schema({
    "summary": {"type": "string"},
    "files": {"type": "array", "items": {"type": "string"}},
})


def test_a_valid_call_is_the_result():
    tool = output_tool.ResultTool(SCHEMA)
    assert tool.submit({"summary": "done", "files": ["a.py"]}) == ("Result recorded. You're done.", False)
    assert tool.result == {"summary": "done", "files": ["a.py"]}


def test_problems_come_back_as_text_with_field_paths():
    tool = output_tool.ResultTool(SCHEMA)
    text, is_error = tool.submit({"summary": 3, "files": ["a.py", 7], "extra": True})
    assert is_error and tool.result is None
    assert "wasn't recorded" in text
    assert "- summary: 3 is not of type 'string'" in text
    assert "- files[1]: 7 is not of type 'string'" in text
    assert "- Additional properties are not allowed ('extra' was unexpected)" in text


def test_the_first_valid_call_wins():
    tool = output_tool.ResultTool(SCHEMA)
    tool.submit({"summary": "bad"})  # missing files: not recorded
    tool.submit({"summary": "first", "files": []})
    text, is_error = tool.submit({"summary": "second", "files": []})
    assert is_error and "already recorded" in text
    assert tool.result == {"summary": "first", "files": []}


def test_the_instruction_ending_and_reminder_match_the_spec():
    assert output_tool.FINISH == (
        "When you're done, call the `submit_result` tool with your result. The task isn't complete "
        "until that call succeeds. If it returns errors, fix them and call it again."
    )
    assert output_tool.REMINDER == "You haven't called submit_result yet. Call it now with your result."
