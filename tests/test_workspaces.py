"""`workspace: new` end to end: worktrees, the runner's commits, resets before retries, cleanup,
and the branch names `branch` gives them.

Worktrees go under WF_HOME, which each test points at its own folder.
"""

import _thread
import asyncio
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import tempfile
from pathlib import Path
from types import SimpleNamespace
from textwrap import dedent, indent

import pytest

from fakes import Fake, edits, submit, talk
from workflow import cli, harnesses, nodes, runner, workspaces
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


def test_an_attempt_after_wf_retry_starts_from_the_commit_the_job_first_started_from(repo, monkeypatch, tmp_path):
    def clean(cwd: Path):
        assert not (cwd / "junk.txt").exists(), "the attempt after wf retry should start from a clean worktree"

    use(monkeypatch, edits({"junk.txt": "half done\n"}), talk(), talk())  # attempt 1 never submits
    assert run("impl: {needs: [start], instruction: work, access: edit, workspace: new, retry: 0}",
               end='{branch: "{{ workspace.impl.branch }}"}') == 1
    use(monkeypatch, edits({"good.txt": "ok\n"}, {"summary": "done"}, check=clean))
    assert cli.main(["retry", only_run().name]) == 0
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


def new_key(folder: Path) -> Path:
    """A new ssh key that needs no passphrase. Returns its public half."""
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(folder / "key")], check=True)
    return folder / "key.pub"


def sign_with_program(folder: Path, script: str) -> None:
    """Sign with a shell script instead of ssh-keygen. Each time it runs, it adds its pid to folder/pids first."""
    program = folder / "signer"
    program.write_text(f"#!/bin/sh\necho $$ >> {folder / 'pids'}\n{script}\n")
    program.chmod(0o755)
    sign_with(folder / "key.pub")
    git("config", "gpg.ssh.program", str(program))


def signers(folder: Path) -> list[int]:
    """The pids of the signing program, each time it ran so far."""
    return [int(pid) for pid in (folder / "pids").read_text().split()] if (folder / "pids").exists() else []


def started(folder: Path, times: int = 1) -> list[int]:
    """The signing program's pids, once it has run `times` times."""
    for _ in range(200):
        if len(pids := signers(folder)) >= times:
            return pids
        time.sleep(0.05)
    pytest.fail(f"the signing program ran {len(signers(folder))} time(s), not {times}")


def gone(pid: int) -> bool:
    for _ in range(50):  # a moment to be reaped
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.1)
    return False


needs_ssh_keygen = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="ssh-keygen isn't installed")


@needs_ssh_keygen
def test_the_runner_signs_its_commits_when_your_git_config_does(repo, monkeypatch, tmp_path):
    sign_with(new_key(tmp_path))
    (tmp_path / "signers").write_text("wf@localhost " + (tmp_path / "key.pub").read_text())
    git("config", "gpg.ssh.allowedSignersFile", str(tmp_path / "signers"))
    use(monkeypatch, edits({"new.txt": "made\n"}, {"summary": "done"}))
    assert run(IMPL, end='{branch: "{{ workspace.impl.branch }}"}') == 0
    assert git("log", "-1", "--format=%G?", output()["branch"]) == "G"  # a good signature


@needs_ssh_keygen
def test_checking_the_key_leaves_nothing_in_the_repository(repo, tmp_path):
    sign_with(new_key(tmp_path))
    before = git("count-objects", "-v")
    workspaces.check_signing(repo)
    assert git("count-objects", "-v") == before
    assert not workspaces._running  # every git it ran has left the list of running ones


def test_a_run_whose_key_cant_sign_fails_at_its_start_and_says_how_to_fix_it(repo, monkeypatch, tmp_path, capsys):
    sign_with(tmp_path / "missing.pub")
    fake = use(monkeypatch, edits({"new.txt": "made\n"}, {"summary": "done"}))
    assert run(IMPL) == 1
    err = capsys.readouterr().err
    assert f"wf run: git can't sign commits in {repo.resolve()} with nobody at a terminal: " in err
    assert f"the signing key ({tmp_path / 'missing.pub'}) needs a touch, a PIN or a passphrase" in err
    assert f"git -C {repo.resolve()} config user.signingkey <key file>" in err
    assert fake.sent == []  # no agent worked for it


