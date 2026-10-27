"""wf metrics export (export.py): the records of run folders as rows of ClickHouse's tables of wf runs.

Most tests export into a store in memory, which keeps each row that the export inserts. The tests marked
docker export into a real ClickHouse in a container of their own, and remove it at the end. Where Docker
doesn't answer, or can't get the image of ClickHouse, pytest skips them."""

import fcntl
import hashlib
import http.server
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import fields
from datetime import datetime, timedelta
from pathlib import Path
from textwrap import dedent

import pytest

from fakes import Fake, submit, talk
from workflow import cli, config, export, harnesses, runner, runs
from workflow.harnesses.base import Usage
from workflow.runs import Run

REPO = "wf-3f9a1c2b4d5e"
TRACED, EVENTED = "20261020-1000-deliver-ab12", "20261019-2300-deliver-cd34"
LONG_ERROR = "cursor: Error: RetriableError: [resource_exhausted] Error" + " (more detail)" * 100  # too long for a record


class Memory:
    """A store in memory, which keeps each insert."""

    def __init__(self, identity: str = "tables-1"):
        self.identity, self.inserts = identity, []
        self.rows_written = 0

    def prepare(self) -> None:
        pass

    def insert(self, table: str, rows: list[dict]) -> None:
        self.inserts.append((table, rows))
        self.rows_written += len(rows)

    def final(self) -> dict[str, list[dict]]:
        """Each table as ClickHouse reads it with FINAL: the last row of each natural key, in the order of the keys."""
        found: dict[str, dict] = {}
        for table, rows in self.inserts:
            for row in rows:
                found.setdefault(table, {})[tuple(row[name] for name in export.keys(table))] = row
        return {table: ordered(table, list(rows.values())) for table, rows in found.items()}


def ordered(table: str, rows: list[dict]) -> list[dict]:
    return sorted(rows, key=lambda row: tuple(row[name] for name in export.keys(table)))


# ---------- the run folders ----------


def at(clock: str, day: str = "2026-10-20") -> str:
    """A time as wf writes it, in the zone +02:00. ClickHouse keeps it in UTC, two hours earlier."""
    return f"{day}T{clock}+02:00"


def folder(parent: Path, run_id: str, files: dict[str, str], status: str = "succeeded") -> Path:
    """A run folder in `parent`, as wf leaves it, with `files` in it."""
    path = parent / run_id
    (path / "workflow").mkdir(parents=True)
    (path / "workflow" / "manifest.json").write_text('{"main": "deliver.yaml"}')
    (path / "run.json").write_text(json.dumps({"workflow": "deliver", "home": {"path": "/repo", "branch": "main", "base": None},
                                               "dry_run": False}))
    (path / "status.json").write_text(json.dumps({"status": status, "seq": 2, "run": run_id}))
    for name, text in files.items():
        (path / name).write_text(text)
    return path


def lines(records: list[dict]) -> str:
    return "".join(json.dumps(record) + "\n" for record in records)


def blob(path: Path, text: str) -> dict:
    """Keep `text` in blobs/ of the run folder, as the trace keeps a long value, and give the reference to it."""
    name = f"blobs/{hashlib.sha256(text.encode()).hexdigest()}.txt"
    (path / "blobs").mkdir(exist_ok=True)
    (path / name).write_text(text)
    return {"$blob": name, "size": len(text), "start": text[:80]}


def used(tokens: int, cost: float, given: int, written: int, read: int, cached: int, reasoning: int) -> dict:
    return {"tokens": tokens, "cost_usd": cost, "input_tokens": given, "output_tokens": written, "cache_read_tokens": read,
            "cache_write_tokens": cached, "reasoning_tokens": reasoning}


NOTHING = used(0, 0.0, 0, 0, 0, 0, 0)


def r(clock: str, kind: str, ident: str, facts: dict | None = None, **more) -> dict:
    return {"time": at(clock), "kind": kind, "id": ident, **(facts or {}), **more}


def traced(error: dict) -> list[dict]:
    """The trace of a run of `deliver`. A command fails once, and an agent has two turns and three tool calls.
    The node review has two items, and one of them runs on a fallback after a rate limit. The sub-workflow
    of a workflow node times out, and `wf retry` runs the node again. Before its rate limit, Cursor names a
    call of a wf tool only in the call's second record, as its ACP does. `error` is the reference to
    LONG_ERROR in blobs/."""
    tests = {"node": "tests", "pass": 1, "command": "make test", "cwd": "/repo", "env": {}}
    implement = {"node": "implement", "pass": 1, "attempt": 1, "harness": "claude-code", "model": "claude-opus-5-5",
                 "effort": "high", "access": "edit", "web": False}
    review = {"node": "review", "pass": 1, "index": 0, "attempt": 1, "harness": "codex", "model": "gpt-6.1-sol",
              "effort": "xhigh", "access": "read-only", "web": False}
    rate_limited = {**review, "index": 1, "harness": "cursor", "model": "gemini-3.8-flash-high", "effort": None}
    fallback = {**rate_limited, "attempt": 2, "harness": "claude-code", "model": "claude-opus-5-5", "effort": "high", "fallback": True}
    build = {"node": "build", "pass": 1, "workflow": "build.yaml", "inputs": {}}
    compiles = {"where": "build/", "node": "compile", "pass": 1, "attempt": 1, "harness": "codex", "model": "gpt-6.1-sol",
                "effort": "low", "access": "edit", "web": False}
    fresh = {"start": "fresh"}

    def turn(facts: dict, number: int) -> dict:
        place = {key: facts[key] for key in ("where", "node", "pass", "index", "attempt") if key in facts}
        return {**place, "turn": number}

    return [
        r("10:00:00.000", "run", "run", action="started", workflow="deliver", inputs={"task": "fix it"}, dry_run=False),
        r("10:00:00.010", "pass", "tests#1", {"node": "tests", "pass": 1}),
        r("10:00:01.000", "attempt", "tests#1.a1", tests, attempt=1, started=at("10:00:01.000")),
        r("10:00:03.500", "attempt_end", "tests#1.a1", tests, attempt=1, exit_code=1, started=at("10:00:01.000"),
          ended=at("10:00:03.500"), duration_s=2.5, outcome="failed", error="exited with code 1"),
        r("10:00:03.600", "attempt", "tests#1.a2", tests, attempt=2, started=at("10:00:03.600")),
        r("10:00:05.600", "attempt_end", "tests#1.a2", tests, attempt=2, exit_code=0, started=at("10:00:03.600"),
          ended=at("10:00:05.600"), duration_s=2.0, outcome="succeeded"),
        r("10:00:05.700", "job_end", "tests#1", {"node": "tests", "pass": 1}, ok=True, attempts=2, usage={"cost_usd": 0.0, "tokens": 0}),
        r("10:00:06.000", "attempt", "implement#1.a1", implement, session=fresh, started=at("10:00:06.000")),
        r("10:00:06.100", "turn", "implement#1.a1.t1", turn(implement, 1), sends="instruction", text="Fix it."),
        r("10:00:07.000", "tool_call", "implement#1.a1.t1.c1", turn(implement, 1), tool="Bash", arguments={"command": "ls"}),
        r("10:00:07.200", "tool_call", "implement#1.a1.t1.c1", turn(implement, 1), tool="Bash",
          arguments={"command": "ls", "description": "list the files"}),
        r("10:00:08.300", "tool_result", "implement#1.a1.t1.c1", turn(implement, 1), is_error=False, result="a.py"),
        r("10:00:09.000", "tool_call", "implement#1.a1.t1.c2", turn(implement, 1), tool="Read", arguments={"file_path": "b.py"}),
        r("10:00:09.040", "tool_result", "implement#1.a1.t1.c2", turn(implement, 1), is_error=True, result="no such file"),
        r("10:01:06.100", "turn_end", "implement#1.a1.t1", turn(implement, 1), status="done",
          usage=used(1000, 0.5, 100, 300, 500, 100, 50)),
        r("10:01:07.000", "turn", "implement#1.a1.t2", turn(implement, 2), sends="reminder", text="Call submit_result."),
        r("10:01:08.000", "tool_call", "implement#1.a1.t2.c1", turn(implement, 2), tool="Grep", arguments={"pattern": "x"}),
        r("10:01:10.000", "turn_end", "implement#1.a1.t2", turn(implement, 2), status="done", usage=used(200, 0.1, 20, 60, 100, 20, 10)),
        r("10:01:10.500", "attempt_end", "implement#1.a1", implement, session={"start": "fresh", "id": "sess-1"},
          usage=used(1200, 0.6, 120, 360, 600, 120, 60), started=at("10:00:06.000"), ended=at("10:01:10.500"), duration_s=64.5,
          outcome="succeeded"),
        r("10:01:10.600", "routes", "implement#1", {"node": "implement", "pass": 1}, routes=[], taken=0, to=["review"]),
        r("10:01:11.000", "attempt", "review#1[0].a1", review, session=fresh, started=at("10:01:11.000")),
        r("10:01:11.000", "attempt", "review#1[1].a1", rate_limited, session=fresh, started=at("10:01:11.000")),
        r("10:01:12.000", "turn", "review#1[1].a1.t1", turn(rate_limited, 1), sends="instruction", text="Review it."),
        r("10:01:13.000", "tool_call", "review#1[1].a1.t1.c1", turn(rate_limited, 1), tool="other", arguments={}),
        r("10:01:13.400", "tool_call", "review#1[1].a1.t1.c1", turn(rate_limited, 1), tool="linear.list_issues",
          arguments={"team": "wf"}),
        r("10:01:14.000", "tool_result", "review#1[1].a1.t1.c1", turn(rate_limited, 1), is_error=False, kept=False, characters=120),
        r("10:01:40.000", "turn_end", "review#1[1].a1.t1", turn(rate_limited, 1), status="failed", message=error, usage=NOTHING),
        r("10:01:40.100", "attempt_end", "review#1[1].a1", rate_limited, session={"start": "fresh", "id": "agent-1"},
          usage=NOTHING, started=at("10:01:11.000"), ended=at("10:01:40.100"), duration_s=29.1, outcome="failed", error=error),
        r("10:01:41.000", "attempt", "review#1[1].a2", fallback, session=fresh, started=at("10:01:41.000")),
        r("10:02:00.000", "attempt_end", "review#1[0].a1", review, session={"start": "fresh", "id": "thr-1"},
          usage=used(5000, 0.0, 1000, 500, 3500, 0, 200), started=at("10:01:11.000"), ended=at("10:02:00.000"), duration_s=49.0,
          outcome="succeeded"),
        r("10:02:30.000", "attempt_end", "review#1[1].a2", fallback, session={"start": "fresh", "id": "sess-2"},
          usage=used(800, 0.2, 50, 150, 600, 0, 0), started=at("10:01:41.000"), ended=at("10:02:30.000"), duration_s=49.0,
          outcome="succeeded"),
        r("10:02:30.100", "routes", "review#1", {"node": "review", "pass": 1}, routes=[], taken=None),
        r("10:02:31.000", "attempt", "build#1.a1", build, attempt=1, started=at("10:02:31.000")),
        r("10:02:31.100", "attempt", "build#1.a1/compile#1.a1", compiles, session=fresh, started=at("10:02:31.100")),
        r("10:02:40.000", "attempt_end", "build#1.a1/compile#1.a1", compiles, session={"start": "fresh", "id": "thr-2"},
          usage=used(300, 0.0, 100, 50, 150, 0, 20), started=at("10:02:31.100"), ended=at("10:02:40.000"), duration_s=8.9,
          outcome="timed_out", error="timed out after 8s"),
        r("10:02:40.100", "attempt_end", "build#1.a1", build, attempt=1, started=at("10:02:31.000"), ended=at("10:02:40.100"),
          duration_s=9.1, outcome="failed", error="sub-workflow build failed: compile: timed out after 8s"),
        r("10:02:41.000", "run_stopped", "run", status="failed", node="build", error="timed out"),
        r("11:00:00.000", "run", "run", action="retried", workflow="deliver"),
        r("11:00:01.000", "attempt", "build#1.a2", build, attempt=2, started=at("11:00:01.000")),
        r("11:00:01.100", "attempt", "build#1.a2/compile#1.a1", compiles, session=fresh, started=at("11:00:01.100")),
        r("11:00:30.000", "attempt_end", "build#1.a2/compile#1.a1", compiles, session={"start": "fresh", "id": "thr-3"},
          usage=used(400, 0.0, 100, 100, 200, 0, 30), started=at("11:00:01.100"), ended=at("11:00:30.000"), duration_s=28.9,
          outcome="succeeded"),
        r("11:00:30.100", "attempt_end", "build#1.a2", build, attempt=2, started=at("11:00:01.000"), ended=at("11:00:30.100"),
          duration_s=29.1, outcome="succeeded"),
        r("11:00:31.000", "run_stopped", "run", status="succeeded", output={"done": True}),
    ]


