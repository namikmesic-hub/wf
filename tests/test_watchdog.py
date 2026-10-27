"""The agent watchdog uses awake time and keeps stall retries separate."""

import asyncio
import json
from pathlib import Path

import pytest

from fakes import Fake, ends, hang, submit
from test_agents import flow, install, repo, run_dir
from workflow import cli, export, firstmate, runs, spec, steer, validate, watchdog
from workflow.harnesses.base import Recorder


class Clock:
    def __init__(self):
        self.wall, self.awake, self.continuous = 1000.0, 0.0, 0.0

    def __call__(self):
        return self.wall, self.awake, self.continuous

    def advance(self, seconds, sleep=0):
        self.wall += seconds + sleep
        self.awake += seconds
        self.continuous += seconds + sleep


class Record(Recorder):
    def __init__(self):
        self.signals = []

    def signal(self, kind, **data):
        self.signals.append({"event": kind, **data})


@pytest.fixture
def measured(monkeypatch):
    clock, record, events = Clock(), Record(), []
    monkeypatch.setattr(watchdog, "clock", clock)
    policy = spec.Stall(after="20s", retries_after="10s", tool_after="1h")
    return clock, watchdog.Watchdog(record, policy, events.append), events


def finished(clock, watch):
    result = watch.finish(clock.wall - 1000)
    assert sum(result[key] for key in watch.parts) == pytest.approx(clock.wall - 1000)
    return result


def test_a_long_tool_call_uses_its_own_limit(measured):
    clock, watch, events = measured
    clock.advance(2)
    watch.event({"event": "session_started"})
    watch.tool_call("one", "shell", {})
    clock.advance(1000)
    watch.check()
    assert not watch.alerted
    watch.tool_result("one", "done")
    result = finished(clock, watch)
    assert (result["startup_s"], result["tool_s"], result["idle_s"]) == (2, 1000, 0)
    assert any(event["event"] == "tool_end" for event in events)


def test_a_tool_call_across_sleep_counts_sleep_once(measured):
    clock, watch, events = measured
    watch.event({"event": "session_started"})
    watch.tool_call("one", "shell", {})
    clock.advance(10, sleep=3600)
    watch.check()
    assert not watch.alerted
    clock.advance(10)
    watch.tool_result("one", "done")
    result = finished(clock, watch)
    assert (result["asleep_s"], result["tool_s"], result["awake_s"]) == (3600, 20, 20)
    assert sum(event["event"] == "host_slept" for event in events) == 1


def test_retry_chatter_is_not_progress(measured):
    clock, watch, events = measured
    watch.event({"event": "session_started"})
    for _ in range(5):
        clock.advance(2)
        watch.event({"event": "sdk_retry"})
        watch.check()
    assert watch.alerted
    alert = next(event for event in events if event["event"] == "stall_detected")
    assert (alert["threshold"], alert["awake_s"], alert["silent_wall_s"]) == ("retries_after", 10, 10)
    assert finished(clock, watch)["sdk_retries"] == 5


def test_silence_fires_once_until_the_wait_action(measured):
    clock, watch, events = measured
    watch.event({"event": "session_started"})
    clock.advance(20)
    watch.check()
    watch.check()
    assert sum(event["event"] == "stall_detected" for event in events) == 1
    assert watch.resolve("wait")
    watch.check()
    assert not watch.alerted
    clock.advance(20)
    watch.check()
    assert watch.resolve("end") and watch.stop.is_set()
    finished(clock, watch)


def test_sleep_gives_silence_a_new_window(measured):
    clock, watch, events = measured
    watch.event({"event": "session_started"})
    clock.advance(19)
    watch.check()
    clock.advance(1, sleep=3600)
    watch.check()
    watch.check()
    assert not watch.alerted
    clock.advance(19)
    watch.check()
    assert not watch.alerted
    assert finished(clock, watch)["asleep_s"] == 3600


def test_a_turn_with_tokens_and_no_tools_is_active(measured):
    clock, watch, events = measured
    watch.event({"event": "session_started"})
    for _ in range(6):
        clock.advance(100)
        watch.event({"event": "progress", "source": "tokens"})
        watch.check()
    result = finished(clock, watch)
    assert not watch.alerted
    assert (result["active_s"], result["idle_s"], result["tool_s"]) == (600, 0, 0)


def test_true_silence_is_idle_and_sets_the_longest_gap(measured):
    clock, watch, events = measured
    watch.event({"event": "session_started"})
    clock.advance(400)
    result = finished(clock, watch)
    assert (result["idle_s"], result["active_s"], result["longest_gap_s"]) == (400, 0, 400)