@pytest.mark.parametrize("nodes", [
    "look: {needs: [start], instruction: look, workspace: new}",  # read-only, in a worktree
    "look: {needs: [start], instruction: look, workspace: new}\nfix: {needs: [look], instruction: fix, access: edit}",
    "build: {needs: [start], command: 'true', workspace: new}\nfix: {needs: [build], instruction: fix, access: edit}",
])  # the `edit` agents work in your checkout, where the runner never commits
def test_a_run_the_runner_makes_no_commits_in_doesnt_check_the_key(repo, monkeypatch, tmp_path, nodes):
    sign_with(tmp_path / "missing.pub")
    use(monkeypatch, submit({"summary": "done"}), submit({"summary": "done"}))
    assert run(nodes) == 0


def test_an_agent_that_edits_another_nodes_worktree_needs_a_key_that_signs(repo, monkeypatch, tmp_path, capsys):
    sign_with(tmp_path / "missing.pub")
    fake = use(monkeypatch, edits({"new.txt": "made\n"}, {"summary": "done"}))
    assert run("build: {needs: [start], command: 'true', workspace: new}\n"
               "fix: {needs: [build], instruction: fix, access: edit, workspace: build}") == 1
    assert "wf run: git can't sign commits in" in capsys.readouterr().err
    assert fake.sent == []


SUB = """version: 1
name: sub
defaults: {harness: claude-code}
start: {input: {}}
nodes:
  impl: {needs: [start], instruction: work, access: edit}
end:
  output: {}
"""


@pytest.mark.parametrize("workspace, checked", [("new", True), ("repo", False)])
def test_an_edit_agent_in_a_sub_workflow_needs_a_key_that_signs_when_it_works_in_a_worktree(
        repo, monkeypatch, tmp_path, capsys, workspace, checked):
    Path("sub.yaml").write_text(SUB)  # its agent works in the parent node's workspace
    sign_with(tmp_path / "missing.pub")
    fake = use(monkeypatch, submit({"summary": "done"}))
    assert run(f"build: {{needs: [start], workflow: sub.yaml, workspace: {workspace}}}") == (1 if checked else 0)
    assert ("wf run: git can't sign commits in" in capsys.readouterr().err) == checked
    assert (fake.sent == []) == checked


def test_a_run_from_an_unborn_branch_with_a_base_of_its_own_checks_the_key(repo, monkeypatch, tmp_path, capsys):
    first = git("branch", "--show-current")
    git("checkout", "-q", "--orphan", "fresh")  # HEAD has no commit, but `base` names one
    sign_with(tmp_path / "missing.pub")
    fake = use(monkeypatch, edits({"new.txt": "made\n"}, {"summary": "done"}))
    assert run(f"impl: {{needs: [start], instruction: work, access: edit, workspace: new, base: {first}}}") == 1
    assert "wf run: git can't sign commits in" in capsys.readouterr().err
    assert fake.sent == []


def test_a_relative_signing_key_fails_the_run_at_its_start(repo, monkeypatch, capsys):
    sign_with(Path("keys/key.pub"))  # git would look for it in each worktree, not in this checkout
    fake = use(monkeypatch, edits({"new.txt": "made\n"}, {"summary": "done"}))
    assert run(IMPL) == 1
    err = capsys.readouterr().err
    assert "wf run: user.signingkey is a relative path (keys/key.pub)" in err
    assert f"git -C {repo.resolve()} config user.signingkey <absolute key file>" in err
    assert fake.sent == []


def test_a_retried_run_checks_the_key_again_before_any_agent_works(repo, monkeypatch, tmp_path, capsys):
    fake = use(monkeypatch, *[talk()] * 6)  # both attempts end without a result
    assert run(IMPL) == 1
    sign_with(tmp_path / "missing.pub")
    sent = len(fake.sent)
    assert cli.main(["retry", only_run().name]) == 1
    assert "wf retry: git can't sign commits in" in capsys.readouterr().err
    assert len(fake.sent) == sent


def test_retrying_a_run_that_succeeded_says_so_without_checking_the_key(repo, monkeypatch, tmp_path, capsys):
    use(monkeypatch, edits({"new.txt": "made\n"}, {"summary": "done"}))
    assert run(IMPL) == 0
    sign_with(tmp_path / "missing.pub")
    assert cli.main(["retry", only_run().name]) == 0
    assert f"Run {only_run().name} already succeeded." in capsys.readouterr().out


