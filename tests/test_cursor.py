"""Cursor's runtime on its SDK (harnesses/cursor.py and the Cursor host), through `wf run`.

The real Cursor host runs, on a fake Cursor SDK (tests/cursor_sdk.py, tests/fake_cursor_sdk/),
so these tests need Node.js but no Cursor account. The fake logs what the host asks of the SDK:
the options for each agent, each send, steer and cancel, and what each tool call got back.
The opt-in tests at the bottom run the real SDK, with your Cursor API key (they cost quota):
    uv run pytest -m harness tests/test_cursor.py
"""

import json
import os
import stat
import subprocess
import time
from pathlib import Path
from textwrap import dedent, indent

import pytest

from cursor_sdk import KEY, entries, needs_node, use
from workflow import cli, runner, runs, workspaces
from workflow.output_tool import FINISH

pytestmark = needs_node

GOOD = {"tool": {"name": "submit_result", "arguments": {"summary": "hi"}}}


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / "repo").mkdir()
    monkeypatch.chdir(tmp_path / "repo")
    monkeypatch.setattr(runner, "interactive", lambda: False)
    subprocess.run(["git", "init", "-q"], check=True)
    return tmp_path


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


def run_folder() -> Path:
    (folder,) = (Path(".wf") / "runs").iterdir()
    return folder


def output() -> dict:
    return json.loads((run_folder() / "output.json").read_text())


def attempt_log(attempt: int = 1, pass_no: int = 1) -> list[dict]:
    path = run_folder() / "nodes" / "ask" / str(pass_no) / f"attempt-{attempt}.log"
    return [json.loads(line) for line in path.read_text().splitlines()]


def events(kind: str) -> list[dict]:
    lines = [json.loads(line) for line in (run_folder() / "events.jsonl").read_text().splitlines()]
    return [e for e in lines if e["event"] == kind]


def test_a_read_only_session_runs_on_the_sdk_with_wfs_settings(repo, monkeypatch, cursor_host, wf_home):
    log = use(monkeypatch, repo, cursor_host, [GOOD])
    monkeypatch.setenv("CURSOR_API_KEY", "key_from_the_environment")
    monkeypatch.setenv("CURSOR_AUTH_TOKEN", "token")
    assert run() == 0
    assert output() == {"out": "hi"}
    (loaded,) = entries(log, "loaded")
    assert (loaded["api_key_env"], loaded["auth_token_env"], loaded["no_browser"]) == (False, False, "1")
    home = wf_home / "temp" / run_folder().name / "cursor-sdk"
    assert Path(loaded["cwd"]).resolve() == home.resolve()  # outside the repo
    (create,) = entries(log, "create")
    assert create["apiKey"] == KEY  # from the Keychain, through stdin
    assert create["model"] == {"id": "composer-2.5"}
    assert (create["mode"], create["tools"], create["disallowedTools"], create["mcpServers"]) == (
        "agent", ["mcp"], ["task", "askQuestion"], {})
    local = create["local"]
    assert Path(local["cwd"]).resolve() == Path.cwd().resolve()
    assert (local["settingSources"], local["sandboxOptions"], local["enableAgentRetries"]) == (["project"], {"enabled": True}, True)
    assert sorted(local["customTools"]) == ["find_files", "git", "list_dir", "read_file", "search_files", "submit_result"]
    assert Path(local["store"]["store"]).parent.resolve() == home.resolve()
    (send,) = entries(log, "send")
    assert send["text"] == f"Say hi\n\n{FINISH}"
    assert send["options"]["mcpServers"] == {} and sorted(send["options"]["local"]["customTools"]) == sorted(local["customTools"])
    (result,) = entries(log, "tool_result")
    assert result == {"name": "submit_result", "result": {"content": [{"type": "text", "text": "Result recorded. You're done."}],
                                                           "isError": False}}
    assert not Path(local["store"]["store"]).exists()  # the session's state goes with it
    assert not home.exists()  # the run succeeded: its temp folder is gone


def test_tokens_that_the_sdk_reports_count_in_the_run_and_its_events(repo, monkeypatch, cursor_host):
    use(monkeypatch, repo, cursor_host, [{"text": "Looking."}], [GOOD])  # a reminder: two turns
    assert run(end='{tokens: {type: integer, value: "{{ run.tokens }}"}}') == 0
    assert output() == {"tokens": 2400}
    (finished,) = events("node_finished")
    assert finished["tokens"] == 2400
    turns = [e for e in attempt_log() if e["kind"] == "turn"]
    assert [t["usage"]["total_tokens"] for t in turns] == [1200, 1200]
    assert [t["usage"]["cost_source"] for t in turns] == ["pending", "pending"]
    (billed,) = [e for e in attempt_log() if e["kind"] == "usage"]
    assert billed["billed"]["cost"] == {"raw_cost_cents": 1.5, "charged_cents": 0}  # what Cursor bills, for the record


def trace(kind: str) -> list[dict]:
    return [r for r in map(json.loads, (run_folder() / "trace.jsonl").read_text().splitlines()) if r["kind"] == kind]


def test_the_trace_keeps_the_parts_of_the_tokens_that_the_sdk_reports(repo, monkeypatch, cursor_host):
    reported = {"inputTokens": 300, "outputTokens": 250, "cacheReadTokens": 600, "cacheWriteTokens": 50, "reasoningTokens": 120}
    use(monkeypatch, repo, cursor_host, [{"usage": reported}, {"text": "Looking."}], [{"usage": reported}, GOOD], usage=False)
    assert run() == 0
    each = {"tokens": 1200, "cost_usd": 0.0, "input_tokens": 300, "output_tokens": 250, "cache_read_tokens": 600,
            "cache_write_tokens": 50, "reasoning_tokens": 120}
    assert [r["usage"] for r in trace("turn_end")] == [each, each]
    (attempt,) = trace("attempt_end")
    assert attempt["usage"] == {name: 2 * value for name, value in each.items()}


def test_a_token_limit_counts_cursor_work(repo, monkeypatch, cursor_host, capsys):
    use(monkeypatch, repo, cursor_host, [GOOD])
    Path("flow.yaml").write_text(dedent("""
        version: 1
        name: cu
        defaults: {harness: cursor, harnesses: {cursor: {model: composer-2.5}}}
        limits: {tokens: 1000}
        start: {input: {}}
        nodes:
          ask: {needs: [start], instruction: Say hi}
          more: {needs: [ask], instruction: Say more}
        end: {output: {out: "{{ more.summary }}"}}
        """))
    assert cli.main(["run", "flow.yaml"]) == 1
    assert json.loads((run_folder() / "status.json").read_text())["status"] == "limit"
    assert "used 1,200 tokens, limit 1,000" in capsys.readouterr().err


def test_the_transcript_records_the_model_and_parameters_a_cursor_model_id_maps_to(repo, monkeypatch, cursor_host):
    log = use(monkeypatch, repo, cursor_host, [GOOD])
    assert run("model: gpt-5.6-sol-max-fast\n") == 0
    (create,) = entries(log, "create")
    assert create["model"] == {"id": "gpt-5.6-sol", "params": [{"id": "reasoning", "value": "max"}, {"id": "fast", "value": "true"},
                                                               {"id": "context", "value": "1m"}]}
    (session,) = [e for e in attempt_log() if e["kind"] == "session"]
    assert session["effective"] == {"model": "gpt-5.6-sol", "reasoning": "max", "fast": "true", "context": "1m"}
    assert session["sdk"] == "1.0.38"