def e(clock: str, event: str, node: str | None = None, **more) -> dict:
    return {"time": at(clock, "2026-10-19"), "event": event, **({"node": node} if node else {}), **more}


EVENTS = [  # a run from before the trace, with no agent_model after its first attempt, as such runs have
    e("23:00:00", "node_started", "implement", **{"pass": 1}),
    e("23:00:00", "agent_model", "implement", **{"pass": 1}, attempt=1, harness="claude-code", model="claude-opus-5-5", effort="high"),
    e("23:00:01", "session_started", "implement", **{"pass": 1}, attempt=1, harness="claude-code", model="claude-opus-5-5",
      effort="high", pid=7, log="nodes/implement/1/attempt-1.log"),
    e("23:00:05", "agent_tool", "implement", **{"pass": 1}, attempt=1, harness="claude-code", tool="Bash", detail="ls"),
    e("23:00:09", "agent_tool", "implement", **{"pass": 1}, attempt=1, harness="claude-code", tool="Read", detail="a.py"),
    e("23:00:30", "agent_usage", "implement", **{"pass": 1}, attempt=1, harness="claude-code", tokens=1000, cost_usd=0.5),
    e("23:10:00", "agent_usage", "implement", **{"pass": 1}, attempt=1, harness="claude-code", tokens=3000, cost_usd=1.5),
    e("23:10:00", "session_ended", "implement", **{"pass": 1}, attempt=1, harness="claude-code"),
    e("23:10:00", "node_finished", "implement", **{"pass": 1}, cost_usd=1.5, tokens=3000),
    e("23:10:00", "route", "implement", route=0, to=["review"], when=None, taken=1, max=None),
    e("23:10:01", "node_started", "review", **{"pass": 1}, items=2),
    e("23:10:05", "agent_tool", "review", **{"pass": 1}, index=0, attempt=1, harness="cursor", tool="read", detail="a.py"),
    e("23:10:06", "agent_tool", "review", **{"pass": 1}, index=1, attempt=1, harness="cursor", tool="grep", detail="x"),
    e("23:15:00", "attempt_failed", "review", **{"pass": 1}, index=1, harness="cursor", attempt=1,
      error="cursor: Error: RetriableError: [canceled] http/2 stream closed with error code CANCEL (0x8)"),
    e("23:15:00", "attempt_started", "review", **{"pass": 1}, index=1, harness="cursor", attempt=2),
    e("23:15:30", "agent_tool", "review", **{"pass": 1}, index=1, attempt=2, harness="cursor", tool="read", detail="b.py"),
    e("23:20:00", "node_finished", "review", **{"pass": 1}, cost_usd=0.0, tokens=0),
    e("23:20:00", "route", "review", route=0, to=["implement"], when="review.blocking", taken=1, max=2),
    e("23:20:00", "node_started", "implement", **{"pass": 2}),
    e("23:20:01", "session_continued", "implement", **{"pass": 2}, attempt=1),
    e("23:20:05", "agent_tool", "implement", **{"pass": 2}, attempt=1, harness="claude-code", tool="Edit", detail="a.py"),
    e("23:25:00", "attempt_failed", "implement", **{"pass": 2}, harness="claude-code", attempt=1, error="timed out after 5m"),
    e("23:25:00", "attempt_started", "implement", **{"pass": 2}, harness="claude-code", attempt=2),
    e("23:25:10", "agent_tool", "implement", **{"pass": 2}, attempt=2, harness="claude-code", tool="Edit", detail="a.py"),
    e("23:30:00", "node_finished", "implement", **{"pass": 2}, cost_usd=0.75, tokens=2000),
    e("23:30:00", "run_finished", status="succeeded"),
]


@pytest.fixture
def runs_dir(tmp_path) -> Path:
    return tmp_path / "runs"


def traced_folder(parent: Path) -> Path:
    path = folder(parent, TRACED, {})
    (path / "trace.jsonl").write_text(lines(traced(blob(path, LONG_ERROR))))
    (path / "events.jsonl").write_text('{"time": "2026-10-20T10:00:00+02:00", "event": "node_started", "node": "tests", "pass": 1}\n')
    return path


def evented_folder(parent: Path) -> Path:
    return folder(parent, EVENTED, {"events.jsonl": lines(EVENTS)})


def of(path: Path) -> Run:
    return Run(path.name, path, path)


# ---------- the rows that the run folders give ----------


def counts(tokens=0, cost=0.0, given=0, written=0, read=0, cached=0, reasoning=0) -> dict:
    return {"tokens": tokens, "input_tokens": given, "output_tokens": written, "cache_read_tokens": read,
            "cache_write_tokens": cached, "reasoning_tokens": reasoning, "cost_usd": cost}


def attempt(run_id: str, node: str, pass_no: int, item: int, number: int, **row) -> dict:
    return {"run_id": run_id, "workflow": "deliver", "node": node, "pass": pass_no, "item": item, "attempt": number,
            "kind": "agent", "harness": "", "model": "", "effort": "", "access": "", "session_id": "", "session_start": "",
            "ended": None, "duration_s": None, "outcome": "succeeded", "error": "", "error_class": "", **counts(),
            "exit_code": None, "duration_basis": "monotonic" if run_id == TRACED else "wall",
            "awake_s": row.get("duration_s") if run_id == TRACED else None, "asleep_s": None, **row}


def call(run_id: str, node: str, pass_no: int, item: int, number: int, turn: int, call_id: str, tool: str, harness: str,
         model: str, started: str, took_ms: float | None = None, is_error: bool | None = None) -> dict:
    return {"run_id": run_id, "workflow": "deliver", "node": node, "pass": pass_no, "item": item, "attempt": number,
            "turn": turn, "call_id": call_id, "tool": tool, "harness": harness, "model": model, "started": started,
            "duration_ms": took_ms, "is_error": is_error}


OPUS = {"harness": "claude-code", "model": "claude-opus-5-5", "effort": "high"}
SOL = {"harness": "codex", "model": "gpt-6.1-sol"}

TRACE_ROWS = {
    "runs": [{"run_id": TRACED, "repo_key": REPO, "workflow": "deliver", "status": "succeeded", "dry_run": False,
              "started": "2026-10-20 08:00:00.000", "ended": "2026-10-20 09:00:31.000", "duration_s": 192.0,
              "cost_usd": 0.8, "tokens": 7700, "source": "trace"}],
    "attempts": [
        attempt(TRACED, "build", 1, -1, 1, kind="workflow", started="2026-10-20 08:02:31.000", ended="2026-10-20 08:02:40.100",
                duration_s=9.1, outcome="failed", error="sub-workflow build failed: compile: timed out after 8s",
                error_class="timeout"),
        attempt(TRACED, "build", 1, -1, 2, kind="workflow", started="2026-10-20 09:00:01.000", ended="2026-10-20 09:00:30.100",
                duration_s=29.1),
        attempt(TRACED, "build#1.a1/compile", 1, -1, 1, **SOL, effort="low", access="edit", session_id="thr-2",
                session_start="fresh", started="2026-10-20 08:02:31.100", ended="2026-10-20 08:02:40.000", duration_s=8.9,
                outcome="timed_out", error="timed out after 8s", error_class="timeout", **counts(300, 0.0, 100, 50, 150, 0, 20)),
        attempt(TRACED, "build#1.a2/compile", 1, -1, 1, **SOL, effort="low", access="edit", session_id="thr-3",
                session_start="fresh", started="2026-10-20 09:00:01.100", ended="2026-10-20 09:00:30.000", duration_s=28.9,
                **counts(400, 0.0, 100, 100, 200, 0, 30)),
        attempt(TRACED, "implement", 1, -1, 1, **OPUS, access="edit", session_id="sess-1", session_start="fresh",
                started="2026-10-20 08:00:06.000", ended="2026-10-20 08:01:10.500", duration_s=64.5,
                **counts(1200, 0.6, 120, 360, 600, 120, 60)),
        attempt(TRACED, "review", 1, 0, 1, **SOL, effort="xhigh", access="read-only", session_id="thr-1", session_start="fresh",
                started="2026-10-20 08:01:11.000", ended="2026-10-20 08:02:00.000", duration_s=49.0,
                **counts(5000, 0.0, 1000, 500, 3500, 0, 200)),
        attempt(TRACED, "review", 1, 1, 1, harness="cursor", model="gemini-3.8-flash-high", access="read-only",
                session_id="agent-1", session_start="fresh", started="2026-10-20 08:01:11.000", ended="2026-10-20 08:01:40.100",
                duration_s=29.1, outcome="failed", error=LONG_ERROR, error_class="rate_limit"),
        attempt(TRACED, "review", 1, 1, 2, **OPUS, access="read-only", session_id="sess-2", session_start="fresh",
                started="2026-10-20 08:01:41.000", ended="2026-10-20 08:02:30.000", duration_s=49.0, **counts(800, 0.2, 50, 150, 600)),
        attempt(TRACED, "tests", 1, -1, 1, kind="command", started="2026-10-20 08:00:01.000", ended="2026-10-20 08:00:03.500",
                duration_s=2.5, outcome="failed", error="exited with code 1", error_class="other", exit_code=1),
        attempt(TRACED, "tests", 1, -1, 2, kind="command", started="2026-10-20 08:00:03.600", ended="2026-10-20 08:00:05.600",
                duration_s=2.0, exit_code=0),
    ],
    "turns": [
        {"run_id": TRACED, "node": "implement", "pass": 1, "item": -1, "attempt": 1, "turn": 1, "started": "2026-10-20 08:00:06.100",
         "ended": "2026-10-20 08:01:06.100", "duration_s": 60.0, "status": "done", "message": "",
         **counts(1000, 0.5, 100, 300, 500, 100, 50)},
        {"run_id": TRACED, "node": "implement", "pass": 1, "item": -1, "attempt": 1, "turn": 2, "started": "2026-10-20 08:01:07.000",
         "ended": "2026-10-20 08:01:10.000", "duration_s": 3.0, "status": "done", "message": "",
         **counts(200, 0.1, 20, 60, 100, 20, 10)},
        {"run_id": TRACED, "node": "review", "pass": 1, "item": 1, "attempt": 1, "turn": 1, "started": "2026-10-20 08:01:12.000",
         "ended": "2026-10-20 08:01:40.000", "duration_s": 28.0, "status": "failed", "message": LONG_ERROR, **counts()},
    ],
    "tool_calls": [
        call(TRACED, "implement", 1, -1, 1, 1, "t1.c1", "Bash", "claude-code", "claude-opus-5-5", "2026-10-20 08:00:07.000", 1300.0, False),
        call(TRACED, "implement", 1, -1, 1, 1, "t1.c2", "Read", "claude-code", "claude-opus-5-5", "2026-10-20 08:00:09.000", 40.0, True),
        call(TRACED, "review", 1, 1, 1, 1, "t1.c1", "linear.list_issues", "cursor", "gemini-3.8-flash-high",
             "2026-10-20 08:01:13.000", 1000.0, False),
        call(TRACED, "implement", 1, -1, 1, 2, "t2.c1", "Grep", "claude-code", "claude-opus-5-5", "2026-10-20 08:01:08.000"),
    ],
    "routes": [
        {"run_id": TRACED, "node": "implement", "pass": 1, "time": "2026-10-20 08:01:10.600", "taken": 0, "to": ["review"]},
        {"run_id": TRACED, "node": "review", "pass": 1, "time": "2026-10-20 08:02:30.100", "taken": -1, "to": []},
    ],
}

