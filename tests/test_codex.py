"""The Codex adapter, against a scripted fake app server (fake_codex.py), through `wf run`.

The fake logs every request the adapter sends, so these tests check the
protocol: thread settings, dynamic tool replies, interrupts, declined approvals.
The opt-in test at the bottom runs the real `codex` (uses your Codex quota):
    uv run pytest -m harness tests/test_codex.py
"""

import json
import shlex
import subprocess
import sys
from pathlib import Path
from textwrap import dedent, indent

import pytest

from steering import STEER, at_pause, left_in_mailbox, steer_events, steer_lines
from workflow import cli, harnesses, runner
from workflow.harnesses.codex import Codex, _tokens
from workflow.output_tool import FINISH, REMINDER

FAKE = Path(__file__).parent / "fake_codex.py"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / "repo").mkdir()
    monkeypatch.chdir(tmp_path / "repo")
    monkeypatch.setattr(runner, "interactive", lambda: False)
    subprocess.run(["git", "init", "-q"], check=True)
    return tmp_path


def codex(monkeypatch, tmp_path: Path, *turns, models=None, silent=(), skills=(), steer=None) -> Path:
    """Use the fake app server for `codex`; returns its request log."""
    scenario, log = tmp_path / "scenario.json", tmp_path / "requests.jsonl"
    scenario.write_text(json.dumps({"turns": list(turns), "silent": list(silent), "skills": list(skills), "steer": steer,
                                    **({"models": models} if models else {})}))
    monkeypatch.setitem(harnesses.FACTORIES, "codex",
                        lambda: Codex(command=[sys.executable, str(FAKE), str(scenario), str(log)], request_timeout=1))
    return log


def sent(log: Path, method: str) -> list[dict]:
    lines = [json.loads(line) for line in log.read_text().splitlines()]
    return [e["data"]["params"] for e in lines if e["kind"] == "request" and e["data"]["method"] == method]


def replies(log: Path) -> list[dict]:
    return [json.loads(line)["data"] for line in log.read_text().splitlines() if json.loads(line)["kind"] == "reply"]


