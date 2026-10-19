"""Runs: the run folder on disk.

Every `wf run` gets a folder, and `wf resume` finds it again by id:

    .wf/runs/<run-id>/              <run-id> is like 20261018-1432-implement-k3f9
      workflow/                     copies of the workflow files as they were run
      input.json
      run.json                      the workflow's name and `repo` workspace, for starting it later
      branches.json                 the branches `branch` named, by node path, once there are any
      output.json                   the end output, once the run succeeds
      status.json                   running | waiting | succeeded | failed | limit, with a sequence
                                    number that goes up on every change (while running, also the
                                    attempts waiting for an agent slot: capacity.py)
      lock                          held by the one process driving the run (its pid inside)
      events.jsonl                  one line per progress event
      checkpoints.sqlite            LangGraph checkpoints, used by wf resume
      nodes/<node>/<pass>/[<index>/]
        instruction.md              the rendered instruction (or command, or message)
        output.json
        attempt-<n>.log             harness transcript or command output

`wf resume` loads the copies in workflow/, so editing the YAML doesn't change a
run already in progress. `.wf/` goes into the repo's .git/info/exclude, so run
folders never show up in `git status`.

A runs folder (--runs-dir, or WF_RUNS_DIR) keeps run folders outside the repo
instead: <runs-dir>/<repo key>/<run-id>/, where the key (folder name and a
hash of its path) keeps two repos' runs apart. Then nothing is written in the
repo, not even .git/info/exclude. run.json's `repo` workspace is the repo's
path, so a run folder found by its path alone (Run.at) still knows its repo.

The checkpoints are LangGraph's; this module only says where they live.

Only one process may drive a run at a time: two would both write its
checkpoints. Run.driving() holds an exclusive lock on the run's `lock` file
(fcntl.flock) for as long as a `wf run` or `wf resume` process works on it; the
operating system releases it if that process dies, so a crash never leaves a
stale lock behind.

A process that starts a driver in the background (operate.detach) hands it the
lock instead of letting go: the child inherits the locked file descriptor
(its number in WF_LOCK_FD) and adopts it in driving(). So the run is locked
from the moment it is started or answered until its driver exits, and two
operators can't both start one. A flock belongs to the open file, shared by
every process holding a descriptor to it, so the lock is never unlocked
explicitly, only closed: unlocking in the parent would release it for the child.
Every status change happens under the lock.
"""

import fcntl
import hashlib
import json
import os
import re
import secrets
import shutil
import string
import subprocess
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from . import workspaces

ROOT = Path(".wf") / "runs"