EVENT_ROWS = {
    "runs": [{"run_id": EVENTED, "repo_key": REPO, "workflow": "deliver", "status": "succeeded", "dry_run": False,
              "started": "2026-10-19 21:00:00.000", "ended": "2026-10-19 21:30:00.000", "duration_s": 1800.0,
              "cost_usd": 2.25, "tokens": 5000, "source": "events"}],
    "attempts": [
        attempt(EVENTED, "implement", 1, -1, 1, **OPUS, session_start="fresh", started="2026-10-19 21:00:00.000",
                ended="2026-10-19 21:10:00.000", duration_s=600.0, **counts(3000, 1.5)),
        attempt(EVENTED, "implement", 2, -1, 1, harness="claude-code", session_start="fork", started="2026-10-19 21:20:01.000",
                ended="2026-10-19 21:25:00.000", duration_s=299.0, outcome="timed_out", error="timed out after 5m",
                error_class="timeout"),
        attempt(EVENTED, "implement", 2, -1, 2, harness="claude-code", started="2026-10-19 21:25:00.000",
                ended="2026-10-19 21:30:00.000", duration_s=300.0, **counts(2000, 0.75)),
        attempt(EVENTED, "review", 1, 0, 1, harness="cursor", started="2026-10-19 21:10:05.000"),
        attempt(EVENTED, "review", 1, 1, 1, harness="cursor", started="2026-10-19 21:10:06.000", ended="2026-10-19 21:15:00.000",
                duration_s=294.0, outcome="failed",
                error="cursor: Error: RetriableError: [canceled] http/2 stream closed with error code CANCEL (0x8)",
                error_class="dropped_connection"),
        attempt(EVENTED, "review", 1, 1, 2, harness="cursor", started="2026-10-19 21:15:00.000"),
    ],
    "tool_calls": [
        call(EVENTED, "implement", 1, -1, 1, 0, "e1", "Bash", "claude-code", "claude-opus-5-5", "2026-10-19 21:00:05.000"),
        call(EVENTED, "implement", 1, -1, 1, 0, "e2", "Read", "claude-code", "claude-opus-5-5", "2026-10-19 21:00:09.000"),
        call(EVENTED, "implement", 2, -1, 1, 0, "e1", "Edit", "claude-code", "", "2026-10-19 21:20:05.000"),
        call(EVENTED, "implement", 2, -1, 2, 0, "e1", "Edit", "claude-code", "", "2026-10-19 21:25:10.000"),
        call(EVENTED, "review", 1, 0, 1, 0, "e1", "read", "cursor", "", "2026-10-19 21:10:05.000"),
        call(EVENTED, "review", 1, 1, 1, 0, "e1", "grep", "cursor", "", "2026-10-19 21:10:06.000"),
        call(EVENTED, "review", 1, 1, 2, 0, "e1", "read", "cursor", "", "2026-10-19 21:15:30.000"),
    ],
    "routes": [
        {"run_id": EVENTED, "node": "implement", "pass": 1, "time": "2026-10-19 21:10:00.000", "taken": 0, "to": ["review"]},
        {"run_id": EVENTED, "node": "review", "pass": 1, "time": "2026-10-19 21:20:00.000", "taken": 0, "to": ["implement"]},
    ],
}


for rows in (TRACE_ROWS, EVENT_ROWS):
    for row in rows["attempts"]:
        for name in export.PROGRESS_COLUMNS:
            row.setdefault(name, None)


# ---------- a trace ----------


def test_a_trace_gives_a_row_for_each_run_attempt_turn_tool_call_and_route(runs_dir):
    store = Memory()
    written = export.export(of(traced_folder(runs_dir / REPO)), store)
    assert store.final() == {table: ordered(table, rows) for table, rows in TRACE_ROWS.items()}
    assert written == {"runs": 1, "attempts": 10, "turns": 3, "tool_calls": 4, "routes": 2}


def test_each_row_has_the_columns_of_its_table():
    for table, rows in [*TRACE_ROWS.items(), *EVENT_ROWS.items()]:
        columns = set(export.schema()[table]["columns"]) - {"loaded_at"}
        assert all(set(row) == columns for row in rows), table


def test_the_schema_has_the_tables_of_the_metrics_stack_with_each_count_of_usage_and_the_metric_columns():
    tables = export.schema()
    assert sorted(tables) == ["attempts", "export_runs", "metric_points", "routes", "runs", "schema_version", "tool_calls", "turns"]
    for table in ("attempts", "turns"):
        assert {count.name for count in fields(Usage)} <= set(tables[table]["columns"])
    assert {"harness": "LowCardinality(String)", "model": "LowCardinality(String)",
            "effort": "LowCardinality(String)"}.items() <= tables["metric_points"]["columns"].items()
    assert all(table["engine"] == "ReplacingMergeTree" for table in tables.values())
    assert tables["attempts"]["key"] == "run_id, node, pass, item, attempt"


def test_an_export_gives_the_same_rows_wherever_the_one_before_stopped(tmp_path):
    # No process holds the run's lock, so each cut shows the run as stopped there, until the rest comes
    for cut in range(len(traced({})) + 1):
        path = traced_folder(tmp_path / str(cut) / REPO)
        text = (path / "trace.jsonl").read_text()
        (path / "trace.jsonl").write_text("".join(text.splitlines(keepends=True)[:cut]))
        store = Memory()
        export.export(of(path), store)
        (path / "trace.jsonl").write_text(text)
        export.export(of(path), store)
        assert store.final() == {table: ordered(table, rows) for table, rows in TRACE_ROWS.items()}, f"cut after line {cut}"


def test_a_row_of_something_that_runs_is_written_again_when_it_ends(runs_dir):
    path = traced_folder(runs_dir / REPO)
    text = (path / "trace.jsonl").read_text()
    upto = text.index('"kind": "tool_result", "id": "implement#1.a1.t1.c1"')
    (path / "trace.jsonl").write_text(text[:text.rindex("\n", 0, upto) + 1])
    store = Memory()
    with Run.at(path).driving():  # the process that writes the trace
        export.export(of(path), store)
    running = store.final()
    assert [(row["node"], row["outcome"], row["ended"]) for row in running["attempts"]] == [
        ("implement", "running", None), ("tests", "failed", "2026-10-20 08:00:03.500"), ("tests", "succeeded", "2026-10-20 08:00:05.600")]
    assert [(row["status"], row["ended"]) for row in running["turns"]] == [("running", None)]
    assert [(row["call_id"], row["duration_ms"], row["is_error"]) for row in running["tool_calls"]] == [("t1.c1", None, None)]
    assert [(row["status"], row["ended"], row["duration_s"]) for row in running["runs"]] == [("running", None, None)]
    (path / "trace.jsonl").write_text(text)
    export.export(of(path), store)
    assert store.final() == {table: ordered(table, rows) for table, rows in TRACE_ROWS.items()}


def test_a_line_that_is_still_being_written_waits_for_the_next_export(runs_dir):
    path = traced_folder(runs_dir / REPO)
    text = (path / "trace.jsonl").read_text()
    half = len(text) - 40  # in the last line, the run_stopped of the retry
    (path / "trace.jsonl").write_text(text[:half])
    store = Memory()
    with Run.at(path).driving():  # the process that writes the trace
        export.export(of(path), store)
    assert json.loads((path / "export.json").read_text())["offset"] == text.rindex("\n", 0, half) + 1
    assert store.final()["runs"][0]["status"] == "running"
    (path / "trace.jsonl").write_text(text)
    assert export.export(of(path), store) == {"runs": 1}
    assert store.final() == {table: ordered(table, rows) for table, rows in TRACE_ROWS.items()}


def killed(path: Path) -> str:
    """The trace of the run folder, cut after the call that implement starts in its second turn, where its
    process dies. Returns the whole trace."""
    text = (path / "trace.jsonl").read_text()
    (path / "trace.jsonl").write_text(text[:text.index("\n", text.index('"id": "implement#1.a1.t2.c1"')) + 1])
    return text


IMPLEMENT = {"node": "implement", "pass": 1, "attempt": 1, "harness": "claude-code", "model": "claude-opus-5-5", "effort": "high",
             "access": "edit", "web": False}
RERUN = [  # `wf retry` runs implement again from the checkpoint, in an attempt of the same number
    r("11:00:00.000", "run", "run", action="retried", workflow="deliver"),
    r("11:00:01.000", "attempt", "implement#1.a1", IMPLEMENT, session={"start": "fresh"}, started=at("11:00:01.000")),
    r("11:00:01.100", "turn", "implement#1.a1.t1", {"node": "implement", "pass": 1, "attempt": 1}, turn=1, sends="instruction"),
    r("11:00:30.000", "turn_end", "implement#1.a1.t1", {"node": "implement", "pass": 1, "attempt": 1}, turn=1, status="done",
      usage=used(100, 0.05, 10, 30, 50, 10, 0)),
    r("11:00:31.000", "turn", "implement#1.a1.t2", {"node": "implement", "pass": 1, "attempt": 1}, turn=2, sends="reminder"),
    r("11:00:32.000", "tool_call", "implement#1.a1.t2.c1", {"node": "implement", "pass": 1, "attempt": 1}, turn=2, tool="Edit"),
    r("11:00:32.250", "tool_result", "implement#1.a1.t2.c1", {"node": "implement", "pass": 1, "attempt": 1}, turn=2, is_error=False),
    r("11:00:33.000", "turn_end", "implement#1.a1.t2", {"node": "implement", "pass": 1, "attempt": 1}, turn=2, status="done",
      usage=used(50, 0.05, 5, 15, 25, 5, 0)),
    r("11:00:33.500", "attempt_end", "implement#1.a1", IMPLEMENT, session={"start": "fresh", "id": "sess-9"},
      usage=used(150, 0.1, 15, 45, 75, 15, 0), started=at("11:00:01.000"), ended=at("11:00:33.500"), duration_s=32.5,
      outcome="succeeded"),
    r("11:00:34.000", "run_stopped", "run", status="succeeded"),
]