@pytest.mark.parametrize("model, said", [
    ("claude-fable-9-max", "Cursor has no model `claude-fable-9-max`"),
    ("gpt-5.6-sol-turbo", "Cursor model id gpt-5.6-sol-turbo names none of its variants of gpt-5.6-sol"),
])
def test_a_model_cursor_cannot_run_is_unavailable_and_the_next_harness_runs(repo, monkeypatch, cursor_host, capsys, model, said):
    from fakes import Fake, submit
    from workflow import harnesses
    log = use(monkeypatch, repo, cursor_host)
    claude = Fake("claude-code", submit({"summary": "from claude"}))
    monkeypatch.setitem(harnesses.FACTORIES, "claude-code", claude.factory)
    assert run(f"harness: [cursor, claude-code]\nmodel: {{cursor: {model}, claude-code: opus}}\n") == 0
    assert output() == {"out": "from claude"}
    assert f"cursor is unavailable: {said}" in capsys.readouterr().out
    assert entries(log, "create") == []  # no agent was made for it


@pytest.mark.parametrize("settings, tools, disallowed, sandbox", [
    ("access: full\n", None, ["askQuestion", "webSearch", "webFetch"], False),
    ("access: full\nweb: true\n", None, ["askQuestion"], False),
    ("web: true\n", ["mcp", "webSearch", "webFetch"], ["task", "askQuestion"], True),
])
def test_each_access_level_and_web_setting_maps_to_the_sdks_tool_lists(repo, monkeypatch, cursor_host, settings, tools, disallowed,
                                                                       sandbox):
    log = use(monkeypatch, repo, cursor_host, [GOOD])
    assert run(settings) == 0
    (create,) = entries(log, "create")
    assert (create.get("tools"), create["disallowedTools"], create["local"]["sandboxOptions"]) == (tools, disallowed, {"enabled": sandbox})
    full = "full" in settings
    assert ("mcpServers" in create) is not full  # full, without `tools`: the MCP servers Cursor loads stay
    assert ("subagentInherit" in create["local"]) is full
    custom = sorted(create["local"]["customTools"])
    assert custom == (["submit_result"] if full else ["find_files", "git", "list_dir", "read_file", "search_files", "submit_result"])


def test_edit_runs_through_cursor_agents_acp(repo, monkeypatch, cursor_host):
    from fakes import Fake, submit
    acp = Fake("cursor", submit({"summary": "from acp"}))
    log = use(monkeypatch, repo, cursor_host, acp=acp.factory)
    assert run("access: edit\n") == 0
    assert output() == {"out": "from acp"}
    assert entries(log, "loaded") == []  # no Cursor host was started
    (spec,) = acp.specs
    assert spec.access == "edit"
    (line,) = [e for e in attempt_log() if e["kind"] == "runtime"]
    assert line["runtime"] == "acp" and "no test has shown that its sandbox keeps writes in it" in line["why"]


def test_without_project_instructions_no_cursor_settings_layer_loads(repo, monkeypatch, cursor_host):
    log = use(monkeypatch, repo, cursor_host, [GOOD])
    assert run("project_instructions: false\n") == 0
    (create,) = entries(log, "create")
    assert "settingSources" not in create["local"]


def test_a_reminder_is_another_send_to_the_same_agent(repo, monkeypatch, cursor_host):
    from workflow.output_tool import REMINDER
    log = use(monkeypatch, repo, cursor_host, [{"text": "done!"}], [GOOD])
    assert run() == 0
    first, second = entries(log, "send")
    assert len(entries(log, "create")) == 1 and first["agentId"] == second["agentId"]
    assert second["text"] == REMINDER
    assert len(entries(log, "loaded")) == 1  # one host for the session


def test_progress_the_transcript_and_the_trace_show_text_and_tool_calls(repo, monkeypatch, cursor_host, capsys):
    log = use(monkeypatch, repo, cursor_host, [
        {"text": "Looking."},
        {"call": {"name": "shell", "args": {"command": "git status"}, "result": {"stdout": "clean"}}},
        {"call": {"name": "read", "args": {}, "late_args": {"path": str(Path.cwd() / "a.py")}, "result": "x = 1"}},
        GOOD])
    assert run("access: full\n") == 0
    out = capsys.readouterr().out
    assert "ask: command git status" in out
    assert "ask: submit_result" in out
    log_lines = attempt_log()
    texts = [e["text"] for e in log_lines if e["kind"] == "text"]
    assert texts == ["Looking."]
    tools = [(e["tool"], e["status"]) for e in log_lines if e["kind"] == "tool"]
    assert tools == [("command", "completed"), ("read", "completed"), ("submit_result", "completed")]
    trace = [json.loads(line) for line in (run_folder() / "trace.jsonl").read_text().splitlines()]
    calls = [r for r in trace if r["kind"] == "tool_call"]
    reads = [r for r in calls if r["tool"] == "read"]
    assert [r["arguments"] for r in reads] == [{}, {"path": str(Path.cwd() / "a.py")}]  # again when its input came late
    assert reads[0]["id"] == reads[1]["id"]
    results = [r for r in trace if r["kind"] == "tool_result"]
    assert {"stdout": "clean"} in [r.get("result") for r in results]
    assert [r["text"] for r in trace if r["kind"] == "message"] == ["Looking."]


def test_the_trace_has_cursors_thinking_with_its_time_and_the_id_of_each_request(repo, monkeypatch, cursor_host, capsys):
    use(monkeypatch, repo, cursor_host, [
        {"thinking": ["Let me ", "look."], "thinking_ms": 1234},
        {"text": "Looking."},
        {"request": "req-1"},  # the run's own id again
        {"request": "req-from-the-stream"},
        {"thinking": ["Half a thought"]},  # the turn ends before the block does
    ], [GOOD])
    assert run() == 0
    trace = [json.loads(line) for line in (run_folder() / "trace.jsonl").read_text().splitlines()]
    noted = [(r["kind"], r["id"], r["parent"], r.get("duration_ms"), r.get("text"), r.get("request_id"))
             for r in trace if r["kind"] in ("thinking", "request", "message")]
    assert noted == [
        ("request", "ask#1.a1.t1.r1", "ask#1.a1.t1", None, None, "req-1"),
        ("thinking", "ask#1.a1.t1.k1", "ask#1.a1.t1", 1234, "Let me look.", None),
        ("message", "ask#1.a1.t1.m1", "ask#1.a1.t1", None, "Looking.", None),
        ("request", "ask#1.a1.t1.r2", "ask#1.a1.t1", None, None, "req-from-the-stream"),
        ("thinking", "ask#1.a1.t1.k2", "ask#1.a1.t1", None, "Half a thought", None),
        ("request", "ask#1.a1.t2.r1", "ask#1.a1.t2", None, None, "req-2"),  # the reminder's own request
    ]
    capsys.readouterr()
    assert cli.main(["trace", run_folder().name]) == 0
    shown = capsys.readouterr().out
    assert 'ask#1.a1.t1.k1 thinks for 1,234 ms: "Let me look."' in shown
    assert 'ask#1.a1.t1.k2 thinks: "Half a thought"' in shown
    assert "ask#1.a1.t1.r1 request req-1" in shown