@needs_ssh_keygen
def test_a_detached_run_whose_background_check_fails_records_why(repo, tmp_path, capsys):
    new_key(tmp_path)
    sign_with_program(tmp_path, f'[ -e {tmp_path / "signed"} ] && exit 1\n'  # the background check fails
                                f'touch {tmp_path / "signed"}\nexec ssh-keygen "$@"')  # the one before it signs
    write(IMPL)
    Path("script.yaml").write_text("agents:\n  impl:\n    - {_files: {new.txt: made}, summary: done}\n")
    assert cli.main(["run", "flow.yaml", "--dry-run", "script.yaml", "--detach", "--json"]) == 0
    run_id = json.loads(capsys.readouterr().out)["run"]
    assert cli.main(["wait", run_id, "--timeout", "30"]) == 0
    state = json.loads(capsys.readouterr().out)
    assert state["status"] == "failed"  # not "stopped", as if its process had crashed
    assert state["error"].startswith("git can't sign commits in")


def test_the_fix_for_an_openpgp_key_names_a_key_id(repo, tmp_path):
    program = tmp_path / "gpg"
    program.write_text("#!/bin/sh\nexit 1\n")
    program.chmod(0o755)
    git("config", "user.signingkey", "ABCD1234")
    git("config", "gpg.program", str(program))
    git("config", "commit.gpgsign", "true")
    with pytest.raises(workspaces.WorkspaceError) as error:
        workspaces.check_signing(repo)
    assert str(error.value).endswith(f"git -C {repo} config user.signingkey <key id>")


def test_a_retried_run_without_its_run_json_checks_the_key_where_it_ran(repo, monkeypatch, tmp_path, capsys):
    fake = use(monkeypatch, *[talk()] * 6)
    assert run(IMPL) == 1
    (only_run() / "run.json").unlink()  # the run's checkpoints still say where it ran
    sign_with(tmp_path / "missing.pub")
    sent = len(fake.sent)
    assert cli.main(["retry", only_run().name]) == 1
    assert "wf retry: git can't sign commits in" in capsys.readouterr().err
    assert len(fake.sent) == sent


def test_a_retried_run_checks_the_key_for_an_edit_agent_in_a_sub_workflow(repo, monkeypatch, tmp_path, capsys):
    Path("sub.yaml").write_text(SUB)
    fake = use(monkeypatch, *[talk()] * 6)
    assert run("build: {needs: [start], workflow: sub.yaml, workspace: new}") == 1
    sign_with(tmp_path / "missing.pub")
    sent = len(fake.sent)
    assert cli.main(["retry", only_run().name]) == 1
    assert "wf retry: git can't sign commits in" in capsys.readouterr().err
    assert len(fake.sent) == sent


def test_a_failed_commit_keeps_the_attempt_number_so_wf_retry_starts_at_the_next_one(repo, monkeypatch):
    commit_all = workspaces.commit_all

    def full_disk(path, message):
        raise workspaces.WorkspaceError("git commit failed: No space left on device")

    monkeypatch.setattr(workspaces, "commit_all", full_disk)
    use(monkeypatch, talk(), talk(), talk(), edits({"new.txt": "made\n"}, {"summary": "done"}))
    assert run(IMPL) == 1  # attempt 1 fails, attempt 2 succeeds but can't commit
    monkeypatch.setattr(workspaces, "commit_all", commit_all)
    use(monkeypatch, edits({"new.txt": "made\n"}, {"summary": "done"}))
    assert cli.main(["retry", only_run().name]) == 0
    events = [json.loads(line) for line in (only_run() / "events.jsonl").read_text().splitlines()]
    assert [e["attempt"] for e in events if e["event"] == "attempt_started"] == [2, 3]


@needs_ssh_keygen
def test_a_commit_that_fails_for_another_reason_doesnt_blame_signing(repo, tmp_path):
    sign_with(new_key(tmp_path))
    Path("new.txt").write_text("made\n")
    Path(".git", "refs", "heads", git("branch", "--show-current") + ".lock").touch()  # a stale lock
    with pytest.raises(workspaces.WorkspaceError) as error:
        workspaces.commit_all(repo, "wf: impl (run)")
    assert str(error.value).startswith("git commit failed: fatal: cannot lock ref 'HEAD'")


def test_a_commit_that_cant_be_signed_says_so(repo, tmp_path):
    sign_with(tmp_path / "missing.pub")  # e.g. the key was removed after the run started
    Path("new.txt").write_text("made\n")
    with pytest.raises(workspaces.WorkspaceError) as error:
        workspaces.commit_all(repo, "wf: impl (run)")
    assert str(error.value).startswith(
        f"can't sign the commit with {tmp_path / 'missing.pub'} (commit.gpgsign is on): git commit failed: ")


