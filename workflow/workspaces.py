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

The runner commits, not the agent: Codex's sandbox keeps .git read-only. After
an `edit` or `full` agent finishes in a worktree the runner made, anything
left uncommitted becomes one commit. Its message is the node's `commit_message`
(nodes.py renders it), or else default_message(), which names the changed
files: "Update docs/tools.md and 2 more files". The default message puts no node
id or run id into the project's history. A `commit_message` holds the text that
its template writes. Before a retry, the worktree goes back to the
commit the job started from, so a retry is the same instruction on the same
commit.
The runner never commits, or resets, your own checkout. Commits skip hooks, so
the message is only that text. They are signed when your git config says so
(commit.gpgsign), with your key. A run that may commit first checks that the
key signs with nobody there (check_signing), so a key that can't fails the run
before any agent works for it.

git never waits for input: nobody is at a terminal to answer. Each git command
runs in its own session, without a terminal, so a key that asks for a passphrase
or a PIN fails at once. wf stops git when signing takes longer than git.signing_timeout
(config/), as with a key that waits for a touch. A commit that fails says signing failed
only when a test shows signing is the cause. git is also stopped when its
caller is cancelled (stopped_by), on Ctrl-C, and on SIGTERM or a lost terminal
(stopping_git_on_exit). Stopping git stops whatever it started: it's asked
first, so it removes its temporary and lock files, and killed GRACE seconds
later if it's still there. Nothing can stop git on SIGKILL, which ends wf at
once.

When a run succeeds its worktrees go and their branches stay, and `wf continue` brings them back
(restore). A failed run keeps its worktrees for `wf retry`.

An agent with `access: review` works in a copy of its workspace (review_copy), which nodes.py makes
for each attempt in ~/.wf/temp/<run-id>/review-copies/ and removes when the job ends (discard_copy).
state() records the reviewed worktree when the job's first attempt starts, with an index and objects of its own
in ~/.wf/temp/<run-id>/review-objects/, and changed_since() says before and after each attempt whether
the worktree, its index or its branch changed. Only you can open those two folders (review_folders). The copy
isn't a clone or a linked worktree: a clone makes the worktree its `origin` remote, and a linked
worktree shares the refs and the config of the repository.

These are plain blocking functions (git is a subprocess). nodes.py runs them
in a thread, one at a time per run, because parallel `git worktree add`
calls in one repo fight over git's lock files.
"""

import contextlib
import contextvars
import hashlib
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from . import config
from .config import home

GRACE = 5  # seconds a stopped git gets to remove its temporary and lock files before wf kills it
NO_PUSH = "wf-review-copy-cannot-push::"  # a transport that git has no program for, where a review copy pushes
PRE_PUSH = "#!/bin/sh\necho 'wf-review-copy-cannot-push: this is a review copy, and its pushes fail' >&2\nexit 1\n"
LISTED = 10  # changed files that the review check names
AS_IS = "* -text -filter -ident -working-tree-encoding\n"  # gitattributes for git to read and write each file's bytes


class WorkspaceError(Exception):
    pass


class _TimedOut(WorkspaceError):
    pass


class NotReviewable(WorkspaceError):
    """The workspace of an `access: review` node isn't in a git worktree with a commit. A retry finds it the same."""


class _Stopped(WorkspaceError):
    """git was stopped, or never started, because its caller was cancelled or wf is exiting."""


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
    if not (path / ".git").exists():  # first pass (later passes and `wf retry` reuse it)
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


