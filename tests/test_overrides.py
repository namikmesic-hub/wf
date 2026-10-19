"""Overrides: `wf retry --model`, with --harness and --effort, gives a node or one item another
model for the rest of the run. When an override is wrong, wf changes nothing.

Most tests are dry runs, which record the harness, model and effort that each attempt got in its
log. The tests that check whether a harness can run here use fake harnesses (fakes.py).
"""

import json
import subprocess
from pathlib import Path
from textwrap import dedent

import pytest
import yaml

from fakes import Fake, ends, submit
from workflow import cli, harnesses, overrides, runner


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(runner, "interactive", lambda: False)
    subprocess.run(["git", "init", "-q"], check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "first"], check=True)
    return tmp_path


@pytest.fixture
def no_real_harness(monkeypatch):
    for name in list(harnesses.FACTORIES):
        monkeypatch.setitem(harnesses.FACTORIES, name, lambda: pytest.fail("a dry run opened a real harness"))


def write(name: str, text: str) -> str:
    Path(name).write_text(dedent(text).lstrip())
    return name


def script(data: dict) -> str:
    Path("script.yaml").write_text(yaml.safe_dump(data))
    return "script.yaml"


def only_run() -> Path:
    (folder,) = (Path(".wf") / "runs").iterdir()
    return folder


def events(kind: str) -> list[dict]:
    lines = (only_run() / "events.jsonl").read_text().splitlines()
    return [{k: v for k, v in e.items() if k != "time"} for e in map(json.loads, lines) if e["event"] == kind]


def given(log: Path) -> tuple:
    """The harness, model and effort that a dry-run attempt got, from its log."""
    entry = next(e for e in map(json.loads, log.read_text().splitlines()) if e["kind"] == "dry_run")
    return entry["harness"], entry["model"], entry["effort"]


def record() -> dict:
    """What a refused override must leave as it was."""
    return {name: (only_run() / name).read_text() for name in ("run.json", "events.jsonl", "status.json")}


FAN = """
version: 1
name: fan
defaults: {harness: claude-code, harnesses: {claude-code: {model: opus, effort: high}}}
start: {input: {}}
nodes:
  review:
    needs: [start]
    for_each: "[{'harness': 'cursor', 'model': 'grok'}, {'harness': 'cursor', 'model': 'flash'}, {'harness': 'claude-code', 'model': 'opus'}]"
    harness: "{{ item.harness }}"
    model: "{{ item.model }}"
    instruction: Review the change
end: {output: {said: {type: array, value: "{{ review | map(attribute='summary') | list }}"}}}
"""


def test_a_failed_item_runs_again_with_the_model_it_is_given_and_the_others_stay_done(repo, no_real_harness, capsys):
    write("fan.yaml", FAN)
    steps = {"agents": {"review[0]": [{"summary": "a"}], "review[1]": [{"_unavailable": "resource_exhausted"}, {"summary": "b"}],
                        "review[2]": [{"summary": "c"}]}}
    assert cli.main(["run", "fan.yaml", "--dry-run", script(steps)]) == 1
    run_id = only_run().name
    capsys.readouterr()
    assert cli.main(["retry", run_id, "--harness", "review[1]=claude-code", "--model", "review[1]=sonnet",
                     "--effort", "review[1]=max"]) == 0
    printed = capsys.readouterr().out
    assert "Override for the rest of this run: review[1]: claude-code, model sonnet, effort max" in printed
    assert "review[1]: attempt 2 runs on claude-code sonnet, as the override for review[1] says" in printed
    assert json.loads((only_run() / "output.json").read_text()) == {"said": ["a", "b", "c"]}
    items = only_run() / "nodes" / "review" / "1"
    assert given(items / "1" / "attempt-1.log") == ("cursor", "flash", None)
    assert given(items / "1" / "attempt-2.log") == ("claude-code", "sonnet", "max")
    assert [sorted(p.name for p in (items / i).glob("attempt-*")) for i in ("0", "2")] == [["attempt-1.log"]] * 2
    assert events("override") == [{"event": "override", "node": "review", "index": 1, "harness": "claude-code",
                                   "model": "sonnet", "effort": "max",
                                   "last_error": "cursor is unavailable: dry run: resource_exhausted"}]
    assert sorted((e["index"], e["attempt"], e["harness"], e["model"], e.get("override")) for e in events("agent_model")) == [
        (0, 1, "cursor", "grok", None), (1, 1, "cursor", "flash", None), (1, 2, "claude-code", "sonnet", "review[1]"),
        (2, 1, "claude-code", "opus", None)]
    kept = json.loads((only_run() / "run.json").read_text())["overrides"]
    assert kept == {"review[1]": {"harness": "claude-code", "model": "sonnet", "effort": "max"}}