def test_with_no_api_key_sessions_run_through_cursor_agents_acp_and_one_warning_says_how_to_add_one(
        repo, monkeypatch, cursor_host, capsys):
    from fakes import Fake, submit
    acp = Fake("cursor", submit({"summary": "from acp"}), submit({"summary": "from acp too"}))
    log = use(monkeypatch, repo, cursor_host, key=None, acp=acp.factory)
    monkeypatch.setenv("CURSOR_API_KEY", KEY)  # never read from the environment
    Path("flow.yaml").write_text(dedent("""
        version: 1
        name: cu
        defaults: {harness: cursor, harnesses: {cursor: {model: composer-2.5}}}
        start: {input: {}}
        nodes:
          ask: {needs: [start], instruction: Say hi}
          other: {needs: [ask], instruction: Say hi too, access: full}
        end: {output: {ask: "{{ ask.summary }}", other: "{{ other.summary }}"}}
        """))
    assert cli.main(["run", "flow.yaml"]) == 0
    assert output() == {"ask": "from acp", "other": "from acp too"}
    assert [spec.access for spec in acp.specs] == ["read-only", "full"]
    assert entries(log, "loaded") == []  # no Cursor host was started
    (warning,) = events("warning")
    assert warning["message"].startswith("no Cursor API key is set, so cursor runs read-only and full sessions "
                                         "through cursor-agent's ACP, which counts no tokens. Create a Cursor API key")
    assert "`wf runtimes cursor signin`" in warning["message"] and "`api_key_file` under [cursor]" in warning["message"]
    assert capsys.readouterr().out.count("⚠ no Cursor API key is set") == 1
    (line,) = [e for e in attempt_log() if e["kind"] == "runtime"]
    assert line["runtime"] == "acp" and line["why"].startswith("no Cursor API key is set")


def test_a_refused_api_key_makes_cursor_unavailable(repo, monkeypatch, cursor_host, capsys):
    from fakes import Fake, submit
    acp = Fake("cursor", submit({"summary": "from acp"}))
    use(monkeypatch, repo, cursor_host, key="key_revoked", acp=acp.factory)
    assert run() == 1
    out = capsys.readouterr().out
    assert "cursor is unavailable: Cursor refused the API key: Invalid User API Key. Create a Cursor API key" in out
    assert "attempt 2" not in out  # no retry on the same harness
    assert acp.specs == [] and events("warning") == []  # a key that is set never hands the session to cursor-agent


def test_the_api_key_can_come_from_a_file_that_a_setting_names(repo, monkeypatch, cursor_host, wf_home, fake_keyring):
    log = use(monkeypatch, repo, cursor_host, [GOOD], key="key_in_the_keychain")
    (repo / "cursor-key").write_text(f"{KEY}\n")
    (wf_home / "config.toml").write_text(f'[cursor]\napi_key_file = "{repo / "cursor-key"}"\n')
    assert run() == 0
    assert entries(log, "create")[0]["apiKey"] == KEY  # the file wins over the Keychain


def test_a_key_file_that_cannot_be_read_makes_cursor_unavailable(repo, monkeypatch, cursor_host, wf_home, capsys):
    use(monkeypatch, repo, cursor_host)
    (wf_home / "config.toml").write_text(f'[cursor]\napi_key_file = "{repo / "nowhere"}"\n')
    assert run() == 1
    assert (f"cursor is unavailable: can't read the Cursor API key from {repo / 'nowhere'} (cursor.api_key_file in "
            f"{wf_home / 'config.toml'})") in capsys.readouterr().out


@pytest.mark.parametrize("failure, said", [
    ({"error": {"name": "RateLimitError", "message": "You've hit your usage limit"}}, "cursor: You've hit your usage limit"),
    ({"error": {"name": "AuthenticationError", "message": "Key expired"}}, "cursor: Cursor refused the API key: Key expired. Create"),
    ({"run_error": {"message": "Error: [resource_exhausted] out of credits"}}, "cursor: Error: [resource_exhausted] out of credits"),
    ({"run_error": {"message": "Upgrade your plan to continue"}}, "cursor: Upgrade your plan to continue"),
])
def test_a_usage_limit_or_a_sign_in_problem_hands_over_to_the_next_harness(repo, monkeypatch, cursor_host, capsys, failure, said):
    from fakes import Fake, record_pauses, submit
    from workflow import harnesses
    pauses = record_pauses(monkeypatch)
    log = use(monkeypatch, repo, cursor_host, [{"text": "Working on it"}, failure])
    claude = Fake("claude-code", submit({"summary": "from claude"}))
    monkeypatch.setitem(harnesses.FACTORIES, "claude-code", claude.factory)
    assert run("harness: [cursor, claude-code]\nmodel: {cursor: composer-2.5, claude-code: opus}\n") == 0
    assert output() == {"out": "from claude"}
    assert f"cursor is unavailable: {said}" in capsys.readouterr().out
    assert pauses == [] and len(entries(log, "create")) == 1  # no wait, and no retry on Cursor


@pytest.mark.parametrize("failure, said", [
    ({"run_error": {"message": "Error: RetriableError: [internal] HTTP/2 keepalive ping timed out after 5000ms"}},
     "attempt 1 failed: cursor: Error: RetriableError: [internal] HTTP/2 keepalive ping timed out after 5000ms"),
    ({"error": {"name": "NetworkError", "message": "Service unavailable"}}, "attempt 1 failed: cursor: Service unavailable"),
])
def test_an_error_that_a_retry_can_clear_fails_the_attempt_and_the_retry_waits(repo, monkeypatch, cursor_host, capsys, failure, said):
    pauses = []

    async def pause(ctx, wait):  # the wait is recorded, not made
        pauses.append(wait)

    monkeypatch.setattr("workflow.nodes._pause", pause)
    log = use(monkeypatch, repo, cursor_host, [failure], [GOOD])
    assert run() == 0
    out = capsys.readouterr().out
    assert said in out and "is unavailable" not in out
    assert len(entries(log, "loaded")) == 2  # the retry runs on a new host
    assert pauses == [30.0]


def test_an_error_notice_fails_the_turn(repo, monkeypatch, cursor_host, capsys):
    use(monkeypatch, repo, cursor_host, [{"run_error": {"message": "the model broke", "code": "internal"}}], [GOOD])
    assert run() == 0
    assert "attempt 1 failed: cursor: the model broke" in capsys.readouterr().out


def test_a_timeout_cancels_the_turn(repo, monkeypatch, cursor_host, capsys):
    log = use(monkeypatch, repo, cursor_host, [{"hang": True}])
    assert run("retry: 0\ntimeout: 2s\n") == 1
    assert "timed out after 2s" in capsys.readouterr().err
    assert len(entries(log, "cancel")) == 1
    assert len(entries(log, "close")) == 1  # and the session closed


def test_a_turn_that_a_timeout_cancels_still_counts_what_cursor_reports_as_it_ends(repo, monkeypatch, cursor_host):
    late = {"inputTokens": 900, "outputTokens": 100, "cacheReadTokens": 0, "cacheWriteTokens": 0}
    use(monkeypatch, repo, cursor_host, [{"hang": True, "usage": late}])
    assert run("retry: 0\ntimeout: 2s\n") == 1
    (failed,) = events("node_failed")
    assert failed["tokens"] == 1000
    assert events("agent_usage")[-1]["tokens"] == 1000
    (attempt,) = trace("attempt_end")
    assert attempt["usage"] == {"tokens": 1000, "cost_usd": 0.0, "input_tokens": 900, "output_tokens": 100,
                                "cache_read_tokens": 0, "cache_write_tokens": 0, "reasoning_tokens": 0}


