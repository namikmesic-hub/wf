"""wf's file tools for read-only agents on Cursor's SDK (files_tool.py): they read and search only
the workspace and the folders the step's instruction names."""

import os
import subprocess
from pathlib import Path

import pytest

from workflow.harnesses import files_tool


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "work"
    (root / "src").mkdir(parents=True)
    (root / "src" / "a.py").write_text("def main():\n    return 'quince'\n")
    (root / "notes.txt").write_text("one\ntwo\nthree\n")
    (tmp_path / "private").mkdir()
    (tmp_path / "private" / "secret.txt").write_text("quince is the secret\n")
    return root


def call(name: str, workspace: Path, roots=(), **args) -> tuple[str, bool]:
    return files_tool.run(name, workspace, list(roots), args)


def test_read_file_numbers_lines_and_reads_part_of_a_file(workspace):
    assert call("read_file", workspace, path="notes.txt") == ("     1\tone\n     2\ttwo\n     3\tthree", False)
    text, is_error = call("read_file", workspace, path="notes.txt", offset=2, limit=1)
    assert not is_error and text == "     2\ttwo\n[1 more lines: read on with offset 3]"
    assert call("read_file", workspace, path=str(workspace / "src" / "a.py"))[0].startswith("     1\tdef main():")


@pytest.mark.parametrize("path", ["../private/secret.txt", "{tmp}/private/secret.txt", "~", "/", "/Users"])
def test_a_path_outside_the_workspace_is_refused(workspace, path):
    text, is_error = call("read_file", workspace, path=path.format(tmp=workspace.parent))
    assert is_error and "is outside the workspace and the folders your instruction names" in text


def test_a_search_of_the_home_folder_is_refused_before_it_walks_anything(workspace, monkeypatch):
    walked = []
    monkeypatch.setattr(files_tool, "_files", lambda folder, allowed: walked.append(folder) or iter(()))
    for name, args in [("search_files", {"pattern": "status.json", "path": str(Path.home())}),
                       ("find_files", {"pattern": "**/status.json", "path": str(Path.home())}),
                       ("list_dir", {"path": str(Path.home())})]:
        text, is_error = call(name, workspace, **args)
        assert is_error and "is outside the workspace" in text
    assert walked == []


def test_a_symlink_out_of_the_workspace_is_neither_read_nor_searched(workspace):
    (workspace / "link.txt").symlink_to(workspace.parent / "private" / "secret.txt")
    (workspace / "linked").symlink_to(workspace.parent / "private")
    assert call("read_file", workspace, path="link.txt")[1] is True
    assert call("read_file", workspace, path="linked/secret.txt")[1] is True
    text, is_error = call("search_files", workspace, pattern="quince")
    assert not is_error and text == "src/a.py:2:    return 'quince'"


def test_a_folder_the_instruction_names_can_be_read(workspace):
    other = workspace.parent / "private"
    text, is_error = call("search_files", workspace, roots=[other], pattern="secret", path=str(other))
    assert not is_error and text == f"{other / 'secret.txt'}:1:quince is the secret"
    assert call("read_file", workspace, roots=[other], path=str(other / "secret.txt"))[1] is False


def test_search_files_finds_lines_and_paths(workspace):
    assert call("search_files", workspace, pattern=r"^t\w+")[0] == "notes.txt:2:two\nnotes.txt:3:three"
    assert call("search_files", workspace, pattern="QUINCE", case_insensitive=True, files_only=True)[0] == "src/a.py"
    assert call("search_files", workspace, pattern="re", glob="*.py")[0] == "src/a.py:2:    return 'quince'"
    text, is_error = call("search_files", workspace, pattern="(")
    assert is_error and "isn't a valid regular expression" in text


def test_find_files_and_list_dir(workspace):
    assert call("find_files", workspace, pattern="**/*.py")[0] == "src/a.py"
    assert call("find_files", workspace, pattern="*.txt")[0] == "notes.txt"
    assert call("list_dir", workspace)[0] == "notes.txt\nsrc/"
    assert call("list_dir", workspace, path="src")[0] == "a.py"


def test_a_search_in_a_repo_leaves_out_what_git_ignores(workspace):
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    (workspace / ".gitignore").write_text("build/\n")
    (workspace / "build").mkdir()
    (workspace / "build" / "out.py").write_text("quince = 1\n")
    assert call("search_files", workspace, pattern="quince", files_only=True)[0] == "src/a.py"
    (workspace / "build" / "only.txt").write_text("x\n")
    assert call("find_files", workspace, pattern="*", path="build")[0] == "build/only.txt\nbuild/out.py"  # ignored all through: walked


def test_answers_stop_at_the_limit(workspace, monkeypatch):
    monkeypatch.setattr(files_tool, "LIMIT", 40)
    (workspace / "long.txt").write_text("quince\n" * 100)
    text, is_error = call("read_file", workspace, path="long.txt")
    assert not is_error and text.endswith("[answer cut at 40 characters: narrow the call]")


def test_a_binary_file_or_a_folder_is_not_read_as_text(workspace):
    (workspace / "blob.bin").write_bytes(b"\x00\x01\x02")
    assert call("read_file", workspace, path="blob.bin") == ("Error: `blob.bin` is a binary file", True)
    assert call("read_file", workspace, path="src") == ("Error: `src` is a folder: use list_dir", True)
    assert call("read_file", workspace, path="nope.txt") == ("Error: `nope.txt` doesn't exist", True)


def test_the_tools_names_differ_from_cursors_own():
    assert set(files_tool.NAMES).isdisjoint({"read", "grep", "glob", "ls", "semSearch", "edit", "shell"})
    for name in files_tool.NAMES:
        assert files_tool.schema(name)["type"] == "object" and files_tool.description(name)
    assert os.sep == "/"  # the tools speak in / paths, as the agent does