def test_a_run_whose_process_stopped_without_run_stopped_is_stopped_and_what_it_left_running_is_cancelled(runs_dir):
    path = traced_folder(runs_dir / REPO)
    killed(path)
    store = Memory()
    with Run.at(path).driving():  # while its process lives
        export.export(of(path), store)
    assert store.final()["runs"][0]["status"] == "running"
    assert export.export(of(path), store) == {"runs": 1, "attempts": 1, "turns": 1}  # nothing new, but no process drives it
    found = store.final()
    assert [(row["status"], row["ended"], row["duration_s"]) for row in found["runs"]] == [
        ("stopped", "2026-10-20 08:01:08.000", 68.0)]  # as `wf status` says, at its last record
    assert [(row["outcome"], row["ended"], row["duration_s"]) for row in found["attempts"] if row["node"] == "implement"] == [
        ("cancelled", "2026-10-20 08:01:08.000", 62.0)]
    assert [(row["turn"], row["status"], row["ended"], row["duration_s"]) for row in found["turns"]] == [
        (1, "done", "2026-10-20 08:01:06.100", 60.0), (2, "cancelled", "2026-10-20 08:01:08.000", 1.0)]
    assert found["tool_calls"][-1]["call_id"] == "t2.c1" and found["tool_calls"][-1]["duration_ms"] is None
    assert export.export(of(path), store) == {}


def test_a_rerun_of_an_attempt_after_its_process_stopped_starts_with_none_of_what_that_process_left(runs_dir):
    path = traced_folder(runs_dir / REPO)
    killed(path)
    with (path / "trace.jsonl").open("a") as trace:
        trace.write(lines(RERUN))
    store = Memory()
    export.export(of(path), store)
    found = store.final()
    edit = next(row for row in found["tool_calls"] if row["call_id"] == "t2.c1")
    assert (edit["tool"], edit["started"], edit["duration_ms"], edit["is_error"]) == ("Edit", "2026-10-20 09:00:32.000", 250.0, False)
    assert [(row["turn"], row["started"], row["status"]) for row in found["turns"]] == [
        (1, "2026-10-20 09:00:01.100", "done"), (2, "2026-10-20 09:00:31.000", "done")]
    implement = next(row for row in found["attempts"] if row["node"] == "implement")
    assert (implement["started"], implement["outcome"], implement["tokens"]) == ("2026-10-20 09:00:01.000", "succeeded", 150)
    assert [(row["status"], row["ended"], row["duration_s"], row["tokens"]) for row in found["runs"]] == [
        ("succeeded", "2026-10-20 09:00:34.000", 102.0, 150)]  # 68 s until the process died, and 34 s of the retry


def test_a_trace_shorter_than_its_offset_is_read_again_from_its_start(runs_dir):
    path = traced_folder(runs_dir / REPO)
    export.export(of(path), Memory())
    shorter = "".join((path / "trace.jsonl").read_text().splitlines(keepends=True)[:6])  # the run and the attempts of tests
    (path / "trace.jsonl").write_text(shorter)
    store = Memory()
    assert export.export(of(path), store) == {"runs": 1, "attempts": 2}
    assert json.loads((path / "export.json").read_text())["offset"] == len(shorter.encode())


def test_exporting_twice_writes_nothing_the_second_time(runs_dir):
    path = traced_folder(runs_dir / REPO)
    store = Memory()
    export.export(of(path), store)
    kept = (path / "export.json").read_text()
    inserts = len(store.inserts)
    assert export.export(of(path), store) == {}
    assert len(store.inserts) == inserts and (path / "export.json").read_text() == kept


def test_tables_that_are_made_again_get_each_row_again(runs_dir):
    path = traced_folder(runs_dir / REPO)
    export.export(of(path), Memory("tables-1"))
    again = Memory("tables-2")  # after `docker compose down -v`, say
    assert export.export(of(path), again) == {"runs": 1, "attempts": 10, "turns": 3, "tool_calls": 4, "routes": 2}
    assert again.final() == {table: ordered(table, rows) for table, rows in TRACE_ROWS.items()}


@pytest.mark.parametrize("kept", ["{", "[]", '{"store": "tables-1", "file": "trace.jsonl", "offset": 99, "state": {}}'])
def test_a_damaged_export_json_counts_as_none(runs_dir, kept):
    path = traced_folder(runs_dir / REPO)
    (path / "export.json").write_text(kept)
    store = Memory("tables-1")
    export.export(of(path), store)
    assert store.final() == {table: ordered(table, rows) for table, rows in TRACE_ROWS.items()}


def test_the_export_writes_only_export_json_and_its_lock_and_works_while_a_process_drives_the_run(runs_dir):
    path = traced_folder(runs_dir / REPO)
    run = Run.at(path)
    with run.driving():  # as `wf run` holds it
        before = {file: file.read_bytes() for file in path.rglob("*") if file.is_file()}
        export.export(run, Memory())
        after = {file: file.read_bytes() for file in path.rglob("*") if file.is_file()}
    assert set(after) - set(before) == {path / "export.json", path / "export.lock"}
    assert all(after[file] == data for file, data in before.items())