def test_a_turn_that_a_timeout_cancels_still_gives_the_trace_its_cut_off_thinking_and_the_request_id_of_its_result(
        repo, monkeypatch, cursor_host):
    use(monkeypatch, repo, cursor_host, [{"thinking": ["Half a ", "thought"], "result_request": "req-late", "hang": True}])
    assert run("retry: 0\ntimeout: 2s\n") == 1
    assert [(r["parent"], r["duration_ms"], r["text"]) for r in trace("thinking")] == [("ask#1.a1.t1", None, "Half a thought")]
    assert [(r["parent"], r["request_id"]) for r in trace("request")] == [("ask#1.a1.t1", "req-1"), ("ask#1.a1.t1", "req-late")]


def test_a_lost_connection_as_a_session_opens_fails_the_attempt_and_the_retry_waits(repo, monkeypatch, cursor_host, capsys):
    pauses = []

    async def pause(ctx, wait):
        pauses.append(wait)

    monkeypatch.setattr("workflow.nodes._pause", pause)
    use(monkeypatch, repo, cursor_host, create_error={"name": "NetworkError", "message": "Service unavailable"})
    assert run("retry: 1\n") == 1
    out = capsys.readouterr().out
    assert "attempt 1 failed: cursor: Service unavailable" in out and "is unavailable" not in out
    assert pauses == [30.0]


def test_a_turn_too_big_for_a_frame_fails_and_says_why(repo, monkeypatch, cursor_host, capsys):
    log = use(monkeypatch, repo, cursor_host, [GOOD])
    Path("flow.yaml").write_text(dedent("""
        version: 1
        name: cu
        defaults: {harness: cursor, harnesses: {cursor: {model: composer-2.5}}}
        start: {input: {}}
        nodes:
          ask: {needs: [start], retry: 0, instruction: "BIG"}
        end: {output: {}}
        """).replace("BIG", "x" * 1_100_000))
    assert cli.main(["run", "flow.yaml"]) == 1
    assert "cursor: the turn's text is too big to send: a req frame of 1,100," in capsys.readouterr().err
    assert entries(log, "send") == []


def test_the_host_dying_mid_turn_fails_the_attempt_and_a_retry_starts_another(repo, monkeypatch, cursor_host, capsys):
    log = use(monkeypatch, repo, cursor_host, [{"exit": True}], [GOOD])
    assert run() == 0
    assert "attempt 1 failed: cursor: the Cursor host exited (its output is in" in capsys.readouterr().out
    assert len(entries(log, "create")) == 2


def test_a_promise_that_the_sdk_leaves_unawaited_ends_the_host_and_the_attempt(repo, monkeypatch, cursor_host, capsys):
    use(monkeypatch, repo, cursor_host, [{"unhandled": True}, {"sleep": 2000}], [GOOD])
    assert run() == 0
    assert "attempt 1 failed: cursor: the Cursor host exited" in capsys.readouterr().out


def test_a_host_that_does_not_answer_is_unavailable_not_a_node_timeout(repo, monkeypatch, cursor_host, capsys):
    use(monkeypatch, repo, cursor_host, silent_create=True, timeout=2)
    assert run() == 1
    out = capsys.readouterr()
    assert "cursor is unavailable: the Cursor host didn't start a session" in out.out
    assert "timed out after" not in out.out + out.err