def test_a_commit_whose_signing_test_cant_write_either_doesnt_blame_signing(repo, monkeypatch, tmp_path):
    sign_with(tmp_path / "missing.pub")
    Path("new.txt").write_text("made\n")
    git("add", "new.txt")
    (tmp_path / "tmp").mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "tmp"))  # where the signing test writes
    folders = [tmp_path / "tmp", *(p for p in [repo / ".git" / "objects", *(repo / ".git" / "objects").rglob("*")]
                                   if p.is_dir())]
    for folder in folders:  # like a full disk: no object can be written anywhere
        folder.chmod(0o555)
    try:
        with pytest.raises(workspaces.WorkspaceError) as error:
            workspaces.commit_all(repo, "wf: impl (run)")
    finally:
        for folder in folders:
            folder.chmod(0o755)
    assert str(error.value).startswith("git commit failed: ")
    assert "insufficient permission" in str(error.value)


def signing_timeout(seconds: float) -> None:
    """git.signing_timeout in the config.toml of this test's WF_HOME, which wf processes it starts read too."""
    workspaces.home().mkdir(parents=True, exist_ok=True)
    (workspaces.home() / "config.toml").write_text(f"[git]\nsigning_timeout = {seconds}\n")


def test_a_key_waiting_for_a_touch_fails_the_run_at_its_start_within_the_timeout(repo, monkeypatch, tmp_path, capsys):
    sign_with_program(tmp_path, "exec sleep 60")  # like ssh-keygen waiting for a security key to be touched
    signing_timeout(2)
    use(monkeypatch, edits({"new.txt": "made\n"}, {"summary": "done"}))
    start = time.monotonic()
    assert run(IMPL) == 1
    assert 2 <= time.monotonic() - start < 10
    assert "git commit-tree took longer than 2 seconds" in capsys.readouterr().err
    assert gone(started(tmp_path)[0])  # stopped with git, not left waiting


def test_a_signing_timeout_lets_git_remove_its_temporary_file(repo, monkeypatch, tmp_path):
    sign_with_program(tmp_path, "exec sleep 60")
    signing_timeout(1)
    (tmp_path / "tmp").mkdir()
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))  # where git keeps the data being signed
    Path("new.txt").write_text("made\n")
    with pytest.raises(workspaces.WorkspaceError, match="git commit took longer than 1 seconds"):
        workspaces.commit_all(repo, "wf: impl (run)")
    assert list((tmp_path / "tmp").iterdir()) == []  # git got SIGTERM first, not SIGKILL
    assert not workspaces._running


def test_a_signing_program_that_ignores_sigterm_is_killed(repo, monkeypatch, tmp_path):
    sign_with_program(tmp_path, "trap '' TERM\nwhile :; do sleep 1; done")
    signing_timeout(1)
    monkeypatch.setattr(workspaces, "GRACE", 1)
    Path("new.txt").write_text("made\n")
    start = time.monotonic()
    with pytest.raises(workspaces.WorkspaceError, match="git commit took longer than 1 seconds"):
        workspaces.commit_all(repo, "wf: impl (run)")
    assert time.monotonic() - start < 5
    assert gone(started(tmp_path)[0])


def a_job(repo: Path) -> SimpleNamespace:
    """What nodes._git needs from a job's context."""
    lock = asyncio.Lock()
    return SimpleNamespace(git_lock=lambda: lock)


def test_cancelling_a_job_stops_its_git_command_and_the_signing_program(repo, tmp_path):
    sign_with_program(tmp_path, "exec sleep 60")
    Path("new.txt").write_text("made\n")

    async def cancel():
        job = asyncio.create_task(nodes._git(a_job(repo), workspaces.commit_all, repo, "wf: impl (run)"))
        for _ in range(200):
            if signers(tmp_path) or job.done():
                break
            await asyncio.sleep(0.05)
        job.cancel()
        with pytest.raises(asyncio.CancelledError):
            await job

    start = time.monotonic()
    asyncio.run(cancel())  # it waits for the thread that ran git, as wf does when it stops
    assert time.monotonic() - start < 10  # well before git.signing_timeout.
    assert len(signers(tmp_path)) == 1  # Nothing ran after it, not even a test of the key.
    assert gone(started(tmp_path)[0])
    assert not workspaces._running