def commit_all(path: str | Path, message: str | None = None) -> str | None:
    """Commit everything left uncommitted in a runner-made worktree, with `message`, or else with
    default_message(). Returns the new commit, if any."""
    # A name that isn't UTF-8 gets a replacement character, not a failed commit. Your config can set core.quotePath off.
    if not _git(path, "status", "--porcelain", errors="replace"):
        return None
    _git(path, "add", "--all")
    changed = _git(path, "-c", "core.quotePath=false", "diff", "--cached", "--name-only", "--no-renames", strip=False,
                   errors="replace")
    changed = [name for name in changed.split("\n") if name]
    if not changed:
        return None  # e.g. only a submodule's own files changed: nothing to commit here
    # No hooks, not even prepare-commit-msg, which --no-verify still runs: one could add a trailer.
    # The message goes on stdin, because an argument has a size limit.
    commit = [*_identity(path), "-c", "core.hooksPath=/dev/null", "commit", "--quiet", "--file=-"]
    text = message or default_message(changed)
    if not _signs(path):
        _git(path, *commit, input=text)
        return head(path)
    try:
        _git(path, *commit, input=text, timeout=config.get().git.signing_timeout)
    except _Stopped:
        raise
    except WorkspaceError as error:
        if not isinstance(error, _TimedOut) and not _signing_fails(path):
            raise  # the commit failed for another reason
        key = _signing_key(path) or "your default key"
        raise WorkspaceError(f"can't sign the commit with {key} (commit.gpgsign is on): {error}") from error
    return head(path)


def default_message(changed: list[str]) -> str:
    """The message of a commit for a node without `commit_message`: it names the changed files, in git's order,
    and no part of wf. `Update docs/tools.md`, or `Update docs/tools.md and 2 more files`."""
    more = len(changed) - 1
    return f"Update {changed[0]}" + (f" and {more} more file{'s' if more > 1 else ''}" if more else "")


def check_signing(repo: str | Path) -> None:
    """When your git config signs commits (commit.gpgsign), sign a test commit the way the runner
    will: with nobody at a terminal, within git.signing_timeout. Raises WorkspaceError, naming the fix,
    if it can't. Outside a git repository nothing is committed, so nothing is checked."""
    if not _git(repo, "rev-parse", "--git-dir", check=False) or not _signs(repo):
        return
    key, ssh = _signing_key(repo), _git(repo, "config", "gpg.format", check=False) == "ssh"
    if ssh and _relative_path(key):
        raise WorkspaceError(
            f"user.signingkey is a relative path ({key}), which git looks for in the checkout it commits in. "
            f"The runner commits in worktrees under {root()}, so set the key file's absolute path: "
            f"git -C {repo} config user.signingkey <absolute key file>")
    try:
        problem = _signing_problem(repo)
    except WorkspaceError as error:
        raise WorkspaceError(f"can't test signing in {repo}: {error}") from error
    if problem:
        raise WorkspaceError(
            f"git can't sign commits in {repo} with nobody at a terminal: {problem}\n"
            f"commit.gpgsign is on, and the signing key ({key or 'your default key'}) needs a touch, a PIN or a "
            f"passphrase, or doesn't work. Set a key that needs none as this repository's signing key: "
            f"git -C {repo} config user.signingkey {'<key file>' if ssh else '<key id>'}")


def _signs(path: str | Path) -> bool:
    return _git(path, "config", "--bool", "commit.gpgsign", check=False) == "true"


def _signing_key(path: str | Path) -> str:
    return _git(path, "config", "user.signingkey", check=False)


def _relative_path(key: str) -> bool:
    """Is this ssh signing key a relative file path? Not a literal key (key::..., or the older
    ssh-...), and not a path git expands (~/..., %(prefix)/...)."""
    return bool(key) and not key.startswith(("key::", "ssh-", "~", "%(prefix)/")) and not Path(key).is_absolute()


def _identity(path: str | Path) -> list[str]:
    """wf as the author, when your git config names nobody."""
    named = _git(path, "config", "user.email", check=False) and _git(path, "config", "user.name", check=False)
    return [] if named else ["-c", "user.name=wf", "-c", "user.email=wf@localhost"]


