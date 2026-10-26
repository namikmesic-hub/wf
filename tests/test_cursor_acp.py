"""The ACP adapter for Cursor (cursor_acp.py), against a scripted fake `cursor-agent` (fake_acp.py), through `wf run`.

The fake logs every request the adapter sends and every answer it gets, so these tests check the
protocol: how cursor-agent is started, session settings, submit_result over the session's MCP
server, permission answers per access level, continued sessions, and how turns end.
The opt-in test at the bottom runs the real `cursor-agent` (uses your Cursor quota):
    uv run pytest -m harness tests/test_cursor_acp.py
"""

import asyncio
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from textwrap import dedent, indent

import pytest

from steering import STEER, at_pause, left_in_mailbox, steer_events, steer_lines
from workflow import cli, harnesses, runner, runs, schemas, workspaces
from workflow.harnesses.base import SessionSpec
from workflow.harnesses.cursor_acp import TESTED, WRITES_WITHOUT_ASKING, CursorAcp
from workflow.output_tool import FINISH, NEW_RESULT, REMINDER, ResultTool

FAKE = Path(__file__).parent / "fake_acp.py"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / "repo").mkdir()
    monkeypatch.chdir(tmp_path / "repo")
    monkeypatch.setattr(runner, "interactive", lambda: False)
    subprocess.run(["git", "init", "-q"], check=True)
    return tmp_path


def cursor(monkeypatch, tmp_path: Path, *turns, models=None, logged_out=False, silent=(), version=TESTED) -> Path:
    """Use the fake for `cursor-agent`; returns its log."""
    scenario, log = tmp_path / "scenario.json", tmp_path / "requests.jsonl"
    scenario.write_text(json.dumps({"turns": list(turns), "logged_out": logged_out, "silent": list(silent),
                                    "version": version, **({"models": models} if models else {})}))
    monkeypatch.setitem(harnesses.FACTORIES, "cursor",
                        lambda: CursorAcp(command=[sys.executable, str(FAKE), str(scenario), str(log)], request_timeout=5))
    return log


def entries(log: Path, kind: str) -> list:
    lines = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    return [e["data"] for e in lines if e["kind"] == kind]


def sent(log: Path, method: str) -> list[dict]:
    return [e["params"] for e in entries(log, "request") if e["method"] == method]


def answers(log: Path) -> list[str]:
    return [e["outcome"].get("optionId", e["outcome"]["outcome"]) for e in entries(log, "permission")]


def run(node: str = "", top: str = "", end: str = "{out: \"{{ ask.summary }}\"}") -> int:
    Path("flow.yaml").write_text(dedent(f"""
        version: 1
        name: cu
        {top}
        defaults: {{harness: cursor, harnesses: {{cursor: {{model: composer-2.5}}}}}}
        start: {{input: {{}}}}
        nodes:
          ask:
            needs: [start]
            instruction: Say hi
        """) + indent(dedent(node), "    ") + f"end:\n  output: {end}\n")
    return cli.main(["run", "flow.yaml"])


def output() -> dict:
    (folder,) = (Path(".wf") / "runs").iterdir()
    return json.loads((folder / "output.json").read_text())


def cursor_home(wf_home: Path) -> Path:
    """The run's Cursor folders, outside the repo."""
    (folder,) = (Path(".wf") / "runs").iterdir()
    return wf_home / "temp" / folder.name / "cursor-agent"