def test_a_job_cancelled_while_it_waits_for_git_starts_none(repo, tmp_path):
    sign_with_program(tmp_path, "exec sleep 60")
    Path("new.txt").write_text("made\n")

    async def cancel():
        nodes._GIT.acquire()  # another job's git operation runs
        try:
            job = asyncio.create_task(nodes._git(a_job(repo), workspaces.commit_all, repo, "wf: impl (run)"))
            await asyncio.sleep(0.2)
            job.cancel()
            with pytest.raises(asyncio.CancelledError):
                await job
        finally:
            nodes._GIT.release()

    asyncio.run(cancel())
    assert signers(tmp_path) == []
    assert git("status", "--porcelain") == "?? new.txt"  # not even `git add` ran


def test_a_stopped_commit_isnt_called_a_signing_failure(repo, tmp_path):
    sign_with_program(tmp_path, "exec sleep 60")
    Path("new.txt").write_text("made\n")
    stop, errors = threading.Event(), []

    def commit():
        with workspaces.stopped_by(stop):
            try:
                workspaces.commit_all(repo, "wf: impl (run)")
            except workspaces.WorkspaceError as error:
                errors.append(str(error))

    thread = threading.Thread(target=commit)
    thread.start()
    started(tmp_path)
    stop.set()
    thread.join(timeout=20)
    assert errors == ["git commit was stopped: its caller was cancelled"]
    assert len(signers(tmp_path)) == 1  # no test of the key after the stop


def test_ctrl_c_stops_git_and_the_signing_program(repo, tmp_path):
    sign_with_program(tmp_path, "exec sleep 60")
    threading.Thread(target=lambda: started(tmp_path) and _thread.interrupt_main(), daemon=True).start()
    start = time.monotonic()
    with pytest.raises(KeyboardInterrupt):
        workspaces.check_signing(repo)  # in the main thread, as before a run starts
    assert time.monotonic() - start < 10
    assert gone(started(tmp_path)[0])


WF = "import sys; from workflow.cli import main; sys.exit(main(sys.argv[1:]))"


