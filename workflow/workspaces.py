"""Workspaces: the git worktrees behind `workspace: new`, and the runner's commits.

Where a node runs (see "Workspaces" in the spec):
- repo: the workflow's repo. At the top level, the folder `wf run` started in.
- new: a git worktree on a new branch, made from `base` (by default the commit
  the repo was at when the run started).
- <node id>: the same folder as that node's latest pass.
- temp: an empty folder outside the repo (nodes.py makes those).

Worktrees live under ~/.wf/worktrees/<run-id>/ (set WF_HOME to move ~/.wf),
outside the repo, so an agent searching the repo doesn't find copies of it.
Their branches are wf/<run-id>/<node>, or wf/<run-id>/<node>/<item index>
for the items of a for_each node, unless the node's `branch` names one
(nodes.py checks that name, and records it in the run folder). A name must
not clash with a branch that exists: git can't have fix/a beside fix/a/b, or
FIX/a beside fix/a where it ignores case. A node keeps its worktree across
passes, so a loop keeps working on one branch.

The runner commits, not the agent: Codex's sandbox (and probably Cursor's)
keeps .git read-only. After an `edit` or `full` agent finishes in a worktree
the runner made, anything left uncommitted becomes the commit
"wf: <node> (<run-id>)". Before a retry, the worktree goes back to the commit
the job started from, so a retry is the same instruction on the same commit.
The runner never commits, or resets, your own checkout. Commits skip hooks, so
the message is only that line. They are signed when your git config says so
(commit.gpgsign), with your key.

git never waits for input: nobody is at a terminal to answer. It runs without
one, so a key that asks for a passphrase or a PIN fails at once, and a commit
that isn't signed within SIGNING_TIMEOUT (a key waiting for a touch) is
stopped. Either way the error says signing failed.

When a run succeeds its worktrees are removed and its branches kept; a failed
run keeps them for `wf resume`.

These are plain blocking functions (git is a subprocess). nodes.py runs them
in a thread, one at a time per run, because parallel `git worktree add`
calls in one repo fight over git's lock files.
"""

import contextlib
import os
import shutil
import signal
import subprocess
from pathlib import Path

SIGNING_TIMEOUT = 30  # seconds: signing takes a moment, unless the key waits for a touch


class WorkspaceError(Exception):
    pass


def home() -> Path:
    """~/.wf, or WF_HOME: wf's own folder, per user."""
    return Path(os.environ.get("WF_HOME") or Path.home() / ".wf")


def root() -> Path:
    return home() / "worktrees"


def made_by_runner(path: str | Path) -> bool:
    """Is this a worktree `workspace: new` created (so the runner may commit and reset in it)?"""
    try:
        Path(path).resolve().relative_to(root().resolve())
    except ValueError:
        return False
    return True


def worktree(repo: str | Path, run_id: str, parts: list[str], base: str, branch: str | None = None) -> dict:
    """The worktree for one node (or item): created on first use, reused on later passes.
    `parts` is the node path, e.g. ["implement"] or ["build", "2"]. Its branch is `branch`,
    or wf/<run-id>/<parts>; an existing one is used, so the caller decides if it may be."""
    path = root() / run_id / Path(*parts)
    branch = branch or "/".join(["wf", run_id, *parts])
    commit = _git(repo, "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}", check=False)
    if not commit:
        raise WorkspaceError(f"`{base}` isn't a commit in {repo}")
    if not (path / ".git").exists():  # first pass (later passes and resumed runs reuse it)
        path.parent.mkdir(parents=True, exist_ok=True)
        _git(repo, "worktree", "prune", check=False)  # its folder may be gone but still registered
        if branch_exists(repo, branch):
            _git(repo, "worktree", "add", "--quiet", str(path), branch)  # the branch outlived its folder
        else:
            _git(repo, "worktree", "add", "--quiet", "-b", branch, str(path), commit)
    return {"path": str(path), "branch": branch, "base": commit}


def branch_exists(repo: str | Path, branch: str) -> bool:
    return tip(repo, branch) is not None