def post(url: str) -> int:
    request = urllib.request.Request(url, json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode(),
                                     {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=5) as reply:
            return reply.status
    except urllib.error.HTTPError as error:
        return error.code


def attempt_log(attempt: int = 1, pass_no: int = 1) -> list[dict]:
    (folder,) = (Path(".wf") / "runs").iterdir()
    path = folder / "nodes" / "ask" / str(pass_no) / f"attempt-{attempt}.log"
    return [json.loads(line) for line in path.read_text().splitlines()]


GOOD = {"tool": {"arguments": {"summary": "hi"}}}


def test_paths_cursor_agent_names_the_cursor_agent_that_wf_runs(repo, monkeypatch, wf_home):
    scenario, log = repo / "scenario.json", repo / "requests.jsonl"
    scenario.write_text(json.dumps({"turns": [[GOOD]], "logged_out": False, "silent": [], "version": TESTED}))
    program = repo / "other-install" / "cursor-agent"
    program.parent.mkdir()
    program.write_text(f'#!/bin/sh\nexec {shlex.join([sys.executable, str(FAKE), str(scenario), str(log)])} "$@"\n')
    program.chmod(0o755)
    (wf_home / "config.toml").write_text(f'[paths]\ncursor_agent = "{program}"\n')
    monkeypatch.setitem(harnesses.FACTORIES, "cursor", lambda: CursorAcp(request_timeout=5))
    assert run() == 0
    assert output() == {"out": "hi"} and len(entries(log, "start")) == 1


def test_a_session_runs_on_the_local_login_with_wfs_own_cursor_config(repo, monkeypatch, wf_home):
    log = cursor(monkeypatch, repo, [GOOD])
    monkeypatch.setenv("CURSOR_API_KEY", "key_from_the_dashboard")
    monkeypatch.setenv("CURSOR_AUTH_TOKEN", "token")
    assert run() == 0
    assert output() == {"out": "hi"}
    (start,) = entries(log, "start")
    assert start["args"] == ["--model", "composer-2.5", "--sandbox", "disabled", "--disable-project-configs", "acp"]
    assert (start["api_key"], start["auth_token"], start["no_browser"]) == (False, False, "1")  # the local login, never a key
    assert Path(start["cwd"]).resolve() == Path.cwd().resolve()
    config = Path(start["config"])
    assert (config.parent, start["data"]) == (cursor_home(wf_home), start["config"])  # outside the repo
    settings = start["settings"]
    assert settings["permissions"] == {"allow": [], "deny": ["Write(**)"]}
    assert (settings["approvalMode"], settings["autoAcceptWebSearch"], settings["sandbox"]) == ("allowlist", False, {"mode": "disabled"})
    assert sent(log, "authenticate") == [{"methodId": "cursor_login"}]
    assert sent(log, "initialize")[0]["clientCapabilities"] == {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False,
                                                                "_meta": {"parameterizedModelPicker": True}}
    (new,) = sent(log, "session/new")
    (server,) = new["mcpServers"]
    assert (server["type"], server["name"]) == ("http", "wf") and server["url"].startswith("http://127.0.0.1:")
    (mode,) = sent(log, "session/set_mode")
    assert mode["modeId"] == "ask"
    (prompt,) = sent(log, "session/prompt")
    assert prompt["prompt"] == [{"type": "text", "text": f"Say hi\n\n{FINISH}"}]
    assert entries(log, "tools") == [["submit_result", "git"]]  # read-only gets the runner's git tool
    assert entries(log, "reply") == [{"content": [{"type": "text", "text": "Result recorded. You're done."}], "isError": False}]
    assert not config.exists()  # the folder goes with the session


def test_the_transcript_records_the_model_and_effort_cursor_uses(repo, monkeypatch):
    cursor(monkeypatch, repo, [GOOD])
    assert run("model: gpt-5.6-sol-max-fast\n") == 0
    (session,) = [e for e in attempt_log() if e["kind"] == "session"]
    assert session["effective"] == {"model": "gpt-5.6-sol", "reasoning": "max", "fast": "true"}


@pytest.mark.parametrize("access, mode, deny", [("edit", "agent", WRITES_WITHOUT_ASKING), ("full", "agent", [])])
def test_edit_and_full_run_in_agent_mode_with_writes_allowed(repo, monkeypatch, access, mode, deny):
    log = cursor(monkeypatch, repo, [GOOD])
    assert run(f"access: {access}\n") == 0
    (start,) = entries(log, "start")
    assert start["settings"]["permissions"] == {"allow": [], "deny": deny}
    assert sent(log, "session/set_mode")[0]["modeId"] == mode
    assert entries(log, "tools") == [["submit_result"]]


def test_edit_denies_the_writes_cursor_makes_outside_the_workspace_without_asking():
    assert WRITES_WITHOUT_ASKING == ["Write(/tmp/*)", "Write(/private/tmp/*)", "Write(/var/tmp/*)", "Write(/var/folders/*)",
                                     "Write(/private/var/folders/*)", "Write(~/.cursor/*)"]


def test_each_cursor_agent_gets_a_new_config_folder_and_none_is_left(repo, monkeypatch, wf_home):
    log = cursor(monkeypatch, repo, [{"exit": True}], [GOOD])
    assert run() == 0
    folders = [Path(e["config"]) for e in entries(log, "start")]
    assert len(set(folders)) == 2 and all(folder.parent == cursor_home(wf_home) for folder in folders)
    assert not any(folder.exists() for folder in folders)
    assert not cursor_home(wf_home).exists()  # the run succeeded: its temp folder is gone


def permission_requests() -> list[dict]:
    here = str(Path.cwd())
    return [
        {"permission": {"kind": "read", "title": "Read `/etc/hosts`", "rawInput": {"path": "/etc/hosts"}}},
        {"permission": {"kind": "execute", "title": "`git status`", "rawInput": {"command": "git status"}}},
        {"permission": {"kind": "edit", "title": "Edit `a.py`", "content": [{"type": "diff", "path": f"{here}/a.py", "newText": "x"}]}},
        {"permission": {"kind": "edit", "title": "Edit `/tmp/x`", "content": [{"type": "diff", "path": f"{here}/../outside.py", "newText": "x"}]}},
        {"permission": {"kind": "other", "title": "github-create_issue: create_issue",
                        "rawInput": {"providerIdentifier": "github", "toolName": "create_issue"}}},
        {"permission": {"kind": "search", "title": "Web search: wf"}},
        {"permission": {"kind": "fetch", "title": "Fetch https://example.com"}},
    ]


def test_without_project_instructions_cursor_agent_starts_in_an_empty_folder_and_works_in_the_workspace(repo, monkeypatch):
    log = cursor(monkeypatch, repo, [GOOD], [GOOD])
    Path("AGENTS.md").write_text("You are someone else.\n")
    assert run("project_instructions: false\nroutes: [{to: ask, max: 1}, {to: end}]\nsession: continue\n") == 0
    new, loaded = entries(log, "start")
    for start in (new, loaded):  # it reads rules and AGENTS.md from the folder it starts in: an empty one
        folder = Path(start["cwd"])
        assert start["files"] == [] and not folder.exists()  # and the folder goes with the session
        assert not folder.is_relative_to(Path.home()) and not folder.is_relative_to(repo)
    assert Path(new["cwd"]) != Path(loaded["cwd"])
    workspace = str(Path.cwd().resolve())
    assert [Path(p["cwd"]).resolve() for p in sent(log, "session/new") + sent(log, "session/load")] == [Path(workspace)] * 2


@pytest.mark.parametrize("above, found", [
    ("AGENTS.md", "AGENTS.md"),
    (".cursor/rules/x.mdc", ".cursor/rules"),
    (".agents/skills/x/SKILL.md", ".agents/skills"),
    (".claude/skills/x/SKILL.md", ".claude/skills"),
    (".git/HEAD", ".git"),  # a repo: Cursor reads nested files from the whole of it
])
def test_a_temp_folder_under_what_cursor_loads_makes_it_unavailable_without_project_instructions(
        repo, monkeypatch, capsys, above, found):
    log = cursor(monkeypatch, repo, [GOOD])
    outer = repo / "outer"
    (outer / above).parent.mkdir(parents=True)
    (outer / above).write_text("You are someone else.\n")
    (outer / "tmp").mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(outer / "tmp"))  # TMPDIR under it
    assert run("project_instructions: false\n") == 1
    out = capsys.readouterr().out
    assert f"cursor is unavailable: {(outer / found).resolve()} is in a folder above the temp folder" in out
    assert entries(log, "start") == [] and list((outer / "tmp").iterdir()) == []  # no session, no folder left


