"""Limits: a run that reaches one pauses, and `wf retry --limit` continues it with a bigger budget.

Spending comes from dry-run scripts (`_tokens`), so these run in a second and cost nothing.
"""

import json
import subprocess
from pathlib import Path
from textwrap import dedent

import pytest
import yaml

from workflow import cli, harnesses, limits, runner
from workflow.spec import Limits


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(runner, "interactive", lambda: False)
    for name in list(harnesses.FACTORIES):
        monkeypatch.setitem(harnesses.FACTORIES, name, lambda: pytest.fail("a dry run called a real harness"))
    subprocess.run(["git", "init", "-q"], check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "first"], check=True)
    return tmp_path


def write(name: str, text: str) -> None:
    Path(name).write_text(dedent(text).lstrip())


def script(data: dict) -> str:
    Path("script.yaml").write_text(yaml.safe_dump(data))
    return "script.yaml"


def only_run() -> Path:
    (folder,) = (Path(".wf") / "runs").iterdir()
    return folder


def status() -> dict:
    return json.loads((only_run() / "status.json").read_text())


def output() -> dict:
    return json.loads((only_run() / "output.json").read_text())


TWO = """
version: 1
name: two
defaults: {harness: claude-code}
limits: {tokens: 1000}
start: {input: {}}
nodes:
  a: {needs: [start], instruction: x}
  b: {needs: [a], instruction: y}
end: {output: {said: "{{ b.summary }}", tokens: {type: integer, value: "{{ run.tokens }}"}}}
"""


def test_a_limit_pauses_the_run_and_a_bigger_one_continues_it(repo, capsys):
    write("two.yaml", TWO)
    steps = {"agents": {"a": [{"summary": "big", "_tokens": 5000}], "b": [{"summary": "done", "_tokens": 10}]}}
    assert cli.main(["run", "two.yaml", "--dry-run", script(steps)]) == 1
    run_id = only_run().name
    assert status()["status"] == "limit"
    assert f"wf retry {run_id} --limit" in capsys.readouterr().err
    assert cli.main(["retry", run_id, "--limit", "tokens=10000"]) == 0
    assert output() == {"said": "done", "tokens": 5010}  # a ran once, and its tokens counted once
    assert json.loads((only_run() / "run.json").read_text())["limits"] == {"tokens": 10000}


def test_retrying_without_more_budget_says_so_and_changes_nothing(repo, capsys):
    write("two.yaml", TWO)
    steps = {"agents": {"a": [{"summary": "big", "_tokens": 5000}], "b": [{"summary": "done"}]}}
    cli.main(["run", "two.yaml", "--dry-run", script(steps)])
    run_id = only_run().name
    capsys.readouterr()
    assert cli.main(["retry", run_id]) == 1
    assert "used 5,000 tokens, limit 1,000" in capsys.readouterr().err
    assert cli.main(["retry", run_id, "--limit", "tokens=2000"]) == 1  # not enough either
    assert "--limit tokens=" in capsys.readouterr().err
    assert "limits" not in json.loads((only_run() / "run.json").read_text())
    assert status()["status"] == "limit"
    assert cli.main(["retry", run_id, "--limit", "tokens=none"]) == 0  # no token limit at all
    assert output()["said"] == "done"


def test_a_raised_limit_holds_for_later_retries(repo, capsys):
    write("ask.yaml", """
        version: 1
        name: ask
        defaults: {harness: claude-code}
        limits: {tokens: 1000}
        start: {input: {}}
        nodes:
          a: {needs: [start], instruction: x}
          ok: {needs: [a], human: "Go on?", output: {go: {type: boolean}}}
          b: {needs: [ok], instruction: y}
        end: {output: {said: "{{ b.summary }}"}}
    """)
    steps = {"agents": {"a": [{"summary": "s", "_tokens": 5000}], "b": [{"summary": "done"}]}}
    cli.main(["run", "ask.yaml", "--dry-run", script(steps)])
    run_id = only_run().name
    assert cli.main(["retry", run_id, "--limit", "tokens=10000"]) == 2  # now it waits for the person
    Path("answers.json").write_text(json.dumps({status()["questions"][0]["key"]: {"go": True}}))
    assert cli.main(["answer", run_id, "answers.json"]) == 0  # still under the raised limit
    assert output() == {"said": "done"}


def test_an_attempt_the_limit_kept_from_retrying_gets_its_retry(repo):
    write("flaky.yaml", """
        version: 1
        name: flaky
        defaults: {harness: claude-code}
        limits: {tokens: 1000}
        start: {input: {}}
        nodes:
          a: {needs: [start], instruction: x, retry: 2}
        end: {output: {said: "{{ a.summary }}"}}
    """)
    steps = {"agents": {"a": [{"_fail": "crashed", "_tokens": 5000}, {"summary": "second try"}]}}
    assert cli.main(["run", "flaky.yaml", "--dry-run", script(steps)]) == 1
    assert cli.main(["retry", only_run().name, "--limit", "tokens=100000"]) == 0
    assert output() == {"said": "second try"}
    assert (only_run() / "nodes" / "a" / "1" / "attempt-2.log").exists()


