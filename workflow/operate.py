"""Operate: running wf without a terminal, for an autonomous operator.

An operator (firstmate's first mate, a script, a person on another machine)
starts runs in the background, waits for the moment one needs attention,
answers its questions and retries it, all through commands with JSON output:

    wf run <file> --detach --json        start in the background, print the run id
    wf wait <run-id> [--consumer NAME]   block until the run needs attention, print its state
    wf answer <run-id> <answers.json>    answer the questions a run is waiting on, and continue
    wf retry <run-id> --detach           continue in the background
    wf status <run-id> --json            the state right now
    wf list --json                       every run in this repo

A run "needs attention" when it's waiting for a person, failed, hit a limit,
succeeded, or stopped: status.json says running but no process holds the run's
lock any more (the process crashed or was killed). `wf retry` picks it up.

`wait --consumer NAME` remembers which state it last reported to NAME (by the
status sequence number, in the run folder), so a watcher that polls in a loop
hears about each state once.
It also reads each stall notice from the event journal, even after the attempt ends.
Without a consumer it reports the current state
as soon as it needs attention.

Questions are flattened to one list, each with a key: the interrupt id, or for a
question inside a sub-workflow, the ids along the way joined with "/". An
answers file maps those keys to answers; nest() turns it back into the nested
{interrupt id: answer} that Command(resume=...) takes (see nodes._ask_up).
"""

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from . import limits, schemas
from .runs import LOCK_FD, Lock, Run, write_json

ATTENTION = ("waiting", "failed", "limit", "succeeded", "stopped", "stall_detected")
GRACE_SECONDS = 15  # a run just created may not hold its lock yet


def driven(folder: Path) -> bool:
    return Run.at(folder).driven()


# ---------- the state of a run ----------