def _signing_problem(path: str | Path) -> str | None:
    """Why git can't sign a commit in `path`, or None if it can. Raises WorkspaceError when it can't
    tell, because git can't make even an unsigned test commit (a full disk, say). The test commits
    go to a temporary object folder, so the repository doesn't keep them."""
    try:
        folder = tempfile.TemporaryDirectory()
    except OSError as error:
        raise WorkspaceError(f"can't make a folder for a test commit: {error}") from error
    with folder as objects:
        env, identity = {"GIT_OBJECT_DIRECTORY": objects}, _identity(path)
        tree = _git(path, "mktree", env=env)  # the empty tree

        def test_commit(*sign: str, timeout: float = 600) -> str:
            return _git(path, *identity, "commit-tree", *sign, "-m", "wf: signing check", tree, env=env, timeout=timeout)

        test_commit()  # unsigned: everything but signing works
        try:
            test_commit("-S", timeout=config.get().git.signing_timeout)
        except _Stopped:
            raise
        except WorkspaceError as error:
            return str(error)
    return None


def _signing_fails(path: str | Path) -> bool:
    """Does a test show that signing fails in `path`? False when the test can't tell."""
    try:
        return _signing_problem(path) is not None
    except _Stopped:
        raise
    except WorkspaceError:
        return False


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


def review_folders(run_id: str, parts: list[str]) -> tuple[Path, Path]:
    """Where an `access: review` job's copy goes, and the index and objects of its check: review-copies/<parts> and
    review-objects/<parts> in temp_root(). Both hold the untracked files of the reviewed worktree, which can be
    secrets, so only you can open review-copies and review-objects."""
    found = []
    for kind in ("review-copies", "review-objects"):
        folder = temp_root(run_id) / kind
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        folder.chmod(0o700)  # also when it was there before
        found.append(folder.joinpath(*parts))
    return found[0], found[1]


def mailbox(run_dir: Path) -> Path:
    """Where `wf steer` passes messages to the process that drives a run (steer.py):
    ~/.wf/steer/<run-id>-<hash of the run folder's path>, so two repos' runs with one id keep
    apart. It stays as long as the run folder, and `wf discard` removes it."""
    return home() / "steer" / _run_key(run_dir)


def sessions_root(run_dir: Path) -> Path:
    """Where harnesses keep the agent sessions of a run that a later pass may continue:
    ~/.wf/sessions/<run-id>-<hash of the run folder's path>, with a folder for each harness, and
    one for each session in that (saved_sessions). Unlike temp_root, it outlives the run's success,
    so that `wf continue` continues them: success keeps only those that a follow-up can continue
    (keep_sessions). `wf discard` removes it."""
    return home() / "sessions" / _run_key(run_dir)


def saved_sessions(run_dir: Path, harness: str) -> Path:
    """The folder where one harness keeps a run's sessions, as <session id>/ folders. It may not
    exist yet: make_private() makes it."""
    return sessions_root(run_dir) / harness


def make_private(folder: Path) -> Path:
    """Make a folder of ~/.wf/sessions, and the folders between, that only you can open. Returns it."""
    top = home() / "sessions"
    if not folder.is_relative_to(top):
        raise ValueError(f"{folder} isn't in {top}")
    for one in [*reversed(folder.relative_to(top).parents), folder.relative_to(top)]:
        (top / one).mkdir(mode=0o700, parents=True, exist_ok=True)
        (top / one).chmod(0o700)  # also when it was there before
    return folder


def keep_sessions(run_dir: Path, ids: set[str]) -> None:
    """Remove the run's saved sessions but those with these ids, and its folder when none stays."""
    root = sessions_root(run_dir)
    for harness in _folders(root):
        for saved in _folders(harness):
            if saved.name not in ids:
                shutil.rmtree(saved, ignore_errors=True)
        with contextlib.suppress(OSError):
            harness.rmdir()  # when no session of it stays
    with contextlib.suppress(OSError):
        root.rmdir()


def _folders(folder: Path) -> list[Path]:
    return [one for one in folder.iterdir() if one.is_dir() and not one.is_symlink()] if folder.is_dir() else []


