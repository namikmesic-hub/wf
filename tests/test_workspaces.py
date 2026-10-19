"""`workspace: new` end to end: worktrees, the runner's commits, resets before retries, cleanup,
and the branch names `branch` gives them.

Worktrees go under WF_HOME, which each test points at its own folder.
"""

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from textwrap import dedent, indent

import pytest

from fakes import Fake, edits, submit, talk
from workflow import cli, harnesses, runner, workspaces
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


IMPL = "impl: {needs: [start], instruction: work, access: edit, workspace: new}"


def test_the_runners_commit_message_has_no_trailer_even_when_a_hook_would_add_one(repo, monkeypatch):
    hook = repo / ".git" / "hooks" / "prepare-commit-msg"  # git commit --no-verify still runs this one
    hook.write_text('#!/bin/sh\necho "Co-authored-by: An Agent <agent@example.com>" >> "$1"\n')
    hook.chmod(0o755)
    use(monkeypatch, edits({"new.txt": "made\n"}, {"summary": "done"}))
    assert run(IMPL, end='{branch: "{{ workspace.impl.branch }}"}') == 0
    assert git("log", "-1", "--format=%B", output()["branch"]) == f"wf: impl ({only_run().name})"


def sign_with(key: Path) -> None:
    """A git config that signs every commit with an ssh key."""
    git("config", "gpg.format", "ssh")
    git("config", "user.signingkey", str(key))
    git("config", "commit.gpgsign", "true")


@pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="ssh-keygen isn't installed")
def test_the_runner_signs_its_commits_when_your_git_config_does(repo, monkeypatch, tmp_path):
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(tmp_path / "key")], check=True)
    sign_with(tmp_path / "key.pub")
    (tmp_path / "signers").write_text("wf@localhost " + (tmp_path / "key.pub").read_text())
    git("config", "gpg.ssh.allowedSignersFile", str(tmp_path / "signers"))
    use(monkeypatch, edits({"new.txt": "made\n"}, {"summary": "done"}))
    assert run(IMPL, end='{branch: "{{ workspace.impl.branch }}"}') == 0
    assert git("log", "-1", "--format=%G?", output()["branch"]) == "G"  # a good signature


def test_a_commit_that_cant_be_signed_fails_the_node_and_says_so(repo, monkeypatch, tmp_path, capsys):
    sign_with(tmp_path / "missing.pub")
    use(monkeypatch, edits({"new.txt": "made\n"}, {"summary": "done"}))
    assert run(IMPL) == 1
    assert f"impl: workspace: can't sign the commit with {tmp_path / 'missing.pub'} (commit.gpgsign is on)" in \
        capsys.readouterr().err


def test_a_key_waiting_for_a_touch_fails_the_node_quickly(repo, monkeypatch, tmp_path, capsys):
    signer = tmp_path / "signer"  # stands in for ssh-keygen waiting for a security key to be touched
    signer.write_text(f"#!/bin/sh\necho $$ > {tmp_path / 'pid'}\nexec sleep 60\n")
    signer.chmod(0o755)
    sign_with(tmp_path / "key.pub")
    git("config", "gpg.ssh.program", str(signer))
    monkeypatch.setattr(workspaces, "SIGNING_TIMEOUT", 2)
    use(monkeypatch, edits({"new.txt": "made\n"}, {"summary": "done"}))
    assert run(IMPL) == 1
    assert "can't sign the commit with" in capsys.readouterr().err
    pid = int((tmp_path / "pid").read_text())
    for _ in range(50):  # the signing program is stopped with git, not left waiting
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        pytest.fail("the signing program still runs")


# ---------- branch ----------

NAMED = 'impl: {needs: [start], instruction: work, access: edit, workspace: new, branch: "{{ start.branch }}"}'
BRANCH_INPUT = "{branch: {type: string}}"