def test_closing_a_session_stops_the_commands_the_agent_started(repo, monkeypatch, cursor_host):
    log = use(monkeypatch, repo, cursor_host, [{"child": True}, GOOD])
    assert run("access: full\n") == 0
    (child,) = entries(log, "child")  # it ignores SIGTERM, so only the process group's SIGKILL stops it
    for _ in range(50):
        try:
            os.kill(child["pid"], 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        os.kill(child["pid"], 9)
        pytest.fail("the command the agent started still runs after its session closed")


# ---------- what a read-only agent may read ----------


def test_a_read_only_agent_reads_and_searches_only_inside_its_workspace(repo, monkeypatch, cursor_host):
    Path("notes.txt").write_text("The secret word is: quince\n")
    outside = repo / "outside.txt"
    outside.write_text("not for this agent\n")
    log = use(monkeypatch, repo, cursor_host, [
        {"tool": {"name": "read_file", "arguments": {"path": "notes.txt"}}},
        {"tool": {"name": "search_files", "arguments": {"pattern": "secret", "path": str(Path.home())}}},
        {"tool": {"name": "read_file", "arguments": {"path": "../outside.txt"}}},
        {"tool": {"name": "find_files", "arguments": {"pattern": "**/*.txt"}}},
        GOOD])
    assert run() == 0
    read, home, escape, listed, _ = [e["result"] for e in entries(log, "tool_result")]
    assert (read["isError"], read["content"][0]["text"]) == (False, "     1\tThe secret word is: quince")
    for refused in (home, escape):
        assert refused["isError"] and "is outside the workspace and the files and folders your instruction names" in refused["content"][0]["text"]
    assert listed["content"][0]["text"] == "notes.txt"


@pytest.mark.parametrize("action, said", [
    ({"call": {"name": "grep", "args": {"pattern": "status.json", "path": "/Users"}}},
     "Cursor ran grep, a tool outside the session's read-only list"),
    ({"mcp": {"server": "github", "tool": "create_issue", "args": {}}},
     "Cursor called github.create_issue, an MCP tool wf didn't give this session"),
])
def test_a_tool_that_a_read_only_session_was_not_given_stops_the_turn(repo, monkeypatch, cursor_host, capsys, action, said):
    log = use(monkeypatch, repo, cursor_host, [action, {"sleep": 3000}, GOOD])
    assert run("retry: 0\n") == 1
    assert said in capsys.readouterr().err
    assert len(entries(log, "cancel")) >= 1
    assert entries(log, "tool_result") == []  # the turn stopped before it went on


def test_a_folder_of_wfs_that_the_instruction_names_is_readable_too(repo, monkeypatch):
    from fakes import Fake, submit
    from workflow import harnesses
    fake = Fake("cursor", submit({"summary": "done"}), submit({"summary": "done"}))
    monkeypatch.setitem(harnesses.FACTORIES, "cursor", fake.factory)
    Path("flow.yaml").write_text(dedent("""
        version: 1
        name: cu
        defaults: {harness: cursor, harnesses: {cursor: {model: composer-2.5}}}
        start: {input: {}}
        nodes:
          ask: {needs: [start], instruction: "Read {{ run.dir }}/run.json"}
          other: {needs: [ask], workspace: temp, instruction: "Look at {{ workspace.ask.path }}"}
        end: {output: {}}
        """))
    assert cli.main(["run", "flow.yaml"]) == 0
    ask, other = sorted(fake.specs, key=lambda spec: spec.node)
    assert [path.resolve() for path in ask.read_roots] == [run_folder().resolve()]
    assert [path.resolve() for path in other.read_roots] == [Path.cwd().resolve()]  # the repo, ask's workspace


def test_a_file_or_folder_that_the_instruction_names_is_readable_too(repo, monkeypatch):
    from fakes import Fake, submit
    from workflow import harnesses
    refs = repo / "refs"
    (refs / "clone" / "src").mkdir(parents=True)
    (refs / "report.md").write_text("# Report\n")
    (refs / "clone" / "src" / "a.ts").write_text("export {}\n")
    (refs / "other.md").write_text("not named\n")
    fake = Fake("cursor", submit({"summary": "done"}))
    monkeypatch.setitem(harnesses.FACTORIES, "cursor", fake.factory)
    named = (f"Read {refs}/report.md, then the clone at `{refs}/clone`. Start at {refs}/clone/src/a.ts:12-30. "
             f"Skip {refs}/missing.md, ~/, {Path.home()} and /.")
    Path("flow.yaml").write_text(dedent(f"""
        version: 1
        name: cu
        defaults: {{harness: cursor, harnesses: {{cursor: {{model: composer-2.5}}}}}}
        start: {{input: {{}}}}
        nodes:
          ask: {{needs: [start], instruction: {json.dumps(named)}}}
        end: {{output: {{}}}}
        """))
    assert cli.main(["run", "flow.yaml"]) == 0
    (spec,) = fake.specs
    assert [path.resolve() for path in spec.read_roots] == [(refs / "report.md").resolve(), (refs / "clone").resolve()]


def test_a_quoted_file_or_folder_whose_path_has_spaces_is_readable_too(repo, monkeypatch):
    from fakes import Fake, submit
    from workflow import harnesses
    from workflow.harnesses import files_tool
    refs = repo / "refs"
    app, notes = refs / "Visual Studio Code.app", refs / "my notes"
    (app / "Contents").mkdir(parents=True)
    notes.mkdir()
    (notes / "report one.md").write_text("# Report\n")
    (refs / "plain.md").write_text("named without quotes\n")
    fake = Fake("cursor", submit({"summary": "done"}))
    monkeypatch.setitem(harnesses.FACTORIES, "cursor", fake.factory)
    named = f"List \"{app}\", read `{notes}/report one.md:1-3`, and see '{refs}/plain.md as well'."
    Path("flow.yaml").write_text(dedent(f"""
        version: 1
        name: cu
        defaults: {{harness: cursor, harnesses: {{cursor: {{model: composer-2.5}}}}}}
        start: {{input: {{}}}}
        nodes:
          ask: {{needs: [start], instruction: {json.dumps(named)}}}
        end: {{output: {{}}}}
        """))
    assert cli.main(["run", "flow.yaml"]) == 0
    (spec,) = fake.specs
    assert [path.resolve() for path in spec.read_roots] == [
        app.resolve(), (notes / "report one.md").resolve(), (refs / "plain.md").resolve()]
    listed, is_error = files_tool.run("list_dir", spec.cwd, spec.read_roots, {"path": str(app)})
    assert not is_error and "Contents/" in listed
    read, is_error = files_tool.run("read_file", spec.cwd, spec.read_roots, {"path": str(notes / "report one.md")})
    assert not is_error and "# Report" in read


def test_a_continued_pass_may_read_what_its_own_instruction_names(repo, monkeypatch):
    from fakes import Fake, submit
    from workflow import harnesses
    fake = Fake("cursor", submit({"summary": "done"}), submit({"summary": "done"}))
    monkeypatch.setitem(harnesses.FACTORIES, "cursor", fake.factory)
    Path("flow.yaml").write_text(dedent("""
        version: 1
        name: cu
        defaults: {harness: cursor, harnesses: {cursor: {model: composer-2.5}}}
        start: {input: {}}
        nodes:
          ask:
            needs: [start]
            workspace: temp
            session: continue
            routes: [{to: ask, max: 1}, {to: end}]
            instruction: "{% if session.continued %}Look again at {{ run.dir }}{% else %}Look{% endif %}"
        end: {output: {}}
        """))
    assert cli.main(["run", "flow.yaml"]) == 0
    first, second = fake.specs
    assert first.read_roots == []
    assert [path.resolve() for path in second.read_roots] == [run_folder().resolve()]  # named by the continued text alone


# ---------- session: continue ----------


CONTINUE = "session: continue\nroutes: [{to: ask, max: 1}, {to: end}]\n"


def test_continue_loads_the_last_passs_agent(repo, monkeypatch, cursor_host, capsys, wf_home):
    from workflow.output_tool import NEW_RESULT
    log = use(monkeypatch, repo, cursor_host, [GOOD], [GOOD])
    assert run(CONTINUE) == 0
    (created,) = entries(log, "create")
    (resumed,) = entries(log, "resume")
    first, second = entries(log, "send")
    assert resumed["agentId"] == first["agentId"] == second["agentId"]  # Cursor can't fork: the agent itself goes on
    assert Path(resumed["local"]["store"]["store"]).name == first["agentId"]  # kept in sessions/<agent id>
    assert resumed["local"]["customTools"] and resumed["tools"] == ["mcp"]  # the restrictions again: the SDK doesn't keep them
    assert second["options"]["local"]["force"] is True and "force" not in first["options"]["local"]
    assert second["text"] == f"Say hi\n\n{NEW_RESULT}\n\n{FINISH}"
    assert "continuing the last pass's session" in capsys.readouterr().out
    assert not (wf_home / "temp" / run_folder().name).exists()
    assert not any((wf_home / "sessions").glob("*"))  # no follow-up can continue it


def test_after_a_continued_attempt_fails_the_retry_starts_fresh(repo, monkeypatch, cursor_host, capsys):
    log = use(monkeypatch, repo, cursor_host, [GOOD], [{"run_error": {"message": "boom"}}], [GOOD])
    assert run(CONTINUE) == 0
    assert (len(entries(log, "resume")), len(entries(log, "create"))) == (1, 2)
    assert "starting a fresh session: the last attempt that continued this session failed" in capsys.readouterr().out


def test_a_continued_attempt_that_times_out_after_its_result_is_not_continued_again(repo, monkeypatch, cursor_host, capsys):
    log = use(monkeypatch, repo, cursor_host, [GOOD], [GOOD, {"hang": True}], [GOOD])
    assert run(CONTINUE + "timeout: 2s\n") == 0
    assert (len(entries(log, "resume")), len(entries(log, "create"))) == (1, 2)
    assert "starting a fresh session: the last attempt that continued this session failed" in capsys.readouterr().out


def test_a_third_pass_continues_the_agent_a_successful_second_pass_continued(repo, monkeypatch, cursor_host):
    log = use(monkeypatch, repo, cursor_host, [GOOD], [GOOD], [GOOD])
    assert run("session: continue\nroutes: [{to: ask, max: 2}, {to: end}]\n") == 0
    assert (len(entries(log, "resume")), len(entries(log, "create"))) == (2, 1)
    assert len({send["agentId"] for send in entries(log, "send")}) == 1


FOLLOW_UP = """
    version: 1
    name: cu
    defaults: {harness: cursor, harnesses: {cursor: {model: composer-2.5}}}
    start: {input: {}}
    follow_up: {to: work}
    nodes:
      plan: {needs: [start], instruction: Plan it, session: continue}
      work:
        needs: [plan]
        session: continue
        instruction: "{% if follow_up %}Follow-up: {{ follow_up.request }}{% else %}Do it{% endif %}"
    end: {output: {}}
    """


def follow_up_run(workflow: str = FOLLOW_UP) -> runs.Run:
    """A run of the workflow that succeeded, in a repo with a commit."""
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "first"],
                   check=True)
    Path("flow.yaml").write_text(dedent(workflow))
    assert cli.main(["run", "flow.yaml"]) == 0
    (run,) = runs.all_runs(Path.cwd())
    return run