def _run_key(run_dir: Path) -> str:
    """<run-id>-<hash of the run folder's path>: the run's own name in a folder that every run shares."""
    folder = Path(run_dir).resolve()
    return f"{folder.name}-{hashlib.sha256(os.fsencode(folder)).hexdigest()[:12]}"


def uncommitted(repo: str | Path) -> list[Path]:
    """Files with changes that aren't committed (untracked ones too), as absolute paths: a new
    worktree won't have them."""
    top = _git(repo, "rev-parse", "--show-toplevel", check=False)
    out = _git(repo, "-c", "core.quotePath=false", "status", "--porcelain", "-z", "--no-renames",
               "--untracked-files=all", check=False, strip=False)
    # -z: entries end in NUL, paths unquoted. Each is "XY path", relative to the repo's top folder
    return [Path(top) / entry[3:] for entry in out.split("\0") if len(entry) > 3] if top else []


def review_copy(path: str | Path, copy: Path, tree: str, check: Path) -> Path:
    """Make `copy` a copy of the git worktree that holds `path`, for an agent with `access: review`, and
    return the folder in it that matches `path`. A copy that is there already goes first. `tree` is the
    worktree's files as state() found them, and `check` the folder that state() kept its objects in.

    The copy is a repository of its own that borrows the worktree's objects (objects/info/alternates).
    It has the worktree's branches, remote-tracking branches and tags, its remotes, its filters, its sparse
    checkout and the config of its branch, and the same branch or commit checked out. The copy of a shallow clone
    has its list of shallow commits, so that its history ends at the same commits. git then writes the bytes of
    each file of `tree`, so the copy has each change that isn't committed, and each untracked file, but not the files
    that git ignores, such as a virtual environment. Only the objects are shared: git writes each file again.
    Git LFS uses a folder in the copy. Most pushes fail there (NO_PUSH, PRE_PUSH). What the agent commits,
    fetches or configures stays in the copy."""
    top = _top(path)
    discard_copy(copy)
    copy.mkdir(parents=True)
    _git(copy, "init", "--quiet", f"--object-format={_git(top, 'rev-parse', '--show-object-format')}")
    alternates = [_git(top, "rev-parse", "--path-format=absolute", "--git-path", "objects"), str(check / "objects")]
    (copy / ".git" / "objects" / "info" / "alternates").write_text("\n".join(alternates) + "\n", encoding="utf-8")
    shallow = Path(_git(top, "rev-parse", "--path-format=absolute", "--git-path", "shallow"))
    if shallow.is_file():  # before the fetch: without the list, git looks for the parents of the shallow commits
        shutil.copyfile(shallow, copy / ".git" / "shallow")
    _git(copy, "fetch", "--quiet", "--no-tags", "--no-recurse-submodules", "--update-head-ok", "--no-write-fetch-head",
         str(top), "+refs/heads/*:refs/heads/*", "+refs/remotes/*:refs/remotes/*", "+refs/tags/*:refs/tags/*")
    branch = _git(top, "symbolic-ref", "--quiet", "HEAD", check=False)
    if branch:
        _git(copy, "symbolic-ref", "HEAD", branch)
    else:
        _git(copy, "update-ref", "--no-deref", "HEAD", head(top))
    _git(copy, "--attr-source", _as_is(copy), "read-tree", "--reset", "-u", tree)  # before the config: all files
    for remote in _copy_config(top, copy, branch):
        _git(copy, "config", f"remote.{remote}.pushurl", NO_PUSH)
    _git(copy, "config", f"url.{NO_PUSH}.pushInsteadOf", "")  # a push to a URL goes there too, unless a longer rule wins
    _git(copy, "config", "lfs.storage", str(copy / ".git" / "lfs"))  # also when your own git config sets one
    sparse = Path(_git(top, "rev-parse", "--path-format=absolute", "--git-path", "info/sparse-checkout"))
    if sparse.is_file():
        (copy / ".git" / "info").mkdir(exist_ok=True)
        shutil.copyfile(sparse, copy / ".git" / "info" / "sparse-checkout")
    hooks = copy / ".git" / "wf-hooks"
    hooks.mkdir()
    (hooks / "pre-push").write_text(PRE_PUSH, encoding="utf-8")
    (hooks / "pre-push").chmod(0o755)
    _git(copy, "config", "core.hooksPath", str(hooks))
    _git(copy, "reset", "--quiet")  # the index of the commit, with the sparse checkout: the changes show as not staged
    folder = copy / Path(path).resolve().relative_to(top)
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def discard_copy(copy: Path) -> None:
    """Remove a review copy, with all that the agent left in it. A folder that a tool made read-only
    is made writable first. Raises WorkspaceError when the copy is still there."""
    if copy.is_symlink() or copy.is_file():
        with contextlib.suppress(OSError):
            copy.unlink()
    elif copy.exists():
        try:
            shutil.rmtree(copy)
        except OSError:
            with contextlib.suppress(OSError):
                os.chmod(copy, 0o700)
            for folder, folders, _ in os.walk(copy):  # top down: each folder opens before os.walk goes in
                for name in folders:
                    if not os.path.islink(os.path.join(folder, name)):
                        with contextlib.suppress(OSError):
                            os.chmod(os.path.join(folder, name), 0o700)
            shutil.rmtree(copy, ignore_errors=True)
    if os.path.lexists(copy):
        raise WorkspaceError(f"can't remove the review copy {copy}")


