"""Agent nodes through `wf run`, on a fake harness.

The fake follows a script: each send() takes the next turn from a list. A turn
can call submit_result, talk without submitting, fail, report the harness
unavailable, or hang. This tests the attempt loop in nodes.py (reminders,
retries, fallbacks, timeouts, usage, limits) without running an agent.
"""

import json
import subprocess
from pathlib import Path
from textwrap import dedent, indent

import pytest

from workflow import cli, harnesses, runner
from fakes import Fake, ends, hang, submit, talk
from workflow.output_tool import FINISH, REMINDER


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    subprocess.run(["git", "init", "-q"], check=True)
    monkeypatch.setattr(runner, "interactive", lambda: False)
    return tmp_path


def install(monkeypatch, *fakes: Fake) -> None:
    for fake in fakes:
        monkeypatch.setitem(harnesses.FACTORIES, fake.name, fake.factory)


def flow(nodes: str, top: str = "") -> str:
    steps = '    steps: {type: [array, "null"], value: "{{ plan.steps if plan else none }}"}\n' if "plan:" in nodes else ""
    text = f"""
version: 1
name: agents
{dedent(top).strip()}
defaults:
  harness: claude-code
  harnesses:
    claude-code: {{model: opus, effort: high}}
    codex: {{model: gpt-6}}
start: {{input: {{task: {{type: string, default: tidy up}}}}}}
nodes:
{indent(dedent(nodes).strip(), '  ')}
end:
  output:
    tokens: {{type: integer, value: "{{{{ run.tokens }}}}"}}
    cost: {{type: number, value: "{{{{ run.cost_usd }}}}"}}
{steps}"""
    Path("flow.yaml").write_text(text)
    return "flow.yaml"


PLAN = """
plan:
  needs: [start]
  instruction: "Plan this: {{ start.task }}"
  output: {steps: {type: array, items: {type: string}}}
"""


def run_dir() -> Path:
    (folder,) = (Path(".wf") / "runs").iterdir()
    return folder


def output() -> dict:
    return json.loads((run_dir() / "output.json").read_text())


def status() -> str:
    return json.loads((run_dir() / "status.json").read_text())["status"]


# ---------- the result ----------


def test_an_agent_node_returns_what_it_submits(repo, monkeypatch, capsys):
    fake = Fake("claude-code", submit({"steps": ["a", "b"]}, tokens=120, cost=0.02))
    install(monkeypatch, fake)
    assert cli.main(["run", flow(PLAN)]) == 0
    assert output() == {"tokens": 120, "cost": 0.02, "steps": ["a", "b"]}
    instruction = f"Plan this: tidy up\n\n{FINISH}"
    assert fake.sent == [instruction]
    assert (run_dir() / "nodes" / "plan" / "1" / "instruction.md").read_text() == instruction
    spec = fake.specs[0]
    assert (spec.cwd, spec.access, spec.web, spec.model, spec.effort) == (repo.resolve(), "read-only", False, "opus", "high")
    assert spec.log == (run_dir() / "nodes" / "plan" / "1" / "attempt-1.log").resolve()
    assert fake.closed == 1
    assert "plan: Read notes.txt" in capsys.readouterr().out


def test_problems_go_back_to_the_agent_and_its_fixed_call_counts(repo, monkeypatch):
    fake = Fake("claude-code", submit({"steps": "a"}, {"steps": ["fixed"]}))
    install(monkeypatch, fake)
    assert cli.main(["run", flow(PLAN)]) == 0
    assert output()["steps"] == ["fixed"]
    assert fake.replies[0][1] is True and "steps: 'a' is not of type 'array'" in fake.replies[0][0]


def test_access_web_and_limits_reach_the_session(repo, monkeypatch):
    fake = Fake("claude-code", submit({"steps": []}))
    install(monkeypatch, fake)
    assert cli.main(["run", flow(PLAN + "  access: edit\n  web: true\n  max_turns: 7\n  budget_usd: 1.5\n")]) == 0
    spec = fake.specs[0]
    assert (spec.access, spec.web, spec.max_turns, spec.budget_usd) == ("edit", True, 7, 1.5)


# ---------- reminders and retries ----------


def test_a_turn_without_a_result_gets_a_reminder(repo, monkeypatch):
    fake = Fake("claude-code", talk(), submit({"steps": ["late"]}))
    install(monkeypatch, fake)
    assert cli.main(["run", flow(PLAN)]) == 0
    assert fake.sent[1:] == [REMINDER]
    assert output() == {"tokens": 200, "cost": 0.02, "steps": ["late"]}


def test_after_two_reminders_the_attempt_fails_and_a_retry_starts_fresh(repo, monkeypatch, capsys):
    fake = Fake("claude-code", talk(), talk(), talk(), submit({"steps": ["second try"]}))
    install(monkeypatch, fake)
    assert cli.main(["run", flow(PLAN)]) == 0  # agents get retry: 1 by default
    assert fake.sent[1:3] == [REMINDER, REMINDER]
    assert fake.sent[3] == fake.sent[0]  # a new session, same instruction
    assert [s.log.name for s in fake.specs] == ["attempt-1.log", "attempt-2.log"]
    assert fake.closed == 2
    assert "never made a valid submit_result call" in capsys.readouterr().out


