"""Agent nodes through `wf run`, on a fake harness.

The fake follows a script: each send() takes the next turn from a list. A turn
can call submit_result, talk without submitting, fail, report the harness
unavailable, or hang. This tests the attempt loop in nodes.py (reminders,
retries, fallbacks, timeouts, usage, limits) without running an agent.
"""

import json
import subprocess
import time
from pathlib import Path
from textwrap import dedent, indent

import pytest

from workflow import cli, harnesses, runner
from fakes import Fake, ends, hang, record_pauses, submit, talk
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


def test_project_instructions_reach_the_session_from_the_node_or_defaults(repo, monkeypatch):
    fake = Fake("claude-code", *[submit({"summary": "s"})] * 3)
    install(monkeypatch, fake)
    nodes = """
        a: {needs: [start], instruction: x}
        b: {needs: [start], instruction: x, project_instructions: true}
        c: {needs: [start], instruction: x, project_instructions: false}
        """
    assert cli.main(["run", flow(nodes)]) == 0
    assert sorted((s.log.parts[-3], s.project_instructions) for s in fake.specs) == [("a", True), ("b", True), ("c", False)]
    fake.specs.clear()
    fake.turns = [submit({"summary": "s"})] * 3
    Path("flow.yaml").write_text(Path(flow(nodes)).read_text().replace("defaults:\n", "defaults:\n  project_instructions: false\n"))
    assert cli.main(["run", "flow.yaml"]) == 0
    assert sorted((s.log.parts[-3], s.project_instructions) for s in fake.specs) == [("a", False), ("b", True), ("c", False)]


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


def test_a_failed_turn_can_ask_for_a_wait_before_its_retry_but_not_before_a_fallback(repo, monkeypatch):
    pauses = record_pauses(monkeypatch)
    busy = ends("failed", "the model is busy", retry_after=0.01)
    claude, codex = Fake("claude-code", busy, busy), Fake("codex", submit({"steps": ["from codex"]}))
    install(monkeypatch, claude, codex)
    node = PLAN + "  harness: [claude-code, codex]\n  model: {claude-code: opus, codex: gpt-6-mini}\n"
    assert cli.main(["run", flow(node)]) == 0
    assert output()["steps"] == ["from codex"]
    assert len(claude.specs) == 2
    assert pauses == [0.01]  # before claude-code's retry, and none before codex took over


@pytest.mark.parametrize("limit", ["{cost_usd: 0.01}", "{duration: 1s}"])  # reached before the wait, and during it
def test_a_limit_ends_the_wait_before_a_retry_and_the_retry_does_not_start(repo, monkeypatch, limit):
    fake = Fake("claude-code", ends("failed", "the model is busy", cost=0.02, retry_after=60))
    install(monkeypatch, fake)
    began = time.monotonic()
    assert cli.main(["run", flow(PLAN, top=f"limits: {limit}")]) == 1
    assert time.monotonic() - began < 30  # the wait stopped at the limit, long before its minute
    assert status() == "limit"
    assert len(fake.specs) == 1


def test_a_harness_that_is_not_built_yet_counts_as_unavailable(repo, monkeypatch):
    install(monkeypatch, Fake("claude-code", submit({"steps": ["ok"]})))
    monkeypatch.delitem(harnesses.FACTORIES, "cursor", raising=False)
    node = PLAN + "  harness: [cursor, claude-code]\n  model: {cursor: composer-2.5, claude-code: opus}\n"
    assert cli.main(["run", flow(node)]) == 0
    assert output()["steps"] == ["ok"]


@pytest.mark.parametrize("harness", ["missing", "claude_code", "__class__"])
@pytest.mark.parametrize("global_cap", [None, "1"])
def test_an_unknown_template_harness_keeps_the_unavailable_error(repo, wf_home, monkeypatch, capsys, harness, global_cap):
    (wf_home / "config.toml").write_text("[capacity]\nclaude-code = 1\ncodex = 1\ncursor = 1\n")
    if global_cap is not None:
        monkeypatch.setenv("WF_MAX_AGENTS", global_cap)
    node = PLAN + '  harness: "{{ start.task }}"\n'
    assert cli.main(["run", flow(node), f"task={harness}"]) == 1
    message = f"{harness} is unavailable: the `{harness}` harness isn't supported yet"
    out = capsys.readouterr()
    assert message in out.err
    assert "AttributeError" not in out.out + out.err
    entries = [json.loads(line) for line in (run_dir() / "events.jsonl").read_text().splitlines()]
    assert [entry["error"] for entry in entries if entry["event"] == "harness_unavailable"] == [message]
    assert not any(entry["event"] in ("session_started", "slot_waiting") for entry in entries)


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