def test_your_home_folders_skills_above_the_temp_folder_do_not_count(repo, monkeypatch):
    log = cursor(monkeypatch, repo, [GOOD])
    home = repo / "home"
    (home / ".claude" / "skills" / "mine").mkdir(parents=True)
    (home / "tmp").mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(tempfile, "tempdir", str(home / "tmp"))
    assert run("project_instructions: false\n") == 0  # they load wherever Cursor starts
    (start,) = entries(log, "start")
    assert Path(start["cwd"]).parent == (home / "tmp").resolve()


def test_by_default_cursor_agent_starts_in_the_workspace(repo, monkeypatch):
    log = cursor(monkeypatch, repo, [GOOD])
    assert run() == 0
    (start,) = entries(log, "start")
    assert Path(start["cwd"]).resolve() == Path.cwd().resolve()


@pytest.mark.parametrize(
    "settings, expected",
    [
        # read, command, edit inside, edit outside, another MCP server's tool, web search, web fetch, then submit_result
        ("", ["allow", "reject", "reject", "reject", "reject", "reject", "reject", "allow"]),
        ("web: true\n", ["allow", "reject", "reject", "reject", "reject", "allow", "allow", "allow"]),
        ("access: edit\n", ["allow", "allow", "allow", "reject", "reject", "reject", "reject", "allow"]),
        ("access: full\n", ["allow", "allow", "allow", "allow", "allow", "reject", "reject", "allow"]),
        ("access: full\nweb: true\n", ["allow"] * 8),
    ],
)
def test_permission_requests_are_answered_from_the_access_level(repo, monkeypatch, settings, expected):
    log = cursor(monkeypatch, repo, [*permission_requests(), GOOD])
    assert run(settings) == 0
    assert answers(log) == [f"{answer}-once" for answer in expected]  # never allow-always, which Cursor would save
    logged = [e["allowed"] for e in attempt_log() if e["kind"] == "permission"]
    assert logged == [answer == "allow" for answer in expected]