def branch_problem(repo: str | Path, branch: str) -> str | None:
    """Why `branch` can't be a node's branch name, or None if it can."""
    if not branch:  # before git: check-ref-format's empty output would look like a match
        return "the name is empty"
    if clash(branch, "wf", fold=True):  # in any case: where git ignores case, WF/x is in wf/
        return "wf and wf/... are where wf names branches itself (wf/<run-id>/...)"
    if _git(repo, "check-ref-format", "--branch", branch, check=False) != branch:  # @{-1} would print another name
        return "git doesn't allow it as a branch name"
    return None


def clash(a: str, b: str, fold: bool) -> bool:
    """Can't git have both branches? The same name (in any case, with `fold`), or one under
    the other, like fix/a and fix/a/b: the ref fix/a can't also be a folder."""
    if fold:
        a, b = a.casefold(), b.casefold()
    return a == b or a.startswith(f"{b}/") or b.startswith(f"{a}/")


def ignores_case(repo: str | Path) -> bool:
    """Does git take FIX/a and fix/a for one branch here? It does with core.ignorecase (git sets
    it on macOS and Windows), because a branch can be a file named after it."""
    return _git(repo, "config", "--type=bool", "core.ignorecase", check=False) == "true"


def clashing_branch(repo: str | Path, branch: str) -> str | None:
    """A branch that exists and that git can't have beside `branch` (see clash), if any."""
    fold = ignores_case(repo)
    for ref in _git(repo, "for-each-ref", "--format=%(refname)", "refs/heads/", check=False).splitlines():
        if clash(name := ref.removeprefix("refs/heads/"), branch, fold):
            return name
    return None


def run_worktrees(repo: str | Path, run_id: str) -> dict[str, tuple[str | None, str]]:
    """A run's worktrees, by node path (build/2/implement): the branch each has checked out
    (None if none) and its commit."""
    folder, found = (root() / run_id).resolve(), {}
    for block in _git(repo, "worktree", "list", "--porcelain", check=False).split("\n\n"):
        entry = dict(line.partition(" ")[::2] for line in block.splitlines())
        path = Path(entry.get("worktree") or "/").resolve()
        if path.is_relative_to(folder) and path != folder:
            branch = entry.get("branch", "").removeprefix("refs/heads/") or None
            found[path.relative_to(folder).as_posix()] = (branch, entry.get("HEAD", ""))
    return found


def tip(repo: str | Path, branch: str) -> str | None:
    """The commit a branch is on, or None if there's no such branch."""
    return _git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False) or None


def head(path: str | Path) -> str:
    return _git(path, "rev-parse", "HEAD")


def reset(path: str | Path, commit: str) -> None:
    """Put a runner-made worktree back to `commit`: tracked changes undone, new files removed
    (ignored files such as a virtualenv stay)."""
    _git(path, "reset", "--quiet", "--hard", commit)
    _git(path, "clean", "--quiet", "-ffd")  # -ff: nested git repos (an agent's clone) go too


def commit_all(path: str | Path, message: str) -> str | None:
    """Commit everything left uncommitted in a runner-made worktree. Returns the new commit, if any."""
    if not _git(path, "status", "--porcelain"):
        return None
    _git(path, "add", "--all")
    if not _git(path, "diff", "--cached", "--name-only"):
        return None  # e.g. only a submodule's own files changed: nothing to commit here
    named = _git(path, "config", "user.email", check=False) and _git(path, "config", "user.name", check=False)
    identity = [] if named else ["-c", "user.name=wf", "-c", "user.email=wf@localhost"]
    # No hooks, not even prepare-commit-msg, which --no-verify still runs: one could add a trailer
    commit = [*identity, "-c", "core.hooksPath=/dev/null", "commit", "--quiet", "-m", message]
    if _git(path, "config", "--type=bool", "commit.gpgsign", check=False) != "true":
        _git(path, *commit)
        return head(path)
    try:
        _git(path, *commit, timeout=SIGNING_TIMEOUT)
    except WorkspaceError as error:
        key = _git(path, "config", "user.signingkey", check=False) or "your default key"
        raise WorkspaceError(f"can't sign the commit with {key} (commit.gpgsign is on): {error}") from error
    return head(path)


