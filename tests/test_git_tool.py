"""git_tool: the read-only `git` tool agents get instead of a shell."""

import subprocess
from pathlib import Path

import pytest

from workflow.harnesses import git_tool


@pytest.fixture
def repo(tmp_path):
    def git(*args):
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-q")
    (tmp_path / "a.txt").write_text("one\n")
    git("add", "a.txt")
    git("commit", "-qm", "first")
    (tmp_path / "a.txt").write_text("two\n")
    return tmp_path


def test_read_only_commands_run(repo):
    text, is_error = git_tool.run(repo, ["diff"])
    assert not is_error and "-one" in text and "+two" in text
    text, is_error = git_tool.run(repo, ["log", "--oneline"])
    assert not is_error and "first" in text
    assert git_tool.run(repo, ["show", "HEAD:a.txt"]) == ("one\n", False)


@pytest.mark.parametrize(
    "args, problem",
    [
        (["commit", "-m", "x"], "only diff, log, show, blame and status"),
        (["diff", "--output=out.txt"], "`--output=out.txt` isn't allowed"),
        (["diff", "--ext-diff"], "`--ext-diff` isn't allowed"),
        (["log", "--textconv"], "`--textconv` isn't allowed"),
        ([], "a list of strings, starting with the command"),
    ],
)
def test_anything_that_could_write_or_run_programs_is_refused(repo, args, problem):
    text, is_error = git_tool.run(repo, args)
    assert is_error and problem in text
    assert not (repo / "out.txt").exists()


def test_git_errors_come_back_as_text(repo):
    text, is_error = git_tool.run(repo, ["show", "nope:missing"])
    assert is_error and "exited with code" in text


def test_long_output_is_cut(repo):
    (repo / "big.txt").write_text("x" * 100_000)
    subprocess.run(["git", "add", "-N", "big.txt"], cwd=repo, check=True)
    text, is_error = git_tool.run(repo, ["diff"])
    assert not is_error and len(text) < 60_000 and "output cut" in text


@pytest.mark.parametrize(
    "args, problem",
    [
        (["diff", "--ext"], "`--ext` isn't allowed"),  # git accepts abbreviations of long options
        (["log", "--textc"], "`--textc` isn't allowed"),
        (["diff", "--outp=x"], "`--outp=x` isn't allowed"),
        (["blame", "--help"], "`--help` isn't allowed"),  # would launch the configured man viewer
        (["status", "--hel"], "`--hel` isn't allowed"),
        (["diff", "--no-index", "/dev/null", "/etc/hosts"], "`--no-index` isn't allowed"),  # reads outside the repo
        (["blame", "--contents=/etc/hosts", "a.txt"], "`--contents=/etc/hosts` isn't allowed"),
    ],
)
def test_abbreviations_help_and_reads_outside_the_repo_are_refused(repo, args, problem):
    text, is_error = git_tool.run(repo, args)
    assert is_error and problem in text


def test_arguments_must_be_strings(repo):
    text, is_error = git_tool.run(repo, ["log", -20])
    assert is_error and "list of strings" in text


def test_empty_output_and_failing_exit_codes_still_say_something(repo):
    subprocess.run(["git", "checkout", "-q", "a.txt"], cwd=repo, check=True)
    assert git_tool.run(repo, ["diff"]) == ("(git diff produced no output)", False)
    (repo / "a.txt").write_text("three\n")
    text, is_error = git_tool.run(repo, ["diff", "--exit-code"])
    assert is_error and "+three" in text  # exit code 1 means "there are differences": keep the diff


@pytest.mark.parametrize("args", [
    ["diff", "/etc/hosts", "a.txt"],  # git diff turns on --no-index by itself for a path outside the repo
    ["diff", "../outside.txt", "a.txt"],
    ["log", "--", "/etc"],
    ["show", "HEAD", "--", "../outside.txt"],
])
def test_paths_outside_the_repo_are_refused(repo, args):
    (repo.parent / "outside.txt").write_text("secret\n")
    text, is_error = git_tool.run(repo, args)
    assert is_error and "outside the repo" in text
    assert "secret" not in text