def run(node: str = "", top: str = "", end: str = "{out: \"{{ ask.summary }}\"}") -> int:
    Path("flow.yaml").write_text(dedent(f"""
        version: 1
        name: cx
        {top}
        defaults: {{harness: codex}}
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


GOOD = {"tool": {"arguments": {"summary": "hi"}}}


def test_paths_codex_names_the_codex_that_wf_runs(repo, monkeypatch, wf_home, capsys):
    scenario, log, said = repo / "scenario.json", repo / "requests.jsonl", repo / "args"
    scenario.write_text(json.dumps({"turns": [[GOOD]], "silent": [], "skills": [], "steer": None}))
    program = repo / "other-install" / "codex"
    program.parent.mkdir()
    program.write_text(f'#!/bin/sh\nif [ "$1" = --version ]; then echo codex-cli 0.1.0; exit; fi\necho "$@" > {said}\n'
                       f"exec {shlex.join([sys.executable, str(FAKE), str(scenario), str(log)])}\n")
    program.chmod(0o755)
    (wf_home / "config.toml").write_text(f'[paths]\ncodex = "{program}"\n')
    monkeypatch.setitem(harnesses.FACTORIES, "codex", lambda: Codex(request_timeout=5))
    assert run() == 0
    assert output() == {"out": "hi"}
    assert said.read_text() == "app-server --disable hooks --disable apps\n"
    assert "codex 0.1.0 is installed; wf is tested with" in capsys.readouterr().out  # the version of that codex


def test_a_thread_is_started_with_every_setting_explicit(repo, monkeypatch):
    log = codex(monkeypatch, repo, [GOOD])
    assert run("effort: high\n") == 0
    assert output() == {"out": "hi"}
    (thread,) = sent(log, "thread/start")
    assert thread["cwd"] == str(Path.cwd().resolve())
    assert thread["model"] == "gpt-default"  # nothing set: the app server's default model
    assert (thread["sandbox"], thread["approvalPolicy"], thread["ephemeral"]) == ("read-only", "never", True)
    assert thread["config"] == {"model_reasoning_effort": "high", "web_search": "disabled"}
    (tool,) = thread["dynamicTools"]
    assert (tool["type"], tool["name"]) == ("function", "submit_result")
    assert tool["inputSchema"]["required"] == ["summary"]
    (turn,) = sent(log, "turn/start")
    assert turn["threadId"] == "thread-1"
    assert turn["input"] == [{"type": "text", "text": f"Say hi\n\n{FINISH}", "text_elements": []}]
    assert sent(log, "initialize")[0]["capabilities"] == {"experimentalApi": True}


@pytest.mark.parametrize(
    "settings, sandbox, config",
    [
        ("access: edit\n", "workspace-write", {"web_search": "disabled", "sandbox_workspace_write": {"network_access": False}}),
        ("access: edit\nweb: true\n", "workspace-write", {"web_search": "live", "sandbox_workspace_write": {"network_access": True}}),
        ("access: full\n", "danger-full-access", {"web_search": "disabled"}),
    ],
)
def test_access_levels_map_to_sandboxes(repo, monkeypatch, settings, sandbox, config):
    log = codex(monkeypatch, repo, [GOOD])
    assert run(settings + "model: gpt-6-sol\n") == 0
    (thread,) = sent(log, "thread/start")
    assert (thread["sandbox"], thread["model"], thread["config"]) == (sandbox, "gpt-6-sol", config)


def test_without_project_instructions_a_thread_gets_no_repo_agents_md_and_no_repo_skill(repo, monkeypatch):
    skills = [{"name": "golf", "scope": "repo", "path": "/r/.agents/skills/golf/SKILL.md", "enabled": True},
              {"name": "mine", "scope": "user", "path": "/home/.agents/skills/mine/SKILL.md", "enabled": True}]
    log = codex(monkeypatch, repo, [GOOD], skills=skills)
    assert run("project_instructions: false\n") == 0
    assert sent(log, "skills/list") == [{"cwds": [str(Path.cwd().resolve())], "forceReload": True}]
    (thread,) = sent(log, "thread/start")
    assert thread["config"] == {"model_reasoning_effort": "medium", "web_search": "disabled", "project_doc_max_bytes": 0,
                                "skills": {"config": [{"path": "/r/.agents/skills/golf/SKILL.md", "enabled": False}]}}
    assert [t["input"][0]["text"] for t in sent(log, "turn/start")] == [f"Say hi\n\n{FINISH}"]  # the instruction still goes


def test_a_codex_that_does_not_list_its_skills_is_unavailable_to_a_node_without_project_instructions(repo, monkeypatch, capsys):
    log = codex(monkeypatch, repo, silent=["skills/list"])
    assert run("project_instructions: false\n") == 1
    assert "codex is unavailable: Codex didn't list its skills" in capsys.readouterr().out
    assert sent(log, "thread/start") == []


def test_every_tool_call_gets_a_reply_with_the_problems_in_the_text(repo, monkeypatch):
    log = codex(monkeypatch, repo, [{"tool": {"arguments": {"summary": 3}}},
                                    {"tool": {"arguments": {}, "name": "mystery"}},
                                    GOOD])
    assert run() == 0
    bad, unknown, good = replies(log)
    assert bad["success"] is False and "summary: 3 is not of type 'string'" in bad["contentItems"][0]["text"]
    assert unknown["success"] is False and "mystery" in unknown["contentItems"][0]["text"]
    assert good == {"success": True, "contentItems": [{"type": "inputText", "text": "Result recorded. You're done."}]}


def test_a_reminder_is_another_turn_on_the_same_thread(repo, monkeypatch):
    log = codex(monkeypatch, repo, [{"item": {"type": "agentMessage", "text": "done!"}}], [GOOD])
    assert run() == 0
    turns = sent(log, "turn/start")
    assert [t["threadId"] for t in turns] == ["thread-1", "thread-1"]
    assert turns[1]["input"][0]["text"] == REMINDER


def test_tokens_come_from_the_threads_running_total(repo, monkeypatch):
    log = codex(monkeypatch, repo, [{"tokens": 1000}, {"tokens": 500}], [{"tokens": 200}, GOOD])
    assert run(end='{tokens: {type: integer, value: "{{ run.tokens }}"}, cost: {type: number, value: "{{ run.cost_usd }}"}}') == 0
    assert output() == {"tokens": 1700, "cost": 0}


def test_a_forked_thread_counts_only_its_own_tokens(repo, monkeypatch):
    log = codex(monkeypatch, repo, [{"tokens": 1000}, GOOD], [{"tokens": 300}, GOOD])
    node = "session: continue\nroutes: [{to: ask, max: 1}, {to: end}]\n"
    assert run(node, end='{tokens: {type: integer, value: "{{ run.tokens }}"}}') == 0
    assert len(sent(log, "thread/fork")) == 1
    assert output() == {"tokens": 1300}  # not 2300: the fork inherits the first pass's 1000


def trace(kind: str) -> list[dict]:
    (folder,) = (Path(".wf") / "runs").iterdir()
    return [r for r in map(json.loads, (folder / "trace.jsonl").read_text().splitlines()) if r["kind"] == kind]


RESPONSE = {"inputTokens": 1000, "cachedInputTokens": 600, "cacheWriteInputTokens": 100, "outputTokens": 200,
            "reasoningOutputTokens": 80}
PARTS = {"tokens": 1200, "cost_usd": 0.0, "input_tokens": 300, "output_tokens": 200, "cache_read_tokens": 600,
         "cache_write_tokens": 100, "reasoning_tokens": 80}  # one RESPONSE: its input less what the cache read and wrote


def test_the_trace_keeps_the_parts_of_each_turns_tokens(repo, monkeypatch):
    codex(monkeypatch, repo, [{"tokens": RESPONSE}, {"tokens": RESPONSE}], [{"tokens": RESPONSE}, GOOD])
    assert run() == 0
    first, second = (r["usage"] for r in trace("turn_end"))
    assert (first, second) == ({name: 2 * value for name, value in PARTS.items()}, PARTS)
    (attempt,) = trace("attempt_end")
    assert attempt["usage"] == {name: 3 * value for name, value in PARTS.items()}


def test_a_forked_thread_counts_only_its_own_parts_of_the_tokens(repo, monkeypatch):
    codex(monkeypatch, repo, [{"tokens": RESPONSE}, GOOD], [{"tokens": RESPONSE}, GOOD])
    assert run("session: continue\nroutes: [{to: ask, max: 1}, {to: end}]\n") == 0
    assert [r["usage"] for r in trace("attempt_end")] == [PARTS, PARTS]  # the fork's thread holds both passes' counts


def test_input_that_the_cache_read_and_wrote_all_of_leaves_no_input_tokens(repo, monkeypatch):
    every = {"inputTokens": 100, "cachedInputTokens": 40, "cacheWriteInputTokens": 60, "outputTokens": 10,
             "reasoningOutputTokens": 5}
    codex(monkeypatch, repo, [{"tokens": every}, GOOD])
    assert run() == 0
    (turn,) = trace("turn_end")
    assert turn["usage"] == {"tokens": 110, "cost_usd": 0.0, "input_tokens": 0, "output_tokens": 10, "cache_read_tokens": 40,
                             "cache_write_tokens": 60, "reasoning_tokens": 5}


def test_the_parts_add_up_to_the_total_even_when_codex_reports_more_cache_than_input():
    usage = _tokens({"totalTokens": 1100, "inputTokens": 1000, "cachedInputTokens": 800, "cacheWriteInputTokens": 500,
                     "outputTokens": 100})
    assert usage.input_tokens == -300  # shown as it is, not hidden as 0
    assert usage.input_tokens + usage.cache_read_tokens + usage.cache_write_tokens + usage.output_tokens == usage.tokens


def test_a_turn_that_a_timeout_cuts_off_keeps_the_parts_that_codex_reported(repo, monkeypatch):
    codex(monkeypatch, repo, [{"tokens": RESPONSE}, {"hang": True}])
    assert run("retry: 0\ntimeout: 1s\n") == 1
    (attempt,) = trace("attempt_end")
    assert (attempt["outcome"], attempt["usage"]) == ("timed_out", PARTS)


def test_a_turn_that_overflows_the_context_window_counts_its_tokens_and_no_parts(repo, monkeypatch):
    codex(monkeypatch, repo, [{"tokens": RESPONSE}], [{"fill": 272000}, {"end": "failed", "message": "context window exceeded"}])
    assert run("retry: 0\n") == 1
    first, second = (r["usage"] for r in trace("turn_end"))
    assert first == PARTS
    assert second == {"tokens": 272000 - 1200, "cost_usd": 0.0, "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0,
                      "cache_write_tokens": 0, "reasoning_tokens": 0}  # 0, not below it: Codex set each part of its total to 0


def test_approval_requests_are_declined(repo, monkeypatch):
    log = codex(monkeypatch, repo, [{"approval": "item/commandExecution/requestApproval"},
                                    {"approval": "item/fileChange/requestApproval"},
                                    {"approval": "execCommandApproval"}, GOOD])
    assert run() == 0
    assert replies(log)[:3] == [{"decision": "decline"}, {"decision": "decline"}, {"decision": "denied"}]


def test_a_usage_limit_makes_codex_unavailable(repo, monkeypatch, capsys):
    codex(monkeypatch, repo, [{"error": {"codexErrorInfo": "usageLimitExceeded", "message": "You've hit your usage limit"}},
                              {"end": "failed", "message": "You've hit your usage limit", "codexErrorInfo": "usageLimitExceeded"}])
    assert run() == 1
    out = capsys.readouterr()
    assert "codex is unavailable: You've hit your usage limit" in out.out
    assert "attempt 2" not in out.out  # no retry on the same harness


def test_a_retried_rate_limit_is_ignored(repo, monkeypatch):
    codex(monkeypatch, repo, [{"error": {"codexErrorInfo": "rateLimitExceeded", "message": "slow down", "willRetry": True}}, GOOD])
    assert run() == 0


def test_a_failed_turn_fails_the_attempt(repo, monkeypatch, capsys):
    codex(monkeypatch, repo, [{"end": "failed", "message": "model exploded"}])
    assert run("retry: 0\n") == 1
    assert "codex: model exploded" in capsys.readouterr().err


def test_a_timeout_interrupts_the_turn(repo, monkeypatch, capsys):
    log = codex(monkeypatch, repo, [{"hang": True}])
    assert run("retry: 0\ntimeout: 1s\n") == 1
    assert "timed out after 1s" in capsys.readouterr().err
    assert sent(log, "turn/interrupt") == [{"threadId": "thread-1", "turnId": "turn-2"}]


def test_progress_shows_commands_and_file_changes(repo, monkeypatch, capsys):
    codex(monkeypatch, repo, [{"item": {"type": "commandExecution", "command": "git status", "cwd": "."}},
                              {"item": {"type": "fileChange", "changes": [{"path": str(Path.cwd() / "a.py")}], "status": "completed"}},
                              GOOD])
    assert run() == 0
    out = capsys.readouterr().out
    assert "ask: command git status" in out
    assert "ask: edit a.py" in out


@pytest.mark.harness
def test_a_real_agent_node_on_codex(repo, capsys):
    Path("notes.txt").write_text("The secret word is: quince\n")
    Path("flow.yaml").write_text("""
version: 1
name: real
defaults: {harness: codex}
start: {input: {}}
nodes:
  read:
    needs: [start]
    instruction: Read notes.txt and report the secret word.
    effort: low
    output: {word: {type: string}}
end:
  output:
    word: "{{ read.word }}"
    tokens: {type: integer, value: "{{ run.tokens }}"}
""")
    assert cli.main(["run", "flow.yaml"]) == 0, capsys.readouterr()
    out = output()
    assert out["word"].strip(" .").lower() == "quince"
    assert out["tokens"] > 0


def test_without_settings_the_model_lists_defaults_are_used_not_your_codex_config(repo, monkeypatch):
    models = [{"id": "gpt-a", "isDefault": False, "defaultReasoningEffort": "high"},
              {"id": "gpt-b", "isDefault": True, "defaultReasoningEffort": "low"}]
    log = codex(monkeypatch, repo, [GOOD], models=models)
    assert run() == 0
    (thread,) = sent(log, "thread/start")
    assert (thread["model"], thread["config"]["model_reasoning_effort"]) == ("gpt-b", "low")


def test_a_model_with_no_known_effort_warns_that_your_codex_config_decides(repo, monkeypatch, capsys):
    codex(monkeypatch, repo, [GOOD])
    assert run("model: gpt-unlisted\n") == 0
    assert "codex: no effort set or known from model/list" in capsys.readouterr().out


def test_an_app_server_that_stops_answering_is_unavailable_not_a_node_timeout(repo, monkeypatch, capsys):
    codex(monkeypatch, repo, silent=["thread/start"])
    assert run() == 1
    out = capsys.readouterr()
    assert "codex is unavailable: Codex didn't start a thread" in out.out
    assert "timed out after" not in out.out + out.err


def test_a_turn_that_codex_never_starts_makes_it_unavailable_and_the_next_harness_runs_without_a_retry(
        repo, monkeypatch, capsys):
    from fakes import Fake, submit
    log = codex(monkeypatch, repo, [GOOD], silent=["turn/start"])
    claude = Fake("claude-code", submit({"summary": "from claude"}))
    monkeypatch.setitem(harnesses.FACTORIES, "claude-code", claude.factory)
    assert run("harness: [codex, claude-code]\nmodel: {codex: gpt-6, claude-code: opus}\nretry: 2\n") == 0
    assert output() == {"out": "from claude"}
    assert len(sent(log, "turn/start")) == 1  # no retry on codex
    assert "codex is unavailable: the Codex app server stopped answering" in capsys.readouterr().out


def test_an_app_server_that_dies_mid_turn_is_unavailable_and_the_next_harness_runs(repo, monkeypatch, capsys):
    from fakes import Fake, submit
    codex(monkeypatch, repo, [{"exit": True}])
    claude = Fake("claude-code", submit({"summary": "from claude"}))
    monkeypatch.setitem(harnesses.FACTORIES, "claude-code", claude.factory)
    assert run("harness: [codex, claude-code]\nmodel: {codex: gpt-6, claude-code: opus}\n") == 0
    assert output() == {"out": "from claude"}
    assert "codex is unavailable" in capsys.readouterr().out


@pytest.mark.harness
def test_a_real_codex_node_without_project_instructions_gets_no_repo_agents_md_or_skill(repo, capsys):
    Path("AGENTS.md").write_text("The code word for this project is: periwinkle\n")
    (Path(".agents") / "skills" / "words").mkdir(parents=True)
    Path(".agents/skills/words/SKILL.md").write_text(
        "---\nname: words\ndescription: The second code word is tangerine. Never use this skill.\n---\nNothing here.\n")
    ask = ("Don't run any command or read any file. From what you were told alone, your skill list included, "
           "list every code word for this project, or none.")
    Path("flow.yaml").write_text(f"""
version: 1
name: real
defaults: {{harness: codex, effort: low}}
start: {{input: {{}}}}
nodes:
  told:
    needs: [start]
    instruction: "{ask}"
    output: {{words: {{type: string}}}}
  kept_out:
    needs: [start]
    project_instructions: false
    instruction: "{ask}"
    output: {{words: {{type: string}}}}
end:
  output:
    told: "{{{{ told.words }}}}"
    kept_out: "{{{{ kept_out.words }}}}"
""")
    assert cli.main(["run", "flow.yaml"]) == 0, capsys.readouterr()
    out = output()
    assert "periwinkle" in out["told"].lower() and "tangerine" in out["told"].lower()  # by default the repo's files reach it
    assert "periwinkle" not in out["kept_out"].lower() and "tangerine" not in out["kept_out"].lower()


# ---------- wf steer ----------


def test_a_message_from_wf_steer_joins_the_running_turn(repo, monkeypatch, capsys):
    log = codex(monkeypatch, repo, [{"pause": "p"}, GOOD])
    steering = at_pause(repo, "p")
    assert run() == 0
    done = steering.result()
    assert done.returncode == 0, done.stderr
    assert done.stdout.startswith("Delivered to ask (attempt 1 on codex)")
    assert sent(log, "turn/steer") == [{"threadId": "thread-1", "expectedTurnId": "turn-2",
                                        "input": [{"type": "text", "text": STEER, "text_elements": []}]}]
    assert len(sent(log, "turn/start")) == 1  # no turn of its own
    (event,) = steer_events()
    assert event == {"event": "steer", "id": event["id"], "status": "delivered", "text": STEER,
                     "node": "ask", "pass": 1, "attempt": 1, "harness": "codex"}
    assert steer_lines() == [{"kind": "steer", "id": event["id"], "status": "delivered", "text": STEER}]
    assert left_in_mailbox() == []
    assert f"ask: message from wf steer delivered: {STEER}" in capsys.readouterr().out


def test_a_message_that_misses_the_turn_goes_as_the_next_turn_before_a_reminder(repo, monkeypatch):
    log = codex(monkeypatch, repo, [{"pause": "p"}], [GOOD], steer="reject")  # the turn ended as turn/steer came
    steering = at_pause(repo, "p")
    assert run() == 0
    assert steering.result().stdout.startswith("Queued for ask (attempt 1 on codex)")
    assert [turn["input"][0]["text"] for turn in sent(log, "turn/start")] == [f"Say hi\n\n{FINISH}", STEER]
    assert [(e["status"], e.get("text")) for e in steer_events()] == [("queued", STEER), ("delivered", None)]


def test_a_message_codex_never_answers_for_is_unconfirmed_and_never_sent_again(repo, monkeypatch):
    log = codex(monkeypatch, repo, [{"pause": "p"}, GOOD], silent=["turn/steer"])
    steering = at_pause(repo, "p")
    assert run() == 0
    reason = "codex didn't answer as it took the message, so wf can't tell whether the agent read it"
    assert steering.result().stdout == f"Unconfirmed for ask (attempt 1 on codex): {reason}.\n"
    assert len(sent(log, "turn/steer")) == 1 and len(sent(log, "turn/start")) == 1
    (unconfirmed,) = steer_events()
    assert (unconfirmed["status"], unconfirmed["text"], unconfirmed["reason"]) == ("unconfirmed", STEER, reason)


def test_a_message_codex_never_took_is_dropped_when_the_agent_submits_first(repo, monkeypatch):
    log = codex(monkeypatch, repo, [{"pause": "p"}, GOOD], steer="reject")
    steering = at_pause(repo, "p")
    assert run() == 0
    assert steering.result().returncode == 0
    assert len(sent(log, "turn/start")) == 1
    queued, dropped = steer_events()
    assert (queued["status"], dropped["status"], dropped["id"]) == ("queued", "dropped", queued["id"])
    assert dropped["reason"] == "the agent submitted its result before it read the message"
    assert [line["status"] for line in steer_lines()] == ["queued", "dropped"]
