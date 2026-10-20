"""Observe: what runs do now, for a person at a terminal or a supervisor.

    wf sessions [run-id]     each agent session that runs now, in this repo's runs or in one run
    wf watch <run-id>        the run's steps and tool calls as lines, as they come

An agent session starts and ends with an event in events.jsonl (nodes.py).
The session_started event names its node, pass, item, attempt, harness and
model. It also names the process that runs it, and its transcript,
attempt-<n>.log in the run folder. session_ended follows when the attempt
closes the session.

A session runs now until its session_ended comes, or an event that ends its
attempt, pass or item. Those events are attempt_failed, harness_unavailable,
node_finished, node_failed and item_failed. The same events for
a workflow node end each session inside it, because a sub-workflow that its
attempt stops can't pass up its own session_ended. Also, the process that its
session_started event names must still drive the run: it holds the run's lock
(runs.py). So a session that a killed process left open is not listed.

A session's last tool call is its last agent_tool event. It has been quiet
since its last event or the last change to its transcript, the later of the two.

`wf watch` prints the last WATCH_BACK events, then each new one. Each time the
run comes to need attention (operate.ATTENTION), it prints the run's state,
and goes on: `wf answer` or `wf retry` can continue the run. It stops once
the run succeeded, or its folder is gone.
"""

import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from . import operate, progress, report, runner
from .runs import Run, read_events

WATCH_BACK = 20
POLL_SECONDS = 0.5
_ENDS = ("attempt_failed", "harness_unavailable", "node_finished", "node_failed", "item_failed")


def sessions(run: Run, now: float | None = None) -> list[dict]:
    """The agent sessions of a run that run now, oldest first."""
    if not run.driven():
        return []
    found = Open(run)
    for event in report.events(run):
        found.add(event)
    return found.live(now)


class Open:
    """The agent sessions that a run's events leave open, from its events in turn: live() says which
    of them run now. wf ui keeps one for each run, and adds to it only the events that came since its
    last refresh."""

    def __init__(self, run: Run):
        self.run = run
        self.found: dict[tuple, dict] = {}

    def add(self, event: dict) -> None:
        kind, found = event.get("event"), self.found
        key = (event.get("node"), event.get("pass"), event.get("index"), event.get("attempt"))
        if kind == "session_started":
            found[key] = {
                "run": self.run.id, "node": where(event), "pass": event.get("pass"), "index": event.get("index"),
                "attempt": event.get("attempt"), "harness": event.get("harness"), "model": event.get("model"),
                "started": event.get("time"), "last_tool": None, "_pid": event.get("pid"), "_seen": event.get("time"),
                "transcript": str(self.run.dir / event["log"]) if event.get("log") else None,
            }
        elif kind == "session_ended":
            found.pop(key, None)
        elif kind == "agent_tool" and key in found:
            found[key]["last_tool"] = {"tool": event.get("tool"), "detail": event.get("detail", ""), "time": event.get("time")}
            found[key]["_seen"] = event.get("time")
        elif kind in _ENDS:
            for ended in [k for k in found if _ends(event, k)]:
                del found[ended]

    def live(self, now: float | None = None) -> list[dict]:
        """The open sessions that run now, oldest first: the process that started each one still drives the run."""
        if not self.run.driven():
            return []
        driver = _driver(self.run)
        now = time.time() if now is None else now
        live = []
        for session in map(dict, self.found.values()):
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


def _ends(event: dict, key: tuple) -> bool:
    """Does this event end the attempt, pass or item of the session with this key, or the
    workflow node that the session is inside?"""
    node, pass_no, index, attempt = key
    name, kind = event.get("node"), event.get("event")
    if not isinstance(name, str) or not isinstance(node, str):
        return False
    inside = (f"{name}[{event['index']}]/",) if event.get("index") is not None else (f"{name}/", f"{name}[")
    if node.startswith(inside):
        return True
    if node != name:
        return False
    if kind in ("attempt_failed", "harness_unavailable"):
        return (pass_no, index, attempt) == (event.get("pass"), event.get("index"), event.get("attempt"))
    if kind == "item_failed":
        return index == event.get("index")
    return pass_no == event.get("pass")


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
          poll: float = POLL_SECONDS, tell: Callable[[dict], str] | None = None) -> dict:
    """Print the last `back` events of the run as lines, or all of them for None. Then print each
    new one as it comes. Each time the run comes to need attention, print `tell(state)` and go on.
    Returns the run's last state, once it succeeded or its folder is gone ("discarded")."""
    tell = tell or (lambda state: f"{state.get('run')}: {state.get('status')}")
    path = run.dir / "events.jsonl"
    workflow = run.info().get("workflow")
    events, offset = read_events(path)
    shown = [text for text in map(_stamped, events) if text is not None]
    for text in shown[0 if back is None else max(len(shown) - back, 0):]:
        say(text)
    told = None
    while True:
        if not run.dir.exists():
            return {"run": run.id, "status": "discarded", "workflow": workflow}
        current = operate.state(run)
        events, offset = read_events(path, offset)  # after the state: no event before it is missed
        for text in map(_stamped, events):
            if text is not None:
                say(text)
        if current["status"] == "succeeded":
            return current
        if current["status"] in operate.ATTENTION and (current["status"], current.get("seq")) != told:
            told = (current["status"], current.get("seq"))
            say(tell(current))
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