def test_a_wall_clock_step_does_not_count_as_sleep_or_fire(measured):
    clock, watch, events = measured
    watch.event({"event": "session_started"})
    clock.wall += 3600
    watch.check()
    assert not watch.alerted
    assert not any(event["event"] == "host_slept" for event in events)


def test_updates_do_not_extend_an_open_tools_window(measured):
    clock, watch, events = measured
    watch.event({"event": "session_started"})
    watch.tool_call("one", "shell", {})
    clock.advance(3600)
    watch.tool_call("one", "shell", {"more": True})
    watch.check()
    assert watch.alerted


def fast(monkeypatch):
    monkeypatch.setattr(watchdog, "POLL_SECONDS", 0.005)
    monkeypatch.setattr(watchdog, "seconds", lambda value: 0.02)


def node(action, retries=1, retry=1, harness="claude-code"):
    return f"""
work:
  needs: [start]
  instruction: Check the file
  harness: {harness}
  retry: {retry}
  stall: {{after: 1s, then: {action}, retries: {retries}}}
  output: {{ok: {{type: boolean}}}}
"""


def test_a_stall_retry_does_not_use_the_node_retry(repo, monkeypatch):
    fast(monkeypatch)
    fake = Fake("claude-code", hang(), ends("failed", "try again"), submit({"ok": True}))
    install(monkeypatch, fake)
    assert cli.main(["run", flow(node("retry"))]) == 0
    assert len(fake.specs) == 3 and fake.closed == 3
    records = [json.loads(line) for line in (run_dir() / "trace.jsonl").read_text().splitlines()]
    ended = [record for record in records if record["kind"] == "attempt_end"]
    assert len(ended) == 3
    assert export.error_class(ended[0]["error"]) == "stalled"
    for record in ended:
        result = export._progress_columns(record)
        assert result["measure_version"] == 2
        assert sum(record[key] for key in ("startup_s", "asleep_s", "tool_s", "active_s", "idle_s")) == pytest.approx(
            (export._moment(record["ended"]) - export._moment(record["started"])).total_seconds(), abs=0.002)


def test_a_stall_cannot_spend_the_normal_retry_budget(repo, monkeypatch):
    fast(monkeypatch)
    fake = Fake("claude-code", hang(), hang())
    install(monkeypatch, fake)
    assert cli.main(["run", flow(node("retry", retry=5))]) == 1
    assert len(fake.specs) == 2


def test_stall_fallback_goes_to_the_next_harness(repo, monkeypatch):
    fast(monkeypatch)
    first = Fake("claude-code", hang())
    second = Fake("codex", submit({"ok": True}))
    install(monkeypatch, first, second)
    assert cli.main(["run", flow(node("fallback", harness="[claude-code, codex]"))]) == 0
    assert (len(first.specs), len(second.specs)) == (1, 1)


def test_stall_fallback_needs_a_harness_list(repo):
    problems = validate.check(Path(flow(node("fallback")))).errors
    assert any("stall fallback needs a harness list" in str(problem) for problem in problems)


def test_attention_actions_work_while_the_driver_holds_the_lock(repo, monkeypatch):
    fast(monkeypatch)
    monkeypatch.setattr(firstmate, "POLL_SECONDS", 0)

    async def stalled(session):
        run = runs.Run.at(run_dir().resolve())
        while run.status()["status"] != "stall_detected":
            await asyncio.sleep(0.005)
        assert run.driven()
        poll = await asyncio.to_thread(firstmate._poll, {"source_id": "watchdog", "config_ref": f"wf-run:{run.dir}"})
        assert json.loads(poll["output"])["status"] == "stall_detected"
        assert firstmate._status({"content": poll["output"]}) == "stall_detected"
        assert await asyncio.to_thread(cli.main, ["stall", run.id, "work", "wait", "--json"]) == 0
        while not session.spec.watchdog.alerted:
            await asyncio.sleep(0.005)
        answer = await asyncio.to_thread(steer.send, run, "work", action="end")
        assert answer["status"] == "delivered"
        await asyncio.sleep(60)

    fake = Fake("claude-code", stalled)
    install(monkeypatch, fake)
    assert cli.main(["run", flow(node("attention", retry=3))]) == 1
    assert fake.closed == 1 and len(fake.specs) == 1