LOOP = """
version: 1
name: loop
defaults: {harness: claude-code, harnesses: {claude-code: {model: opus, effort: high}}}
start: {input: {}}
nodes:
  write:
    needs: [start]
    instruction: Write it
    routes:
      - {when: "write.summary == 'done'", to: end}
      - {to: write, max: 2}
      - {to: end}
end: {output: {said: "{{ write.summary }}"}}
"""


def test_an_override_holds_for_later_retries_and_later_passes(repo, no_real_harness):
    write("loop.yaml", LOOP)
    steps = {"agents": {"write": [{"_unavailable": "out"}, {"_unavailable": "out again"}, {"summary": "again"},
                                  {"summary": "done"}]}}
    assert cli.main(["run", "loop.yaml", "--dry-run", script(steps)]) == 1
    run_id = only_run().name
    assert cli.main(["retry", run_id, "--model", "write=sonnet"]) == 1  # the new model is out too
    assert cli.main(["retry", run_id]) == 0  # the override still holds, with no option
    assert json.loads((only_run() / "output.json").read_text()) == {"said": "done"}
    passes = only_run() / "nodes" / "write"
    assert [given(passes / "1" / f"attempt-{n}.log") for n in (1, 2, 3)] == [
        ("claude-code", "opus", "high"), ("claude-code", "sonnet", "high"), ("claude-code", "sonnet", "high")]
    assert given(passes / "2" / "attempt-1.log") == ("claude-code", "sonnet", "high")
    assert [(e["pass"], e["attempt"], e["model"], e.get("override")) for e in events("agent_model")] == [
        (1, 1, "opus", None), (1, 2, "sonnet", "write"), (1, 3, "sonnet", "write"), (2, 1, "sonnet", "write")]
    assert len(events("override")) == 1


MIXED = """
version: 1
name: mixed
defaults: {harness: claude-code, harnesses: {claude-code: {model: opus}}}
start: {input: {}}
nodes:
  plan: {needs: [start], instruction: Plan it}
  count: {needs: [plan], command: "echo 3"}
  review:
    needs: [count]
    for_each: "['a', 'b', 'c']"
    instruction: "Review {{ item }}"
  later:
    needs: [review]
    for_each: "['x']"
    instruction: "Check {{ item }}"
  both:
    needs: [review]
    harness: [claude-code, codex]
    model: {claude-code: opus, codex: gpt}
    instruction: Try both
  notes:
    needs: [review]
    harness: codex
    access: edit
    tools: [slack]
    instruction: Read the notes
  sub: {needs: [review], workflow: inner.yaml}
end: {output: {}}
"""

INNER = """
version: 1
name: inner
defaults: {harness: claude-code}
start: {input: {}}
nodes:
  step: {needs: [start], instruction: Do the step}
end: {output: {}}
"""