def test_a_question_for_the_user_is_skipped_even_with_full_access(repo, monkeypatch):
    question = {"permission": {"kind": "other", "title": "Which database?", "options": [
        {"optionId": "pg", "name": "Postgres", "kind": "allow_once"}, {"optionId": "my", "name": "MySQL", "kind": "allow_once"},
        {"optionId": "__ask_question_skip__", "name": "Skip", "kind": "reject_once"}]}}
    log = cursor(monkeypatch, repo, [question, GOOD])
    assert run("access: full\n") == 0
    assert answers(log)[0] == "__ask_question_skip__"


def test_progress_shows_tool_calls(repo, monkeypatch, capsys):
    cursor(monkeypatch, repo, [{"permission": {"kind": "execute", "title": "`git status`", "rawInput": {"command": "git status"}}},
                               {"permission": {"kind": "edit", "title": "Edit", "rawInput": {"path": str(Path.cwd() / "a.py")}}},
                               GOOD])
    assert run("access: edit\n") == 0
    out = capsys.readouterr().out
    assert "ask: command git status" in out
    assert "ask: edit a.py" in out
    assert "ask: submit_result" in out


def test_closing_a_session_ends_its_mcp_url(tmp_path, monkeypatch):
    cursor(monkeypatch, tmp_path)

    async def main():
        harness = harnesses.create("cursor")
        await harness.open(tmp_path / "run")
        tool = ResultTool(schemas.object_schema({"summary": {"type": "string"}}))
        spec = SessionSpec(cwd=tmp_path, access="read-only", web=False, model="composer-2.5", effort=None, tool=tool,
                           log=tmp_path / "attempt-1.log", emit=lambda event: None)
        try:
            session = await harness.session(spec)
            before = await asyncio.to_thread(post, session.url)
            await session.close()
            return before, await asyncio.to_thread(post, session.url)  # the server still runs: only the URL is gone
        finally:
            await harness.close()

    assert asyncio.run(main()) == (200, 404)


def test_a_cursor_agent_other_than_the_tested_one_gets_one_warning(repo, monkeypatch, capsys):
    cursor(monkeypatch, repo, [{"exit": True}], [GOOD], version="2026.01.01-abcdef0")
    assert run() == 0
    out = capsys.readouterr().out
    assert out.count(f"cursor-agent 2026.01.01-abcdef0 is installed; wf is tested with {TESTED}") == 1


def test_the_tested_cursor_agent_gets_no_warning(repo, monkeypatch, capsys):
    cursor(monkeypatch, repo, [GOOD])
    assert run() == 0
    assert "is installed; wf is tested with" not in capsys.readouterr().out


def test_closing_a_session_stops_the_commands_cursor_agent_started(repo, monkeypatch):
    log = cursor(monkeypatch, repo, [{"child": True}, GOOD])
    assert run() == 0
    (child,) = entries(log, "child")  # it ignores SIGTERM, so only the process group's SIGKILL stops it
    for _ in range(50):
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        os.kill(child, 9)
        pytest.fail("the command cursor-agent started still runs after its session closed")


def test_a_reminder_is_another_prompt_in_the_same_session(repo, monkeypatch):
    log = cursor(monkeypatch, repo, [{"text": "done!"}], [GOOD])
    assert run() == 0
    prompts = sent(log, "session/prompt")
    assert len(entries(log, "start")) == 1 and len({p["sessionId"] for p in prompts}) == 1
    assert prompts[1]["prompt"][0]["text"] == REMINDER


def test_a_model_cursor_does_not_list_is_unavailable_and_the_next_harness_runs(repo, monkeypatch, capsys):
    from fakes import Fake, submit
    log = cursor(monkeypatch, repo, models=["composer-2.5"])
    claude = Fake("claude-code", submit({"summary": "from claude"}))
    monkeypatch.setitem(harnesses.FACTORIES, "claude-code", claude.factory)
    assert run("harness: [cursor, claude-code]\nmodel: {cursor: claude-fable-9-max, claude-code: opus}\n") == 0
    assert output() == {"out": "from claude"}
    assert "Cursor has no model `claude-fable-9-max`" in capsys.readouterr().out
    assert entries(log, "start") == []  # no cursor-agent was started for it