@dataclass(frozen=True)
class Run:
    id: str
    dir: Path
    repo: Path  # the folder `wf run` was started in

    @classmethod
    def at(cls, folder: Path) -> "Run":
        """The run in a run folder: <repo>/.wf/runs/<run-id>, or one in a runs folder."""
        folder = Path(folder).resolve()
        run = cls(folder.name, folder, folder.parent.parent.parent)  # <repo>/.wf/runs/<run-id>
        recorded = (run.info().get("home") or {}).get("path")
        return replace(run, repo=Path(recorded)) if recorded else run

    @property
    def checkpoints(self) -> Path:
        return self.dir / "checkpoints.sqlite"

    def info(self) -> dict:
        """run.json: the workflow's name and the `repo` workspace it started with."""
        try:
            return json.loads((self.dir / "run.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):  # missing or damaged: the run is still listable
            return {}

    def limits(self) -> dict:
        """The limits given with --limit for this run (limits.py), by name; None removes one."""
        return self.info().get("limits") or {}

    def set_limits(self, overrides: dict) -> None:
        """Keep new --limit values for the rest of the run (call it holding the run's lock)."""
        info = self.info()
        info["limits"] = {**(info.get("limits") or {}), **overrides}
        write_json(self.dir / "run.json", info)

    def branches(self) -> dict[str, str]:
        """branches.json: the branches nodes' `branch` named, by node path (build/2/implement)."""
        try:
            return json.loads((self.dir / "branches.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):  # none yet (or damaged: then the run's branches count as not its own)
            return {}

    def add_branch(self, path: str, branch: str) -> None:
        """Record a branch this run made for the node at `path` (call it holding the git lock)."""
        write_json(self.dir / "branches.json", {**self.branches(), path: branch})

    @property
    def workflow_file(self) -> Path:
        """The copy of the top-level workflow file."""
        manifest = json.loads((self.dir / "workflow" / "manifest.json").read_text(encoding="utf-8"))
        return self.dir / "workflow" / manifest["main"]

    def pass_dir(self, node: str, pass_no: int, index: int | None = None) -> Path:
        folder = self.dir / "nodes" / node / str(pass_no)
        if index is not None:
            folder /= str(index)
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def status(self) -> dict:
        try:
            return json.loads((self.dir / "status.json").read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):  # ValueError: not JSON (it's written atomically, so only if damaged)
            return {"status": "unknown"}

    def set_status(self, status: str, **details: Any) -> None:
        """Record the run's state. `seq` goes up by one on every call, so a watcher can tell a
        new state from one it has already seen, even when the status word is the same."""
        seq = self.status().get("seq", 0) + 1
        write_json(self.dir / "status.json", {"status": status, "seq": seq, "run": self.id, "updated": now(), **details})

    @contextmanager
    def driving(self) -> Iterator["Lock"]:
        """Hold the run's lock while this process drives it (or hands it to a background
        driver). Raises RunBusy if another process holds it."""
        fd = self._inherited()
        if fd is None:
            fd = os.open(self.dir / "lock", os.O_RDWR | os.O_CREAT, 0o600)
        deadline = time.monotonic() + BUSY_WAIT_SECONDS
        while True:  # driven() takes the lock for a moment to test it: wait such a test out
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # on an inherited lock: already ours, a no-op
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    holder = os.pread(fd, 32, 0).decode(errors="replace").strip() or "?"
                    os.close(fd)
                    raise RunBusy(f"run {self.id} is already running (process {holder})") from None
                time.sleep(0.05)
        lock = Lock(fd)
        try:
            lock.held_by(os.getpid())
            yield lock
        finally:
            os.close(fd)  # never LOCK_UN: a background driver may share this lock (see above)

    def _inherited(self) -> int | None:
        """The lock's descriptor, if the process that started this one handed it over."""
        number = os.environ.pop(LOCK_FD, None)  # once: commands this run starts don't see it
        try:
            fd = int(number) if number is not None else None
        except ValueError:
            return None
        if fd is None:
            return None
        try:
            if os.path.samestat(os.fstat(fd), os.stat(self.dir / "lock")):
                os.set_inheritable(fd, False)
                return fd
        except OSError:
            pass
        try:  # not this run's lock: don't hold on to another run's
            os.close(fd)
        except OSError:
            pass
        return None

    def driven(self) -> bool:
        """Is some process driving this run right now? (This takes the lock for a moment
        if nobody holds it, which is why driving() waits a little before giving up.)"""
        try:
            fd = os.open(self.dir / "lock", os.O_RDONLY)
        except FileNotFoundError:
            return False
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        finally:
            os.close(fd)  # closing also releases a lock we took
        return False

    def add_event(self, event: dict) -> None:
        with (self.dir / "events.jsonl").open("a", encoding="utf-8") as file:
            file.write(json.dumps({"time": now(), **event}, ensure_ascii=False, default=str) + "\n")

    def set_output(self, output: dict) -> None:
        write_json(self.dir / "output.json", output)


LOCK_FD = "WF_LOCK_FD"
BUSY_WAIT_SECONDS = 1.0


class Lock:
    """A run's lock, held by this process: its descriptor, to hand to a background driver."""

    def __init__(self, fd: int):
        self.fd = fd

    def held_by(self, pid: int) -> None:
        """Write who drives the run into the lock file (RunBusy reports it)."""
        os.ftruncate(self.fd, 0)
        os.pwrite(self.fd, f"{pid}\n".encode(), 0)  # at 0: a parent and child share the file offset


class RunBusy(Exception):
    """Another process is driving this run."""


RUNS_DIR = "WF_RUNS_DIR"


def runs_folder(repo: Path, runs_dir: Path | None = None) -> Path:
    """Where this repo's run folders are: <repo>/.wf/runs, or with a runs folder (`runs_dir`,
    else WF_RUNS_DIR) <runs folder>/<repo key>, outside the repo."""
    repo = repo.resolve()
    chosen = runs_dir or os.environ.get(RUNS_DIR)
    return Path(chosen).expanduser().resolve() / repo_key(repo) if chosen else repo / ROOT


def repo_key(repo: Path) -> str:
    """wf-3f9a1c2b4d5e: the repo folder's name and a hash of its path, so two repos with the
    same name keep their runs apart in one runs folder."""
    name = re.sub(r"[^A-Za-z0-9._-]", "-", repo.name).lstrip(".-") or "repo"
    return f"{name}-{hashlib.sha256(os.fsencode(repo)).hexdigest()[:12]}"


def create(repo: Path, name: str, files: list[Path], main: Path, inputs: dict, home: dict | None = None,
           dry_run: bool = False, limits: dict | None = None, runs_dir: Path | None = None) -> Run:
    """A new run folder, with copies of the workflow files and the input."""
    repo = repo.resolve()
    run_id = new_id(name)
    parent = runs_folder(repo, runs_dir)
    folder = parent / run_id
    folder.mkdir(parents=True)
    copies = folder / "workflow"
    base = _common_folder([f.resolve() for f in files])
    for file in files:
        target = copies / file.resolve().relative_to(base)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(file, target)
    write_json(copies / "manifest.json", {"main": str(main.resolve().relative_to(base))})
    write_json(folder / "input.json", inputs)
    write_json(folder / "run.json", {"workflow": name, "home": home, "dry_run": dry_run, **({"limits": limits} if limits else {})})
    if parent == repo / ROOT:  # a runs folder outside the repo needs no exclude
        exclude_from_git(repo)
    run = Run(run_id, folder, repo)
    run.set_status("running")
    return run


def find(repo: Path, run_id: str, runs_dir: Path | None = None) -> Run:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", run_id) or ".." in run_id:
        raise FileNotFoundError(f"`{run_id}` isn't a run id")  # it becomes part of paths and branch names
    parent = runs_folder(repo, runs_dir)
    folder = parent / run_id
    if not (folder / "workflow" / "manifest.json").is_file():
        raise FileNotFoundError(f"no run `{run_id}` in {parent}")
    return Run(run_id, folder, repo.resolve())


def all_runs(repo: Path, runs_dir: Path | None = None) -> list[Run]:
    """Every run in this repo, oldest first (by when its folder was made)."""
    folder = runs_folder(repo, runs_dir)
    if not folder.is_dir():
        return []
    made = [p for p in folder.iterdir() if not p.name.startswith(".") and (p / "workflow" / "manifest.json").is_file()]
    made.sort(key=lambda p: ((p / "workflow" / "manifest.json").stat().st_mtime, p.name))  # ids only go to the minute
    return [Run(p.name, p, repo.resolve()) for p in made]


def discard(run: Run, branches: bool) -> list[str]:
    """Delete a run: its worktrees, temp folders, folder, and (with `branches`) its branches.
    Holds the run's lock while it does, so a run being driven can't be discarded (RunBusy).
    Returns what it kept, to say so."""
    repo = (run.info().get("home") or {}).get("path") or str(run.repo)
    with run.driving():
        workspaces.remove_run(repo, run.id)
        shutil.rmtree(workspaces.temp_root(run.id), ignore_errors=True)
        kept = []
        named = list(run.branches().values())
        if branches:
            workspaces.delete_branches(repo, run.id, named)
        else:
            kept = workspaces.branches(repo, run.id, named)
        gone = run.dir.with_name(f".{run.dir.name}.discarded")  # first, so no new process can find the run
        shutil.rmtree(gone, ignore_errors=True)  # left by a discard that died halfway
        run.dir.rename(gone)  # (the lock file goes with it; this process still holds it)
        shutil.rmtree(gone)
    return kept


def new_id(name: str) -> str:
    """20261018-1432-implement-k3f9: when, which workflow, and 4 random characters."""
    suffix = "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(4))
    return f"{datetime.now():%Y%m%d-%H%M}-{name}-{suffix}"


# ---------- git facts a run records ----------


def git(repo: Path, *args: str) -> str | None:
    """Output of a git command in `repo`, or None if it fails (not a repo, no commits, ...)."""
    try:
        result = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True,
                                stdin=subprocess.DEVNULL, env=workspaces.git_env(GIT_TERMINAL_PROMPT="0"))
    except OSError:  # git isn't installed
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def base_commit(repo: Path) -> str | None:
    """The commit the repo is at: run.base, and the default base for new worktrees."""
    return git(repo, "rev-parse", "HEAD")


def current_branch(repo: Path) -> str | None:
    branch = git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    return None if branch in (None, "HEAD") else branch  # "HEAD" means detached


def exclude_from_git(repo: Path) -> None:
    """Add .wf/ to the repo's .git/info/exclude, once."""
    git_dir = git(repo, "rev-parse", "--git-common-dir")
    if git_dir is None:
        return
    exclude = (repo / git_dir).resolve() / "info" / "exclude"
    try:  # only tidiness: never fail a run because this can't be written
        exclude.parent.mkdir(parents=True, exist_ok=True)
        lines = exclude.read_text(encoding="utf-8").splitlines() if exclude.exists() else []
        if ".wf/" not in lines:
            with exclude.open("a", encoding="utf-8") as file:
                file.write(("\n" if lines and lines[-1] else "") + ".wf/\n")
    except (OSError, UnicodeDecodeError):
        pass


# ---------- helpers ----------


def _common_folder(files: list[Path]) -> Path:
    parents = [f.parent for f in files]
    common = parents[0]
    while not all(p == common or common in p.parents for p in parents):
        common = common.parent
    return common


def write_json(path: Path, value: Any) -> None:
    """Write a file atomically: a reader never sees half of it, and every writer has its own temp file."""
    with tempfile.NamedTemporaryFile("w", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp",
                                     delete=False, encoding="utf-8") as temp:
        try:
            temp.write(json.dumps(value, indent=2, ensure_ascii=False, default=str) + "\n")
        except BaseException:
            Path(temp.name).unlink(missing_ok=True)  # leave no half file behind
            raise
    Path(temp.name).replace(path)


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")