def session_id(run: runs.Run, node: str) -> str:
    lines = [json.loads(line) for line in (run.dir / "nodes" / node / "1" / "attempt-1.log").read_text().splitlines()]
    (session,) = [line for line in lines if line["kind"] == "session"]
    return session["id"]


def test_a_follow_up_continues_the_agent_that_the_run_ended_with(repo, monkeypatch, cursor_host, capsys, wf_home):
    log = use(monkeypatch, repo, cursor_host, [GOOD], [GOOD], [GOOD])
    run = follow_up_run()
    saved = workspaces.saved_sessions(run.dir, "cursor-sdk")
    assert [folder.name for folder in saved.iterdir()] == [session_id(run, "work")]  # no follow-up reaches plan
    assert all(stat.S_IMODE(folder.stat().st_mode) == 0o700 for folder in (wf_home / "sessions", saved.parent, saved))
    assert not (wf_home / "temp" / run.id).exists()  # the rest goes
    assert cli.main(["continue", run.id, "add a flag"]) == 0
    (resumed,) = entries(log, "resume")
    last = entries(log, "send")[-1]
    assert resumed["agentId"] == last["agentId"] == session_id(run, "work")
    assert last["text"].startswith("Follow-up: add a flag")
    assert "work: continuing the last pass's session" in capsys.readouterr().out


def test_a_follow_up_without_follow_up_continues_the_agent_that_takes_the_request(repo, monkeypatch, cursor_host):
    log = use(monkeypatch, repo, cursor_host, [GOOD], [GOOD], [GOOD])
    run = follow_up_run(FOLLOW_UP.replace("    follow_up: {to: work}\n", "").replace(
        "instruction: \"{% if follow_up %}Follow-up: {{ follow_up.request }}{% else %}Do it{% endif %}\"",
        "instruction: Do it\n        access: full"))
    saved = workspaces.saved_sessions(run.dir, "cursor-sdk")
    assert [folder.name for folder in saved.iterdir()] == [session_id(run, "work")]
    assert cli.main(["continue", run.id, "add a flag"]) == 0
    (resumed,) = entries(log, "resume")
    last = entries(log, "send")[-1]
    assert resumed["agentId"] == last["agentId"] == session_id(run, "work")
    assert "## Follow-up request" in last["text"] and "add a flag" in last["text"]


def test_wf_discard_removes_the_saved_sessions_of_a_run(repo, monkeypatch, cursor_host):
    use(monkeypatch, repo, cursor_host, [GOOD], [GOOD])
    run = follow_up_run()
    saved = workspaces.sessions_root(run.dir)
    assert saved.is_dir()
    assert cli.main(["discard", run.id]) == 0
    assert not saved.exists()


# ---------- wf steer ----------


def test_a_message_from_wf_steer_joins_the_running_turn(repo, monkeypatch, cursor_host, capsys):
    from steering import STEER, at_pause, left_in_mailbox, steer_events, steer_lines
    log = use(monkeypatch, repo, cursor_host, [{"pause": "p"}, GOOD])
    steering = at_pause(repo, "p")
    assert run() == 0
    done = steering.result()
    assert done.returncode == 0, done.stderr
    assert done.stdout.startswith("Delivered to ask (attempt 1 on cursor)")
    assert entries(log, "steer") == [{"text": STEER}]
    assert len(entries(log, "send")) == 1  # no turn of its own
    (event,) = steer_events()
    assert (event["status"], event["text"], event["harness"]) == ("delivered", STEER, "cursor")
    assert [line["status"] for line in steer_lines()] == ["delivered"]
    assert left_in_mailbox() == []
    assert f"ask: message from wf steer delivered: {STEER}" in capsys.readouterr().out


@pytest.mark.parametrize("steer", ["revert", "none", "throw"])
def test_a_message_from_wf_steer_goes_to_cursor_as_the_next_turn(repo, monkeypatch, cursor_host, steer):
    from steering import STEER, at_pause, steer_events
    log = use(monkeypatch, repo, cursor_host, [{"pause": "p"}], [GOOD], steer=steer)
    steering = at_pause(repo, "p")
    assert run() == 0
    assert steering.result().stdout.startswith("Queued for ask (attempt 1 on cursor)")
    assert [send["text"] for send in entries(log, "send")] == [f"Say hi\n\n{FINISH}", STEER]
    assert [(e["status"], e.get("text")) for e in steer_events()] == [("queued", STEER), ("delivered", None)]


class StubHost:
    """Answers a session's requests as the Cursor host would: turn.steer with `queued`. With
    `early`, the steer.status event comes before that answer, which the protocol allows."""

    name, dead = "the stub host", None

    def __init__(self, early: dict | None = None):
        self.early, self.session = early, None

    async def request(self, op, args, timeout=None):
        if op == "turn.steer" and self.early:  # the session's turn reads the event before this answer comes
            self.session.events.put_nowait({"kind": "steer.status", "turn": "trn_0", "data": self.early})
            self.session._take_events()
        return {"turn.steer": {"status": "queued"}}.get(op, {})

    def attach(self, session): pass
    def detach(self, session): pass
    async def stop(self): pass


@pytest.mark.parametrize("early", [False, True])
def test_a_message_that_cursor_declines_late_goes_back_to_the_runner_for_its_next_turn(tmp_path, early):
    import asyncio
    from workflow import schemas, steer
    from workflow.harnesses import cursor, remote
    from workflow.harnesses.base import SessionSpec, TurnEnd
    from workflow.output_tool import ResultTool
    declined = {"id": "st_1", "status": "declined"}

    async def main():
        spec = SessionSpec(cwd=tmp_path, access="read-only", web=False, model="m", effort=None,
                           tool=ResultTool(schemas.object_schema({})), log=tmp_path / "attempt-1.log", emit=lambda e: None)
        host = StubHost(declined if early else None)
        session = remote.RemoteSession(cursor.Cursor(), host, spec)
        host.session, session.session, session.turn = session, "ses_a", "trn_0"
        live = steer.Live(spec, 1, "cursor", session)
        live.start()
        answer = await live.take(steer.Message("st_1", "Use the v2 API"))
        if not early:
            session.events.put_nowait({"kind": "steer.status", "turn": "trn_0", "data": declined})
        session.turn = None  # the turn ended without a result, and no reminder may be left
        turn = await live.next_turn()
        live.sent(TurnEnd("done"))
        await live.stop()
        live.end()
        return answer, turn

    answer, turn = asyncio.run(main())
    assert answer["status"] == "queued"
    assert turn == "Use the v2 API"  # the runner sends it as a turn, before any reminder, or with none left
    lines = [json.loads(line) for line in (tmp_path / "attempt-1.log").read_text().splitlines()]
    assert [line["status"] for line in lines if line["kind"] == "steer"] == ["queued", "delivered"]