def test_an_export_waits_while_another_export_of_the_run_holds_export_lock(runs_dir):
    path = traced_folder(runs_dir / REPO)
    other = os.open(path / "export.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(other, fcntl.LOCK_EX)
    store, done = Memory(), threading.Event()
    waiting = threading.Thread(target=lambda: (export.export(of(path), store), done.set()))
    waiting.start()
    try:
        assert not done.wait(0.3)
        assert store.inserts == [] and not (path / "export.json").exists()  # it read nothing yet
    finally:
        os.close(other)  # the other export ends
    waiting.join(10)
    assert done.is_set()
    assert store.final() == {table: ordered(table, rows) for table, rows in TRACE_ROWS.items()}


def test_each_pass_and_attempt_of_a_workflow_node_keeps_the_rows_of_its_sub_workflow(runs_dir):
    compiles = {"where": "build/", "node": "compile", "pass": 1, "attempt": 1, "harness": "codex", "model": "gpt-6.1-sol"}
    records = [r("10:00:00.000", "run", "run", action="started", workflow="deliver")]
    for number, (minute, tokens) in enumerate([("10:01", 300), ("10:02", 500)], 1):  # two passes of build
        inside = f"build#{number}.a1"
        records += [r(f"{minute}:00.000", "attempt", f"{inside}/compile#1.a1", compiles, started=at(f"{minute}:00.000")),
                    r(f"{minute}:09.000", "attempt_end", f"{inside}/compile#1.a1", compiles, started=at(f"{minute}:00.000"),
                      ended=at(f"{minute}:09.000"), duration_s=9.0, outcome="succeeded", usage=used(tokens, 0.0, tokens, 0, 0, 0, 0)),
                    r(f"{minute}:09.100", "routes", f"{inside}/compile#1", {"where": "build/", "node": "compile", "pass": 1},
                      routes=[], taken=0, to=["end"])]
    records.append(r("10:03:00.000", "run_stopped", "run", status="succeeded"))
    store = Memory()
    export.export(of(folder(runs_dir / REPO, TRACED, {"trace.jsonl": lines(records)})), store)
    found = store.final()
    assert [(row["node"], row["pass"], row["attempt"], row["tokens"]) for row in found["attempts"]] == [
        ("build#1.a1/compile", 1, 1, 300), ("build#2.a1/compile", 1, 1, 500)]
    assert [row["node"] for row in found["routes"]] == ["build#1.a1/compile", "build#2.a1/compile"]
    assert found["runs"][0]["tokens"] == 800  # as wf report counts both


# ---------- events.jsonl only ----------


def test_a_folder_with_only_events_gives_the_rows_that_its_events_say(runs_dir):
    store = Memory()
    written = export.export(of(evented_folder(runs_dir / REPO)), store)
    assert store.final() == {table: ordered(table, rows) for table, rows in EVENT_ROWS.items()}
    assert written == {"runs": 1, "attempts": 6, "tool_calls": 7, "routes": 2}


def test_a_folder_with_only_events_is_read_once(runs_dir):
    path = evented_folder(runs_dir / REPO)
    store = Memory()
    export.export(of(path), store)
    assert export.export(of(path), store) == {}


def test_a_run_without_run_finished_takes_its_status_from_status_json(runs_dir):
    path = folder(runs_dir / REPO, EVENTED, {"events.jsonl": lines(EVENTS[:16])}, status="failed")
    store = Memory()
    export.export(of(path), store)
    (run,) = store.final()["runs"]
    assert (run["status"], run["ended"], run["duration_s"]) == ("failed", "2026-10-19 21:15:30.000", 930.0)


def test_a_run_from_before_the_trace_that_no_process_drives_but_that_says_running_is_stopped(runs_dir):
    path = folder(runs_dir / REPO, EVENTED, {"events.jsonl": lines(EVENTS[:16])}, status="running")
    store = Memory()
    export.export(of(path), store)
    (run,) = store.final()["runs"]
    assert (run["status"], run["ended"], run["duration_s"]) == ("stopped", "2026-10-19 21:15:30.000", 930.0)


def test_an_item_that_succeeded_in_a_pass_that_failed_succeeded(runs_dir):
    events = [e("23:00:00", "node_started", "review", **{"pass": 1}, items=2),
              e("23:00:05", "agent_tool", "review", **{"pass": 1}, index=0, attempt=1, harness="cursor", tool="read", detail="a"),
              e("23:00:06", "agent_tool", "review", **{"pass": 1}, index=1, attempt=1, harness="cursor", tool="read", detail="b"),
              e("23:01:00", "attempt_failed", "review", **{"pass": 1}, index=1, harness="cursor", attempt=1,
                error="cursor: Connection failed repeatedly"),
              e("23:02:00", "node_failed", "review", **{"pass": 1}, error="item 1: cursor: Connection failed repeatedly",
                cost_usd=0.0, tokens=0)]  # on_item_error: fail
    store = Memory()
    export.export(of(folder(runs_dir / REPO, EVENTED, {"events.jsonl": lines(events)}, status="failed")), store)
    assert [(row["item"], row["outcome"]) for row in store.final()["attempts"]] == [(0, "succeeded"), (1, "failed")]


def test_the_event_that_fails_a_job_fails_its_last_attempt_when_no_event_of_the_attempt_says_how_it_ended(runs_dir):
    def read(clock: str, node: str, number: int = 1, **index) -> dict:
        return e(clock, "agent_tool", node, **{"pass": 1}, **index, attempt=number, harness="claude-code", tool="Read")

    events = [  # an exception in an attempt writes no attempt_failed
        e("23:00:00", "node_started", "plan", **{"pass": 1}), read("23:00:05", "plan"),
        e("23:01:00", "node_failed", "plan", **{"pass": 1}, error="the wf tool linear isn't set up"),
        e("23:10:00", "attempt_started", "plan", **{"pass": 1}, harness="claude-code", attempt=2),  # wf retry
        read("23:10:05", "plan", 2), e("23:11:00", "node_finished", "plan", **{"pass": 1}),
        e("23:11:00", "node_started", "check", **{"pass": 1}), read("23:11:05", "check"),
        e("23:12:00", "node_failed", "check", **{"pass": 1}, error="routes: unknown name x"),
        e("23:12:00", "node_started", "note", **{"pass": 1}), read("23:12:05", "note"),
        e("23:13:00", "node_failed", "note", **{"pass": 1}, error="can't write output.json: [Errno 28] No space left"),
        e("23:13:00", "node_started", "sign", **{"pass": 1}), read("23:13:05", "sign"),
        e("23:14:00", "node_failed", "sign", **{"pass": 1},
          error="workspace: can't sign the commit with your default key (commit.gpgsign is on): git commit failed: no agent"),
        e("23:14:00", "node_started", "redo", **{"pass": 1}), read("23:14:05", "redo"),
        e("23:15:00", "attempt_failed", "redo", **{"pass": 1}, harness="claude-code", attempt=1, error="timed out after 1m"),
        e("23:15:00", "attempt_started", "redo", **{"pass": 1}, harness="claude-code", attempt=2),
        e("23:15:01", "node_failed", "redo", **{"pass": 1}, error="workspace: git reset failed: fatal: index.lock exists"),
        e("23:20:00", "node_started", "patch", **{"pass": 1}, items=2),
        read("23:20:05", "patch", index=0), read("23:20:06", "patch", index=1),
        e("23:21:00", "item_failed", "patch", index=0, error="workspace: git commit --quiet failed: fatal: no space"),
        e("23:21:00", "item_failed", "patch", index=1, error="RuntimeError: the session closed twice"),  # on_item_error: continue
        e("23:21:00", "node_finished", "patch", **{"pass": 1}),
        e("23:30:00", "node_started", "fix", **{"pass": 1}, items=2),
        read("23:30:05", "fix", index=0), read("23:30:06", "fix", index=1),
        e("23:31:00", "node_failed", "fix", **{"pass": 1}, error="item 0: OSError: can't write session.json")]  # on_item_error: fail
    store = Memory()
    export.export(of(folder(runs_dir / REPO, EVENTED, {"events.jsonl": lines(events)}, status="failed")), store)
    assert [(row["node"], row["item"], row["attempt"], row["outcome"], row["error"]) for row in store.final()["attempts"]] == [
        ("check", -1, 1, "succeeded", ""),  # its routes failed after its job succeeded
        ("fix", 0, 1, "failed", "OSError: can't write session.json"),
        ("fix", 1, 1, "succeeded", ""),
        ("note", -1, 1, "succeeded", ""),  # its job failed after the attempt succeeded
        ("patch", 0, 1, "succeeded", ""),  # the commit of the changes of the attempt failed
        ("patch", 1, 1, "failed", "RuntimeError: the session closed twice"),
        ("plan", -1, 1, "failed", "the wf tool linear isn't set up"),
        ("plan", -1, 2, "succeeded", ""),
        ("redo", -1, 1, "timed_out", "timed out after 1m"),
        ("redo", -1, 2, "failed", "workspace: git reset failed: fatal: index.lock exists"),  # the reset starts the attempt
        ("sign", -1, 1, "succeeded", "")]


def test_the_spending_of_a_pass_in_a_sub_workflow_counts_once_in_its_run(runs_dir):
    events = [e("23:00:00", "node_started", "build", **{"pass": 1}), e("23:00:00", "node_started", "build/compile", **{"pass": 1}),
              e("23:00:05", "agent_tool", "build/compile", **{"pass": 1}, attempt=1, harness="codex", tool="exec", detail="make"),
              e("23:01:00", "node_finished", "build/compile", **{"pass": 1}, cost_usd=0.25, tokens=500),
              e("23:01:00", "node_finished", "build", **{"pass": 1}, cost_usd=0.25, tokens=500),  # its sub-workflow's
              e("23:01:00", "run_finished", status="succeeded")]
    store = Memory()
    export.export(of(folder(runs_dir / REPO, EVENTED, {"events.jsonl": lines(events)})), store)
    found = store.final()
    assert [(row["node"], row["tokens"], row["cost_usd"]) for row in found["attempts"]] == [("build/compile", 500, 0.25)]
    assert (found["runs"][0]["tokens"], found["runs"][0]["cost_usd"]) == (500, 0.25)


def test_a_trace_that_starts_with_a_retry_of_a_run_from_before_the_trace_keeps_the_events_before_it(runs_dir):
    review = {"node": "review", "pass": 1, "index": 0, "attempt": 1, "harness": "codex", "model": "gpt-6.1-sol"}
    retried = [{"time": "2026-10-20T09:00:00.000+02:00", "kind": "run", "id": "run", "action": "retried", "workflow": "deliver"},
               {"time": "2026-10-20T09:00:01.000+02:00", "kind": "attempt", "id": "review#1[0].a1", **review,
                "started": "2026-10-20T09:00:01.000+02:00"},
               {"time": "2026-10-20T09:00:31.000+02:00", "kind": "attempt_end", "id": "review#1[0].a1", **review,
                "started": "2026-10-20T09:00:01.000+02:00", "ended": "2026-10-20T09:00:31.000+02:00", "duration_s": 30.0,
                "outcome": "succeeded", "usage": used(400, 0.1, 100, 100, 200, 0, 0)},
               {"time": "2026-10-20T09:01:00.000+02:00", "kind": "run_stopped", "id": "run", "status": "succeeded"}]
    override = {"time": "2026-10-20T08:59:58+02:00", "event": "override", "node": "review", "index": 0, "harness": "codex",
                "model": "gpt-6.1-sol", "effort": None}  # `wf retry --model` writes it before the trace starts
    later = [{"time": "2026-10-20T09:00:00+02:00", "event": "node_started", "node": "review", "pass": 1, "items": 1},
             {"time": "2026-10-20T09:00:31+02:00", "event": "node_finished", "node": "review", "pass": 1, "cost_usd": 0.1,
              "tokens": 400},
             {"time": "2026-10-20T09:01:00+02:00", "event": "run_finished", "status": "succeeded"}]  # today's wf writes them too
    path = folder(runs_dir / REPO, EVENTED, {"events.jsonl": lines(EVENTS[:10] + [override] + later),
                                             "trace.jsonl": lines(retried)})
    store = Memory()
    export.export(of(path), store)
    found = store.final()
    assert [(row["node"], row["pass"], row["item"], row["tokens"]) for row in found["attempts"]] == [
        ("implement", 1, -1, 3000), ("review", 1, 0, 400)]
    assert [row["call_id"] for row in found["tool_calls"]] == ["e1", "e2"]
    assert [(row["node"], row["to"]) for row in found["routes"]] == [("implement", ["review"])]
    (run,) = found["runs"]
    assert (run["started"], run["ended"], run["duration_s"], run["tokens"], run["cost_usd"], run["source"]) == (
        "2026-10-19 21:00:00.000", "2026-10-20 07:01:00.000", 660.0, 3400, 1.6, "trace")  # 600 s before the trace, 60 s in it


# ---------- errors ----------


def test_a_folder_without_a_trace_or_events_is_unreadable(runs_dir):
    path = folder(runs_dir / REPO, TRACED, {})
    with pytest.raises(export.UnreadableFolder, match="has neither trace.jsonl nor events.jsonl"):
        export.export(of(path), Memory())
    with pytest.raises(export.UnreadableFolder, match="isn't a folder"):
        export.export(of(path / "gone"), Memory())


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads and writes any file")
def test_a_trace_that_cant_be_read_or_an_offset_that_cant_be_kept_is_an_unreadable_folder(runs_dir):
    path = traced_folder(runs_dir / REPO)
    (path / "trace.jsonl").chmod(0)
    try:
        with pytest.raises(export.UnreadableFolder, match="can't read .*trace.jsonl"):
            export.export(of(path), Memory())
    finally:
        (path / "trace.jsonl").chmod(0o644)
    path.chmod(0)  # wf can't look into the folder
    try:
        with pytest.raises(export.UnreadableFolder, match="can't read .*trace.jsonl: Permission denied"):
            export.export(of(path), Memory())
    finally:
        path.chmod(0o755)
    (path / "export.lock").unlink()
    path.chmod(0o555)  # the lock can't be made
    store = Memory()
    try:
        with pytest.raises(export.UnreadableFolder, match="can't open .*export.lock"):
            export.export(of(path), store)
        assert store.inserts == []
    finally:
        path.chmod(0o755)
    (path / "export.lock").touch()
    path.chmod(0o555)  # the lock is there, but export.json can't be written
    try:
        with pytest.raises(export.UnreadableFolder, match="can't keep the offset in .*export.json"):
            export.export(of(path), store)
    finally:
        path.chmod(0o755)
    assert not (path / "export.json").exists()
    assert store.final() == {table: ordered(table, rows) for table, rows in TRACE_ROWS.items()}  # written again next time


@pytest.mark.parametrize("error", [export.StoreDown("down"), export.SchemaMismatch("no column")])
def test_a_store_that_refuses_the_rows_leaves_the_offset_where_it_was(runs_dir, error):
    path = traced_folder(runs_dir / REPO)

    class Refusing(Memory):
        def insert(self, table, rows):
            if table == "turns":
                raise error
            super().insert(table, rows)

    with pytest.raises(type(error)):
        export.export(of(path), Refusing())
    assert not (path / "export.json").exists()
    store = Memory()
    export.export(of(path), store)
    assert store.final() == {table: ordered(table, rows) for table, rows in TRACE_ROWS.items()}


@pytest.mark.parametrize("text, kind", [
    ("", ""),
    ("cursor is unavailable: cursor: Error: RetriableError: [resource_exhausted] Error", "rate_limit"),
    ("Claude usage limit reached (resets at 5pm)", "rate_limit"),
    ("codex: turn/start: rateLimitExceeded", "rate_limit"),
    ("cursor: Error: RetriableError: [canceled] http/2 stream closed with error code CANCEL (0x8)", "dropped_connection"),
    ("cursor: Error: RetriableError: [aborted] read ECONNRESET", "dropped_connection"),
    ("cursor: Error: RetriableError: Connection stalled", "dropped_connection"),
    ("Claude Code: API Error: Can't reach the API server, check your internet or DNS (ENOTFOUND)", "dropped_connection"),
    ("timed out after 8m", "timeout"),
    ("a call inside the harness timed out", "timeout"),
    ("`cursor-agent --list-models` failed (is it logged in? `cursor-agent login`)", "auth"),
    ("Claude Code: Invalid API key", "auth"),
    ("the agent never made a valid submit_result call", "no_result"),
    ("exited with code 1", "other"),
])
def test_an_error_gets_its_class_from_its_text(text, kind):
    assert export.error_class(text) == kind


def test_the_check_names_each_way_that_a_table_differs_from_the_schema():
    wanted = export.schema()
    tables = {name: {"engine": table["engine"], "sorting_key": table["key"], "uuid": name} for name, table in wanted.items()}
    columns = {name: dict(table["columns"]) for name, table in wanted.items()}
    assert export.differences(wanted, tables, columns, "wf") == []
    del tables["routes"], columns["turns"]["reasoning_tokens"], columns["metric_points"]["harness"]
    columns["runs"]["tokens"] = "UInt32"
    tables["attempts"]["sorting_key"] = "run_id"
    assert export.differences(wanted, tables, columns, "wf") == [  # metric_points isn't written yet, so it isn't checked
        "wf.runs.tokens is UInt32, not UInt64",
        "wf.attempts is a ReplacingMergeTree on (run_id), not a ReplacingMergeTree on (run_id, node, pass, item, attempt)",
        "wf.turns has no column reasoning_tokens",
        "there's no table wf.routes",
    ]


# ---------- ClickHouse's HTTP interface ----------


def metrics_url(address: str, database: str = "wf") -> str:
    """metrics.url for the ClickHouse at `address` (host:port), with the user and the password wf."""
    return f"http://wf:wf@{address}/{database}"


def store_at(address: str, database: str = "wf") -> export.Store:
    return export.Store(config.Connection.parse(metrics_url(address, database)))


def metrics_on(wf_home: Path, url: str) -> None:
    """A config.toml whose [metrics] turns the export on, for the ClickHouse of `url`."""
    file = wf_home / "config.toml"
    file.write_text(f'[metrics]\nenabled = true\nurl = "{url}"\n')
    file.chmod(0o600)  # wf refuses a file that sets metrics.url while other users can open it


@pytest.fixture
def answering():
    """A server on this machine that answers each request as `answer` says, and keeps each request: its address."""
    seen, answer = [], {"status": 200, "body": b"", "code": None}

    class ClickHouse(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            seen.append({"path": self.path, "headers": dict(self.headers),
                         "body": self.rfile.read(int(self.headers.get("Content-Length", 0)))})
            self.send_response(answer["status"])
            if answer["code"]:
                self.send_header("X-ClickHouse-Exception-Code", answer["code"])
            self.send_header("Content-Length", str(len(answer["body"])))
            self.end_headers()
            self.wfile.write(answer["body"])

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), ClickHouse)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"127.0.0.1:{server.server_address[1]}", seen, answer
    server.shutdown()
    server.server_close()


