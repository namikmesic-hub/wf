"""`workspace: new` end to end: worktrees, the runner's commits, resets before retries, cleanup.

Worktrees go under WF_HOME, which each test points at its own folder.
"""

import json
import subprocess
from pathlib import Path
from textwrap import dedent, indent

import pytest

from fakes import Fake, edits, submit, talk
from workflow import cli, harnesses, runner
from workflow.output_tool import FINISH


def git(*args: str, cwd=".") -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / "repo").mkdir()
    monkeypatch.chdir(tmp_path / "repo")
    monkeypatch.setenv("WF_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(runner, "interactive", lambda: False)
    git("init", "-q")
    Path("README").write_text("hello\n")
    git("add", "README")
    git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "first")
    return tmp_path / "repo"


def use(monkeypatch, *turns) -> Fake:
    fake = Fake("claude-code", *turns)
    monkeypatch.setitem(harnesses.FACTORIES, "claude-code", fake.factory)
    return fake


def write(nodes: str, end: str = "{}", inputs: str = "{}") -> None:
    body = indent(dedent(end).strip(), "    ") if not end.strip().startswith("{") else ""
    Path("flow.yaml").write_text(
        f"version: 1\nname: ws\ndefaults: {{harness: claude-code}}\nstart: {{input: {inputs}}}\n"
        f"nodes:\n{indent(dedent(nodes).strip(), '  ')}\n"
        f"end:\n  output:{' ' + end if not body else chr(10) + body}\n"
    )


def run(nodes: str, end: str = "{}", inputs: str = "{}") -> int:
    write(nodes, end, inputs)
    return cli.main(["run", "flow.yaml"])


def only_run() -> Path:
    (folder,) = (Path(".wf") / "runs").iterdir()
    return folder


def output() -> dict:
    return json.loads((only_run() / "output.json").read_text())


def test_an_edit_agent_works_on_its_own_branch_and_the_runner_commits(repo, monkeypatch, tmp_path):
    fake = use(monkeypatch, edits({"new.txt": "made\n"}, {"summary": "added new.txt"}))
    code = run(
        """
        impl: {needs: [start], instruction: Add new.txt, access: edit, workspace: new}
        look: {needs: [impl], workspace: impl, command: "cat new.txt; git log -1 --format=%s"}
        """,
        end="""
        branch: "{{ workspace.impl.branch }}"
        base: "{{ workspace.impl.base }}"
        seen: "{{ look.stdout }}"
        """,
    )
    assert code == 0
    run_id = only_run().name
    out = output()
    assert out["branch"] == f"wf/{run_id}/impl"
    assert out["base"] == git("rev-parse", "HEAD")
    assert out["seen"] == f"made\nwf: impl ({run_id})\n"
    assert Path(fake.specs[0].cwd).is_relative_to(tmp_path / "home" / "worktrees" / run_id)
    assert not (tmp_path / "home" / "worktrees" / run_id).exists()  # removed once the run succeeded
    assert git("show", f"{out['branch']}:new.txt") == "made"  # the branch stays
    assert not Path("new.txt").exists() and git("status", "--porcelain") == "?? flow.yaml"  # your checkout is untouched


def test_edit_agents_in_a_worktree_are_told_not_to_commit(repo, monkeypatch):
    fake = use(monkeypatch, submit({"summary": "x"}), submit({"summary": "y"}))
    assert run("""
        impl: {needs: [start], instruction: Do it, access: edit, workspace: new}
        mine: {needs: [impl], instruction: Do it here, access: edit}
    """) == 0
    assert fake.sent[0].endswith(f"Do it\n\nDon't commit: the runner commits your changes when you're done.\n\n{FINISH}")
    assert fake.sent[1] == f"Do it here\n\n{FINISH}"  # your own checkout: no such note, and no commit


def test_a_loop_keeps_working_on_one_branch(repo, monkeypatch):
    use(monkeypatch, edits({"a.txt": "1\n"}, {"summary": "one"}), edits({"b.txt": "2\n"}, {"summary": "two"}))
    assert run("""
        impl: {needs: [start], instruction: work, access: edit, workspace: new}
        check:
          needs: [impl]
          workspace: impl
          command: "test -e b.txt"
          check: false
          routes: [{when: "check.exit_code != 0", to: impl, max: 3}, {to: end}]
    """, end='{commits: "{{ workspace.impl.branch }}"}') == 0
    branch = output()["commits"]
    assert git("log", "--format=%s", f"{git('rev-parse', 'HEAD')}..{branch}").splitlines() == [
        f"wf: impl ({only_run().name})"] * 2