def test_a_cursor_agent_that_is_not_logged_in_is_unavailable(repo, monkeypatch, capsys):
    cursor(monkeypatch, repo, logged_out=True)
    assert run() == 1
    assert "cursor is unavailable: `cursor-agent --list-models` failed (is it logged in? `cursor-agent login`)" in capsys.readouterr().out


@pytest.mark.parametrize("notice", [
    "Upgrade your plan to continue",
    "Please sign in to continue",
    "Error: ConnectError: [resource_exhausted] You've hit your usage limit",
    "Error: [unauthenticated] Backend rejected authentication.",
])
def test_a_usage_limit_or_a_login_problem_makes_cursor_unavailable(repo, monkeypatch, capsys, notice):
    cursor(monkeypatch, repo, [{"text": "Working on it"}, {"error": notice}])
    assert run() == 1
    out = capsys.readouterr().out
    assert f"cursor is unavailable: cursor: {notice}" in out
    assert "attempt 2" not in out  # no retry on the same harness


def test_a_notice_sent_in_several_chunks_is_still_read(repo, monkeypatch, capsys):
    cursor(monkeypatch, repo, [{"text": "Working on it"}, {"error": ["Error: [resource_exhausted] ", "You've hit your usage limit"]}])
    assert run() == 1
    assert "cursor is unavailable: cursor: Error: [resource_exhausted] You've hit your usage limit" in capsys.readouterr().out


@pytest.mark.parametrize("notice", ["Error: RetriableError: [resource_exhausted] Error", "Error: RetriableError: "])
def test_a_retriable_error_is_not_a_usage_limit_and_the_retry_runs_after_a_wait(repo, monkeypatch, capsys, notice):
    from fakes import record_pauses
    monkeypatch.setattr("workflow.harnesses.cursor_acp.RETRY_AFTER", 0.01)
    pauses = record_pauses(monkeypatch)
    log = cursor(monkeypatch, repo, [{"text": "Working on it"}, {"error": notice}], [GOOD])
    assert run() == 0
    out = capsys.readouterr().out
    assert f"attempt 1 failed: cursor: {notice.rstrip()}" in out  # the second has no message
    assert "unavailable" not in out
    assert pauses == [0.01]
    assert len(entries(log, "start")) == 2  # the retry runs in a new cursor-agent


def test_a_resource_exhausted_error_that_is_not_retriable_hands_over_at_once(repo, monkeypatch, capsys):
    from fakes import Fake, record_pauses, submit
    pauses = record_pauses(monkeypatch)
    log = cursor(monkeypatch, repo, [{"error": "Error: ConnectError: [resource_exhausted] You've hit your usage limit"}])
    claude = Fake("claude-code", submit({"summary": "from claude"}))
    monkeypatch.setitem(harnesses.FACTORIES, "claude-code", claude.factory)
    assert run("harness: [cursor, claude-code]\nmodel: {cursor: composer-2.5, claude-code: opus}\n") == 0
    assert output() == {"out": "from claude"}
    assert "cursor is unavailable: cursor: Error: ConnectError: [resource_exhausted]" in capsys.readouterr().out
    assert pauses == [] and len(entries(log, "start")) == 1  # no wait, and no retry on Cursor


def test_an_error_notice_fails_the_turn_and_other_text_does_not(repo, monkeypatch, capsys):
    cursor(monkeypatch, repo, [{"error": "Error: ConnectError: [internal] the model broke"}], [GOOD, {"text": "\nError: handling is covered"}])
    assert run() == 0  # attempt 2's last message isn't a notice: it mentions an error in passing
    assert "attempt 1 failed: cursor: Error: ConnectError: [internal] the model broke" in capsys.readouterr().out


def test_a_paragraph_that_only_starts_with_error_is_the_agents_own_text(repo, monkeypatch, capsys):
    log = cursor(monkeypatch, repo, [{"text": "Review done.\n\nError: no defects were found"}], [GOOD])
    assert run() == 0
    assert "failed" not in capsys.readouterr().out  # the reminder, in the same session, got the result
    assert len(entries(log, "start")) == 1 and len(sent(log, "session/prompt")) == 2