def test_the_store_signs_in_with_headers_as_the_user_of_the_url_and_writes_in_its_database(answering):
    address, seen, _ = answering
    store = export.Store(config.Connection.parse(f"http://me:p%40ss@{address}/runs%2Fof%2Fwf"))
    store.insert("routes", [{"run_id": "s", "taken": -1}])
    (sent,) = seen
    assert (sent["headers"]["X-ClickHouse-User"], sent["headers"]["X-ClickHouse-Key"]) == ("me", "p@ss")
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(sent["path"]).query)
    assert sent["path"].startswith("/?")
    assert query["query"] == ["INSERT INTO routes FORMAT JSONEachRow"]
    assert query["database"] == ["runs/of/wf"]  # the database of the URL, where the table names go
    assert query["input_format_skip_unknown_fields"] == ["0"]  # a column that the table lacks fails the insert
    assert [json.loads(line) for line in sent["body"].splitlines()] == [{"run_id": "s", "taken": -1}]
    assert store.where == f"http://{address}/runs/of/wf"  # for messages: no user, no password


@pytest.mark.parametrize("status, code, error, said", [
    (404, "60", export.SchemaMismatch, "refused the rows: Code: 60"),
    (500, "16", export.SchemaMismatch, "refused the rows: Code: 16"),
    (403, "516", export.StoreDown, r"refused a request \(403\): Code: 516"),
    (403, None, export.StoreDown, r"refused a request \(403\): Code: 0"),
    (503, "241", export.StoreDown, r"refused a request \(503\)"),
])
def test_what_clickhouse_answers_decides_the_class_of_the_error(answering, status, code, error, said):
    address, _, answer = answering
    answer.update(status=status, code=code, body=f"Code: {code or 0}. DB::Exception: no.\n".encode())
    with pytest.raises(error, match=said):
        store_at(address).insert("runs", [{"run_id": "r"}])


@pytest.mark.parametrize("body", [b"<html><body>Welcome</body></html>\n", b'{"name": "runs"}\n', b"[1, 2]\n"])
def test_an_address_that_answers_but_not_as_clickhouse_does_is_store_down(answering, body):
    address, _, answer = answering
    answer.update(body=body)
    with pytest.raises(export.StoreDown, match=f"http://{address}/wf answers, but not as ClickHouse does"):
        store_at(address).prepare()


def test_prepare_makes_the_database_and_the_tables_that_are_missing_and_names_them_by_their_ids():
    made: dict[str, str] = {}  # table -> its id, as ClickHouse gives each table that it makes one
    databases: list[str] = []

    def answers(query: str) -> bytes:
        if "system.databases" in query:
            return lines([{"name": name} for name in databases]).encode()
        if query.startswith("CREATE DATABASE"):
            databases.append(query)
            return b""
        if "system.tables" in query:
            return lines([{"name": name, "uuid": made[name], "engine": table["engine"], "sorting_key": table["key"]}
                          for name, table in export.schema().items() if name in made]).encode()
        if "system.columns" in query:
            return lines([{"table": name, "name": column, "type": kind} for name, table in export.schema().items()
                          if name in made for column, kind in table["columns"].items()]).encode()
        if made_table := next((name for name in export.schema() if f"EXISTS {name} (" in query), None):
            made[made_table] = str(uuid.uuid4())
        return b""

    class ClickHouse(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            body = answers(self.rfile.read(int(self.headers["Content-Length"])).decode())
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), ClickHouse)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        store = store_at(f"127.0.0.1:{server.server_address[1]}", "my`db")
        store.prepare()
        assert databases == ["CREATE DATABASE IF NOT EXISTS `my\\`db`"]
        assert sorted(made) == sorted(export.schema())
        first = store.identity
        store.prepare()  # nothing missing: nothing made, and the same identity
        assert store.identity == first and len(made) == 8
        del made["attempts"]
        store.prepare()  # made again, with a new id
        assert store.identity not in ("", first) and len(databases) == 1
    finally:
        server.shutdown()
        server.server_close()


def free_port() -> int:
    """A port on 127.0.0.1 that no program listens on now."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_clickhouse_that_doesnt_answer_is_down():
    port = free_port()
    with pytest.raises(export.StoreDown, match=f"ClickHouse at http://127.0.0.1:{port}/wf doesn't answer"):
        store_at(f"127.0.0.1:{port}").prepare()


# ---------- wf metrics export ----------


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    subprocess.run(["git", "init", "-q"], check=True)
    monkeypatch.setattr(runner, "interactive", lambda: False)
    return tmp_path


@pytest.fixture
def stores(monkeypatch, wf_home):
    """The stores that wf metrics export opens, each kept in memory, with the export turned on."""
    opened = []

    class Opened(Memory):
        def __init__(self, url):
            super().__init__()
            self.url = url
            opened.append(self)

    monkeypatch.setattr(export, "Store", Opened)
    metrics_on(wf_home, metrics_url("127.0.0.1:8123"))
    return opened


def test_wf_metrics_export_writes_each_run_of_the_repo_and_then_nothing_new(repo, stores, capsys):
    parent = runs.runs_folder(repo)
    traced_folder(parent), evented_folder(parent)
    os.utime(parent / EVENTED / "workflow" / "manifest.json", (0, 0))  # the older run comes first
    assert cli.main(["metrics", "export"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        f"{EVENTED}: 1 run, 6 attempts, 7 tool calls, 2 routes",
        f"{TRACED}: 1 run, 10 attempts, 3 turns, 4 tool calls, 2 routes"]
    assert str(stores[0].url) == "http://wf:***@127.0.0.1:8123/wf"
    found = stores[0].final()
    (audit,) = found.pop("export_runs")
    assert (audit["runs_read"], audit["rows_written"], audit["error_class"]) == (2, 36, "")
    assert {row["repo_key"] for row in found.pop("runs")} == {runs.repo_key(repo.resolve())}  # from the repo's path
    assert found == {table: ordered(table, TRACE_ROWS[table] + EVENT_ROWS.get(table, [])) for table in TRACE_ROWS if table != "runs"}
    assert cli.main(["metrics", "export", TRACED]) == 0
    assert capsys.readouterr().out == f"{TRACED}: nothing new\n"


def test_wf_metrics_export_all_repos_writes_the_runs_of_each_repo_in_the_runs_folder(repo, stores, runs_dir, capsys):
    traced_folder(runs_dir / REPO), evented_folder(runs_dir / "other-0123456789ab")
    assert cli.main(["metrics", "export", "--all-repos", "--runs-dir", str(runs_dir)]) == 0
    assert sorted(line.split(":")[0] for line in capsys.readouterr().out.splitlines()) == [EVENTED, TRACED]
    assert {row["repo_key"] for row in stores[0].final()["runs"]} == {REPO, "other-0123456789ab"}


@pytest.mark.parametrize("args, said", [
    (["--all-repos"], "--all-repos needs a runs folder"),
    (["--all-repos", "--runs-dir", "runs", TRACED], "name runs, or give --all-repos, but not both"),
])
def test_wf_metrics_export_refuses_what_it_cant_do_before_it_opens_clickhouse(repo, stores, capsys, args, said):
    assert cli.main(["metrics", "export", *args]) == 1
    assert said in capsys.readouterr().err
    assert stores == []


def test_a_missing_named_run_records_a_failed_export(repo, stores, capsys):
    missing = "20261020-0000-missing-zz99"
    assert cli.main(["metrics", "export", missing]) == 1
    assert f"no run `{missing}`" in capsys.readouterr().err
    (audit,) = stores[0].final()["export_runs"]
    assert (audit["error_class"], audit["runs_read"], audit["rows_written"]) == ("UnreadableFolder", 0, 0)


@pytest.mark.parametrize("text", ["", f'[metrics]\nenabled = false\nurl = "{metrics_url("127.0.0.1:8123")}"\n'])
def test_wf_metrics_export_with_metrics_off_says_how_to_turn_them_on(repo, wf_home, stores, capsys, text):
    (wf_home / "config.toml").write_text(text)
    assert cli.main(["metrics", "export"]) == 1
    assert ("wf metrics export: metrics.enabled is false, so wf exports nothing. Under [metrics] in "
            f"{wf_home / 'config.toml'}, set enabled = true, and url to a connection string") in capsys.readouterr().err
    assert stores == []


def test_wf_metrics_export_stops_when_clickhouse_is_down_and_keeps_no_offset(repo, wf_home, capsys):
    path = traced_folder(runs.runs_folder(repo))
    port = free_port()
    metrics_on(wf_home, metrics_url(f"127.0.0.1:{port}"))
    assert cli.main(["metrics", "export"]) == 1
    assert f"wf metrics export: ClickHouse at http://127.0.0.1:{port}/wf doesn't answer" in capsys.readouterr().err
    assert not (path / "export.json").exists()


def test_wf_metrics_export_stops_at_the_first_run_whose_rows_clickhouse_refuses(repo, monkeypatch, wf_home, capsys):
    class Refusing(Memory):
        def __init__(self, url):
            super().__init__()

        def insert(self, table, rows):
            raise export.StoreDown("ClickHouse at http://127.0.0.1:8123 doesn't answer: gone")

    monkeypatch.setattr(export, "Store", Refusing)
    metrics_on(wf_home, metrics_url("127.0.0.1:8123"))
    parent = runs.runs_folder(repo)
    traced_folder(parent), evented_folder(parent)
    os.utime(parent / EVENTED / "workflow" / "manifest.json", (0, 0))  # the older run comes first
    assert cli.main(["metrics", "export"]) == 1
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == ("", f"wf metrics export: run {EVENTED}: ClickHouse at http://127.0.0.1:8123 "
                                                "doesn't answer: gone\n")
    assert not (parent / TRACED / "export.lock").exists()  # the next run wasn't tried


def test_a_run_folder_that_cant_be_read_doesnt_stop_the_others(repo, stores, capsys):
    parent = runs.runs_folder(repo)
    traced_folder(parent), folder(parent, EVENTED, {})
    assert cli.main(["metrics", "export"]) == 1
    captured = capsys.readouterr()
    assert f"wf metrics export: run {EVENTED}: " in captured.err and "has neither trace.jsonl nor events.jsonl" in captured.err
    assert captured.out == f"{TRACED}: 1 run, 10 attempts, 3 turns, 4 tool calls, 2 routes\n"


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads and writes any file")
def test_a_run_folder_that_wf_cant_look_into_is_unreadable_also_when_wf_metrics_export_lists_the_runs(repo, stores, runs_dir,
                                                                                                         capsys):
    parent = runs.runs_folder(repo)
    traced_folder(parent), traced_folder(runs_dir / REPO)
    hidden, other = evented_folder(parent), evented_folder(runs_dir / "other-0123456789ab")
    exported = f"{TRACED}: 1 run, 10 attempts, 3 turns, 4 tool calls, 2 routes\n"
    all_repos = ["metrics", "export", "--all-repos", "--runs-dir", str(runs_dir)]
    hidden.chmod(0), other.chmod(0)
    try:
        for args, out in [([], exported), ([EVENTED, TRACED], f"{TRACED}: nothing new\n")]:
            assert cli.main(["metrics", "export", *args]) == 1
            captured = capsys.readouterr()
            assert (captured.out, captured.err) == (
                out, f"wf metrics export: run {EVENTED}: can't read {hidden / 'trace.jsonl'}: Permission denied\n")
        assert cli.main(all_repos) == 1
        captured = capsys.readouterr()
        assert (captured.out, captured.err) == (
            exported, f"wf metrics export: run {EVENTED}: can't read {other / 'trace.jsonl'}: Permission denied\n")
        (runs_dir / REPO).chmod(0)  # wf can't list the runs of a repo: it can't name them
        assert cli.main(all_repos) == 1
        assert capsys.readouterr().err == f"wf metrics export: can't list the runs in {runs_dir / REPO}: Permission denied\n"
    finally:
        for path in (hidden, other, runs_dir / REPO):
            path.chmod(0o755)


FLOW = """
version: 1
name: flow
defaults: {harness: claude-code, harnesses: {claude-code: {model: opus, effort: high}}}
start: {input: {}}
nodes:
  plan:
    needs: [start]
    instruction: "Plan it."
    output: {steps: {type: array, items: {type: string}, maxItems: 5}}
  each:
    needs: [plan]
    for_each: plan.steps
    command: 'test "$STEP" != b'
    env: {STEP: "{{ item }}"}
    on_item_error: continue
    routes:
      - {when: "each | length == 2", to: end}
      - {to: plan, max: 1}
      - {to: end}
