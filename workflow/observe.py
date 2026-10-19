"""Observe: what runs do now, for a person at a terminal or a supervisor.

    wf sessions [run-id]     each agent session that runs now, in this repo's runs or in one run
    wf watch <run-id>        the run's steps and tool calls as lines, as they come

An agent session starts and ends with an event in events.jsonl (nodes.py).
The session_started event names its node, pass, item, attempt, harness and
model. It also names the process that runs it, and its transcript
(attempt-<n>.log, relative to the run folder). session_ended follows when the
attempt closes the session. A session runs now while it has no session_ended
event. Also, the process that its session_started event names must still drive
the run: it holds the run's lock (runs.py). So a session that a killed process
left open is not listed.

A session's last tool call is its last agent_tool event. It has been quiet
since its last event or the last change to its transcript, the later of the two.

`wf watch` prints the last WATCH_BACK events, then each new one, until the run
needs attention (operate.ATTENTION). Then it prints the run's state and stops.
"""

import json
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from . import operate, progress, report, runner
from .runs import Run

WATCH_BACK = 20
POLL_SECONDS = 0.5


def sessions(run: Run, now: float | None = None) -> list[dict]:
    """The agent sessions of a run that run now, oldest first."""
    if not run.driven():
        return []
    driver = _driver(run)
    now = time.time() if now is None else now
    found: dict[tuple, dict] = {}
    for event in report.events(run):
        kind = event.get("event")
        key = (event.get("node"), event.get("pass"), event.get("index"), event.get("attempt"))
        if kind == "session_started":
            found[key] = {
                "run": run.id, "node": where(event), "pass": event.get("pass"), "index": event.get("index"),
                "attempt": event.get("attempt"), "harness": event.get("harness"), "model": event.get("model"),
                "started": event.get("time"), "last_tool": None, "_pid": event.get("pid"), "_seen": event.get("time"),
                "transcript": str(run.dir / event["log"]) if event.get("log") else None,
            }
        elif kind == "session_ended":
            found.pop(key, None)
        elif kind == "agent_tool" and key in found:
            found[key]["last_tool"] = {"tool": event.get("tool"), "detail": event.get("detail", ""), "time": event.get("time")}
            found[key]["_seen"] = event.get("time")
    live = []
    for session in found.values():
        if driver is not None and session["_pid"] is not None and session["_pid"] != driver:
            continue  # left open by a process that no longer drives the run
        seen = timestamp(session.pop("_seen"))
        session.pop("_pid")
        transcript = Path(session["transcript"]) if session["transcript"] else None
        if transcript is not None:
            try:
                seen = max(seen, transcript.stat().st_mtime)
            except OSError:
                pass
            session["instruction"] = str(transcript.parent / "instruction.md")
        session["quiet_s"] = max(round(now - seen), 0)
        live.append(session)
    return live


def _driver(run: Run) -> int | None:
    """The process that drives the run, as its lock file names it."""
    try:
        return int((run.dir / "lock").read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def where(event: dict) -> str:
    """review[1], or build[2]/implement for a node inside a sub-workflow."""
    return str(event.get("node", "")) + (f"[{event['index']}]" if event.get("index") is not None else "")


def timestamp(when) -> float:
    try:
        return datetime.fromisoformat(when).timestamp()
    except (TypeError, ValueError):
        return 0.0


def table(found: list[dict]) -> str:
    """Sessions for people: two lines each."""
    if not found:
        return "No agent session runs now."
    lines = []
    for s in found:
        model = f" {s['model']}" if s.get("model") else ""
        tool = s["last_tool"]
        last = f"{tool['tool']} {tool['detail']}".rstrip() if tool else "none yet"
        lines += [f"{s['run']}  {s['node']}  pass {s['pass']}, attempt {s['attempt']}  {s['harness']}{model}",
                  f"  started {_clock(s['started'])}, quiet for {progress.duration(s['quiet_s'])}, last tool call: {last}"]
    return "\n".join(lines)


# ---------- the event stream ----------


def read_events(path: Path, offset: int = 0) -> tuple[list[dict], int]:
    """The events after byte `offset` of events.jsonl, and the offset after the last whole line.
    A line still being written stays for the next read."""
    try:
        with path.open("rb") as file:
            file.seek(offset)
            data = file.read()
    except FileNotFoundError:
        return [], offset
    whole = data[: data.rfind(b"\n") + 1]
    found = []
    for line in whole.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            found.append(event)
    return found, offset + len(whole)


def line(event: dict) -> str | None:
    """One line for an event, as `wf run` prints it, plus a line for each agent session that starts."""
    if event.get("event") == "session_started":
        model = f" ({event['model']})" if event.get("model") else ""
        return f"  {where(event)}: attempt {event.get('attempt')} on {event.get('harness')}{model}"
    try:
        return runner.describe(event)
    except (KeyError, TypeError, ValueError):  # a damaged line: say what kind it is
        return f"  {event.get('event')}"


def watch(run: Run, say: Callable[[str], None] = lambda text: print(text, flush=True), back: int | None = WATCH_BACK,
          poll: float = POLL_SECONDS) -> dict:
    """Print the last `back` events of the run as lines, or all of them for None. Then print each
    new one as it comes, until the run needs attention. Returns its state then."""
    path = run.dir / "events.jsonl"
    events, offset = read_events(path)
    shown = [text for text in map(_stamped, events) if text is not None]
    for text in shown[0 if back is None else max(len(shown) - back, 0):]:
        say(text)
    while True:
        current = operate.state(run)
        events, offset = read_events(path, offset)  # after the state: no event before it is missed
        for text in map(_stamped, events):
            if text is not None:
                say(text)
        if current["status"] in operate.ATTENTION:
            return current
        time.sleep(poll)


def _stamped(event: dict) -> str | None:
    text = line(event)
    return None if text is None else f"{_clock(event.get('time'))} {text}"


def _clock(when) -> str:
    """The time of day of an event, in this machine's time zone."""
    try:
        return datetime.fromisoformat(when).astimezone().strftime("%H:%M:%S")
    except (TypeError, ValueError):
        return "--:--:--"