def test_branch_names_the_worktrees_branch(repo, monkeypatch):
    use(monkeypatch, edits({"new.txt": "made\n"}, {"summary": "x"}))
    write(NAMED + "\nother: {needs: [start], command: 'true', workspace: new}", inputs=BRANCH_INPUT,
          end='{branch: "{{ workspace.impl.branch }}", other: "{{ workspace.other.branch }}"}')
    assert cli.main(["run", "flow.yaml", "branch= fm/task "]) == 0  # the name is stripped
    assert output() == {"branch": "fm/task", "other": f"wf/{only_run().name}/other"}  # without `branch`: as before
    assert git("show", "fm/task:new.txt") == "made"
    branches = json.loads((only_run() / "branches.json").read_text())
    assert branches == {"impl": {"branch": "fm/task", "left": git("rev-parse", "fm/task")}}  # where the run left it


def test_a_branch_that_already_exists_is_refused(repo, monkeypatch, capsys):
    git("branch", "fm/task")
    use(monkeypatch)  # the agent never starts
    write(NAMED, inputs=BRANCH_INPUT)
    assert cli.main(["run", "flow.yaml", "branch=fm/task"]) == 1
    assert "branch `fm/task` already exists and this run didn't make it" in capsys.readouterr().err
    assert git("rev-parse", "fm/task") == git("rev-parse", "HEAD")


@pytest.mark.parametrize("existing, name", [("fm", "fm/task"), ("fm/task/review", "fm/task")])
def test_a_branch_git_cannot_have_beside_an_existing_one_is_refused(repo, monkeypatch, capsys, existing, name):
    git("branch", existing)
    use(monkeypatch)
    write(NAMED, inputs=BRANCH_INPUT)
    assert cli.main(["run", "flow.yaml", f"branch={name}"]) == 1
    assert f"branch `{existing}` exists, and git can't have both `{existing}` and `{name}`" in capsys.readouterr().err


def test_where_git_ignores_case_a_name_in_other_case_is_the_same_branch(repo, monkeypatch, capsys):
    git("config", "core.ignorecase", "true")
    git("branch", "fm/task")
    use(monkeypatch)
    write(NAMED, inputs=BRANCH_INPUT)
    assert cli.main(["run", "flow.yaml", "branch=FM/task"]) == 1
    assert "branch `fm/task` exists, and git can't have both `fm/task` and `FM/task`" in capsys.readouterr().err
    assert run("""
        each: {needs: [start], for_each: '["a", "A"]', max_parallel: 1, command: "true", workspace: new,
               branch: "fix/{{ item }}"}
    """) == 1
    assert "`each/0` works on branch `fix/a` in this run, and git can't have both `fix/a` and `fix/A`" in capsys.readouterr().err


@pytest.mark.parametrize("name, problem", [
    ("bad name", "git doesn't allow it as a branch name"),
    ("wf/mine", "where wf names branches itself"),
    ("wf", "where wf names branches itself"),  # git can't have wf/<run-id>/... beside it
    ("WF/mine", "where wf names branches itself"),  # in wf/ where git ignores case
    ("", "the name is empty"),
    ("   ", "the name is empty"),  # names are stripped
])
def test_a_branch_name_git_or_wf_would_refuse_fails_the_node(repo, monkeypatch, capsys, name, problem):
    use(monkeypatch)
    write(NAMED, inputs=BRANCH_INPUT)
    assert cli.main(["run", "flow.yaml", f"branch={name}"]) == 1
    assert problem in capsys.readouterr().err
    assert git("branch", "--list") == "* " + git("branch", "--show-current")  # no wf/ branch in its place


def test_a_resumed_run_keeps_the_branch_it_made(repo, monkeypatch, tmp_path):
    use(monkeypatch, edits({"half.txt": "x\n"}), talk(), talk())  # never submits
    write(NAMED.replace("workspace: new", "workspace: new, retry: 0"), inputs=BRANCH_INPUT,
          end='{branch: "{{ workspace.impl.branch }}"}')
    assert cli.main(["run", "flow.yaml", "branch=fm/task"]) == 1
    shutil.rmtree(tmp_path / "home" / "worktrees")  # the branch outlives its worktree
    git("worktree", "prune")  # and git's record of it: only branches.json says the run made the branch
    use(monkeypatch, edits({"good.txt": "ok\n"}, {"summary": "done"}))
    assert cli.main(["resume", only_run().name]) == 0
    assert output() == {"branch": "fm/task"}
    assert git("ls-tree", "--name-only", "fm/task").splitlines() == ["README", "good.txt"]