# ---------- model and effort as templates ----------


def seats(effort: str) -> str:
    """Review seats as an input: each item names its own harness, model and effort."""
    Path("seats.yaml").write_text(dedent(f"""
        version: 1
        name: seats
        types:
          seat:
            name: {{type: string}}
            harness: {{enum: [claude-code, codex]}}
            model: {{type: string}}
            effort: {{type: string, optional: true}}
        start:
          input:
            seats:
              type: array
              maxItems: 3
              items: {{$ref: seat}}
              default:
                - {{name: opus, harness: claude-code, model: claude-opus-5-5, effort: high}}
                - {{name: sol, harness: codex, model: gpt-6}}
        nodes:
          review:
            needs: [start]
            for_each: start.seats
            harness: "{{{{ item.harness }}}}"
            model: "{{{{ item.model }}}}"
            effort: {effort}
            instruction: "Review as {{{{ item.name }}}}"
        end: {{output: {{}}}}
    """))
    return "seats.yaml"


def test_each_item_gets_the_harness_model_and_effort_its_templates_render(repo, monkeypatch):
    claude, codex = Fake("claude-code", submit({"summary": "c"})), Fake("codex", submit({"summary": "x"}))
    install(monkeypatch, claude, codex)
    assert cli.main(["run", seats('{claude-code: "{{ item.effort }}"}')]) == 0
    assert (claude.specs[0].model, claude.specs[0].effort) == ("claude-opus-5-5", "high")
    assert (codex.specs[0].model, codex.specs[0].effort) == ("gpt-6", None)  # no effort set for codex
    assert json.loads((run_dir() / "nodes" / "review" / "1" / "0" / "session.json").read_text())["model"] == "claude-opus-5-5"


def test_an_effort_that_renders_to_none_fails_the_item_before_a_session_starts(repo, monkeypatch, capsys):
    claude, codex = Fake("claude-code", submit({"summary": "c"})), Fake("codex", submit({"summary": "x"}))
    install(monkeypatch, claude, codex)
    assert cli.main(["run", seats('"{{ item.effort }}"')]) == 1  # the codex seat leaves effort out
    assert codex.specs == []
    assert "item 1: `effort` of node `review` renders to none for codex, but it must be a non-empty string" in capsys.readouterr().err


def test_a_rendered_model_and_effort_lose_their_outer_whitespace(repo, monkeypatch):
    fake = Fake("claude-code", submit({"steps": []}))
    install(monkeypatch, fake)
    assert cli.main(["run", flow(PLAN + "  model: \"{{ ' opus ' }}\"\n  effort: \"{{ ' high ' }}\"\n")]) == 0
    assert (fake.specs[0].model, fake.specs[0].effort) == ("opus", "high")


@pytest.mark.parametrize("model, shown", [('"{{ none }}"', "none"), ('"{{ \' \' }}"', '" "'), ('"{{ 5 }}"', "5")])
def test_a_model_must_render_to_text(repo, monkeypatch, capsys, model, shown):
    fake = Fake("claude-code", submit({"steps": []}))
    install(monkeypatch, fake)
    assert cli.main(["run", flow(PLAN + f"  model: {model}\n")]) == 1
    assert fake.specs == []
    assert f"`model` of node `plan` renders to {shown} for claude-code" in capsys.readouterr().err


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
    events = [json.loads(line) for line in (run_dir() / "events.jsonl").read_text().splitlines()]
    kinds = [e["event"] for e in events]
    assert [(e["attempt"], e["tokens"]) for e in events if e["event"] == "agent_usage"] == [(1, 500)]  # told as the session closes
    assert kinds.index("agent_usage") < kinds.index("session_ended")


def test_the_last_unavailable_harness_does_not_promise_another(repo, monkeypatch, capsys):
    install(monkeypatch, Fake("claude-code", ends("unavailable", "Claude usage limit reached")))
    assert cli.main(["run", flow(PLAN)]) == 1
    out = capsys.readouterr().out
    assert "claude-code is unavailable: Claude usage limit reached" in out
    assert "trying" not in out