def test_an_authentication_error_in_a_turn_makes_cursor_unavailable(repo, monkeypatch, capsys):
    from fakes import Fake, submit
    cursor(monkeypatch, repo, [{"rpc_error": {"code": -32000, "message": "Authentication required"}}])
    claude = Fake("claude-code", submit({"summary": "from claude"}))
    monkeypatch.setitem(harnesses.FACTORIES, "claude-code", claude.factory)
    assert run("harness: [cursor, claude-code]\nmodel: {cursor: composer-2.5, claude-code: opus}\n") == 0
    assert output() == {"out": "from claude"}
    assert "cursor is unavailable: cursor: Authentication required" in capsys.readouterr().out


def test_a_turn_that_ends_another_way_fails(repo, monkeypatch, capsys):
    cursor(monkeypatch, repo, [{"stop": "refusal"}])
    assert run("retry: 0\n") == 1
    assert "cursor: the turn ended with stopReason refusal" in capsys.readouterr().err


def test_a_timeout_cancels_the_turn(repo, monkeypatch, capsys):
    log = cursor(monkeypatch, repo, [{"hang": True}])
    assert run("retry: 0\ntimeout: 1s\n") == 1
    assert "timed out after 1s" in capsys.readouterr().err
    (prompt,) = sent(log, "session/prompt")
    assert sent(log, "session/cancel") == [{"sessionId": prompt["sessionId"]}]


def test_cursor_agent_dying_mid_turn_fails_the_attempt_and_a_retry_starts_another(repo, monkeypatch, capsys):
    log = cursor(monkeypatch, repo, [{"exit": True}], [GOOD])
    assert run() == 0
    assert "attempt 1 failed: cursor: cursor-agent exited" in capsys.readouterr().out
    assert len(entries(log, "start")) == 2


def test_a_cursor_agent_that_does_not_answer_is_unavailable_not_a_node_timeout(repo, monkeypatch, capsys):
    cursor(monkeypatch, repo, silent=["session/new"])
    assert run() == 1
    out = capsys.readouterr()
    assert "cursor is unavailable: Cursor didn't start a session" in out.out
    assert "timed out after" not in out.out + out.err


CONTINUE = "session: continue\nroutes: [{to: ask, max: 1}, {to: end}]\n"


def test_continue_loads_the_last_passs_session(repo, monkeypatch, capsys, wf_home):
    log = cursor(monkeypatch, repo, [GOOD], [GOOD])
    assert run(CONTINUE) == 0
    (first,) = sent(log, "session/new")
    (loaded,) = sent(log, "session/load")
    session = [p["sessionId"] for p in sent(log, "session/prompt")]
    assert loaded["sessionId"] == session[0] == session[1]  # Cursor can't fork: the session itself goes on
    assert loaded["mcpServers"][0]["url"] != first["mcpServers"][0]["url"]  # each attempt has its own tools
    assert sent(log, "session/prompt")[1]["prompt"][0]["text"] == f"Say hi\n\n{NEW_RESULT}\n\n{FINISH}"
    assert "continuing the last pass's session" in capsys.readouterr().out
    texts = [e["text"] for e in attempt_log(pass_no=2) if e["kind"] == "text"]
    assert "an earlier answer" not in texts  # what session/load replays isn't this attempt's
    assert len({e["config"] for e in entries(log, "start")}) == 2  # the session moved to the new process's folder
    assert not cursor_home(wf_home).exists()
    assert not any((wf_home / "sessions").glob("*"))  # no follow-up can continue it


def test_a_follow_up_loads_the_session_that_the_run_ended_with(repo, monkeypatch, capsys, wf_home):
    log = cursor(monkeypatch, repo, [GOOD], [GOOD])
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "first"],
                   check=True)
    Path("flow.yaml").write_text(dedent("""
        version: 1
        name: cu
        defaults: {harness: cursor, harnesses: {cursor: {model: composer-2.5}}}
        start: {input: {}}
        follow_up: {to: work}
        nodes:
          work:
            needs: [start]
            session: continue
            instruction: "{% if follow_up %}Follow-up: {{ follow_up.request }}{% else %}Do it{% endif %}"
        end: {output: {}}
        """))
    assert cli.main(["run", "flow.yaml"]) == 0
    (run,) = runs.all_runs(Path.cwd())
    (session,) = {p["sessionId"] for p in sent(log, "session/prompt")}
    saved = workspaces.saved_sessions(run.dir, "cursor-agent")
    assert [folder.name for folder in saved.iterdir()] == [session]
    assert [file.name for file in (saved / session).iterdir()] == ["meta.json"]  # not the config, with the account's details
    assert cli.main(["continue", run.id, "add a flag"]) == 0
    (loaded,) = sent(log, "session/load")
    assert loaded["sessionId"] == session
    assert sent(log, "session/prompt")[-1]["prompt"][0]["text"].startswith("Follow-up: add a flag")
    assert "work: continuing the last pass's session" in capsys.readouterr().out