def state(run: Run) -> dict:
    """status.json, with "stopped" when it says running but nothing is driving the run,
    plus what an operator needs next: the output, or the command to continue."""
    current = run.status()
    current.setdefault("run", run.id)
    if current.get("status") in ("running", "stall_detected") and not run.driven() and _age(current) > GRACE_SECONDS:
        current = {**{k: v for k, v in current.items() if k != "agents_waiting"}, "status": "stopped",
                   "reason": "no wf process is driving this run (it crashed or was killed)"}
    status = current.get("status")
    if status == "succeeded" and (run.dir / "output.json").exists():
        try:
            current["output"] = json.loads((run.dir / "output.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):  # damaged: the status still matters, and wf list goes on
            pass
    if status == "waiting":
        current["answer"] = run.command("answer", "<answers.json>")
    if status in ("failed", "stopped"):
        current["retry"] = run.command("retry")
    if status == "limit":  # paused: a bigger limit continues it
        current["retry"] = run.command("retry", limits.hint(current.get("limit")))
    current["dir"] = str(run.dir)
    if status == "stall_detected":
        current["stall_actions"] = {"end": run.command("stall", "<node> end"),
                                    "wait": run.command("stall", "<node> wait")}
    info = run.info()  # read once: wait() calls this five times a second
    current["workflow"] = info.get("workflow")
    current["dry_run"] = bool(info.get("dry_run"))
    return current


CONSUMER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")  # consumer names become file names


def wait(run: Run, timeout: float, consumer: str | None = None) -> dict | None:
    """The run's state once it needs attention (and is new to `consumer`), or None at the timeout."""
    if consumer is not None and not CONSUMER.fullmatch(consumer):
        raise ValueError("a consumer name is letters, digits, . _ and - (it names a file in the run folder)")
    cursor_file = run.dir / "consumers" / f"{consumer}.json" if consumer else None
    seen, offset = _cursor(cursor_file)
    deadline = time.monotonic() + timeout
    while True:
        # Read events after status so a terminal result cannot hide an earlier stall.
        current = state(run)
        notice, offset = _stall_notice(run, offset) if cursor_file else (None, offset)
        if notice is not None:
            current = state(run)
            if current["status"] == "stall_detected":
                seen = current.get("seq", 0)
            _save_cursor(cursor_file, seen, offset)
            return {**current, "status": "stall_detected", "current_status": current["status"], "stall": notice}
        # A "stopped" run keeps the sequence number of its last "running" state, so it too is reported once
        if current["status"] in ATTENTION and current.get("seq", 0) > seen:
            _save_cursor(cursor_file, current.get("seq", 0), offset)
            return current
        if time.monotonic() >= deadline:
            _save_cursor(cursor_file, seen, offset)
            return None
        time.sleep(0.2)


def _cursor(cursor_file: Path | None) -> tuple[int, int]:
    """Read the last state sequence and event byte offset for a consumer."""
    try:
        saved = json.loads(cursor_file.read_text(encoding="utf-8")) if cursor_file else {}
        return int(saved.get("seq", -1)), max(0, int(saved.get("event_offset", 0)))
    except (OSError, ValueError, TypeError, AttributeError):
        return -1, 0


def _save_cursor(cursor_file: Path | None, seen: int, offset: int) -> None:
    if cursor_file:
        cursor_file.parent.mkdir(exist_ok=True)
        write_json(cursor_file, {"seq": seen, "event_offset": offset})


def _stall_notice(run: Run, offset: int) -> tuple[dict | None, int]:
    try:
        file = (run.dir / "events.jsonl").open("rb")
    except FileNotFoundError:
        return None, offset
    with file:
        if offset > os.fstat(file.fileno()).st_size:
            offset = 0
        file.seek(offset)
        while line := file.readline():
            if not line.endswith(b"\n"):
                break
            offset = file.tell()
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict) and event.get("event") == "stall_detected":
                return event, offset
    return None, offset


def _age(status: dict) -> float:
    try:
        return time.time() - datetime.fromisoformat(status["updated"]).timestamp()
    except (KeyError, TypeError, ValueError):  # TypeError: `updated` isn't text
        return float("inf")


# ---------- questions and answers ----------


def questions(interrupts) -> list[dict]:
    """The questions a paused run waits on, flattened: [{key, node, message, schema}]."""
    found = []
    for interrupt in interrupts:
        if interrupt.value.get("kind") in ("human", "relay"):
            found += _flatten(interrupt.id, interrupt.value, "")
    return found


def _flatten(key: str, payload: dict, path: str) -> list[dict]:
    if payload.get("kind") == "relay":
        inner = f"{path}{payload['node']}/"
        return [q for item in payload["questions"] for q in _flatten(f"{key}/{item['id']}", item["question"], inner)]
    index = f"[{payload['index']}]" if payload.get("index") is not None else ""  # an item of a for_each human node
    return [{"key": key, "node": f"{path}{payload['node']}{index}", "message": payload["message"], "schema": payload["schema"]}]


def check_answers(asked: list[dict], answers: object) -> list[str]:
    """Problems with an answers file: missing or unknown keys, answers that break their schema."""
    if not isinstance(answers, dict):
        return ["the answers file must hold a JSON object of {question key: answers}"]
    problems = [f"no answer for {q['key']} ({q['node']}: {q['message'].strip()[:80]})" for q in asked if q["key"] not in answers]
    known = {q["key"]: q for q in asked}
    problems += [f"{key} isn't a question this run is waiting on" for key in answers if key not in known]
    for key, answer in answers.items():
        if key in known:
            problems += [f"{known[key]['node']}: {p}" for p in schemas.check(answer, known[key]["schema"])]
    return problems


def nest(answers: dict[str, dict]) -> dict:
    """{"a/b": x, "c": y} -> {"a": {"b": x}, "c": y}: resume values by interrupt id, nested per relay."""
    nested: dict = {}
    for key, value in answers.items():
        *outer, last = key.split("/")
        level = nested
        for part in outer:
            level = level.setdefault(part, {})
        level[last] = value
    return nested


# ---------- background processes ----------


# The background driver: this wf's own package, whatever the repo it works in contains.
# `python -m workflow.cli` would put the repo first on sys.path, so a repo with its own
# workflow/ folder would run instead; -P leaves the current folder out, and the
# first argument says where this package lives.
_DRIVER = "import sys; sys.path.insert(0, sys.argv.pop(1)); from workflow.cli import main; sys.exit(main())"
_PACKAGE_ROOT = str(Path(__file__).resolve().parent.parent)


def detach(run: Run, args: list[str], lock: Lock) -> int:
    """Start `wf <args>` for this run in the background, in its own session, logging to the
    run folder, and hand it the run's lock (`lock`, held by the caller): the run stays
    locked in between, so nobody else can start driving it. It also gets the run's location
    (Run.location), so it finds the run where it is. Returns its pid. The run is marked
    running first, so a `wf wait` right after this doesn't report the state it had before."""
    before = run.status()
    run.set_status("running")
    where, env = run.location()
    try:
        with (run.dir / "driver.log").open("a", encoding="utf-8") as log:
            process = subprocess.Popen(
                [sys.executable, "-P", "-c", _DRIVER, _PACKAGE_ROOT, *args, *where], cwd=run.repo,
                env={**os.environ, **env, LOCK_FD: str(lock.fd)}, pass_fds=(lock.fd,),
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
            )
    except BaseException:  # nothing started: put back the state the run was in
        run.set_status(before.get("status", "unknown"),
                       **{k: v for k, v in before.items() if k not in ("status", "seq", "run", "updated")})
        raise
    lock.held_by(process.pid)
    return process.pid
