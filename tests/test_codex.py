"""The Codex adapter, against a scripted fake app server (fake_codex.py), through `wf run`.

The fake logs every request the adapter sends, so these tests check the
protocol: thread settings, dynamic tool replies, interrupts, declined approvals.
The opt-in test at the bottom runs the real `codex` (uses your Codex quota):
    uv run pytest -m harness tests/test_codex.py
"""

import json
import subprocess
import sys
from pathlib import Path
from textwrap import dedent, indent

import pytest

from workflow import cli, harnesses, runner
from workflow.harnesses.codex import Codex
from workflow.output_tool import FINISH, REMINDER

FAKE = Path(__file__).parent / "fake_codex.py"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / "repo").mkdir()
    monkeypatch.chdir(tmp_path / "repo")
    monkeypatch.setattr(runner, "interactive", lambda: False)
    subprocess.run(["git", "init", "-q"], check=True)
    return tmp_path


def codex(monkeypatch, tmp_path: Path, *turns, models=None, silent=(), skills=()) -> Path:
    """Use the fake app server for `codex`; returns its request log."""
    scenario, log = tmp_path / "scenario.json", tmp_path / "requests.jsonl"
    scenario.write_text(json.dumps({"turns": list(turns), "silent": list(silent), "skills": list(skills),
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


def test_an_app_server_that_dies_mid_turn_is_unavailable_and_the_next_harness_runs(repo, monkeypatch, capsys):
    from fakes import Fake, submit
    codex(monkeypatch, repo, [{"exit": True}])
    claude = Fake("claude-code", submit({"summary": "from claude"}))
    monkeypatch.setitem(harnesses.FACTORIES, "claude-code", claude.factory)
    assert run("harness: [codex, claude-code]\nmodel: {codex: gpt-6, claude-code: opus}\n") == 0
    assert output() == {"out": "from claude"}
    assert "codex is unavailable" in capsys.readouterr().out