def test_after_a_continued_attempt_fails_the_retry_starts_fresh(repo, monkeypatch, capsys):
    log = cursor(monkeypatch, repo, [GOOD], [{"error": "Error: ConnectError: [internal] boom"}], [GOOD])
    assert run(CONTINUE) == 0
    assert len(sent(log, "session/load")) == 1  # attempt 1 of pass 2
    assert len(sent(log, "session/new")) == 2  # pass 1, and the retry
    assert "starting a fresh session: the last attempt that continued this session failed" in capsys.readouterr().out


def test_a_continued_attempt_that_times_out_after_its_result_is_not_continued_again(repo, monkeypatch, capsys):
    log = cursor(monkeypatch, repo, [GOOD], [GOOD, {"hang": True}], [GOOD])
    assert run(CONTINUE + "timeout: 2s\n") == 0
    assert (len(sent(log, "session/load")), len(sent(log, "session/new"))) == (1, 2)
    assert "starting a fresh session: the last attempt that continued this session failed" in capsys.readouterr().out


def test_a_third_pass_continues_the_session_a_successful_second_pass_continued(repo, monkeypatch):
    log = cursor(monkeypatch, repo, [GOOD], [GOOD], [GOOD])
    assert run("session: continue\nroutes: [{to: ask, max: 2}, {to: end}]\n") == 0
    assert (len(sent(log, "session/load")), len(sent(log, "session/new"))) == (2, 1)
    assert len({p["sessionId"] for p in sent(log, "session/prompt")}) == 1


def test_cursor_reports_no_usage(repo, monkeypatch):
    cursor(monkeypatch, repo, [GOOD])
    assert run(end='{tokens: {type: integer, value: "{{ run.tokens }}"}, cost: {type: number, value: "{{ run.cost_usd }}"}}') == 0
    assert output() == {"tokens": 0, "cost": 0}


@pytest.mark.harness
def test_a_real_read_only_agent_node_on_cursor(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("WF_CURSOR_RUNTIME", "acp")
    monkeypatch.chdir(tmp_path)
    git = lambda *a: subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "commit.gpgsign=false", *a],
                                    check=True, capture_output=True)
    git("init", "-q")
    Path("notes.txt").write_text("The secret word is: marmalade\n")
    git("add", "notes.txt")
    git("commit", "-qm", "first")
    Path("notes.txt").write_text("The secret word is: quince\n")  # only `git diff` shows the old one
    Path("flow.yaml").write_text("""
version: 1
name: real
defaults:
  harness: cursor
  harnesses:
    cursor: {model: composer-2.5}
start: {input: {}}
nodes:
  read:
    needs: [start]
    instruction: Use git to see what changed in notes.txt since the last commit. Report the old and the new secret word.
    output: {old: {type: string}, new: {type: string}}
end:
  output:
    old: "{{ read.old }}"
    new: "{{ read.new }}"
""")
    assert cli.main(["run", "flow.yaml"]) == 0, capsys.readouterr()
    (folder,) = Path(".wf/runs").iterdir()
    out = json.loads((folder / "output.json").read_text())
    assert (out["old"].strip(" .").lower(), out["new"].strip(" .").lower()) == ("marmalade", "quince")
    log = [json.loads(line) for line in (folder / "nodes" / "read" / "1" / "attempt-1.log").read_text().splitlines()]
    (session,) = [e for e in log if e["kind"] == "session"]
    assert session["mode"] == "ask"
    # Written by the handler behind the MCP server given in session/new: the result came that way
    assert [e for e in log if e["kind"] == "tool_result"][-1] == {
        "kind": "tool_result", "tool": "submit_result", "is_error": False, "content": "Result recorded. You're done."}
    assert Path("notes.txt").read_text() == "The secret word is: quince\n"  # and changed nothing


def started(tmp_path: Path, model: str, workspace: Path) -> dict:
    """Start one real Cursor session, send it nothing (so it costs no quota), and return the
    session entry of its transcript."""
    async def main():
        harness = CursorAcp()
        await harness.open(tmp_path / "run")
        tool = ResultTool(schemas.object_schema({"summary": {"type": "string"}}))
        spec = SessionSpec(cwd=workspace, access="read-only", web=False, model=model, effort=None, tool=tool,
                           log=tmp_path / "attempt-1.log", emit=lambda event: None)
        try:
            await (await harness.session(spec)).close()
        finally:
            await harness.close()
    asyncio.run(main())
    return next(e for e in map(json.loads, (tmp_path / "attempt-1.log").read_text().splitlines()) if e["kind"] == "session")