def test_a_run_that_stopped_before_recording_its_branch_takes_it_up_again(repo, monkeypatch):
    use(monkeypatch, talk(), talk(), talk())  # never submits
    write(NAMED.replace("workspace: new", "workspace: new, retry: 0"), inputs=BRANCH_INPUT,
          end='{branch: "{{ workspace.impl.branch }}"}')
    assert cli.main(["run", "flow.yaml", "branch=fm/task"]) == 1
    (only_run() / "branches.json").unlink()  # as if the run had stopped between making the branch and recording it
    use(monkeypatch, edits({"good.txt": "ok\n"}, {"summary": "done"}))
    assert cli.main(["resume", only_run().name]) == 0
    assert output() == {"branch": "fm/task"}
    assert json.loads((only_run() / "branches.json").read_text())["impl"]["branch"] == "fm/task"


def test_a_loop_keeps_the_branch_its_first_pass_named(repo, monkeypatch):
    use(monkeypatch, edits({"a.txt": "1\n"}, {"summary": "one"}), edits({"b.txt": "2\n"}, {"summary": "two"}))
    assert run("""
        impl: {needs: [start], instruction: work, access: edit, workspace: new, branch: "try-{{ history.impl | length }}"}
        check:
          needs: [impl]
          workspace: impl
          command: "test -e b.txt"
          check: false
          routes: [{when: "check.exit_code != 0", to: impl, max: 3}, {to: end}]
    """, end='{branch: "{{ workspace.impl.branch }}"}') == 0
    assert output() == {"branch": "try-0"}
    assert git("log", "--format=%s", "HEAD..try-0").count("wf: impl") == 2
    assert git("branch", "--list", "try-1") == ""


def test_each_item_names_its_own_branch_and_two_items_cannot_share_one(repo, monkeypatch, capsys):
    use(monkeypatch, edits({"f.txt": "a\n"}, {"summary": "a"}), edits({"f.txt": "b\n"}, {"summary": "b"}))
    assert run("""
        each: {needs: [start], for_each: '["a", "b"]', max_parallel: 1, instruction: "{{ item }}", access: edit,
               workspace: new, branch: "fix/{{ item }}"}
    """) == 0
    assert [git("show", f"fix/{name}:f.txt") for name in "ab"] == ["a", "b"]
    use(monkeypatch, submit({"summary": "x"}))
    assert run("""
        each: {needs: [start], for_each: '["c", "c"]', max_parallel: 1, instruction: "{{ item }}", access: edit,
               workspace: new, branch: "fix/{{ item }}"}
    """) == 1
    assert "`each/0` already works on branch `fix/c` in this run" in capsys.readouterr().err
    assert run("""
        each: {needs: [start], for_each: '["d", "d/e"]', max_parallel: 1, command: "true", workspace: new,
               branch: "fix/{{ item }}"}
    """) == 1
    assert "`each/0` works on branch `fix/d` in this run, and git can't have both `fix/d` and `fix/d/e`" in capsys.readouterr().err


def test_an_item_keeps_its_branch_only_while_its_index_holds_the_same_item(repo, monkeypatch, capsys):
    loop = """
        each: {needs: [start], for_each: 'ITEMS', command: "true", workspace: new, branch: "fix/{{ item }}"}
        again: {needs: [each], command: "true", routes: [{to: each, max: 1}, {to: end}]}
    """
    assert run(loop.replace("ITEMS", '["a"]')) == 0  # the second pass works on fix/a again
    capsys.readouterr()
    assert run(loop.replace("ITEMS", '["b", "a"] if history.each else ["c"]')) == 1
    assert "`each/0` worked on branch `fix/c` in an earlier pass, and now names `fix/b`" in capsys.readouterr().err
    assert git("branch", "--list", "fix/b") == ""


