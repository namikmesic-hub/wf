"""Ask: questions for a person at the terminal.

Used for a human node's output, and for workflow inputs that weren't given on
the command line. Each field is asked in the way that suits its schema:

- boolean: yes or no
- enum: a numbered choice (the number, or the value itself)
- string, number, integer: a line of text
- array, object, anything else: JSON, in $VISUAL or $EDITOR if one is set,
  else on one line

Every answer is checked against its field's schema, and asked again until it
fits; JSON written in the editor is kept for the next try. An optional field
can be skipped with an empty answer (with `-` when the editor is used). End of
input (Ctrl-D, or stdin closed) raises NoAnswer, so the runner can leave the
run waiting for `wf resume`.

`read` and `write` default to input() and print(), looked up when called, so
tests can pass their own or patch builtins.input.
"""

import json
import os
import shlex
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path

from . import schemas

Read = Callable[[str], str]
Write = Callable[[str], None]
_SKIP = object()
_AGAIN = object()  # the editor was closed without saving: ask again


class NoAnswer(Exception):
    """The input ended before the question was answered."""


def ask_fields(schema: dict, *, read: Read | None = None, write: Write | None = None) -> dict:
    """Ask for every field of an object schema; returns the answers."""
    required = set(schema.get("required", []))
    answers = {}
    for name, field in schema.get("properties", {}).items():
        value = ask_field(name, field, required=name in required, read=read, write=write)
        if value is not _SKIP:
            answers[name] = value
    return answers


def ask_field(name: str, field: dict, *, required: bool = True, read: Read | None = None, write: Write | None = None):
    """Ask for one field until the answer fits its schema. Returns _SKIP for a skipped optional field."""
    write = write or print

    def prompt(text: str) -> str:
        try:
            return (read or input)(text).strip()
        except EOFError:
            raise NoAnswer(name) from None

    kinds = schemas.types_of(field) or set()
    label = name + (f" ({field['description']})" if field.get("description") else "")
    skip = "" if required else ", Enter to skip"
    draft = None  # JSON from the editor, kept across tries
    while True:
        if "enum" in field:
            for number, choice in enumerate(field["enum"], 1):
                write(f"  {number}. {choice}")
            raw = prompt(f"{label} [1-{len(field['enum'])}{skip}]: ")
        elif kinds and "boolean" in kinds and kinds <= {"boolean", "null"}:
            raw = prompt(f"{label} [yes/no{skip}]: ")
        elif kinds and kinds <= {"string", "number", "integer", "null"}:
            raw = prompt(f"{label}{f' [{skip[2:]}]' if skip else ''}: ")
        else:
            raw = _json_answer(label, field, required, draft, prompt, write)
            if raw is _AGAIN:
                continue
            draft = raw or None
        if raw == "" and not required:
            return _SKIP
        if raw == "" and "enum" not in field and "string" in kinds:
            write("  an answer is required")
            continue
        try:
            value = _parse(raw, field, kinds)
        except ValueError as error:
            write(f"  {error}")
            continue
        problems = schemas.check(value, field)
        if not problems:
            return value
        write(f"  {problems[0]}")


def _parse(raw: str, field: dict, kinds: set[str]):
    if "enum" in field:
        matches = [c for c in field["enum"] if str(c) == raw]  # the value itself wins over a position
        if matches:
            return matches[0]
        if raw.isdigit() and 1 <= int(raw) <= len(field["enum"]):
            return field["enum"][int(raw) - 1]
        raise ValueError(f"pick a number from 1 to {len(field['enum'])}")
    if kinds and "boolean" in kinds and kinds <= {"boolean", "null"}:
        answer = raw.lower()
        if answer in ("y", "yes", "true"):
            return True
        if answer in ("n", "no", "false"):
            return False
        raise ValueError("answer yes or no")
    if kinds and kinds <= {"string", "null"}:
        return raw
    if kinds and kinds <= {"string", "number", "integer", "null"}:
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            if "string" in kinds:
                return raw
            raise ValueError("enter a number") from None
        return value if not isinstance(value, (list, dict)) else raw
    try:
        return json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError(f"that isn't valid JSON: {error.msg} (line {error.lineno})") from None


def _json_answer(label: str, field: dict, required: bool, draft: str | None, prompt, write: Write):
    """JSON for an array or object field: in the editor if there is one, else typed on one line."""
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    skip = "" if required else ", Enter to skip"
    if not editor:
        return prompt(f"{label} (JSON{skip}): ")
    if prompt(f"{label}: press Enter to write it as JSON in {editor}{'' if required else ', or - to skip'} ") == "-" and not required:
        return ""
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as file:
        file.write(draft if draft is not None else "[]\n" if "array" in (schemas.types_of(field) or ()) else "{}\n")
    path = Path(file.name)
    try:
        try:
            done = subprocess.run([*shlex.split(editor), str(path)])
        except OSError as error:  # $EDITOR names something we can't run
            write(f"  can't run {editor}: {error.strerror or error}")
            return prompt(f"{label} (JSON{skip}): ")
        if done.returncode != 0:
            write(f"  {editor} exited with code {done.returncode}; nothing was saved")
            return _AGAIN
        try:
            return path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError) as error:  # not UTF-8, or the editor replaced the file
            write(f"  can't read back what {editor} saved: {error}")
            return _AGAIN
    finally:
        path.unlink(missing_ok=True)