def test_a_sub_workflow_the_limit_stopped_continues_where_it_stopped(repo):
    write("inner.yaml", """
        version: 1
        name: inner
        defaults: {harness: claude-code}
        start: {input: {}}
        nodes:
          x: {needs: [start], instruction: x}
          y: {needs: [x], instruction: y}
        end: {output: {said: "{{ y.summary }}"}}
    """)
    write("outer.yaml", """
        version: 1
        name: outer
        limits: {tokens: 1000}
        start: {input: {}}
        nodes:
          sub: {needs: [start], workflow: inner.yaml}
        end: {output: {said: "{{ sub.said }}", tokens: {type: integer, value: "{{ run.tokens }}"}}}
    """)
    # x has one entry: if the sub-workflow started over, x would have none left and fail
    steps = {"agents": {"x": [{"summary": "x", "_tokens": 5000}], "y": [{"summary": "y done", "_tokens": 10}]}}
    assert cli.main(["run", "outer.yaml", "--dry-run", script(steps)]) == 1
    assert status()["status"] == "limit"
    assert cli.main(["retry", only_run().name, "--limit", "tokens=10000"]) == 0
    assert output() == {"said": "y done", "tokens": 5010}


def test_a_run_can_start_with_a_different_limit(repo):
    write("two.yaml", TWO)
    steps = {"agents": {"a": [{"summary": "big", "_tokens": 5000}], "b": [{"summary": "done", "_tokens": 10}]}}
    assert cli.main(["run", "two.yaml", "--limit", "tokens=none", "--dry-run", script(steps)]) == 0
    assert output()["said"] == "done"


@pytest.mark.parametrize("pair, message", [
    ("money=5", "NAME one of cost_usd, tokens, duration"),
    ("tokens=lots", "tokens is a whole number"),
    ("cost_usd=-1", "cost_usd:"),
    ("duration=soon", "duration:"),
])
def test_bad_limits_are_refused(repo, capsys, pair, message):
    write("two.yaml", TWO)
    assert cli.main(["run", "two.yaml", "--limit", pair]) == 1
    assert message in capsys.readouterr().err
    assert not Path(".wf/runs").exists() or not list(Path(".wf/runs").iterdir())


def test_the_rules():
    assert limits.parse(["cost_usd=10", "tokens=none", "duration=3h"]) == {"cost_usd": 10.0, "tokens": None, "duration": "3h"}
    combined = limits.effective(Limits(cost_usd=5, tokens=100), {"tokens": None})
    assert (combined.cost_usd, combined.tokens) == (5, None)
    assert limits.check(combined, {"cost_usd": 5.5, "tokens": 10**9}, 0) == ("cost_usd", "spent $5.50, limit $5")
    assert limits.check(combined, {"cost_usd": 1.0, "tokens": 10**9}, 0) is None


def test_a_sub_workflows_spending_before_a_question_is_counted(repo):
    write("inner.yaml", """
        version: 1
        name: inner
        defaults: {harness: claude-code}
        start: {input: {}}
        nodes:
          x: {needs: [start], instruction: x}
          ok: {needs: [x], human: "Go on?", output: {go: {type: boolean}}}
          y: {needs: [ok], instruction: y}
        end: {output: {}}
    """)
    write("outer.yaml", """
        version: 1
        name: outer
        start: {input: {}}
        nodes:
          sub: {needs: [start], workflow: inner.yaml}
        end: {output: {tokens: {type: integer, value: "{{ run.tokens }}"}}}
    """)
    # The question pauses the parent's job; when it's answered, the job runs again
    steps = {"agents": {"x": [{"summary": "x", "_tokens": 100}], "y": [{"summary": "y", "_tokens": 10}]},
             "humans": {"ok": [{"go": True}]}}
    assert cli.main(["run", "outer.yaml", "--dry-run", script(steps)]) == 0
    assert output() == {"tokens": 110}


def test_a_paused_limit_names_the_limit_to_raise(repo, capsys):
    write("two.yaml", TWO.replace("limits: {tokens: 1000}", "limits: {tokens: 1000000, cost_usd: 1}"))
    steps = {"agents": {"a": [{"summary": "big", "_cost_usd": 2}], "b": [{"summary": "done"}]}}
    cli.main(["run", "two.yaml", "--dry-run", script(steps)])
    assert "--limit cost_usd=<more>" in capsys.readouterr().err
    cli.main(["status", only_run().name, "--json"])
    assert json.loads(capsys.readouterr().out)["retry"] == f"wf retry {only_run().name} --limit cost_usd=<more>"
