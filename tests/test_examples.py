"""The spec's own examples (workflow/examples/*.yaml) run end to end, with fake agents.

Claude Code and Codex are scripted fakes (fakes.py), so this checks the
workflow files and the runner together: worktrees, the review loop, history
in instructions, a human approval, and a sub-workflow node.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from fakes import Fake, edits, submit
from workflow import cli, harnesses, runner

EXAMPLES = Path(__file__).parent.parent / "workflow" / "examples"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(runner, "interactive", lambda: False)
    subprocess.run(["git", "init", "-q"], check=True)
    Path("README").write_text("hi\n")
    subprocess.run(["git", "add", "README"], check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "first"], check=True)
    for name in ("implement.yaml", "feature.yaml"):
        shutil.copy(EXAMPLES / name, name)
    return tmp_path


def agents(monkeypatch, claude: list, codex: list) -> tuple[Fake, Fake]:
    fakes = Fake("claude-code", *claude), Fake("codex", *codex)
    for fake in fakes:
        monkeypatch.setitem(harnesses.FACTORIES, fake.name, fake.factory)
    return fakes


def only_run() -> Path:
    (folder,) = (Path(".wf") / "runs").iterdir()
    return folder


def output() -> dict:
    return json.loads((only_run() / "output.json").read_text())


def test_implement_passes_on_the_first_round(repo, monkeypatch):
    claude, codex = agents(
        monkeypatch,
        claude=[submit({"plan": "add new.txt", "files": ["new.txt"]}),
                edits({"new.txt": "made\n"}, {"summary": "added new.txt", "notes": "first try"})],
        codex=[submit({"blocking_issues": []})],
    )
    assert cli.main(["run", "implement.yaml", "task=Add new.txt", "test_command=test -e new.txt"]) == 0
    run_id = only_run().name
    assert output() == {"branch": f"wf/{run_id}/implement", "summary": "added new.txt", "passed": True}
    assert claude.specs[1].access == "edit" and codex.specs[0].access == "read-only"
    assert Path(codex.specs[0].cwd) == Path(claude.specs[1].cwd)  # the reviewer works in implement's worktree
    assert subprocess.run(["git", "show", f"wf/{run_id}/implement:new.txt"], capture_output=True, text=True).stdout == "made\n"


def test_implement_loops_until_the_reviewer_is_happy(repo, monkeypatch):
    claude, codex = agents(
        monkeypatch,
        claude=[submit({"plan": "add new.txt", "files": ["new.txt"]}),
                edits({"new.txt": "made"}, {"summary": "added", "notes": "wrote the file"}),
                edits({"new.txt": "made\n"}, {"summary": "fixed the newline", "notes": "added the newline"})],
        codex=[submit({"blocking_issues": ["new.txt has no trailing newline"]}), submit({"blocking_issues": []})],
    )
    assert cli.main(["run", "implement.yaml", "task=Add new.txt", "test_command=test -e new.txt"]) == 0
    assert output()["summary"] == "fixed the newline"
    second = claude.sent[2]
    assert "This is attempt 2." in second
    assert "- wrote the file" in second  # notes from history.implement
    assert "- new.txt has no trailing newline" in second  # the reviewer's blocking issue
    assert "git diff" in second and claude.specs[2].cwd == claude.specs[1].cwd  # same worktree, same branch
    assert '"blocking_issues": ["new.txt has no trailing newline"]' in codex.sent[1]  # earlier reviews


def test_feature_ends_early_when_the_plan_is_rejected(repo, monkeypatch, capsys):
    agents(monkeypatch, claude=[submit({"tasks": [{"title": "A", "details": "do a"}]})], codex=[])
    assert cli.main(["run", "feature.yaml", "feature=Two things"]) == 2  # waiting for approval
    capsys.readouterr()
    typed = iter(["no"])
    monkeypatch.setattr(runner, "interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": next(typed))
    assert cli.main(["resume", only_run().name]) == 0
    assert output() == {"pr_url": None}
    assert "- A" in capsys.readouterr().out  # the approval message lists the tasks


def test_feature_with_no_tasks_runs_the_build_node_on_an_empty_list(repo, monkeypatch, capsys):
    agents(monkeypatch, claude=[submit({"tasks": []})], codex=[])
    typed = iter(["yes"])
    monkeypatch.setattr(runner, "interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": next(typed))
    assert cli.main(["run", "feature.yaml", "feature=Nothing"]) == 0
    assert output() == {"pr_url": None}
    assert "▶ build (0 items)" in capsys.readouterr().out