def remove_run(repo: str | Path, run_id: str) -> None:
    """Remove a run's worktrees (after it succeeds). Its branches stay."""
    folder = root() / run_id
    if not folder.exists():
        return
    for entry in _git(repo, "worktree", "list", "--porcelain", check=False).splitlines():
        if entry.startswith("worktree ") and made_by_runner(entry[9:]) and Path(entry[9:]).resolve().is_relative_to(folder.resolve()):
            _git(repo, "worktree", "remove", "--force", entry[9:], check=False)
    shutil.rmtree(folder, ignore_errors=True)
    _git(repo, "worktree", "prune", check=False)


def temp_root(run_id: str) -> Path:
    """Where `workspace: temp` folders go: ~/.wf/temp/<run-id>, outside the repo and private to
    you (a shared /tmp/wf could be another user's, or a symlink). Removed when the run succeeds."""
    return home() / "temp" / run_id


def uncommitted(repo: str | Path) -> list[Path]:
    """Files with changes that aren't committed (untracked ones too), as absolute paths: a new
    worktree won't have them."""
    top = _git(repo, "rev-parse", "--show-toplevel", check=False)
    out = _git(repo, "-c", "core.quotePath=false", "status", "--porcelain", "-z", "--no-renames",
               "--untracked-files=all", check=False, strip=False)
    # -z: entries end in NUL, paths unquoted. Each is "XY path", relative to the repo's top folder
    return [Path(top) / entry[3:] for entry in out.split("\0") if len(entry) > 3] if top else []


def branches(repo: str | Path, run_id: str, named: list[str] | None = None) -> list[str]:
    """A run's wf/<run-id>/... branches, and those of `named` (the ones `branch` named) that exist."""
    found = _git(repo, "for-each-ref", "--format=%(refname:short)", f"refs/heads/wf/{run_id}/", check=False).splitlines()
    return found + [name for name in named or [] if branch_exists(repo, name)]


def delete_branches(repo: str | Path, run_id: str, named: list[str] | None = None) -> dict[str, str]:
    """Delete a run's branches (after a dry run: they hold scripted work). Returns the ones
    git didn't delete, with its error: one checked out somewhere, say."""
    refused = {}
    for name in branches(repo, run_id, named):
        try:
            _git(repo, "branch", "--quiet", "-D", name)
        except WorkspaceError as error:
            refused[name] = str(error)
    return refused


# Variables that point git at another repository than the folder it runs in. A git hook
# sets some of them, so a wf started from one would commit and make worktrees elsewhere.
REPO_ENV = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
            "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR", "GIT_NAMESPACE")


def git_env(**extra: str) -> dict:
    """The environment for running git in a folder: this process's, without REPO_ENV."""
    return {**{k: v for k, v in os.environ.items() if k not in REPO_ENV}, **extra}


def _git(cwd: str | Path, *args: str, check: bool = True, strip: bool = True, timeout: int = 600) -> str:
    # A session of its own: no terminal for a signing program to ask on, and one process
    # group, so a timeout stops the signing program too, not only git
    try:
        process = subprocess.Popen(["git", *args], cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, start_new_session=True,
                                   env=git_env(GIT_TERMINAL_PROMPT="0"))
    except OSError as error:
        raise WorkspaceError(f"can't run git: {error}") from error
    with process:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as error:
            with contextlib.suppress(ProcessLookupError):  # it may have ended just now
                os.killpg(process.pid, signal.SIGKILL)
            raise WorkspaceError(f"git {_what(args)} took longer than {timeout} seconds") from error
    if process.returncode != 0:
        if check:
            raise WorkspaceError(f"git {_what(args)} failed: {stderr.strip() or stdout.strip()}")
        return ""
    return stdout.strip() if strip else stdout  # strip=False: column 1 of porcelain matters


def _what(args: tuple[str, ...]) -> str:
    """The git command for an error message, without leading `-c name=value` pairs."""
    args = list(args)
    while len(args) >= 2 and args[0] == "-c":
        args = args[2:]
    return " ".join(args[:2])