end: {output: {}}
"""


def test_the_records_of_a_real_run_give_a_row_for_each_attempt_turn_tool_call_and_route(repo, monkeypatch):
    fake = Fake("claude-code", talk(tokens=40, cost=0.004), submit({"steps": ["a", "b", "c"]}, tokens=60, cost=0.006))
    monkeypatch.setitem(harnesses.FACTORIES, "claude-code", fake.factory)
    Path("flow.yaml").write_text(dedent(FLOW))
    assert cli.main(["run", "flow.yaml"]) == 0
    (run,) = runs.all_runs(repo)
    records = [json.loads(line) for line in (run.dir / "trace.jsonl").read_text().splitlines()]
    store = Memory()
    export.export(run, store)
    found = store.final()

    ends = [r for r in records if r["kind"] == "attempt_end"]
    assert [(a["node"], a["item"], a["attempt"], a["kind"], a["outcome"], a["tokens"]) for a in found["attempts"]] == sorted(
        (r["node"], r.get("index", -1), r["attempt"], "command" if "command" in r else "agent", r["outcome"],
         (r.get("usage") or {}).get("tokens", 0)) for r in ends)
    assert [(t["turn"], t["status"], t["tokens"]) for t in found["turns"]] == [(1, "done", 40), (2, "done", 60)]
    assert [(c["call_id"], c["tool"], c["harness"], c["model"], c["is_error"]) for c in found["tool_calls"]] == [
        ("t2.c1", "Read", "claude-code", "opus", False)]
    assert [(r["node"], r["taken"], r["to"]) for r in found["routes"]] == [("each", 0, ["end"])]
    for attempt in found["attempts"]:
        if attempt["kind"] == "agent":
            assert attempt["measure_version"] == 2
            assert attempt["last_progress_at"] is not None
            wall = export._seconds(attempt["started"], attempt["ended"])
            assert sum(attempt[key] for key in ("startup_s", "asleep_s", "tool_s", "active_s", "idle_s")) == pytest.approx(wall)
    (row,) = found["runs"]
    assert (row["workflow"], row["status"], row["tokens"], row["cost_usd"], row["source"]) == ("flow", "succeeded", 100, 0.01, "trace")
    assert row["repo_key"] == runs.repo_key(repo.resolve())


# ---------- in a real ClickHouse ----------


IMAGE = "clickhouse/clickhouse-server:25.8.33.6"  # the image of the example in docs/metrics


def docker(*args: str, timeout: float = 600) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)


@pytest.fixture(scope="module")
def clickhouse():
    """The address (host:port) of a new ClickHouse in Docker, with the user and the password wf."""
    try:
        if shutil.which("docker") is None or docker("info", timeout=30).returncode != 0:
            pytest.skip("needs Docker, and a Docker daemon that answers")
        if docker("image", "inspect", IMAGE).returncode != 0 and docker("pull", IMAGE).returncode != 0:
            pytest.skip(f"needs the image {IMAGE}, and Docker can't pull it")
    except (OSError, subprocess.TimeoutExpired):
        pytest.skip("needs Docker, and a Docker daemon that answers")
    name = f"wf-export-test-{uuid.uuid4().hex[:8]}"
    started = docker("run", "-d", "--rm", "--name", name, "-p", "127.0.0.1::8123", "-e", "CLICKHOUSE_USER=wf",
                     "-e", "CLICKHOUSE_PASSWORD=wf", "--ulimit", "nofile=262144:262144", IMAGE)
    assert started.returncode == 0, started.stderr
    try:
        address = docker("port", name, "8123").stdout.split()[0]
        deadline = time.monotonic() + 60
        while True:
            try:
                query(address, "SELECT 1")
                break
            except (urllib.error.URLError, ConnectionError):
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.5)
        yield address
    finally:
        docker("rm", "-f", name, timeout=120)


def query(address: str, sql: str) -> str:
    request = urllib.request.Request(f"http://{address}/?output_format_json_quote_64bit_integers=0", data=sql.encode(),
                                     headers={"X-ClickHouse-User": "wf", "X-ClickHouse-Key": "wf"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode()


def rows_of(address: str, table: str, final: bool = True) -> list[dict]:
    found = query(address, f"SELECT * EXCEPT loaded_at FROM wf.{table} {'FINAL' if final else ''} FORMAT JSONEachRow")
    return ordered(table, [json.loads(line) for line in found.splitlines()])



def panel_nodes(address: str, title: str) -> list[list[str]]:
    """The workflow and node of each row of a panel of the example dashboard, over all time and each workflow."""
    dashboard = json.loads((Path(__file__).parent.parent / "docs/metrics/grafana/wf-runs.json").read_text(encoding="utf-8"))
    (panel,) = [panel for panel in dashboard["panels"] if panel["title"] == title]
    sql = re.sub(r"\$__timeFilter\(\w+\)|\$__conditionalAll\(.*?, \$workflow\)", "1", panel["targets"][0]["rawSql"])
    return sorted([row["workflow"], row["node"]] for row in map(json.loads, query(address, f"{sql} FORMAT JSONEachRow").splitlines()))

@pytest.fixture
def empty(clickhouse, wf_home):
    """The ClickHouse without the database wf, as on a new volume, and metrics.url set to it."""
    query(clickhouse, "DROP DATABASE IF EXISTS wf SYNC")
    metrics_on(wf_home, metrics_url(clickhouse))
    return clickhouse


@pytest.mark.docker
def test_a_backfill_into_clickhouse_gives_the_expected_rows_and_a_second_export_changes_nothing(empty, runs_dir, capsys):
    traced_folder(runs_dir / REPO), evented_folder(runs_dir / REPO)
    expected = {table: ordered(table, TRACE_ROWS[table] + EVENT_ROWS.get(table, [])) for table in TRACE_ROWS}
    store_at(empty).prepare()
    for table in export.WRITTEN:
        query(empty, f"SYSTEM STOP MERGES wf.{table}")  # so that a row that a later one replaces stays until FINAL
    backfill = ["metrics", "export", "--all-repos", "--runs-dir", str(runs_dir)]
    assert cli.main(backfill) == 0
    assert sorted(capsys.readouterr().out.splitlines()) == [
        f"{EVENTED}: 1 run, 6 attempts, 7 tool calls, 2 routes",
        f"{TRACED}: 1 run, 10 attempts, 3 turns, 4 tool calls, 2 routes"]
    for table in export.WRITTEN:
        assert rows_of(empty, table) == expected[table], table
    written = {table: len(rows_of(empty, table, final=False)) for table in export.WRITTEN}
    assert written == {"runs": 2, "attempts": 16, "turns": 3, "tool_calls": 11, "routes": 4}  # each row once
    named = "replaceRegexpAll(node, '#[0-9]+|[.]a[0-9]+', '')"  # the name that events use
    assert query(empty, f"SELECT DISTINCT {named} AS name FROM wf.attempts ORDER BY name").split() == [
        "build", "build/compile", "implement", "review", "tests"]
    assert query(empty, f"SELECT {named} FROM (SELECT 'build#2[3].a1/implement' AS node)") == "build[3]/implement\n"
    assert panel_nodes(empty, "Node duration p50, p95, max") == sorted(list(json.loads(line).values()) for line in query(
        empty, f"SELECT DISTINCT workflow, {named} AS node FROM wf.attempts FINAL WHERE duration_s IS NOT NULL "
               "FORMAT JSONEachRow").splitlines())

    assert cli.main(backfill) == 0
    assert sorted(capsys.readouterr().out.splitlines()) == [f"{EVENTED}: nothing new", f"{TRACED}: nothing new"]
    assert {table: len(rows_of(empty, table, final=False)) for table in export.WRITTEN} == written

    for kept in runs_dir.glob(f"{REPO}/*/export.json"):  # an export from the start of each file
        kept.unlink()
    assert cli.main(backfill) == 0
    assert {table: len(rows_of(empty, table, final=False)) for table in export.WRITTEN} == {
        table: 2 * count for table, count in written.items()}
    for table in export.WRITTEN:
        assert rows_of(empty, table) == expected[table], table


@pytest.mark.docker
def test_clickhouse_with_a_table_that_differs_stops_the_export_until_the_table_is_made_again(empty, runs_dir, capsys):
    path = traced_folder(runs_dir / REPO)
    store_at(empty).prepare()
    query(empty, "ALTER TABLE wf.attempts DROP COLUMN reasoning_tokens")
    backfill = ["metrics", "export", "--all-repos", "--runs-dir", str(runs_dir)]
    assert cli.main(backfill) == 1
    assert "differ from clickhouse.sql: wf.attempts has no column reasoning_tokens" in capsys.readouterr().err
    assert not (path / "export.json").exists()
    assert query(empty, "SELECT count() FROM wf.runs") == "0\n"
    query(empty, "DROP TABLE wf.attempts SYNC")
    assert cli.main(backfill) == 0
    assert rows_of(empty, "attempts") == ordered("attempts", TRACE_ROWS["attempts"])
    query(empty, "DROP DATABASE wf SYNC")  # as `docker compose down -v` leaves it
    assert cli.main(backfill) == 0
    assert capsys.readouterr().out.splitlines()[-1] == f"{TRACED}: 1 run, 10 attempts, 3 turns, 4 tool calls, 2 routes"
    assert rows_of(empty, "runs") == TRACE_ROWS["runs"]


@pytest.mark.docker
def test_the_export_makes_its_database_and_tables_where_the_url_says(clickhouse, wf_home, runs_dir, capsys):
    query(clickhouse, "DROP DATABASE IF EXISTS `runs of wf` SYNC")
    query(clickhouse, "DROP DATABASE IF EXISTS wf SYNC")
    traced_folder(runs_dir / REPO)
    metrics_on(wf_home, metrics_url(clickhouse, "runs%20of%20wf"))
    assert cli.main(["metrics", "export", "--all-repos", "--runs-dir", str(runs_dir)]) == 0
    assert capsys.readouterr().out == f"{TRACED}: 1 run, 10 attempts, 3 turns, 4 tool calls, 2 routes\n"
    made = query(clickhouse, "SELECT name FROM system.tables WHERE database = 'runs of wf' ORDER BY name").split()
    assert made == sorted([*export.schema(), "attempt_times"])
    assert query(clickhouse, "SELECT count() FROM `runs of wf`.runs") == "1\n"
    assert query(clickhouse, "SELECT count() FROM system.databases WHERE name = 'wf'") == "0\n"


@pytest.mark.docker
def test_prepare_adds_version_one_to_the_old_schema_and_keeps_old_rows(empty):
    store = store_at(empty)
    query(empty, "CREATE DATABASE wf")
    old = export.SCHEMA.read_text().split("CREATE TABLE IF NOT EXISTS schema_version")[0]
    old = "\n".join(line for line in old.splitlines()
                    if not any(f"{name} " in line for name in export.PROGRESS_COLUMNS))
    for statement in export._statements(old):
        query(empty, statement.replace("EXISTS ", "EXISTS wf.", 1))
    store.insert("runs", TRACE_ROWS["runs"])
    rows = [{key: value for key, value in row.items() if key not in export.PROGRESS_COLUMNS}
            for row in TRACE_ROWS["attempts"]]
    store.insert("attempts", rows)
    before = query(empty, "SELECT count() FROM wf.attempts FINAL")
    store.prepare()
    assert query(empty, "SELECT max(version) FROM wf.schema_version") == "1\n"
    assert query(empty, "EXISTS TABLE wf.export_runs") == "1\n"
    assert query(empty, "SELECT count() FROM wf.attempts FINAL") == before
    assert query(empty, "SELECT count() FROM wf.attempts FINAL WHERE duration_basis IS NOT NULL") == "0\n"
    expected = query(empty, "SELECT count() FROM wf.attempts FINAL WHERE kind IN ('agent', 'command') "
                           "AND ended IS NOT NULL AND duration_s IS NOT NULL AND outcome != 'cancelled'")
    assert query(empty, "SELECT count() FROM wf.attempt_times") == expected
    first = store.identity
    store.prepare()
    assert store.identity == first
    assert query(empty, "SELECT count() FROM wf.schema_version") == "1\n"
    excluded = dict(TRACE_ROWS["attempts"][-1], node="wall-clock", duration_basis="wall", awake_s=None)
    store.insert("attempts", [excluded, dict(excluded, node="cancelled", duration_basis="monotonic", outcome="cancelled")])
    assert query(empty, "SELECT count() FROM wf.attempt_times") == expected


@pytest.mark.docker
def test_empty_exports_and_three_failures_keep_the_last_success(empty, runs_dir, monkeypatch, capsys):
    traced_folder(runs_dir / REPO)
    args = ["metrics", "export", "--all-repos", "--runs-dir", str(runs_dir)]
    assert cli.main(args) == 0
    assert cli.main(args) == 0
    audits = rows_of(empty, "export_runs")
    assert len(audits) == 2
    assert (audits[-1]["runs_read"], audits[-1]["rows_written"], audits[-1]["error_class"]) == (1, 0, "")
    successful = query(empty, "SELECT maxIfOrNull(finished_at, error_class = '') FROM wf.export_runs FINAL")
    success = datetime.fromisoformat(successful.strip())
    stamps = []

    def later():  # each failure starts and ends a minute after the one before
        stamps.append(success + timedelta(minutes=len(stamps) + 1))
        return stamps[-1].strftime("%Y-%m-%d %H:%M:%S.%f")

    monkeypatch.setattr(export, "export_time", later)
    for error in (export.StoreDown, export.SchemaMismatch, export.UnreadableFolder):
        def fail(*args, error=error):
            raise error("test failure")
        monkeypatch.setattr(export, "export", fail)
        assert cli.main(args) == 1
    audits = rows_of(empty, "export_runs")
    assert [row["error_class"] for row in audits] == ["", "", "StoreDown", "SchemaMismatch", "UnreadableFolder"]
    assert query(empty, "SELECT maxIfOrNull(finished_at, error_class = '') FROM wf.export_runs FINAL") == successful
    dashboard = json.loads((Path(__file__).parent.parent / "docs/metrics/grafana/wf-runs.json").read_text())
    now = (success + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S.%f")
    ages = {"Exporter heartbeat": 3600 - 60 * len(stamps), "Last successful export": 3600}
    for panel in dashboard["panels"]:
        if panel["title"] not in {"Time per node", "Data age", "Exporter heartbeat", "Last successful export", "Failed exports"}:
            continue
        sql = panel["targets"][0]["rawSql"].replace("$__timeFilter(started)", "1")
        sql = re.sub(r"\$__conditionalAll\([^\n]+, \$(?:workflow|run|node)\)", "1", sql)
        sql = sql.replace("now64(6)", f"toDateTime64('{now}', 6, 'UTC')")
        values = [json.loads(line) for line in query(empty, f"{sql} FORMAT JSONEachRow").splitlines()]
        assert values, panel["title"]
        if panel["title"] == "Failed exports":
            assert values == [{"failed": 3}]
        if panel["title"] in ages:
            assert values == [{"age_s": ages[panel["title"]]}], panel["title"]


def test_export_keeps_the_clock_basis_and_sleep_of_a_new_attempt(runs_dir):
    records = traced({})
    ended = next(record for record in records if record["kind"] == "attempt_end")
    ended.update(duration_basis="monotonic", awake_s=3.5, asleep_s=120.0)
    path = folder(runs_dir / REPO, TRACED, {"trace.jsonl": lines(records)})
    store = Memory()
    export.export(of(path), store)
    row = next(row for row in store.final()["attempts"] if row["node"] == ended["node"] and row["attempt"] == ended["attempt"])
    assert (row["duration_basis"], row["awake_s"], row["asleep_s"]) == ("monotonic", 3.5, 120.0)


def test_an_export_with_no_run_records_its_success(repo, stores, capsys):
    assert cli.main(["metrics", "export"]) == 0
    (audit,) = stores[0].final()["export_runs"]
    assert (audit["runs_read"], audit["rows_written"], audit["error_class"]) == (0, 0, "")


def test_an_unreadable_runs_folder_records_a_failed_export(repo, stores, monkeypatch, capsys):
    def unreadable(*args, **kwargs):
        raise PermissionError(13, "Permission denied", str(repo / ".wf/runs"))

    monkeypatch.setattr(export, "runs_in", unreadable)
    assert cli.main(["metrics", "export"]) == 1
    (audit,) = stores[0].final()["export_runs"]
    assert (audit["runs_read"], audit["rows_written"], audit["error_class"]) == (0, 0, "UnreadableFolder")
    assert "can't list the runs" in capsys.readouterr().err


def test_a_store_that_cant_be_prepared_records_a_failed_export(repo, wf_home, monkeypatch, capsys):
    opened = []

    class Mismatched(Memory):
        def __init__(self, url):
            super().__init__()
            opened.append(self)

        def prepare(self):
            raise export.SchemaMismatch("wf.attempts has no column reasoning_tokens")

    monkeypatch.setattr(export, "Store", Mismatched)
    metrics_on(wf_home, metrics_url("127.0.0.1:8123"))
    path = traced_folder(runs.runs_folder(repo))
    assert cli.main(["metrics", "export"]) == 1
    (audit,) = opened[0].final()["export_runs"]
    assert (audit["runs_read"], audit["rows_written"], audit["error_class"]) == (0, 0, "SchemaMismatch")
    assert not (path / "export.json").exists()
    assert "wf.attempts has no column reasoning_tokens" in capsys.readouterr().err


def test_a_failed_export_counts_the_rows_that_clickhouse_took(repo, wf_home, monkeypatch, capsys):
    opened = []

    class Partial(Memory):
        def __init__(self, url):
            super().__init__()
            opened.append(self)

        def insert(self, table, rows):
            if table == "attempts":
                raise export.SchemaMismatch("attempts table differs")
            super().insert(table, rows)

    monkeypatch.setattr(export, "Store", Partial)
    metrics_on(wf_home, metrics_url("127.0.0.1:8123"))
    path = traced_folder(runs.runs_folder(repo))
    assert cli.main(["metrics", "export"]) == 1
    (audit,) = opened[0].final()["export_runs"]
    assert (audit["runs_read"], audit["rows_written"], audit["error_class"]) == (1, 1, "SchemaMismatch")
    assert not (path / "export.json").exists()
