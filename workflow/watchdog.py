"""The watchdog measures agent progress and detects awake time without progress."""

import asyncio
import ctypes
import functools
import sys
import time
from datetime import datetime, timezone

from .harnesses.base import Recorder
from .spec import seconds

POLL_SECONDS = 0.2
PROGRESS_SECONDS = 1.0
ACTIVE_GAP = 300
SLEEP_GAP = 60


@functools.cache
def _continuous_clock():
    if hasattr(time, "CLOCK_BOOTTIME"):
        return lambda: time.clock_gettime(time.CLOCK_BOOTTIME)
    if sys.platform == "darwin":
        library = ctypes.CDLL(None)
        scale = (ctypes.c_uint32 * 2)()
        library.mach_timebase_info(ctypes.byref(scale))
        library.mach_continuous_time.restype = ctypes.c_uint64
        return lambda: library.mach_continuous_time() * scale[0] / scale[1] / 1e9
    return time.monotonic


def clock() -> tuple[float, float, float]:
    return time.time(), time.monotonic(), _continuous_clock()()


def timestamp(value: float) -> str:
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="milliseconds")


class Stalled(Exception):
    """An attempt stopped because it had no progress."""

    def __init__(self, action: str):
        super().__init__("stalled: the agent made no progress")
        self.action = action


