"""The Claude Code adapter: settings per access level, and (opt-in) one real run.

The real run costs a little quota. Run it with:  uv run pytest -m harness
"""

import json
import subprocess
from pathlib import Path

import pytest

from workflow import cli, schemas
from workflow.harnesses.base import SessionSpec
from workflow.harnesses.claude_code import GIT, TOOL, options
from workflow.output_tool import ResultTool


def spec(tmp_path, access="read-only", web=False, **extra) -> SessionSpec:
    tool = ResultTool(schemas.object_schema({"summary": {"type": "string"}}))
    return SessionSpec(cwd=tmp_path, access=access, web=web, model="claude-opus-5-5", effort="high",
                       tool=tool, log=tmp_path / "attempt-1.log", emit=lambda e: None, **extra)


def test_read_only_reads_and_searches_and_gets_git_without_a_shell(tmp_path, monkeypatch):
    monkeypatch.delenv("WF_CLAUDE_AUTH", raising=False)
    o = options(spec(tmp_path))
    assert o.tools == ["Read", "Grep", "Glob"]  # no Bash at all
    assert o.allowed_tools == ["Read", "Grep", "Glob", GIT, TOOL]
    assert o.permission_mode == "dontAsk"  # anything not allowed is refused, never asked about


def test_edit_adds_editing_and_any_shell_command(tmp_path):
    o = options(spec(tmp_path, access="edit"))
    assert o.tools == ["Read", "Grep", "Glob", "Edit", "Write", "Bash"]
    assert o.allowed_tools == ["Read", "Grep", "Glob", "Edit", "Write", "Bash", TOOL]
    assert o.permission_mode == "dontAsk"


def test_full_has_no_restrictions_except_web(tmp_path):
    o = options(spec(tmp_path, access="full"))
    assert o.tools is None  # Claude Code's whole default tool set
    assert o.permission_mode == "bypassPermissions"
    assert o.disallowed_tools == ["WebSearch", "WebFetch"]
    assert options(spec(tmp_path, access="full", web=True)).disallowed_tools == []


def test_web_adds_search_and_fetch(tmp_path):
    o = options(spec(tmp_path, web=True))
    assert o.tools[-2:] == ["WebSearch", "WebFetch"]
    assert o.allowed_tools[-3:] == ["WebSearch", "WebFetch", TOOL]


def test_every_session_sets_everything_explicitly(tmp_path, monkeypatch):
    monkeypatch.delenv("WF_CLAUDE_AUTH", raising=False)
    o = options(spec(tmp_path, max_turns=30, budget_usd=2.5))
    assert (o.cwd, o.model, o.effort, o.max_turns, o.max_budget_usd) == (tmp_path, "claude-opus-5-5", "high", 30, 2.5)
    assert o.setting_sources == ["project"]  # the repo's CLAUDE.md, not your user settings
    assert o.system_prompt == {"type": "preset", "preset": "claude_code"}
    assert list(o.mcp_servers) == ["wf"]
    assert o.env == {"ANTHROPIC_API_KEY": "", "ANTHROPIC_AUTH_TOKEN": ""}  # use the subscription login


def test_api_key_auth_can_be_chosen(tmp_path, monkeypatch):
    monkeypatch.setenv("WF_CLAUDE_AUTH", "api-key")
    assert options(spec(tmp_path)).env == {}


@pytest.mark.harness
def test_a_real_read_only_agent_node_on_claude_code(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    git = lambda *a: subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *a], check=True, capture_output=True)
    git("init", "-q")
    Path("notes.txt").write_text("The secret word is: marmalade\n")
    git("add", "notes.txt")
    git("commit", "-qm", "first")
    Path("notes.txt").write_text("The secret word is: quince\n")  # only `git diff` shows the old one
    Path("flow.yaml").write_text("""
version: 1
name: real
defaults:
  harness: claude-code
  harnesses:
    claude-code: {model: claude-haiku-5-5, effort: low}
start: {input: {}}
nodes:
  read:
    needs: [start]
    instruction: Use git to see what changed in notes.txt since the last commit. Report the old and the new secret word.
    output:
      old: {type: string, pattern: "^[A-Za-z]+$", description: The old secret word only}
      new: {type: string, pattern: "^[A-Za-z]+$", description: The new secret word only}
end:
  output:
    old: "{{ read.old }}"
    new: "{{ read.new }}"
    cost: {type: number, value: "{{ run.cost_usd }}"}
""")
    assert cli.main(["run", "flow.yaml"]) == 0, capsys.readouterr()
    (folder,) = Path(".wf/runs").iterdir()
    out = json.loads((folder / "output.json").read_text())
    assert (out["old"].lower(), out["new"].lower()) == ("marmalade", "quince")
    assert out["cost"] > 0
    log = [json.loads(line) for line in (folder / "nodes" / "read" / "1" / "attempt-1.log").read_text().splitlines()]
    assert any(e["kind"] == "tool" and e["name"] == GIT for e in log)  # it used the runner's git tool
    assert Path("notes.txt").read_text() == "The secret word is: quince\n"  # and changed nothing