@pytest.mark.parametrize("args, message", [
    (["--model", "nope=m"], "nope: mixed has no node `nope`"),
    (["--model", "step=m"], "step: `step` is a step of the sub-workflow inner"),
    (["--model", "sub/step=m"], "--model: `sub/step` is a step in a sub-workflow"),
    (["--model", "count=m"], "count: `count` is a command node, and only an agent node has a model"),
    (["--model", "sub=m"], "sub: `sub` is a workflow node"),
    (["--model", "plan[0]=m"], "plan[0]: `plan` isn't a for_each node"),
    (["--model", "review[3]=m"], "review[3]: `review` has only items 0 to 2"),
    (["--model", "later[0]=m"], "later[0]: `later` hasn't started yet"),
    (["--model", "both=m"], "both: `both` picks its harness from a list, so name one: --harness both=NAME"),
    (["--harness", "review=gemini"], "review: unknown harness `gemini`"),
    (["--harness", "review=cursor"], "review: cursor needs a model"),
    (["--harness", "review=cursor", "--model", "review=grok", "--effort", "review=max"], "review: cursor has no effort setting"),
    (["--harness", "notes=claude-code"], "notes: `notes` has tools, and on claude-code at `edit` access its commands"),
    (["--model", "review"], "--model: expected NODE=VALUE"),
    (["--model", "review[x]=m"], "--model: `review[x]` isn't a node or an item"),
    (["--model", "review=a", "--model", "review=b"], "--model names review twice"),
])
def test_a_wrong_override_changes_nothing(repo, no_real_harness, capsys, args, message):
    write("mixed.yaml", MIXED)
    write("inner.yaml", INNER)
    steps = {"agents": {"plan": [{"summary": "p"}], "review[0]": [{"summary": "a"}], "review[1]": [{"_unavailable": "out"}],
                        "review[2]": [{"summary": "c"}]}}
    assert cli.main(["run", "mixed.yaml", "--dry-run", script(steps)]) == 1
    before = record()
    capsys.readouterr()
    assert cli.main(["retry", only_run().name, *args]) == 1
    error = capsys.readouterr().err
    assert message in error
    assert record() == before


def test_an_override_for_a_run_that_has_ended_changes_nothing(repo, no_real_harness, capsys):
    write("loop.yaml", LOOP)
    assert cli.main(["run", "loop.yaml", "--dry-run", script({"agents": {"write": [{"summary": "done"}]}})]) == 0
    before = record()
    assert cli.main(["retry", only_run().name, "--model", "write=sonnet"]) == 1
    assert "has ended, so no node is left to run with another model" in capsys.readouterr().err
    assert record() == before


def test_an_override_and_a_wrong_answer_are_both_reported_and_change_nothing(repo, no_real_harness, capsys):
    write("ask.yaml", """
        version: 1
        name: ask
        defaults: {harness: claude-code}
        start: {input: {}}
        nodes:
          ok: {needs: [start], human: "Go on?", output: {go: {type: boolean}}}
          write: {needs: [ok], instruction: Write it}
        end: {output: {said: "{{ write.summary }}"}}
    """)
    assert cli.main(["run", "ask.yaml", "--dry-run", script({"agents": {"write": [{"summary": "done"}]}})]) == 2
    key = json.loads((only_run() / "status.json").read_text())["questions"][0]["key"]
    before = record()
    capsys.readouterr()
    Path("wrong.json").write_text(json.dumps({key: {"go": "yes"}}))
    assert cli.main(["retry", only_run().name, "--answers", "wrong.json", "--model", "nope=m"]) == 1
    error = capsys.readouterr().err
    assert "nope: ask has no node `nope`" in error and "'yes' is not of type 'boolean'" in error
    assert record() == before
    Path("right.json").write_text(json.dumps({key: {"go": True}}))
    assert cli.main(["retry", only_run().name, "--answers", "right.json", "--model", "write=sonnet"]) == 0
    assert given(only_run() / "nodes" / "write" / "1" / "attempt-1.log") == ("claude-code", "sonnet", None)


def test_a_new_limit_is_kept_only_when_the_overrides_are_right(repo, no_real_harness, capsys):
    write("two.yaml", """
        version: 1
        name: two
        defaults: {harness: claude-code}
        limits: {tokens: 1000}
        start: {input: {}}
        nodes:
          a: {needs: [start], instruction: x}
          b: {needs: [a], instruction: y}
        end: {output: {said: "{{ b.summary }}"}}
    """)
    steps = {"agents": {"a": [{"summary": "big", "_tokens": 5000}], "b": [{"summary": "done"}]}}
    assert cli.main(["run", "two.yaml", "--dry-run", script(steps)]) == 1  # paused at the limit
    before = record()
    assert cli.main(["retry", only_run().name, "--limit", "tokens=10000", "--model", "nope=m"]) == 1
    assert record() == before
    assert cli.main(["retry", only_run().name, "--limit", "tokens=10000", "--model", "b=sonnet"]) == 0
    assert json.loads((only_run() / "run.json").read_text())["limits"] == {"tokens": 10000}
    assert given(only_run() / "nodes" / "b" / "1" / "attempt-1.log") == ("claude-code", "sonnet", None)


