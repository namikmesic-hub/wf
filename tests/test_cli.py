"""The wf command: init, validate and graph (run and resume are in test_run.py)."""

import re
import shutil
from pathlib import Path

import pytest

from workflow import cli

DOCS = Path(__file__).parent.parent / "docs"
EXAMPLES = Path(__file__).parent.parent / "workflow" / "examples"


@pytest.fixture
def here(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


# ---------- wf init ----------


def test_init_writes_the_starter_file_from_the_spec(here, capsys):
    assert cli.main(["init", "my-flow"]) == 0
    spec = (DOCS / "workflow-spec.md").read_text()
    expected = next(b for b in re.findall(r"```yaml\n(.*?)```", spec, re.S) if "name: my-flow\n" in b)
    assert (here / "my-flow.yaml").read_text() == expected
    assert "my-flow.yaml" in capsys.readouterr().out


def test_the_starter_file_is_valid(here, capsys):
    cli.main(["init", "my-flow"])
    capsys.readouterr()
    assert cli.main(["validate", "my-flow.yaml"]) == 0
    assert capsys.readouterr().out == "my-flow.yaml: valid\n"


def test_init_refuses_to_overwrite(here, capsys):
    (here / "my-flow.yaml").write_text("mine")
    assert cli.main(["init", "my-flow"]) == 1
    assert (here / "my-flow.yaml").read_text() == "mine"
    assert "already exists" in capsys.readouterr().err


def test_init_checks_the_name(here, capsys):
    assert cli.main(["init", "My Flow"]) == 1
    assert "lowercase letters, digits and -" in capsys.readouterr().err
    assert list(here.iterdir()) == []


# ---------- wf validate ----------


def test_validate_reports_warnings_and_passes(here, capsys):
    for name in ("implement.yaml", "feature.yaml"):
        shutil.copy(EXAMPLES / name, here / name)
    assert cli.main(["validate", "feature.yaml"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("feature.yaml: warning: limits.cost_usd: codex doesn't report cost")
    assert out[-1] == "feature.yaml: valid, 1 warning"


def test_validate_fails_on_errors(here, capsys):
    (here / "bad.yaml").write_text("version: 1\nname: bad\nstart: {}\nnodes:\n  a: {command: x}\nend: {}\n")
    assert cli.main(["validate", "bad.yaml"]) == 1
    assert capsys.readouterr().out.splitlines() == [
        "bad.yaml: nodes.a: this node never runs: give it `needs` (use [start] to run first) or point a route at it",
        "bad.yaml: 1 error",
    ]


def test_validate_a_missing_file(here, capsys):
    assert cli.main(["validate", "nope.yaml"]) == 1
    assert "can't read the file" in capsys.readouterr().out


# ---------- wf graph ----------


def test_graph_prints_mermaid(here, capsys):
    (here / "g.yaml").write_text(
        """version: 1
name: g
description: A "small" graph
defaults: {harness: claude-code}
start: {input: {task: {type: string}}}
nodes:
  plan:
    needs: [start]
    instruction: plan
    output: {tasks: {type: array, maxItems: 3, items: {type: string}}}
  approve: {needs: [plan], human: "ok?", output: {ok: {type: boolean}}, routes: [{when: approve.ok, to: build}, {to: end}]}
  build: {for_each: plan.tasks, command: "echo {{ item | quote }}", description: One per task}
  fix:
    needs: [build]
    instruction: fix
    access: edit
    workspace: new
    harness: codex
    routes: [{when: "true", to: fix, max: 2}, {to: end}]
end: {output: {}}
"""
    )
    assert cli.main(["graph", "g.yaml"]) == 0
    assert capsys.readouterr().out == """---
title: "g: A #quot;small#quot; graph"
---
flowchart TD
  wf_start(["start"])
  n_plan("plan<br/>agent")
  n_approve{{"approve<br/>human"}}
  n_build[/"build<br/>command · for each plan.tasks<br/>One per task"/]
  n_fix("fix<br/>agent · codex · edit · workspace: new")
  wf_end(["end"])
  wf_start --> n_plan
  n_plan --> n_approve
  n_approve -. "approve.ok" .-> n_build
  n_approve -.-> wf_end
  n_build --> n_fix
  n_fix -. "true · max 2" .-> n_fix
  n_fix -.-> wf_end
"""


def test_graph_of_an_invalid_file_fails(here, capsys):
    (here / "bad.yaml").write_text("version: 1\n")
    assert cli.main(["graph", "bad.yaml"]) == 1
    assert "missing required key" in capsys.readouterr().err


def test_graph_keeps_multi_line_descriptions_on_one_mermaid_line(here, capsys):
    (here / "m.yaml").write_text(
        'version: 1\nname: m\ndescription: "two\\nlines"\nstart: {}\nnodes:\n'
        '  a: {needs: [start], command: x, description: "first\\nsecond"}\nend: {}\n'
    )
    assert cli.main(["graph", "m.yaml"]) == 0
    out = capsys.readouterr().out
    assert 'title: "m: two<br/>lines"' in out
    assert 'n_a[/"a<br/>command<br/>first<br/>second"/]' in out


def test_examples_are_copied_out_and_valid(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["examples"]) == 0
    assert sorted(p.name for p in Path("wf-examples").iterdir()) == ["feature.yaml", "implement.yaml"]
    assert cli.main(["validate", "wf-examples/feature.yaml"]) == 0
    assert cli.main(["examples"]) == 0  # again: nothing is overwritten
    assert "already exists" in capsys.readouterr().out