def state(path: str | Path, check: Path, fresh: bool = False) -> dict:
    """What the review check compares in a git worktree: its branch (None when HEAD is detached), its
    commit, a tree of its files, and a hash of its index. `path` is in the worktree. The tree has the bytes of
    each file, committed or not, with no filter or line end rule of the repository, but not the files that git
    ignores or can't read. The rules of $GIT_DIR/info/attributes still apply, except filters. Needs git 2.41.

    git builds the tree in an index of its own, in `check`, which keeps the file data of each check, so that git
    reads only the files that changed. `fresh` starts that index again from the commit, with the file data
    that still holds. The objects that git writes go to `check`, not to the worktree's repository. Raises
    WorkspaceError when git fails, but not for a file that git can't read."""
    top = _top(path)
    objects = check / "objects"
    objects.mkdir(parents=True, exist_ok=True)
    index = check / "index"
    env = {"GIT_INDEX_FILE": str(index), **_apart(top, objects)}
    raw = ["--attr-source", _as_is(top, env), "-c", "core.sparseCheckout=false", *_no_filters(top)]
    if fresh or not index.exists():
        _git(top, "read-tree", *(["-m"] if index.exists() else []), "HEAD", env=env)  # with HEAD's ignored files
    _git(top, *raw, "add", "--all", "--ignore-errors", env=env, codes=(0, 1))  # 1: some files git can't read
    # write-tree reads again each file that changed in the same second as the index, so it gets `raw` too
    staged = _git(top, "ls-files", "--stage", "-z", strip=False)
    return {"branch": _git(top, "symbolic-ref", "--quiet", "HEAD", check=False) or None, "commit": head(top),
            "tree": _git(top, *raw, "write-tree", env=env), "staged": hashlib.sha256(staged.encode()).hexdigest()}


def _apart(path: str | Path, objects: Path) -> dict:
    """The environment for a git command that writes its objects to `objects` and reads those of the repository too."""
    stored = _git(path, "rev-parse", "--path-format=absolute", "--git-path", "objects")
    return {"GIT_OBJECT_DIRECTORY": str(objects), "GIT_ALTERNATE_OBJECT_DIRECTORIES": stored}