# ---------- whether the harness can run here ----------


ONE = """
version: 1
name: one
defaults: {harness: claude-code, harnesses: {claude-code: {model: opus, effort: high}, codex: {model: gpt-6}}}
start: {input: {}}
nodes:
  plan: {needs: [start], instruction: Plan it}
end: {output: {said: "{{ plan.summary }}"}}
"""


def failed_run(monkeypatch, codex: Fake) -> str:
    claude = Fake("claude-code", ends("failed", "overloaded"), ends("failed", "overloaded"))
    for fake in (claude, codex):
        monkeypatch.setitem(harnesses.FACTORIES, fake.name, fake.factory)
    write("one.yaml", ONE)
    assert cli.main(["run", "one.yaml"]) == 1
    return only_run().name


def test_a_harness_that_cant_start_here_changes_nothing(repo, monkeypatch, capsys):
    codex = Fake("codex", unavailable="the Codex app server didn't start: no such file")
    run_id = failed_run(monkeypatch, codex)
    before = record()
    capsys.readouterr()
    assert cli.main(["retry", run_id, "--harness", "plan=codex"]) == 1
    assert "plan: codex can't run on this machine: the Codex app server didn't start" in capsys.readouterr().err
    assert record() == before


def test_a_model_the_harness_doesnt_have_changes_nothing(repo, monkeypatch, capsys):
    codex = Fake("codex", models={"gpt-6"})
    run_id = failed_run(monkeypatch, codex)
    before = record()
    capsys.readouterr()
    assert cli.main(["retry", run_id, "--harness", "plan=codex", "--model", "plan=gpt-7"]) == 1
    assert "plan: codex has no model `gpt-7`" in capsys.readouterr().err
    assert record() == before


def test_a_harness_that_can_run_here_takes_the_failed_node(repo, monkeypatch):
    codex = Fake("codex", submit({"summary": "planned"}), models={"gpt-6"})
    run_id = failed_run(monkeypatch, codex)
    assert cli.main(["retry", run_id, "--harness", "plan=codex"]) == 0  # the workflow's model for codex
    assert json.loads((only_run() / "output.json").read_text()) == {"said": "planned"}
    assert codex.opened == 2  # once for the check, once for the run
    assert [(spec.model, spec.effort) for spec in codex.specs] == [("gpt-6", None)]
    assert [(e["attempt"], e["harness"], e.get("override")) for e in events("agent_model")] == [
        (1, "claude-code", None), (2, "claude-code", None), (3, "codex", "plan")]
    assert events("override")[0]["last_error"] == "overloaded"


def test_a_detached_retry_hands_the_override_to_its_background_process(repo, capsys):
    write("loop.yaml", LOOP)
    assert cli.main(["run", "loop.yaml", "--dry-run", script({"agents": {"write": [{"_unavailable": "out"}, {"summary": "done"}]}})]) == 1
    run_id = only_run().name
    assert cli.main(["retry", run_id, "--detach", "--json", "--model", "write=sonnet"]) == 0
    capsys.readouterr()
    assert cli.main(["wait", run_id, "--timeout", "30"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "succeeded"
    assert given(only_run() / "nodes" / "write" / "1" / "attempt-2.log") == ("claude-code", "sonnet", "high")


# ---------- the rules ----------


def test_an_item_override_wins_over_its_node():
    table = {"review": {"model": "a"}, "review[1]": {"model": "b"}}
    assert overrides.find(table, "review", 1) == ("review[1]", {"model": "b"})
    assert overrides.find(table, "review", 0) == ("review", {"model": "a"})
    assert overrides.find(table, "plan", None) is None


def test_an_item_index_is_read_as_a_number():
    assert overrides.parse({"model": ["review[02]=a"], "effort": [" review[2] = max "]}) == {
        "review[2]": {"model": "a", "effort": "max"}}