def test_the_export_adds_progress_columns_to_an_existing_store(monkeypatch):
    wanted = export.schema()
    store = export.Store.__new__(export.Store)
    store.database, store.where = "wf", "test"
    old_columns = [{"table": table, "name": name, "type": kind}
                   for table, definition in wanted.items() for name, kind in definition["columns"].items()
                   if table != "attempts" or name not in export.PROGRESS_COLUMNS]
    tables = {table: {"uuid": table, "engine": definition["engine"], "sorting_key": definition["key"]}
              for table, definition in wanted.items()}
    statements = []
    monkeypatch.setattr(store, "_tables", lambda: tables)
    monkeypatch.setattr(store, "_select", lambda *args: old_columns)
    monkeypatch.setattr(store, "_post", lambda statement, **kwargs: statements.append(statement))
    store.prepare()
    assert len(statements) == len(export.PROGRESS_COLUMNS)
    assert all(statement.startswith("ALTER TABLE attempts ADD COLUMN IF NOT EXISTS ") for statement in statements)
    assert store.identity


def test_the_watchdog_keeps_a_result_when_the_timeout_cuts_a_message_handover(tmp_path):
    from test_steer import a_spec

    class Session:
        async def steer(self, text, ident):
            await asyncio.sleep(60)

    async def attempt(deadline):
        async with steer.Steering().live(a_spec(tmp_path, []), 1, "claude-code", Session(), deadline) as live:
            asyncio.create_task(live.take(steer.Message("one", "Check the file")))
            await asyncio.sleep(0.01)
            return {"ok": True}

    async def drive():
        watch = watchdog.Watchdog(Record(), None, lambda event: None)
        async with asyncio.timeout(0.1) as deadline:
            return await watch.run(attempt(deadline))

    assert asyncio.run(drive()) == {"ok": True}


def test_the_watchdog_keeps_an_external_cancellation():
    async def drive():
        watch = watchdog.Watchdog(Record(), None, lambda event: None)
        task = asyncio.create_task(watch.run(asyncio.sleep(60)))
        await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert task.cancelled()

    asyncio.run(drive())


def test_sleep_resets_the_watchdog_without_resetting_the_measured_progress_gap(measured):
    clock, watch, events = measured
    watch.event({"event": "session_started"})
    clock.advance(200)
    watch.sample()
    clock.advance(0, sleep=3600)
    watch.check()
    assert not watch.alerted
    clock.advance(200)
    result = finished(clock, watch)
    assert (result["asleep_s"], result["idle_s"], result["active_s"], result["longest_gap_s"]) == (3600, 400, 0, 400)


def test_progress_clears_attention_and_arms_the_watchdog_again(measured):
    clock, watch, events = measured
    watch.event({"event": "session_started"})
    clock.advance(20)
    watch.check()
    assert watch.alerted
    watch.text("The tool answered.")
    assert not watch.alerted
    assert next(event for event in events if event["event"] == "stall_resolved")["action"] == "progress"
    clock.advance(19)
    watch.check()
    assert not watch.alerted
    clock.advance(1)
    watch.check()
    assert sum(event["event"] == "stall_detected" for event in events) == 2


@pytest.mark.parametrize("action", ["attention", "retry", "fallback"])
def test_an_attempt_end_clears_its_stall(measured, action):
    clock, watch, events = measured
    watch.policy = watch.policy.model_copy(update={"then": action})
    watch.event({"event": "session_started"})
    clock.advance(20)
    watch.check()
    assert watch.alerted
    finished(clock, watch)
    assert not watch.alerted
    assert [event["action"] for event in events if event["event"] == "stall_resolved"] == ["attempt_end"]


def test_streamed_progress_keeps_its_latest_time_with_bounded_writes(measured):
    clock, watch, events = measured
    watch.event({"event": "session_started"})
    for _ in range(2000):
        clock.advance(0.001)
        watch.event({"event": "progress", "source": "thinking"})
        watch.check()
    result = finished(clock, watch)
    written = [event for event in events if event["event"] == "progress"]
    traced = [event for event in watch.record.signals if event["event"] == "progress"]
    assert len(written) <= 4 and len(traced) == len(written)
    assert written[-1]["last_progress_at"] == watchdog.timestamp(clock.wall) == result["last_progress_at"]
    assert result["active_s"] == pytest.approx(2)
    assert result["idle_s"] == 0 and not watch.alerted