def _as_is(cwd: str | Path, env: dict | None = None) -> str:
    """A tree with only a .gitattributes file of AS_IS, for `git --attr-source`: then git hashes and writes each file
    as its bytes are. It replaces each .gitattributes file of the worktree. An older git than 2.41 fails at once."""
    blob = _git(cwd, "hash-object", "-w", "--stdin", env=env, input=AS_IS)
    return _git(cwd, "mktree", env=env, input=f"100644 blob {blob}\t.gitattributes\n")


def _no_filters(top: Path) -> list[str]:
    """Options for git that switch off each filter that a git config names, also for a path that
    $GIT_DIR/info/attributes gives one. That file wins over `--attr-source`."""
    found = _git(top, "config", "--includes", "--null", "--name-only", "--get-regexp", r"^filter\.", check=False, strip=False)
    names = {key[len("filter."):].rpartition(".")[0] for key in filter(None, found.split("\0"))}
    return [option for name in sorted(names) for variable in ("clean", "smudge", "process")
            for option in ("-c", f"filter.{name}.{variable}=")] + \
           [option for name in sorted(names) for option in ("-c", f"filter.{name}.required=false")]


def changed_since(path: str | Path, before: dict, check: Path) -> list[str]:
    """How the worktree that holds `path` changed since state() gave `before`, in words. [] when it didn't."""
    after, said = state(path, check), []
    if after["branch"] != before["branch"]:
        said.append(f"it has {_checked_out(after)} checked out now, not {_checked_out(before)}")
    elif after["commit"] != before["commit"]:
        moved = _checked_out(before) if before["branch"] else "its detached HEAD"
        said.append(f"{moved} moved from {before['commit'][:12]} to {after['commit'][:12]}")
    if after["tree"] != before["tree"]:
        names = _git(path, "diff-tree", "-r", "-z", "--name-only", "--no-renames", before["tree"], after["tree"],
                     check=False, strip=False, env=_apart(path, check / "objects")).split("\0")
        names = [name for name in names if name]
        more = f" and {len(names) - LISTED} more" if len(names) > LISTED else ""
        said.append(f"files changed: {', '.join(names[:LISTED])}{more}" if names else "its files changed")
    if after["staged"] != before.get("staged", after["staged"]):  # a record from before wf kept `staged` has none
        said.append("what is staged in its index changed")
    return said


def _checked_out(found: dict) -> str:
    branch = found["branch"]
    return f"branch `{branch.removeprefix('refs/heads/')}`" if branch else f"commit {found['commit'][:12]}"


def _top(path: str | Path) -> Path:
    """The top folder of the git worktree that holds `path`. Raises NotReviewable when there's none,
    or when it has no commit yet: `access: review` needs one."""
    top = _git(path, "rev-parse", "--show-toplevel", check=False)
    if not top or not _git(top, "rev-parse", "--verify", "--quiet", "HEAD", check=False):
        raise NotReviewable(f"`access: review` works in a copy of a git worktree with a commit, and {path} isn't in one")
    return Path(top).resolve()


def _copy_config(top: Path, copy: Path, branch: str) -> list[str]:
    """Copy the worktree's remotes, without their push URLs, its filters (such as Git LFS), its line end
    settings, its sparse checkout and the config of its branch into the copy. They come from the config of the
    repository and of the worktree ($GIT_DIR/config.worktree), also from a file that one of them includes.
    Then `git fetch` and `gh` work there as in the worktree, and git reads each file as it does there.
    Returns the remotes' names."""
    scopes = ["--local"]
    if _git(top, "config", "--type=bool", "--get", "extensions.worktreeConfig", check=False) == "true":
        scopes.append("--worktree")
    remotes = []
    for scope in scopes:
        found = _git(top, "config", scope, "--includes", "--null", "--get-regexp",
                     r"^(remote|branch|filter|lfs)\.|^core\.(autocrlf|eol|sparsecheckout|sparsecheckoutcone)$|^index\.sparse$",
                     check=False, strip=False)
        for entry in filter(None, found.split("\0")):
            key, _, value = entry.partition("\n") if "\n" in entry else (entry, "", "true")  # a key alone is true
            section, _, rest = key.partition(".")
            name, _, variable = rest.rpartition(".")
            if (section == "branch" and name != branch.removeprefix("refs/heads/")) or variable == "pushurl" \
                    or key == "lfs.storage":  # the copy keeps its own
                continue
            if section == "remote" and name and name not in remotes:
                remotes.append(name)
            _git(copy, "config", "--add", key, value)
    return remotes


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