@pytest.mark.harness
def test_a_real_cursor_session_takes_the_effort_in_the_model_id(tmp_path):
    session = started(tmp_path, "gpt-5.6-sol-max-fast", tmp_path)
    assert (session["effective"]["model"], session["effective"]["reasoning"], session["effective"]["fast"]) == ("gpt-5.6-sol", "max", "true")


@pytest.mark.harness
def test_a_real_cursor_session_ignores_the_repos_cursor_settings(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".cursor").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / ".cursor" / "cli.json").write_text('{"permissions": 5}')  # cursor-agent exits at once when it reads this
    assert started(tmp_path, "composer-2.5", repo)["id"]


@pytest.mark.harness
def test_a_real_cursor_node_without_project_instructions_gets_no_repo_rules_and_still_edits(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("WF_CURSOR_RUNTIME", "acp")
    monkeypatch.chdir(tmp_path)
    git = lambda *a: subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "commit.gpgsign=false", *a],
                                    check=True, capture_output=True)
    git("init", "-q")
    Path("AGENTS.md").write_text("The code word for this project is: periwinkle\n")
    (tmp_path / ".cursor" / "rules").mkdir(parents=True)
    Path(".cursor/rules/words.mdc").write_text("---\nalwaysApply: true\n---\nThe second code word is: tangerine\n")
    git("add", "-A")
    git("commit", "-qm", "first")
    ask = "Don't read, search or open any file. From what you were told alone, list every code word for this project, or none."
    Path("flow.yaml").write_text(f"""
version: 1
name: real
defaults:
  harness: cursor
  harnesses:
    cursor: {{model: composer-2.5}}
start: {{input: {{}}}}
nodes:
  told:
    needs: [start]
    instruction: "{ask}"
    output: {{words: {{type: string}}}}
  kept_out:
    needs: [start]
    access: edit
    project_instructions: false
    instruction: "Write the text done into a new file out.txt here. Then: {ask}"
    output: {{words: {{type: string}}}}
end:
  output:
    told: "{{{{ told.words }}}}"
    kept_out: "{{{{ kept_out.words }}}}"
""")
    assert cli.main(["run", "flow.yaml"]) == 0, capsys.readouterr()
    (folder,) = Path(".wf/runs").iterdir()
    out = json.loads((folder / "output.json").read_text())
    assert "periwinkle" in out["told"].lower() and "tangerine" in out["told"].lower()  # by default the repo's files reach it
    assert "periwinkle" not in out["kept_out"].lower() and "tangerine" not in out["kept_out"].lower()
    assert Path("out.txt").read_text().strip() == "done"  # it still worked in the workspace


# ---------- wf steer ----------


def test_a_message_from_wf_steer_goes_to_cursor_as_the_next_turn(repo, monkeypatch, capsys):
    log = cursor(monkeypatch, repo, [{"pause": "p"}], [GOOD])
    steering = at_pause(repo, "p")
    assert run() == 0
    done = steering.result()
    assert done.returncode == 0, done.stderr
    assert done.stdout.startswith("Queued for ask (attempt 1 on cursor)")  # ACP can't add to a running turn
    assert [prompt["prompt"][0]["text"] for prompt in sent(log, "session/prompt")] == [f"Say hi\n\n{FINISH}", STEER]
    queued, delivered = steer_events()
    assert (queued["status"], queued["text"], queued["harness"]) == ("queued", STEER, "cursor")
    assert (delivered["status"], delivered["id"]) == ("delivered", queued["id"])
    assert [line["status"] for line in steer_lines()] == ["queued", "delivered"]
    assert left_in_mailbox() == []
    out = capsys.readouterr().out
    assert f"ask: message from wf steer queued: {STEER}" in out
    assert "ask: message from wf steer delivered" in out


def test_a_message_for_cursor_is_dropped_when_the_agent_submits_first(repo, monkeypatch):
    log = cursor(monkeypatch, repo, [{"pause": "p"}, GOOD])
    steering = at_pause(repo, "p")
    assert run() == 0
    assert steering.result().returncode == 0
    assert len(sent(log, "session/prompt")) == 1
    queued, dropped = steer_events()
    assert (queued["status"], dropped["status"]) == ("queued", "dropped")
    assert dropped["reason"] == "the agent submitted its result before it read the message"