def test_a_failed_turn_fails_the_attempt(repo, monkeypatch, capsys):
    install(monkeypatch, Fake("claude-code", ends("failed", "Claude Code: it broke")))
    assert cli.main(["run", flow(PLAN + "  retry: 0\n")]) == 1
    assert status() == "failed"
    assert "plan: Claude Code: it broke" in capsys.readouterr().err


def test_a_timeout_fails_the_attempt_and_closes_the_session(repo, monkeypatch, capsys):
    fake = Fake("claude-code", hang())
    install(monkeypatch, fake)
    assert cli.main(["run", flow(PLAN + "  retry: 0\n  timeout: 1s\n")]) == 1
    assert "timed out after 1s" in capsys.readouterr().err
    assert fake.closed == 1


# ---------- falling back to other harnesses ----------


def test_an_unavailable_harness_falls_back_to_the_next_one_at_once(repo, monkeypatch, capsys):
    claude = Fake("claude-code", ends("unavailable", "Claude usage limit reached"))
    codex = Fake("codex", submit({"steps": ["from codex"]}))
    install(monkeypatch, claude, codex)
    node = PLAN + "  harness: [claude-code, codex]\n  model: {claude-code: opus, codex: gpt-6-mini}\n"
    assert cli.main(["run", flow(node)]) == 0
    assert output()["steps"] == ["from codex"]
    assert len(claude.specs) == 1  # no retry on the same harness
    assert codex.specs[0].model == "gpt-6-mini"
    assert codex.specs[0].log.name == "attempt-2.log"
    assert "claude-code is unavailable" in capsys.readouterr().out


def test_a_harness_that_is_not_built_yet_counts_as_unavailable(repo, monkeypatch):
    install(monkeypatch, Fake("claude-code", submit({"steps": ["ok"]})))
    monkeypatch.delitem(harnesses.FACTORIES, "cursor", raising=False)
    node = PLAN + "  harness: [cursor, claude-code]\n  model: {cursor: composer-2.5, claude-code: opus}\n"
    assert cli.main(["run", flow(node)]) == 0
    assert output()["steps"] == ["ok"]


def test_best_of_n_runs_the_same_task_on_each_harness(repo, monkeypatch):
    claude, codex = Fake("claude-code", submit({"summary": "c"})), Fake("codex", submit({"summary": "x"}))
    install(monkeypatch, claude, codex)
    nodes = """
        each:
          needs: [start]
          for_each: '["claude-code", "codex"]'
          harness: "{{ item }}"
          instruction: "Try it on {{ item }}"
        """
    assert cli.main(["run", flow(nodes)]) == 0
    assert (claude.sent, codex.sent) == ([f"Try it on claude-code\n\n{FINISH}"], [f"Try it on codex\n\n{FINISH}"])


# ---------- usage and limits ----------


def test_a_cost_limit_stops_new_nodes(repo, monkeypatch, capsys):
    fake = Fake("claude-code", submit({"steps": ["a"]}, cost=0.06))  # a second node would need another turn
    install(monkeypatch, fake)
    nodes = PLAN + "later: {needs: [plan], instruction: more}\n"
    assert cli.main(["run", flow(nodes, top="limits: {cost_usd: 0.05}")]) == 1
    assert status() == "limit"
    assert "$0.06" in capsys.readouterr().err


def test_spending_by_a_failed_attempt_counts_and_stops_its_retry(repo, monkeypatch):
    fake = Fake("claude-code", talk(cost=0.02), talk(cost=0.02), talk(cost=0.02))
    install(monkeypatch, fake)
    assert cli.main(["run", flow(PLAN, top="limits: {cost_usd: 0.05}")]) == 1
    assert status() == "limit"
    assert len(fake.specs) == 1  # the retry never started


def test_a_timed_out_turn_still_counts_the_tokens_it_used(repo, monkeypatch):
    fake = Fake("claude-code", hang(tokens=500))
    install(monkeypatch, fake)
    assert cli.main(["run", flow(PLAN + "  timeout: 1s\n", top="limits: {tokens: 400}")]) == 1
    assert status() == "limit"  # the 500 tokens count, so the retry doesn't start
    assert len(fake.specs) == 1


def test_the_last_unavailable_harness_does_not_promise_another(repo, monkeypatch, capsys):
    install(monkeypatch, Fake("claude-code", ends("unavailable", "Claude usage limit reached")))
    assert cli.main(["run", flow(PLAN)]) == 1
    out = capsys.readouterr().out
    assert "claude-code is unavailable: Claude usage limit reached" in out
    assert "trying" not in out