def test_a_message_that_cursor_declines_late_goes_before_a_later_one_in_the_next_turn(tmp_path):
    import asyncio
    from workflow import schemas, steer
    from workflow.harnesses import cursor, remote
    from workflow.harnesses.base import SessionSpec, TurnEnd
    from workflow.output_tool import ResultTool

    async def main():
        spec = SessionSpec(cwd=tmp_path, access="read-only", web=False, model="m", effort=None,
                           tool=ResultTool(schemas.object_schema({})), log=tmp_path / "attempt-1.log", emit=lambda e: None)
        host = StubHost()
        session = remote.RemoteSession(cursor.Cursor(), host, spec)
        host.session, session.session, session.turn = session, "ses_a", "trn_0"
        live = steer.Live(spec, 1, "cursor", session)
        live.start()
        first = await live.take(steer.Message("st_1", "Use API v1"))
        session.events.put_nowait({"kind": "steer.status", "turn": "trn_0", "data": {"id": "st_1", "status": "declined"}})
        session.turn = None
        second = await live.take(steer.Message("st_2", "Correction: use API v2"))  # between turns: kept by the runner
        turn = await live.next_turn()
        live.sent(TurnEnd("done"))
        await live.stop()
        live.end()
        return first, second, turn

    first, second, turn = asyncio.run(main())
    assert (first["status"], second["status"]) == ("queued", "queued")
    assert turn == "Use API v1\n\nCorrection: use API v2"  # the user's correction stays last


def test_a_message_that_cursor_never_answers_for_is_unconfirmed_when_the_session_closes(tmp_path):
    import asyncio
    from workflow import schemas
    from workflow.harnesses import cursor, remote
    from workflow.harnesses.base import SessionSpec
    from workflow.output_tool import ResultTool

    async def main():
        spec = SessionSpec(cwd=tmp_path, access="read-only", web=False, model="m", effort=None,
                           tool=ResultTool(schemas.object_schema({})), log=tmp_path / "attempt-1.log", emit=lambda e: None)
        harness = cursor.Cursor()
        harness.home = tmp_path
        session = remote.RemoteSession(harness, StubHost(), spec)
        session.session, session.turn = "", "trn_0"
        assert await session.steer("Use the v2 API", "st_1") == "queued"
        session.turn = None
        await session.close()

    asyncio.run(main())
    lines = [json.loads(line) for line in (tmp_path / "attempt-1.log").read_text().splitlines()]
    (line,) = [line for line in lines if line["kind"] == "steer"]
    assert (line["status"], line["reason"]) == ("unconfirmed", "cursor never said whether the agent read it")


def test_a_message_for_cursor_is_dropped_when_the_agent_submits_first(repo, monkeypatch, cursor_host):
    from steering import at_pause, steer_events
    log = use(monkeypatch, repo, cursor_host, [{"pause": "p"}, GOOD], steer="revert")
    steering = at_pause(repo, "p")
    assert run() == 0
    assert steering.result().returncode == 0
    assert len(entries(log, "send")) == 1
    queued, dropped = steer_events()
    assert (queued["status"], dropped["status"]) == ("queued", "dropped")
    assert dropped["reason"] == "the agent submitted its result before it read the message"


# ---------- the real Cursor SDK: opt in with -m harness, and it skips without your Cursor API key ----------


def real_repo(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(runner, "interactive", lambda: False)
    git = lambda *a: subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "commit.gpgsign=false", *a],
                                    check=True, capture_output=True)
    git("init", "-q")
    Path("notes.txt").write_text("The secret word is: marmalade\n")
    git("add", "notes.txt")
    git("commit", "-qm", "first")


@pytest.mark.harness
def test_a_real_cursor_api_refuses_a_key_it_does_not_know_as_a_sign_in_problem():
    import asyncio
    from workflow.harnesses import Unavailable, cursor
    with pytest.raises(Unavailable, match="Cursor refused the API key: Invalid User API Key. Create a Cursor API key"):
        asyncio.run(cursor.check(key="key_wf_test_not_a_real_key"))


@pytest.mark.harness
def test_a_real_read_only_node_on_cursors_sdk_uses_wfs_tools_and_counts_its_tokens(tmp_path, monkeypatch, capsys, live_key):
    real_repo(tmp_path, monkeypatch)
    Path("notes.txt").write_text("The secret word is: quince\n")  # only `git diff` shows the old one
    Path("flow.yaml").write_text("""
version: 1
name: real
defaults: {harness: cursor, harnesses: {cursor: {model: composer-2.5}}}
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
    tokens: {type: integer, value: "{{ run.tokens }}"}
""")
    assert cli.main(["run", "flow.yaml"]) == 0, capsys.readouterr()
    out = output()
    assert (out["old"].strip(" .").lower(), out["new"].strip(" .").lower()) == ("marmalade", "quince")
    assert out["tokens"] > 0
    (finished,) = events("node_finished")
    assert finished["tokens"] == out["tokens"]  # Cursor's tokens, in events.jsonl
    log = [json.loads(line) for line in (run_folder() / "nodes" / "read" / "1" / "attempt-1.log").read_text().splitlines()]
    (session,) = [e for e in log if e["kind"] == "session"]
    assert session["sdk"] == "1.0.38"
    assert {e["tool"] for e in log if e["kind"] == "tool"} <= {"git", "read_file", "search_files", "find_files", "list_dir",
                                                                "submit_result"}
    assert Path("notes.txt").read_text() == "The secret word is: quince\n"  # and changed nothing


@pytest.mark.harness
def test_a_real_cursor_turn_gives_the_trace_its_request_id_and_the_time_of_its_thinking(tmp_path, monkeypatch, capsys, live_key):
    real_repo(tmp_path, monkeypatch)
    Path("flow.yaml").write_text("""
version: 1
name: real
defaults: {harness: cursor, harnesses: {cursor: {model: composer-2.5}}}
start: {input: {}}
nodes:
  count:
    needs: [start]
    instruction: How many prime numbers are there below 30? Work it out, then submit the number as your summary.
end: {output: {summary: "{{ count.summary }}"}}
""")
    assert cli.main(["run", "flow.yaml"]) == 0, capsys.readouterr()
    assert "10" in output()["summary"]
    trace = [json.loads(line) for line in (run_folder() / "trace.jsonl").read_text().splitlines()]
    turns = [r["id"] for r in trace if r["kind"] == "turn"]
    requests = [r for r in trace if r["kind"] == "request"]
    assert sorted(r["parent"] for r in requests) == sorted(turns)  # one for each turn
    ids = [r["request_id"] for r in requests]
    assert all(isinstance(i, str) and i for i in ids) and len(set(ids)) == len(ids)
    thinking = [r for r in trace if r["kind"] == "thinking"]
    assert thinking, "Cursor reported no thinking for the turn"
    assert all(r["parent"] in turns and isinstance(r["duration_ms"], int) and r["duration_ms"] >= 0 and isinstance(r["text"], str)
               for r in thinking)