class Watchdog(Recorder):
    def __init__(self, record, policy, emit):
        self.record, self.policy, self.emit = record, policy, emit
        self.before = self.started = clock()
        self.awake = 0.0
        self.parts = dict.fromkeys(("startup_s", "asleep_s", "tool_s", "active_s", "idle_s"), 0.0)
        self.session = False
        self.tools: dict[str, float] = {}
        self.quiet = 0.0
        self.gap_at = self.started[0]
        self.last = self.started[0]
        self.window = 0.0
        self.retrying = False
        self.sdk_retries = 0
        self.longest = 0.0
        self.longest_at = None
        self.detected = None
        self.alerted = False
        self._stalled_tools: set[str] = set()
        self.stop = asyncio.Event()
        self.action = "end"
        self._pending_progress = None
        self._progress_written = None

    def _write(self, kind, **data):
        self.record.signal(kind, **data)
        self.emit({"event": kind, **data})

    def _flush(self):
        self.parts["idle_s" if self.quiet > ACTIVE_GAP else "active_s"] += self.quiet
        if self.quiet > self.longest:
            self.longest, self.longest_at = self.quiet, timestamp(self.gap_at)
        self.quiet = 0.0
        self.gap_at = self.before[0]

    def sample(self):
        current = clock()
        wall, awake, continuous = (current[i] - self.before[i] for i in range(3))
        awake = max(0.0, awake)
        slept = max(0.0, continuous - awake)
        self.awake += awake
        if not self.session:
            self.parts["startup_s"] += awake
        elif self.tools:
            self.parts["tool_s"] += awake
        else:
            self.quiet += awake
        self.before = current
        if slept > SLEEP_GAP and wall - awake > SLEEP_GAP:
            self.parts["asleep_s"] += slept
            self.window = self.awake
            self.tools = dict.fromkeys(self.tools, self.awake)
            self._write("host_slept", started=timestamp(current[0] - slept), ended=timestamp(current[0]),
                        asleep_s=slept)
            return True
        return False

    def progress(self, source):
        self.sample()
        self._flush()
        self.last = self.before[0]
        self.window = self.awake
        self.retrying = False
        self._pending_progress = {"source": source, "last_progress_at": timestamp(self.last)}
        if self.alerted and not self.stop.is_set() and not self._stalled_tools.intersection(self.tools):
            self._clear("progress")
        self._write_progress(force=source in ("session_start", "tool_start", "tool_end", "turn_end"))

    def _write_progress(self, force=False):
        if self._pending_progress is not None and (force or self._progress_written is None
                                                  or self.awake - self._progress_written >= PROGRESS_SECONDS):
            pending, self._pending_progress = self._pending_progress, None
            self._progress_written = self.awake
            self._write("progress", **pending)

    def _clear(self, action):
        if self.alerted:
            self.alerted = False
            self._stalled_tools.clear()
            self._write("stall_resolved", action=action)

    def event(self, event):
        kind = event.get("event")
        if kind == "session_started":
            self.sample()
            self.session = True
            self.progress("session_start")
        elif kind == "sdk_retry":
            self.sample()
            self.retrying = True
            self.sdk_retries += 1
            self.record.signal("sdk_retry", **{k: v for k, v in event.items() if k != "event"})
        elif kind == "progress":
            self.progress(event.get("source", "harness"))
            return
        self.emit(event)

    def text(self, text):
        if text:
            self.progress("text")
        self.record.text(text)

    def thinking(self, text, duration_ms):
        if text:
            self.progress("thinking")
        self.record.thinking(text, duration_ms)

    def request(self, request_id):
        self.record.request(request_id)

    def tool_call(self, call, tool, arguments):
        if str(call) not in self.tools:
            self.progress("tool_start")
            self.tools[str(call)] = self.awake
        self.record.tool_call(call, tool, arguments)

    def tool_result(self, call, result, is_error=False, kept=True, characters=None):
        self.sample()
        self.tools.pop(str(call), None)
        self.progress("tool_end")
        self._write("tool_end", call=str(call), is_error=is_error)
        self.record.tool_result(call, result, is_error, kept, characters)

    def check(self):
        slept = self.sample()
        self._write_progress()
        if slept or not self.session or self.policy is None or self.alerted:
            return
        policy = self.policy
        if self.tools:
            quiet, limit, reason = self.awake - min(self.tools.values()), seconds(policy.tool_after), "tool_after"
        else:
            reason = "retries_after" if self.retrying else "after"
            quiet, limit = self.awake - self.window, seconds(getattr(policy, reason))
        if quiet < limit:
            return
        self.alerted = True
        self._stalled_tools = {call for call, started in self.tools.items() if self.awake - started >= limit}
        self.detected = timestamp(self.before[0])
        self._write_progress(force=True)
        self._write("stall_detected", silent_wall_s=max(0.0, self.before[0] - self.last), awake_s=quiet,
                    last_progress_at=timestamp(self.last), action=policy.then, threshold=reason)
        if policy.then != "attention":
            self.action = policy.then
            self.stop.set()

    def resolve(self, action):
        if not self.alerted or action not in ("end", "wait"):
            return False
        self.sample()
        self._clear(action)
        if action == "end":
            self.action = action
            self.stop.set()
        else:
            self.window = self.awake
            self.tools = dict.fromkeys(self.tools, self.awake)
        return True

    async def run(self, sending):
        owner = asyncio.current_task()
        cancels = owner.cancelling()
        interrupted, failure = False, None

        async def watch():
            nonlocal interrupted, failure
            try:
                while not self.stop.is_set():
                    try:
                        await asyncio.wait_for(self.stop.wait(), POLL_SECONDS)
                    except TimeoutError:
                        self.check()
            except Exception as error:
                failure = error
            interrupted = True
            owner.cancel()

        watching = asyncio.create_task(watch())
        try:
            return await sending
        except asyncio.CancelledError:
            if interrupted:
                interrupted = False
                if owner.uncancel() <= cancels:
                    raise failure or Stalled(self.action) from None
            raise
        finally:
            watching.cancel()
            await asyncio.gather(watching, return_exceptions=True)
            if interrupted:
                owner.uncancel()

    def finish(self, wall_s):
        self.sample()
        self._flush()
        self._write_progress(force=True)
        self._clear("attempt_end")
        # Keep the split equal to the trace's wall time at millisecond precision.
        total = sum(self.parts.values())
        if total > wall_s and total:
            self.parts = {key: value * max(0.0, wall_s) / total for key, value in self.parts.items()}
        else:
            self.parts["idle_s"] += wall_s - total
        return {**self.parts, "duration_basis": "monotonic", "awake_s": self.awake, "measure_version": 2,
                "longest_gap_s": self.longest, "longest_gap_at": self.longest_at,
                "last_progress_at": timestamp(self.last), "stall_detected_at": self.detected,
                "stall_action": self.policy.then if self.detected else None, "sdk_retries": self.sdk_retries}