_running: set[subprocess.Popen] = set()  # every git command running now, in any thread
_registry = threading.Lock()  # held while a git command starts and joins _running
_starting: set[int] = set()  # the threads in _start
_exiting = threading.Event()  # set by the exit handler: no new git command starts
_deferred: int | None = None  # an exit signal that came while the main thread was starting git
_stop: contextvars.ContextVar[threading.Event | None] = contextvars.ContextVar("stop", default=None)


@contextlib.contextmanager
def stopped_by(event: threading.Event):
    """Inside this block, the git command running when `event` is set stops, and no new one starts:
    for a caller in another thread, which can't interrupt this one."""
    token = _stop.set(event)
    try:
        yield
    finally:
        _stop.reset(token)


@contextlib.contextmanager
def stopping_git_on_exit():
    """Inside this block, a lost terminal (SIGHUP) or SIGTERM stops the git commands still running,
    then exits wf as before: git has a session of its own, which those signals don't reach. A signal
    that is ignored (nohup) stays ignored. Main thread only, as for every signal handler."""
    changed = [signum for signum in (signal.SIGHUP, signal.SIGTERM) if signal.getsignal(signum) == signal.SIG_DFL]
    for signum in changed:
        signal.signal(signum, _on_exit_signal)
    try:
        yield
    finally:
        for signum in changed:
            signal.signal(signum, signal.SIG_DFL)


def _on_exit_signal(signum, frame) -> None:
    global _deferred
    _exiting.set()
    if threading.get_ident() in _starting:  # this thread was starting git: _start exits once git is in _running
        _deferred = signum
    else:
        _exit(signum)


def _exit(signum: int) -> None:
    """Stop every git command, then exit by `signum`, as wf would without a handler."""
    if _registry.acquire(timeout=GRACE):  # a git command another thread is starting joins _running first
        _registry.release()
    for process in list(_running):
        _end(process)
    signal.signal(signum, signal.SIG_DFL)
    os.kill(os.getpid(), signum)


def _git(cwd: str | Path, *args: str, check: bool = True, strip: bool = True, timeout: float = 600,
         env: dict[str, str] | None = None, input: str | None = None, codes: tuple[int, ...] = (0,),
         errors: str | None = None) -> str:
    """git's output. `codes`: the exit codes that aren't a failure. `errors`: how to decode bytes that aren't
    UTF-8, as for open(). A failure raises WorkspaceError, or with check=False returns ""."""
    stop = _stop.get()
    process = _start(cwd, args, env, stop, input is not None, errors)
    try:
        with process:
            stdout, stderr = _output(process, args, timeout, stop, input)
    finally:
        _running.discard(process)
    if process.returncode not in codes:
        if check:
            raise WorkspaceError(f"git {_what(args)} failed: {stderr.strip() or stdout.strip()}")
        return ""
    return stdout.strip() if strip else stdout  # strip=False: column 1 of porcelain matters