def test_discard_deletes_a_named_branch_only_while_it_is_the_runs_work(repo, monkeypatch, capsys):
    use(monkeypatch, *[edits({"f.txt": f"{n}\n"}, {"summary": "x"}) for n in range(3)])
    write(NAMED, inputs=BRANCH_INPUT)
    folders = Path(".wf") / "runs"
    assert cli.main(["run", "flow.yaml", "branch=fm/task"]) == 0
    (first,) = [p.name for p in folders.iterdir()]
    git("branch", "-D", "fm/task")
    assert cli.main(["run", "flow.yaml", "branch=fm/task"]) == 0  # another run makes it again
    (second,) = {p.name for p in folders.iterdir()} - {first}
    made_again = git("rev-parse", "fm/task")
    capsys.readouterr()
    assert cli.main(["discard", first, "--branches"]) == 0
    assert "kept branch fm/task: wf can't tell it's still this run's work" in capsys.readouterr().out
    assert git("rev-parse", "fm/task") == made_again
    assert cli.main(["discard", second, "--branches"]) == 0
    assert git("branch", "--list", "fm/task") == ""
    assert cli.main(["run", "flow.yaml", "branch=fm/gone"]) == 0
    git("branch", "-D", "fm/gone")
    capsys.readouterr()
    assert cli.main(["discard", only_run().name]) == 0
    assert "fm/gone" not in capsys.readouterr().out  # a branch that's gone isn't kept


def test_discard_says_which_branches_git_would_not_delete(repo, monkeypatch, capsys):
    use(monkeypatch, submit({"summary": "x"}))
    write(NAMED, inputs=BRANCH_INPUT)
    assert cli.main(["run", "flow.yaml", "branch=fm/checked"]) == 0
    git("checkout", "-q", "fm/checked")  # git can't delete the branch you're on
    capsys.readouterr()
    assert cli.main(["discard", only_run().name, "--branches"]) == 0
    assert "kept branch fm/checked: git couldn't delete it (" in capsys.readouterr().out
    assert git("branch", "--show-current") == "fm/checked"


def test_discard_deletes_the_branch_a_run_made_without_recording_it(repo, monkeypatch):
    use(monkeypatch, talk(), talk(), talk())  # never submits: the worktree stays for wf resume
    write(NAMED.replace("workspace: new", "workspace: new, retry: 0"), inputs=BRANCH_INPUT)
    assert cli.main(["run", "flow.yaml", "branch=fm/lost"]) == 1
    (only_run() / "branches.json").unlink()
    assert cli.main(["discard", only_run().name, "--branches"]) == 0
    assert git("branch", "--list", "fm/lost") == ""  # its worktree had it checked out


def test_discard_keeps_or_deletes_named_branches_and_a_dry_run_deletes_them(repo, monkeypatch, capsys):
    use(monkeypatch, submit({"summary": "x"}), submit({"summary": "y"}))
    write(NAMED, inputs=BRANCH_INPUT)
    for name, discard in (("fm/kept", []), ("fm/gone", ["--branches"])):
        assert cli.main(["run", "flow.yaml", f"branch={name}"]) == 0
        (run_id,) = [p.name for p in (Path(".wf") / "runs").iterdir()]
        capsys.readouterr()
        assert cli.main(["discard", run_id, *discard]) == 0
        assert ("kept its branches fm/kept" in capsys.readouterr().out) == (not discard)
    assert git("branch", "--list", "fm/*") == "fm/kept"
    Path("script.yaml").write_text('agents: {impl: [{summary: "scripted"}]}\n')
    assert cli.main(["run", "flow.yaml", "branch=fm/dry", "--dry-run", "script.yaml"]) == 0
    assert git("branch", "--list", "fm/dry") == ""  # it held scripted work
