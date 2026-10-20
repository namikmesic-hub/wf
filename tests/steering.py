"""`wf steer` while a fake agent waits mid-turn, for the steering tests of each harness.

The fake agents (fake_codex.py, fake_acp.py, fake_claude.py) have a `pause` action: the turn
writes <folder>/<name>.paused, and goes on once <folder>/<name>.go is there. at_pause() starts
a thread that waits for the pause, runs `wf steer` as its own process (as a supervisor does),
and then lets the turn go on. The test runs `wf run` meanwhile, and then reads the result.
"""

import json
import subprocess
import sys
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

from workflow import operate, workspaces

STEER = "Use the v2 API instead"


def at_pause(folder: Path, name: str, node: str = "ask", *more: str, message: str | None = STEER,
             stdin: str | None = None) -> Future:
    """`wf steer <run-id> <node> <message> <more>...` (no message: None), once the pause is reached.
    The future's result is the finished process (returncode, stdout, stderr)."""
    repo = Path.cwd()

    def steer() -> subprocess.CompletedProcess:
        try:
            deadline = time.monotonic() + 30
            while not (folder / f"{name}.paused").exists():
                if time.monotonic() > deadline:
                    raise TimeoutError(f"the agent never reached pause {name}")
                time.sleep(0.05)
            (run,) = (repo / ".wf" / "runs").iterdir()
            return subprocess.run([sys.executable, "-P", "-c", operate._DRIVER, operate._PACKAGE_ROOT, "steer", run.name,
                                   node, *([message] if message is not None else []), *more], cwd=repo,
                                  input=stdin, capture_output=True, text=True, timeout=60)
        finally:
            (folder / f"{name}.go").touch()

    executor = ThreadPoolExecutor(1)
    try:
        return executor.submit(steer)
    finally:
        executor.shutdown(wait=False)


def run_folder() -> Path:
    (folder,) = (Path(".wf") / "runs").iterdir()
    return folder


def steer_events() -> list[dict]:
    """The run's `steer` events, without their times."""
    lines = [json.loads(line) for line in (run_folder() / "events.jsonl").read_text().splitlines()]
    return [{k: v for k, v in e.items() if k != "time"} for e in lines if e["event"] == "steer"]


def steer_lines(node: str = "ask", attempt: int = 1) -> list[dict]:
    """The `steer` lines of an attempt's log."""
    log = run_folder() / "nodes" / node / "1" / f"attempt-{attempt}.log"
    return [line for line in map(json.loads, log.read_text().splitlines()) if line["kind"] == "steer"]


def left_in_mailbox() -> list[str]:
    """Files left in the run's mailbox (steer.mailbox): none, once every message has its answer."""
    folder = workspaces.mailbox(run_folder())
    return sorted(p.name for p in folder.iterdir()) if folder.is_dir() else []