def test_each_item_of_a_for_each_gets_its_own_worktree(repo, monkeypatch):
    use(monkeypatch, edits({"f.txt": "a\n"}, {"summary": "a"}), edits({"f.txt": "b\n"}, {"summary": "b"}))
    assert run("""
        each: {needs: [start], for_each: '["a", "b"]', max_parallel: 1, instruction: "{{ item }}", access: edit, workspace: new}
    """, end='{branches: {type: array, value: "{{ workspace.each | map(attribute=\'branch\') | list }}"}}') == 0
    run_id = only_run().name
    assert output()["branches"] == [f"wf/{run_id}/each/0", f"wf/{run_id}/each/1"]
    assert [git("show", f"wf/{run_id}/each/{i}:f.txt") for i in (0, 1)] == ["a", "b"]


def test_a_retry_starts_from_the_same_commit_and_a_failed_run_keeps_its_worktree(repo, monkeypatch, tmp_path):
    def clean(cwd: Path):
        assert not (cwd / "junk.txt").exists(), "the retry should start from a clean worktree"

    fake = use(monkeypatch,
               edits({"junk.txt": "half done\n"}), talk(), talk(),  # attempt 1: never submits
               edits({"good.txt": "ok\n"}, {"summary": "done"}, check=clean))
    assert run("impl: {needs: [start], instruction: work, access: edit, workspace: new}",
               end='{branch: "{{ workspace.impl.branch }}"}') == 0
    assert git("show", f"{output()['branch']}:good.txt") == "ok"
    assert git("ls-tree", "--name-only", output()["branch"]).splitlines() == ["README", "good.txt"]
    assert len(fake.specs) == 2

    first = only_run().name
    use(monkeypatch, talk(), talk(), talk())
    assert run("impl: {needs: [start], instruction: work, access: edit, workspace: new, retry: 0}") == 1
    (failed,) = [r.name for r in (Path(".wf") / "runs").iterdir() if r.name != first]
    assert (tmp_path / "home" / "worktrees" / failed / "impl" / ".git").exists()


def test_base_picks_the_commit_a_worktree_starts_from(repo, monkeypatch):
    first = git("rev-parse", "HEAD")
    Path("later.txt").write_text("x\n")
    git("add", "later.txt")
    git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "second")
    use(monkeypatch, submit({"summary": "x"}))
    write("""
        impl: {needs: [start], instruction: work, access: edit, workspace: new, base: "{{ start.ref }}"}
        look: {needs: [impl], workspace: impl, command: "ls"}
    """, inputs="{ref: {type: string}}", end='{files: "{{ look.stdout }}", base: "{{ workspace.impl.base }}"}')
    assert cli.main(["run", "flow.yaml", f"ref={first}"]) == 0
    assert output() == {"files": "README\n", "base": first}


def test_new_needs_a_repo_with_a_commit(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("WF_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(runner, "interactive", lambda: False)
    use(monkeypatch, submit({"summary": "x"}))
    assert run("impl: {needs: [start], instruction: work, access: edit, workspace: new, retry: 0}") == 1
    assert "`workspace: new` needs a git repo with at least one commit" in capsys.readouterr().err


def test_a_resumed_retry_starts_from_the_commit_the_job_first_started_from(repo, monkeypatch, tmp_path):
    def clean(cwd: Path):
        assert not (cwd / "junk.txt").exists(), "the resumed retry should start from a clean worktree"

    use(monkeypatch, edits({"junk.txt": "half done\n"}), talk(), talk())  # attempt 1 never submits
    assert run("impl: {needs: [start], instruction: work, access: edit, workspace: new, retry: 0}",
               end='{branch: "{{ workspace.impl.branch }}"}') == 1
    use(monkeypatch, edits({"good.txt": "ok\n"}, {"summary": "done"}, check=clean))
    assert cli.main(["resume", only_run().name]) == 0
    assert git("ls-tree", "--name-only", output()["branch"]).splitlines() == ["README", "good.txt"]


def test_a_retry_also_removes_a_git_repo_the_failed_attempt_made(repo, monkeypatch):
    from workflow.harnesses import TurnEnd

    async def clone(session):  # like an agent cloning a dependency into its worktree
        nested = Path(session.spec.cwd) / "vendor"
        subprocess.run(["git", "init", "-q", str(nested)], check=True)
        (nested / "x.txt").write_text("x\n")
        return TurnEnd("done")

    def clean(cwd: Path):
        assert not (cwd / "vendor").exists(), "the retry should start from the job's start commit"

    use(monkeypatch, clone, talk(), talk(), edits({"good.txt": "ok\n"}, {"summary": "done"}, check=clean))
    assert run("impl: {needs: [start], instruction: work, access: edit, workspace: new}") == 0