def start_wf(code: str = WF) -> subprocess.Popen:
    """`wf run` in a process of its own, as a dry run, waiting in its signing check."""
    write(IMPL)
    Path("script.yaml").write_text("agents:\n  impl:\n    - {_files: {new.txt: made}, summary: done}\n")
    return subprocess.Popen([sys.executable, "-c", code, "run", "flow.yaml", "--dry-run", "script.yaml"],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@pytest.mark.parametrize("signum", [signal.SIGHUP, signal.SIGTERM])
def test_wf_stops_git_and_the_signing_program_when_its_terminal_closes_or_it_is_killed(repo, tmp_path, signum):
    sign_with_program(tmp_path, "exec sleep 60")
    wf = start_wf()
    (pid,) = started(tmp_path)  # in the signing check before the run, in wf's main thread
    wf.send_signal(signum)
    assert wf.wait(timeout=20) == -signum  # wf still exits as that signal makes it
    assert gone(pid)


@needs_ssh_keygen
@pytest.mark.parametrize("signum", [signal.SIGHUP, signal.SIGTERM])
def test_wf_stops_the_runners_commit_and_starts_no_more_git_when_it_is_killed(repo, tmp_path, signum):
    new_key(tmp_path)
    sign_with_program(tmp_path, f'[ -e {tmp_path / "signed"} ] && exec sleep 60\n'  # the commit waits for a touch
                                f'touch {tmp_path / "signed"}\nexec ssh-keygen "$@"')  # the check before the run signs
    wf = start_wf()
    started(tmp_path, times=2)  # the second: the runner's commit, in a worker thread
    wf.send_signal(signum)
    assert wf.wait(timeout=20) == -signum
    time.sleep(1)  # time for a git that wf left behind to start its signing program
    assert len(signers(tmp_path)) == 2  # the failed commit wasn't followed by a test of the key
    assert all(gone(pid) for pid in signers(tmp_path))


SIGNAL_WHILE_GIT_STARTS = """
import os, signal, subprocess, sys
from workflow import workspaces

popen = subprocess.Popen

def started_then_signalled(argv, **options):
    process = popen(argv, **options)
    if "-S" in argv:  # the signed test commit
        print(process.pid, flush=True)
        os.kill(os.getpid(), signal.SIGTERM)  # it lands before git is in the list of running ones
    return process

subprocess.Popen = started_then_signalled
with workspaces.stopping_git_on_exit():
    workspaces.check_signing(sys.argv[1])
"""


def test_a_signal_while_git_starts_still_stops_that_git(repo, tmp_path):
    sign_with_program(tmp_path, "exec sleep 60")
    done = subprocess.run([sys.executable, "-c", SIGNAL_WHILE_GIT_STARTS, str(repo)], capture_output=True, text=True,
                          timeout=30)
    assert done.returncode == -signal.SIGTERM
    assert gone(int(done.stdout))
    assert all(gone(pid) for pid in signers(tmp_path))


def test_a_group_with_only_exited_processes_left_has_nothing_to_stop():
    exited = subprocess.Popen(["sleep", "0"], start_new_session=True)
    os.waitid(os.P_PID, exited.pid, os.WEXITED | os.WNOWAIT)  # it has exited, and nobody reaped it yet
    unreaped = SimpleNamespace(pid=exited.pid, poll=lambda: None)  # as git's signing program, until launchd reaps it
    try:  # macOS refuses a signal to such a group (EPERM): neither call raises
        workspaces._signal(unreaped, signal.SIGKILL)
        workspaces._group_alive(unreaped)
    finally:
        exited.wait()


def test_a_hangup_that_wf_ignores_stays_ignored(repo, tmp_path):
    sign_with_program(tmp_path, "exec sleep 60")
    signing_timeout(2)
    wf = start_wf("import signal; signal.signal(signal.SIGHUP, signal.SIG_IGN)  # nohup\n" + WF)
    started(tmp_path)
    wf.send_signal(signal.SIGHUP)
    assert wf.wait(timeout=20) == 1  # it ran on, until the signing check timed out


SIGN_AT_A_TERMINAL = """
import fcntl, os, sys, termios, time
terminal = os.open(sys.argv[1], os.O_RDWR | os.O_NOCTTY)
fcntl.ioctl(terminal, termios.TIOCSCTTY, 0)  # this process gets a terminal, as wf does when a person starts it
from workflow import workspaces
start = time.monotonic()
try:
    workspaces.check_signing(sys.argv[2])
except workspaces.WorkspaceError as error:
    print(str(error).splitlines()[0])
print(time.monotonic() - start)
"""


def test_the_signing_program_has_no_terminal_to_ask_on(repo, tmp_path):
    sign_with_program(tmp_path, "exec sleep 60 </dev/tty")  # waits for an answer, if it has a terminal to ask on
    signing_timeout(10)
    leader, follower = os.openpty()
    try:
        done = subprocess.run([sys.executable, "-c", SIGN_AT_A_TERMINAL, os.ttyname(follower), str(repo)],
                              capture_output=True, text=True, start_new_session=True, timeout=30)
    finally:
        os.close(leader)
        os.close(follower)
    message, elapsed = done.stdout.splitlines()
    assert message.startswith("git can't sign commits in")
    assert float(elapsed) < 10  # it failed at once, not after git.signing_timeout


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


def test_a_retried_run_keeps_the_branch_it_made(repo, monkeypatch, tmp_path):
    use(monkeypatch, edits({"half.txt": "x\n"}), talk(), talk())  # never submits
    write(NAMED.replace("workspace: new", "workspace: new, retry: 0"), inputs=BRANCH_INPUT,
          end='{branch: "{{ workspace.impl.branch }}"}')
    assert cli.main(["run", "flow.yaml", "branch=fm/task"]) == 1
    shutil.rmtree(tmp_path / "home" / "worktrees")  # the branch outlives its worktree
    git("worktree", "prune")  # and git's record of it: only branches.json says the run made the branch
    use(monkeypatch, edits({"good.txt": "ok\n"}, {"summary": "done"}))
    assert cli.main(["retry", only_run().name]) == 0
    assert output() == {"branch": "fm/task"}
    assert git("ls-tree", "--name-only", "fm/task").splitlines() == ["README", "good.txt"]


def test_a_run_that_stopped_before_recording_its_branch_takes_it_up_again(repo, monkeypatch):
    use(monkeypatch, talk(), talk(), talk())  # never submits
    write(NAMED.replace("workspace: new", "workspace: new, retry: 0"), inputs=BRANCH_INPUT,
          end='{branch: "{{ workspace.impl.branch }}"}')
    assert cli.main(["run", "flow.yaml", "branch=fm/task"]) == 1
    (only_run() / "branches.json").unlink()  # as if the run had stopped between making the branch and recording it
    use(monkeypatch, edits({"good.txt": "ok\n"}, {"summary": "done"}))
    assert cli.main(["retry", only_run().name]) == 0
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
    use(monkeypatch, talk(), talk(), talk())  # never submits: the worktree stays for wf retry
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