def _start(cwd: str | Path, args: tuple[str, ...], env: dict[str, str] | None,
           stop: threading.Event | None, piped: bool = False, errors: str | None = None) -> subprocess.Popen:
    """git, started and in _running, unless its caller was cancelled or wf is exiting. A session of
    its own: no terminal for a signing program to ask on, and one process group, so stopping git
    stops the signing program too."""
    me = threading.get_ident()
    _starting.add(me)
    try:
        with _registry:
            if _exiting.is_set() or (stop is not None and stop.is_set()):
                why = "wf is exiting" if _exiting.is_set() else "its caller was cancelled"
                raise _Stopped(f"git {_what(args)} wasn't run: {why}")
            try:
                process = subprocess.Popen(["git", *args], cwd=cwd, stdin=subprocess.PIPE if piped else subprocess.DEVNULL,
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors=errors,
                                           start_new_session=True, env=git_env(GIT_TERMINAL_PROMPT="0", **(env or {})))
            except OSError as error:
                raise WorkspaceError(f"can't run git: {error}") from error
            _running.add(process)
            return process
    finally:
        _starting.discard(me)
        if _deferred is not None and threading.current_thread() is threading.main_thread():
            _exit(_deferred)


def _output(process: subprocess.Popen, args: tuple[str, ...], timeout: float,
            stop: threading.Event | None, input: str | None = None) -> tuple[str, str]:
    """git's stdout and stderr once it exits, after it got `input`. It's stopped if it runs longer than `timeout`
    seconds, when `stop` is set, and when this thread is interrupted (Ctrl-C)."""
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:  # wake up every 0.1 seconds to look at `stop`
                return process.communicate(input, timeout=min(0.1, max(0.0, deadline - time.monotonic())))
            except subprocess.TimeoutExpired:
                input = None  # communicate() keeps sending what it got first
                if stop is not None and stop.is_set():
                    raise _Stopped(f"git {_what(args)} was stopped: its caller was cancelled") from None
                if time.monotonic() >= deadline:
                    raise _TimedOut(f"git {_what(args)} took longer than {timeout} seconds") from None
    except BaseException:
        _end(process)
        raise


def _end(process: subprocess.Popen) -> None:
    """Stop git and everything it started (its process group): SIGTERM first, so git removes its
    temporary and lock files, then SIGKILL for whatever is still there after GRACE seconds."""
    _signal(process, signal.SIGTERM)
    deadline = time.monotonic() + GRACE
    while _group_alive(process) and time.monotonic() < deadline:
        time.sleep(0.05)
    if _group_alive(process):
        _signal(process, signal.SIGKILL)
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(GRACE)


def _group_alive(process: subprocess.Popen) -> bool:
    process.poll()  # reap git once it exits: only running processes should keep its group
    try:
        os.killpg(process.pid, 0)
    except (ProcessLookupError, PermissionError):  # macOS refuses (EPERM) when only exited processes are left
        return False
    return True


def _signal(process: subprocess.Popen, signum: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):  # the group is gone, or only exited processes are left
        os.killpg(process.pid, signum)


def _what(args: tuple[str, ...]) -> str:
    """The git command for an error message: `commit`, `worktree add`. No `-c name=value` pairs, `--attr-source`
    trees or options."""
    args = list(args)
    while len(args) >= 2 and args[0] in ("-c", "--attr-source"):
        args = args[2:]
    return " ".join(args[:1] + [arg for arg in args[1:2] if not arg.startswith("-")])


def restore(repo: str | Path, path: Path, branch: str, commit: str, remake: bool = False) -> None:
    """Bring back a worktree that a run removed when it succeeded, on its branch, for a follow-up (`wf continue`).
    With `remake`, a branch that is gone starts again at `commit`, where the run left it. Raises WorkspaceError:
    the branch is gone, git can't check it out (another worktree has it), or the folder can't be made."""
    if (path / ".git").exists():
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise WorkspaceError(f"can't make {path.parent}: {error.strerror or error}") from error
    _git(repo, "worktree", "prune", check=False)  # its folder is gone but may still be registered
    if branch_exists(repo, branch):
        _git(repo, "worktree", "add", "--quiet", str(path), branch)
    elif remake:
        _git(repo, "worktree", "add", "--quiet", "-b", branch, str(path), commit)
    else:
        raise WorkspaceError(f"its branch `{branch}` is gone")