def test_a_delayed_progress_write_does_not_extend_the_watchdog_window(measured):
    clock, watch, events = measured
    watch.event({"event": "session_started"})
    clock.advance(0.1)
    watch.text("A piece of text")
    last = watchdog.timestamp(clock.wall)
    clock.advance(20)
    watch.check()
    assert watch.alerted
    alert = next(event for event in events if event["event"] == "stall_detected")
    assert alert["last_progress_at"] == last
    assert alert["awake_s"] == pytest.approx(20)


def test_a_stall_retry_on_a_later_harness_is_not_a_fallback(repo, monkeypatch, capsys):
    fast(monkeypatch)
    first = Fake("claude-code", ends("failed", "try the next harness"))
    second = Fake("codex", hang(), submit({"ok": True}))
    install(monkeypatch, first, second)
    assert cli.main(["run", flow(node("retry", retry=0, harness="[claude-code, codex]"))]) == 0
    assert capsys.readouterr().out.count("falling back to codex") == 1
    events = [json.loads(line) for line in (run_dir() / "events.jsonl").read_text().splitlines()]
    attempts = [event for event in events if event["event"] == "attempt_started"]
    assert [(event["attempt"], event.get("fallback", False)) for event in attempts] == [(2, True), (3, False)]
    records = [json.loads(line) for line in (run_dir() / "trace.jsonl").read_text().splitlines()]
    assert [(record["attempt"], record.get("fallback", False)) for record in records if record["kind"] == "attempt"] == [
        (1, False), (2, True), (3, False)]


@pytest.mark.parametrize("ended", ["session_ended", "attempt_failed", "harness_unavailable", "stall_resolved"])
def test_the_driver_removes_an_ended_attempt_from_its_stall_state(repo, monkeypatch, ended):
    from types import SimpleNamespace
    from workflow import operate, runner

    flow(node("retry"))
    run = runs.create(Path.cwd(), "agents", [Path("flow.yaml")], Path("flow.yaml"), {})
    place = {"node": "work", "pass": 1, "attempt": 1, "harness": "claude-code"}

    class Compiled:
        async def aget_state(self, config):
            return SimpleNamespace(values={})

        async def astream(self, *args, **kwargs):
            yield {"event": "stall_detected", **place, "action": "retry"}
            assert run.status()["status"] == "stall_detected"
            assert operate.wait(run, 0, consumer="test")["status"] == "stall_detected"
            yield {"event": ended, **place, "error": "stalled", "next": None}
            assert run.status()["status"] == "running"
            assert run.status()["stalls"] == []
            yield {"event": "attempt_started", **place, "attempt": 2}
            yield {"event": "progress", **place, "attempt": 2, "source": "text"}
            yield {"event": "session_ended", **place, "attempt": 2}
            assert operate.wait(run, 0, consumer="test") is None

    async def close():
        pass

    ctx = SimpleNamespace(run=run, _agents_waiting={}, new_event_loop=lambda: None, resume=lambda values: None,
                          trace=SimpleNamespace(know=lambda callback: None), steering=steer.Steering(), close_harnesses=close)
    monkeypatch.setattr(runner.graph, "build", lambda saver: Compiled())
    with run.driving():
        asyncio.run(runner._stream(ctx, None))


def test_one_attempt_cannot_clear_a_siblings_stall(repo, monkeypatch):
    from types import SimpleNamespace
    from workflow import runner

    flow(node("attention"))
    run = runs.create(Path.cwd(), "agents", [Path("flow.yaml")], Path("flow.yaml"), {})
    place = {"pass": 1, "attempt": 1}

    class Compiled:
        async def aget_state(self, config):
            return SimpleNamespace(values={})

        async def astream(self, *args, **kwargs):
            yield {"event": "stall_detected", **place, "node": "work", "action": "attention"}
            yield {"event": "stall_detected", **place, "node": "sibling", "action": "attention"}
            yield {"event": "session_ended", **place, "node": "work"}
            current = run.status()
            assert current["status"] == "stall_detected"
            assert [stall["node"] for stall in current["stalls"]] == ["sibling"]
            yield {"event": "stall_resolved", **place, "node": "work", "action": "attempt_end"}
            assert run.status()["seq"] == current["seq"]
            yield {"event": "session_ended", **place, "node": "sibling"}
            assert run.status()["status"] == "running"

    async def close():
        pass

    ctx = SimpleNamespace(run=run, _agents_waiting={}, new_event_loop=lambda: None, resume=lambda values: None,
                          trace=SimpleNamespace(know=lambda callback: None), steering=steer.Steering(), close_harnesses=close)
    monkeypatch.setattr(runner.graph, "build", lambda saver: Compiled())
    with run.driving():
        asyncio.run(runner._stream(ctx, None))