def test_revision_ranges_and_real_options_still_work(repo):
    text, is_error = git_tool.run(repo, ["diff", "--text", "HEAD"])  # --text is real, not --textconv
    assert not is_error and "+two" in text
    assert not git_tool.run(repo, ["log", "HEAD~0..HEAD", "--oneline"])[1]


def test_a_null_byte_is_an_error_not_a_crash(repo):
    text, is_error = git_tool.run(repo, ["log", "bad\x00arg"])
    assert is_error


def test_error_replies_stay_within_the_limit(repo, monkeypatch):
    monkeypatch.setattr(git_tool, "LIMIT", 100)
    (repo / "big.txt").write_text("y\n" * 20)  # under the read cap, over LIMIT once wrapped in the error text
    subprocess.run(["git", "add", "-N", "big.txt"], cwd=repo, check=True)
    text, is_error = git_tool.run(repo, ["diff", "--exit-code"])
    assert is_error and len(text) < 200 and "output cut" in text


def test_option_values_that_name_files_outside_the_repo_are_refused(repo):
    text, is_error = git_tool.run(repo, ["blame", "--ignore-revs-file=/etc/hosts", "a.txt"])
    assert is_error and "outside the repo" in text


def test_blame_revisions_from_a_file_and_order_files_are_refused(repo):
    assert git_tool.run(repo, ["blame", "-S", "/etc/hosts", "a.txt"])[1]
    assert git_tool.run(repo, ["diff", "-O/etc/hosts"])[1]
    text, is_error = git_tool.run(repo, ["log", "-S", "needle"])  # log's -S is a search, still fine
    assert not is_error


def test_a_configured_fsmonitor_hook_does_not_run(repo):
    hook = repo / "hook.sh"
    hook.write_text(f"#!/bin/sh\ntouch {repo}/ran\n")
    hook.chmod(0o755)
    subprocess.run(["git", "config", "core.fsmonitor", str(hook)], cwd=repo, check=True)
    git_tool.run(repo, ["status"])
    git_tool.run(repo, ["diff"])
    assert not (repo / "ran").exists()


def test_bundled_short_options_cannot_name_files_outside_the_repo(repo):
    assert git_tool.run(repo, ["blame", "-wS/etc/hosts", "a.txt"])[1]  # -w plus -S /etc/hosts
    assert git_tool.run(repo, ["diff", "-wO/etc/hosts"])[1]  # -w plus -O /etc/hosts
    assert not git_tool.run(repo, ["diff", "-w"])[1]


def test_a_value_that_merely_contains_a_letter_is_not_an_option(repo):
    text, is_error = git_tool.run(repo, ["blame", "-L/one/,+1", "a.txt"])  # -L takes the rest: no -S here
    assert "isn't allowed" not in text
    assert "isn't allowed" not in git_tool.run(repo, ["log", "-SOrder"])[0]


def test_git_variables_cannot_point_the_tool_at_another_repo(repo, tmp_path_factory, monkeypatch):
    other = tmp_path_factory.mktemp("other")
    subprocess.run(["git", "init", "-q", str(other)], check=True)
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.fsmonitor")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "/bin/false")
    text, is_error = git_tool.run(repo, ["log", "--format=%s"])
    assert (text, is_error) == ("first\n", False)


def test_flags_bundled_before_blames_revs_file_option_are_read_as_git_reads_them(repo):
    assert git_tool.run(repo, ["blame", "-lS/etc/hosts", "a.txt"])[1]  # -l is a flag in blame
    assert git_tool.run(repo, ["blame", "-nS/etc/hosts", "a.txt"])[1]
    assert not git_tool.run(repo, ["blame", "-ln", "a.txt"])[1]


def test_a_cut_output_keeps_a_failing_exit_code(repo, monkeypatch):
    monkeypatch.setattr(git_tool, "LIMIT", 10)
    (repo / "a.txt").write_text("changed a lot\n" * 50)
    text, is_error = git_tool.run(repo, ["diff", "--exit-code"])  # exits 1: there are differences
    assert is_error and "[output cut at 10 characters" in text