@pytest.mark.harness
def test_a_real_cursor_node_without_project_instructions_gets_no_repo_rules(tmp_path, monkeypatch, capsys, live_key):
    real_repo(tmp_path, monkeypatch)
    Path("AGENTS.md").write_text("The code word for this project is: periwinkle\n")
    (tmp_path / ".cursor" / "rules").mkdir(parents=True)
    Path(".cursor/rules/words.mdc").write_text("---\nalwaysApply: true\n---\nThe second code word is: tangerine\n")
    ask = "Don't read, search or open any file. From what you were told alone, list every code word for this project, or none."
    Path("flow.yaml").write_text(f"""
version: 1
name: real
defaults: {{harness: cursor, harnesses: {{cursor: {{model: composer-2.5}}}}}}
start: {{input: {{}}}}
nodes:
  told: {{needs: [start], instruction: "{ask}", output: {{words: {{type: string}}}}}}
  kept_out: {{needs: [start], project_instructions: false, instruction: "{ask}", output: {{words: {{type: string}}}}}}
end:
  output:
    told: "{{{{ told.words }}}}"
    kept_out: "{{{{ kept_out.words }}}}"
""")
    assert cli.main(["run", "flow.yaml"]) == 0, capsys.readouterr()
    out = output()
    assert "periwinkle" in out["told"].lower() and "tangerine" in out["told"].lower()  # by default the repo's files reach it
    assert "periwinkle" not in out["kept_out"].lower() and "tangerine" not in out["kept_out"].lower()


def real_session(tmp_path: Path, access: str, harness=None):
    """A real Cursor session on the SDK, opened outside a run, with the access level given, `edit`
    too: the SDK's own mapping, which wf doesn't use for runs yet."""
    from workflow import schemas
    from workflow.harnesses import cursor, remote
    from workflow.harnesses.base import SessionSpec
    from workflow.output_tool import ResultTool
    harness = harness or cursor.Cursor()
    tool = ResultTool(schemas.object_schema({"summary": {"type": "string"}}))
    spec = SessionSpec(cwd=tmp_path, access=access, web=False, model="composer-2.5", effort=None, tool=tool,
                       log=tmp_path.parent / f"{tmp_path.name}-attempt.log", emit=lambda event: None)
    return harness, spec, lambda: harness.start(spec, {**remote.open_args(spec, "cursor", harness.file_tools),
                                                       "state_dir": str(harness.home / "state")})


@pytest.mark.harness
def test_a_real_cursor_session_takes_a_message_from_wf_steer_or_says_it_cannot(tmp_path, live_key):
    import asyncio
    for number in range(1, 7):
        (tmp_path / f"step{number}.txt").write_text(f"word {number}\n")

    async def main():
        harness, spec, start = real_session(tmp_path, "read-only")
        await harness.open(tmp_path / "run")
        session = await start()
        try:
            async def steer_soon():
                await asyncio.sleep(8)
                return await session.steer("Also put the word kumquat at the end of your summary.", "st_1")
            steered = asyncio.ensure_future(steer_soon())
            end = await session.send("Read step1.txt to step6.txt one at a time, in order, with read_file. "
                                     "Then submit the six words as your summary. " + FINISH)
            return end, await steered, spec.tool.result
        finally:
            await session.close()
            await harness.close()

    end, status, result = asyncio.run(main())
    assert end.status == "done" and result
    assert status in ("delivered", None)  # None: Cursor declined it, and the runner sends it as the next turn
    if status == "delivered":
        assert "kumquat" in result["summary"].lower()


def sandboxed_edit(tmp_path: Path, task: str) -> Path:
    """Run one real `edit` session on the SDK, with Cursor's sandbox, on this task. Returns the workspace."""
    import asyncio
    workspace = tmp_path / "work"
    workspace.mkdir()

    async def main():
        harness, spec, start = real_session(workspace, "edit")
        await harness.open(tmp_path / "run")
        session = await start()
        try:
            await session.send(task + " Whatever fails, report it in your summary. " + FINISH)
        finally:
            await session.close()
            await harness.close()

    asyncio.run(main())
    return workspace


@pytest.mark.harness
def test_cursors_sandbox_keeps_edit_writes_in_the_workspace_and_lets_commands_reach_the_network(tmp_path, live_key):
    """What lets `edit` leave cursor-agent's ACP: a write outside the workspace, by a command or by
    Cursor's edit tool, fails under the SDK's sandbox, and commands still reach the network, as
    they do on ACP."""
    outside = tmp_path / "outside"
    outside.mkdir()
    workspace = sandboxed_edit(tmp_path, f"Run the shell command `echo x > {outside}/shell.txt`. Then use your file edit "
                                         f"tool to create {outside}/edit.txt with the text x. Then create inside.txt here "
                                         f"with the text x. Then run `curl -sS -m 20 -o /dev/null https://example.com; "
                                         f"echo $? > network.txt` here.")
    assert (workspace / "inside.txt").exists()  # it could write in the workspace
    assert not (outside / "shell.txt").exists(), "a command wrote outside the workspace"
    assert not (outside / "edit.txt").exists(), "the edit tool wrote outside the workspace"
    assert (workspace / "network.txt").read_text().strip() == "0", "a command couldn't reach the network"


@pytest.mark.harness
def test_cursors_sandbox_keeps_edit_commands_from_the_keychain(tmp_path, live_key):
    """Whether an `edit` agent on the SDK could read stored sign-ins, which decides KEYCHAIN_SHELL
    for Cursor at `edit` once `edit` runs on the SDK (docs/tools.md, Who can reach the Keychain)."""
    workspace = sandboxed_edit(tmp_path, "Run `security show-keychain-info > keychain.txt 2>&1; echo $? >> keychain.txt` "
                                         "here.")
    assert (workspace / "keychain.txt").read_text().split()[-1] != "0", "a command reached the Keychain"


@pytest.mark.harness
def test_a_45_second_network_blackhole_mid_turn_ends_in_one_attempt(tmp_path, monkeypatch, capsys, live_key):
    """The stalls report's reproduction (wf-cursor-stalls-1020, section 8): the SDK's own retries,
    with enableAgentRetries on, carry the turn over a 45-second silence of the network."""
    import threading
    from blackhole import Blackhole
    real_repo(tmp_path, monkeypatch)
    for number in range(1, 7):
        Path(f"step{number}.txt").write_text(f"word{number}\n")
    proxy = Blackhole().start()
    monkeypatch.setenv("HTTPS_PROXY", proxy.url)
    monkeypatch.setenv("NODE_USE_ENV_PROXY", "1")
    Path("flow.yaml").write_text("""
version: 1
name: real
defaults: {harness: cursor, harnesses: {cursor: {model: composer-2.5}}}
start: {input: {}}
nodes:
  read:
    needs: [start]
    retry: 0
    timeout: 15m
    instruction: >-
      Read step1.txt to step6.txt one at a time, in order, with read_file, one call for each file.
      Then submit the six words, joined by spaces, as your summary.
end: {output: {summary: "{{ read.summary }}"}}
""")

    def cut_after_the_second_read():
        for _ in range(3000):
            logs = list(Path(".wf").glob("runs/*/nodes/read/1/attempt-1.log"))
            reads = [line for log in logs for line in log.read_text().splitlines() if '"tool": "read_file"' in line]
            if len(reads) >= 2:
                proxy.pause(45)
                return
            threading.Event().wait(0.1)

    watcher = threading.Thread(target=cut_after_the_second_read, daemon=True)
    watcher.start()
    try:
        code = cli.main(["run", "flow.yaml"])
    finally:
        proxy.stop()
    if proxy.connects == 0:
        pytest.skip("the SDK's traffic didn't go through HTTPS_PROXY, so this test can't cut it")
    assert code == 0, capsys.readouterr()
    assert proxy.held >= 40  # the turn's own stream stalled, for most of the 45 seconds
    assert events("attempt_failed") == [] and events("attempt_started") == []  # one attempt
    assert output()["summary"].split() == [f"word{number}" for number in range(1, 7)]
