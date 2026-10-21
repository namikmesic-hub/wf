"""wf ui: the JSON the page reads, the files it may show, and the server on 127.0.0.1 (ui.py)."""

import errno
import http.client
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from textwrap import dedent

import pytest

from fakes import Fake, submit, talk
from workflow import cli, diagram, harnesses, observe, progress, report, runner, runs, spec, ui
from workflow.runs import Run

EXAMPLES = Path(__file__).parent.parent / "workflow" / "examples"
HELD_AGENT = Path(__file__).parent / "held_agent.py"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(runner, "interactive", lambda: False)
    subprocess.run(["git", "init", "-q"], check=True)
    return tmp_path


FLOW = """
version: 1
name: flow
defaults: {harness: claude-code, harnesses: {claude-code: {model: opus}}}
start: {input: {}}
nodes:
  think: {needs: [start], instruction: Think about it.}
  check:
    needs: [think]
    command: "echo checked"
    routes: [{when: "check.exit_code != 0", to: think, max: 2}, {to: end}]
end: {output: {}}
"""


@pytest.fixture
def finished(repo, monkeypatch) -> Run:
    fake = Fake("claude-code", submit({"summary": "ok"}, tokens=300, cost=0.25))
    monkeypatch.setitem(harnesses.FACTORIES, "claude-code", fake.factory)
    Path("flow.yaml").write_text(FLOW.lstrip())
    assert cli.main(["run", "flow.yaml"]) == 0
    (folder,) = (Path(".wf") / "runs").iterdir()
    return Run.at(folder)


# ---------- the data ----------


def test_the_run_list_names_each_run_of_the_repo_newest_first(finished):
    found = ui.run_list(ui.Source(Path.cwd()))
    assert found["folder"] == str(finished.dir.parent)
    (only,) = found["runs"]
    assert (only["id"], only["run"], only["workflow"], only["status"], only["dry_run"]) == (
        finished.id, finished.id, "flow", "succeeded", False)
    assert "progress" not in only  # only runs that move show how far they are


def test_a_run_has_its_graph_progress_metrics_and_attempts(finished):
    found = ui.run_detail(finished)
    assert found["state"]["status"] == "succeeded"
    assert found["progress"]["percent"] == 100
    assert {n: i["state"] for n, i in found["progress"]["nodes"].items()} == {"think": "done", "check": "done"}
    assert [(n["id"], n["row"]) for n in found["graph"]["nodes"]] == [("start", 0), ("think", 1), ("check", 2), ("end", 3)]
    assert [(e["from"], e["to"], e["kind"]) for e in found["graph"]["edges"]] == [
        ("start", "think", "needs"), ("think", "check", "needs"), ("check", "think", "loop"), ("check", "end", "route")]
    assert found["metrics"]["nodes"] == report.build([finished])["nodes"]
    assert found["metrics"]["nodes"]["think"]["tokens"] == 300
    assert found["sessions"] == []
    think, check = found["attempts"]  # the fake agent writes no transcript: its session's event names it
    assert (think["node"], think["pass"], think["attempt"], think["harness"], think["model"]) == ("think", 1, 1, "claude-code", "opus")
    assert (think["log"], think["instruction"]) == (None, "nodes/think/1/instruction.md")
    assert (check["node"], check["log"], check["output"]) == ("check", "nodes/check/1/attempt-1.log", "nodes/check/1/output.json")


def test_a_transcript_shows_once_the_running_session_writes_it(repo):
    Path("flow.yaml").write_text(FLOW.lstrip())
    run = runs.create(Path.cwd(), "flow", [Path("flow.yaml")], Path("flow.yaml"), {})
    folder = run.pass_dir("think", 1, None)
    (folder / "instruction.md").write_text("Think about it.")
    run.add_event({"event": "node_started", "node": "think", "pass": 1})
    run.add_event({"event": "session_started", "node": "think", "pass": 1, "attempt": 1, "harness": "claude-code",
                   "model": "opus", "log": "nodes/think/1/attempt-1.log"})
    (waiting,) = ui.attempts(run)
    assert (waiting["log"], waiting["instruction"], waiting["harness"]) == (None, "nodes/think/1/instruction.md", "claude-code")
    (folder / "attempt-1.log").write_text('{"kind": "sent", "text": "Think about it."}\n')  # no event: the node runs
    (think,) = ui.attempts(run)
    assert (think["log"], think["harness"], think["started"] is not None) == ("nodes/think/1/attempt-1.log", "claude-code", True)


def test_a_refresh_reads_only_the_new_events_and_the_files_of_attempts_that_can_change(finished, monkeypatch):
    parsed, checked = [], []
    real_parse, real_check = runs.parse_events, ui._is_file
    monkeypatch.setattr(runs, "parse_events", lambda lines: parsed.append(real_parse(lines)) or parsed[-1])
    monkeypatch.setattr(ui, "_is_file", lambda path: checked.append(path.relative_to(finished.dir).as_posix()) or real_check(path))
    first = ui.run_detail(finished)
    assert len(parsed[0]) == len(report.events(finished))
    assert sorted({path.rsplit("/", 1)[0] for path in checked}) == ["nodes/check/1", "nodes/think/1"]
    parsed.clear(), checked.clear()
    assert ui.run_detail(finished) == first and parsed == [[]] and checked == []  # the run ended: nothing can change

    (finished.dir / "nodes" / "check" / "2").mkdir()
    (finished.dir / "nodes" / "check" / "2" / "attempt-1.log").write_text("checked\n")
    finished.add_event({"event": "node_started", "node": "check", "pass": 2})
    parsed.clear()
    assert [a["log"] for a in ui.attempts(finished)][-1] == "nodes/check/2/attempt-1.log"
    assert [len(events) for events in parsed] == [1]
    assert checked == ["nodes/check/2/attempt-1.log", "nodes/check/2/instruction.md", "nodes/check/2/output.json"]
    checked.clear()
    ui.attempts(finished)
    assert checked == ["nodes/check/2/instruction.md", "nodes/check/2/output.json"]  # check runs: it can still write them

    assert ui.run_detail(finished)["metrics"]["nodes"]["check"]["attempts"] == 2  # with the attempt that runs
    finished.add_event({"event": "node_finished", "node": "check", "pass": 2, "tokens": 0, "cost_usd": 0.0})
    found = ui.run_detail(finished)  # what it adds up, event by event, is what wf report and wf status count
    assert found["metrics"]["nodes"] == report.build([finished])["nodes"]
    assert found["progress"] == progress.of(finished)


def test_a_refresh_starts_over_when_events_jsonl_is_cut_back_and_grows_again(finished):
    size = finished.add_events([{"event": "node_started", "node": "check", "pass": 2}])
    assert ui.run_detail(finished)["metrics"]["nodes"]["check"]["passes"] == 2
    finished.take_back_events(size)
    finished.add_events([{"event": "warning", "message": "a line as long as the one that was taken back"}])
    found = ui.run_detail(finished)
    assert found["metrics"]["nodes"]["check"]["passes"] == 1 and found["states"]["check"] == "done"


def test_an_error_while_looking_for_a_finished_node_s_files_hides_its_attempt_only_until_the_next_refresh(finished, monkeypatch):
    log = finished.dir / "nodes" / "check" / "1" / "attempt-1.log"
    real = Path.stat

    def failing(path, *args, **kwargs):
        if path == log:
            raise OSError(errno.EIO, "Input/output error", str(path))
        return real(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", failing)
    assert [a["node"] for a in ui.attempts(finished)] == ["think"]
    monkeypatch.setattr(Path, "stat", real)
    assert [(a["node"], a["log"]) for a in ui.attempts(finished)] == [("think", None), ("check", "nodes/check/1/attempt-1.log")]


INNER = """
version: 1
name: rules
start: {input: {}}
nodes:
  print: {needs: [start], command: "echo rules"}
end: {output: {}}
"""

DELIVERY = """
version: 1
name: delivery
defaults: {harness: claude-code, harnesses: {claude-code: {model: opus}}}
start: {input: {}}
nodes:
  rules: {needs: [start], workflow: rules.yaml}
  implement: {needs: [rules], instruction: Implement it.}
  review: {needs: [implement], command: "echo reviewed"}
end: {output: {}}
"""


def until(condition, seconds: float = 30) -> None:
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.05)


def test_each_step_has_a_state_and_a_step_that_runs_shows_its_numbers_so_far(repo, tmp_path_factory):
    Path("rules.yaml").write_text(INNER.lstrip())
    Path("delivery.yaml").write_text(DELIVERY.lstrip())
    marks, release = tmp_path_factory.mktemp("marks"), tmp_path_factory.mktemp("release") / "go"
    process = subprocess.Popen([sys.executable, str(HELD_AGENT), str(marks), str(release), "run", "delivery.yaml"],
                               env={**os.environ, "USAGE_SECONDS": "0.05"}, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        until(lambda: (marks / f"{process.pid}.started").exists())
        (folder,) = (Path(".wf") / "runs").iterdir()
        run = Run.at(folder)
        until(lambda: any(e["event"] == "agent_usage" for e in report.events(run)))  # what the turn has used so far
        found = ui.run_detail(run)
        assert found["states"] == {"rules": "done", "implement": "running", "review": "waiting", "rules/print": "done"}
        assert {name: found["states"][name] for name in found["metrics"]["nodes"]} == {
            "rules/print": "done", "rules": "done", "implement": "running"}
        assert all(found["states"][n] == info["state"] for n, info in found["progress"]["nodes"].items())  # the graph's
        implement = found["metrics"]["nodes"]["implement"]
        assert (implement["passes"], implement["attempts"], implement["tokens"], implement["cost_usd"]) == (1, 1, 1234, 0.5)
        began = next(e["time"] for e in report.events(run) if e["event"] == "node_started" and e["node"] == "implement")
        time.sleep(0.2)
        asked = time.time()
        later = ui.run_detail(run)["metrics"]
        assert later["nodes"]["implement"]["duration_s"] >= round(asked - observe.timestamp(began), 1) >= 0.2
        assert (later["run"]["tokens"], later["run"]["cost_usd"]) == (1234, 0.5)
    finally:
        release.touch()
        out, _ = process.communicate(timeout=60)
    assert process.returncode == 0, out
    found = ui.run_detail(run)
    assert set(found["states"].values()) == {"done"}
    assert found["metrics"]["nodes"] == report.build([run])["nodes"]
    assert found["metrics"]["nodes"]["implement"]["tokens"] == 10  # the turn's own numbers, at its end


def test_a_run_that_moves_shows_its_progress_in_the_run_list(repo):
    Path("flow.yaml").write_text(FLOW.lstrip())
    run = runs.create(Path.cwd(), "flow", [Path("flow.yaml")], Path("flow.yaml"), {})  # running: just started
    run.add_event({"event": "node_started", "node": "think", "pass": 1})
    (only,) = ui.run_list(ui.Source(Path.cwd()))["runs"]
    assert (only["status"], only["progress"]) == ("running", {"step": 1, "steps": 2, "percent": 0, "left_s": None})


def test_a_damaged_run_leaves_out_only_its_own_progress_from_the_run_list(repo, monkeypatch):
    Path("flow.yaml").write_text(FLOW.lstrip())
    healthy, damaged = (runs.create(Path.cwd(), "flow", [Path("flow.yaml")], Path("flow.yaml"), {}) for _ in range(2))
    healthy.add_event({"event": "node_started", "node": "think", "pass": 1})
    with (damaged.dir / "events.jsonl").open("a") as file:
        file.write(json.dumps({"event": "node_started", "node": "think", "pass": 1}) + "\n")  # no time
    found = {run["run"]: run for run in ui.run_list(ui.Source(Path.cwd()))["runs"]}
    assert found[healthy.id]["progress"] == found[damaged.id]["progress"] == {"step": 1, "steps": 2, "percent": 0, "left_s": None}

    real = ui._View.progress
    monkeypatch.setattr(ui._View, "progress", lambda view, status: real(view, status) if view.run.dir == healthy.dir else 1 / 0)
    found = {run["run"]: run for run in ui.run_list(ui.Source(Path.cwd()))["runs"]}
    assert "progress" in found[healthy.id] and "progress" not in found[damaged.id] and found[damaged.id]["status"] == "running"


def test_the_views_of_runs_that_a_page_asks_for_stay_when_more_than_64_move(repo, monkeypatch):
    Path("flow.yaml").write_text(FLOW.lstrip())
    for _ in range(65):
        runs.create(Path.cwd(), "flow", [Path("flow.yaml")], Path("flow.yaml"), {}).add_event(
            {"event": "node_started", "node": "think", "pass": 1})
    started, real = [], ui._View._start
    monkeypatch.setattr(ui._View, "_start", lambda view: started.append(view.run.id) or real(view))
    for _ in range(2):
        listed = ui.run_list(ui.Source(Path.cwd()))["runs"]
        assert len(listed) == 65 and all(run["progress"]["step"] == 1 for run in listed)
    assert len(started) == 65  # each run read once: the second list read only what came since


def test_a_workflow_file_that_can_t_be_read_for_now_is_read_at_the_next_refresh(repo, monkeypatch):
    Path("flow.yaml").write_text(FLOW.lstrip())
    run = runs.create(Path.cwd(), "flow", [Path("flow.yaml")], Path("flow.yaml"), {})
    run.add_event({"event": "node_started", "node": "think", "pass": 1})
    copy, real, failed = run.workflow_file, Path.read_text, []

    def read_text(path, *args, **kwargs):
        if path == copy and not failed:
            failed.append(path)
            raise OSError(errno.EIO, "Input/output error", str(path))
        return real(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    started, start = [], ui._View._start
    monkeypatch.setattr(ui._View, "_start", lambda view: started.append(1) or start(view))
    first = ui.run_detail(run)
    assert first["graph"] is None and first["states"] == {}
    found = ui.run_detail(run)
    assert found["graph"] is not None and found["states"] == {"think": "running", "check": "waiting"}
    ui.run_detail(run)
    assert len(started) == 2  # read again once, then only the new events

    gone = runs.create(Path.cwd(), "flow", [Path("flow.yaml")], Path("flow.yaml"), {})
    gone.workflow_file.unlink()  # a damaged run folder stays so: no reason to read it all again
    started.clear()
    assert ui.run_detail(gone)["graph"] is None and ui.run_detail(gone)["graph"] is None and len(started) == 1


def test_attempts_inside_workflow_items_end_with_their_item_or_their_attempt_and_not_a_sibling_s(repo, monkeypatch):
    Path("inner.yaml").write_text(dedent("""
        version: 1
        name: inner
        start: {input: {}}
        nodes:
          print: {needs: [start], command: "echo inner"}
        end: {output: {}}
    """).lstrip())
    Path("outer.yaml").write_text(dedent("""
        version: 1
        name: outer
        start: {input: {}}
        nodes:
          build: {needs: [start], for_each: "range(11) | list", workflow: inner.yaml, retry: 1}
        end: {output: {}}
    """).lstrip())
    run = runs.create(Path.cwd(), "outer", [Path("outer.yaml"), Path("inner.yaml")], Path("outer.yaml"), {})

    def printed(item: int, attempt: int) -> str:
        folder = run.dir / "nodes" / "build" / "1" / str(item) / f"attempt-{attempt}" / "nodes" / "print" / "1"
        folder.mkdir(parents=True)
        (folder / "attempt-1.log").write_text("inner\n")
        return (folder / "attempt-1.log").relative_to(run.dir).as_posix()

    run.add_event({"event": "node_started", "node": "build", "pass": 1, "items": 11})
    logs = {}
    for item in (1, 10):
        run.add_event({"event": "node_started", "node": f"build[{item}]/print", "pass": 1})
        logs[item] = printed(item, 1)
    assert [(a["node"], a["log"]) for a in ui.attempts(run)] == [("build[1]/print", logs[1]), ("build[10]/print", logs[10])]

    checked, real = [], ui._is_file
    monkeypatch.setattr(ui, "_is_file", lambda path: checked.append(path.relative_to(run.dir).as_posix()) or real(path))
    run.add_event({"event": "item_failed", "node": "build", "index": 1, "error": "sub-workflow inner failed"})
    ui.attempts(run)  # a last look in item 1, then none
    checked.clear()
    ui.attempts(run)
    assert not [path for path in checked if path.startswith("nodes/build/1/1/")]
    assert "nodes/build/1/10/attempt-1/nodes/print/1/output.json" in checked  # item 10 goes on

    run.add_event({"event": "attempt_failed", "node": "build", "index": 10, "pass": 1, "attempt": 1, "error": "timed out"})
    run.add_event({"event": "attempt_started", "node": "build", "index": 10, "pass": 1, "attempt": 2})
    run.add_event({"event": "node_started", "node": "build[10]/print", "pass": 1})
    logs["again"] = printed(10, 2)
    assert [a["log"] for a in ui.attempts(run)] == [logs[1], logs[10], logs["again"]]
    checked.clear()
    ui.attempts(run)
    assert not [path for path in checked if "/attempt-1/" in path]  # attempt 1 of item 10 ended, with what ran in it
    assert "nodes/build/1/10/attempt-2/nodes/print/1/output.json" in checked


def test_attempts_are_found_in_items_and_in_sub_workflows(repo, monkeypatch):
    fake = Fake("claude-code", *[submit({"summary": f"item {n}"}) for n in range(2)], submit({"summary": "inner"}))
    monkeypatch.setitem(harnesses.FACTORIES, "claude-code", fake.factory)
    Path("inner.yaml").write_text(dedent("""
        version: 1
        name: inner
        defaults: {harness: claude-code, harnesses: {claude-code: {model: opus}}}
        start: {input: {}}
        nodes:
          think: {needs: [start], instruction: Think.}
          note: {needs: [think], command: "echo inner"}
        end: {output: {}}
    """).lstrip())
    Path("outer.yaml").write_text(dedent("""
        version: 1
        name: outer
        defaults: {harness: claude-code, harnesses: {claude-code: {model: opus}}}
        start: {input: {}}
        nodes:
          each: {needs: [start], for_each: "[1, 2]", instruction: "Think about {{ item }}."}
          list: {needs: [each], for_each: "[1, 2]", command: "echo {{ item }}"}
          sub: {needs: [list], workflow: inner.yaml}
        end: {output: {}}
    """).lstrip())
    assert cli.main(["run", "outer.yaml"]) == 0
    (folder,) = (Path(".wf") / "runs").iterdir()
    found = {(a["node"], a["pass"], a["attempt"]): a for a in ui.attempts(Run.at(folder))}
    assert set(found) == {("each[0]", 1, 1), ("each[1]", 1, 1), ("list[0]", 1, 1), ("list[1]", 1, 1),
                          ("sub/think", 1, 1), ("sub/note", 1, 1)}
    assert found[("list[1]", 1, 1)]["log"] == "nodes/list/1/1/attempt-1.log"
    assert found[("list[1]", 1, 1)]["instruction"] == "nodes/list/1/1/instruction.md"
    assert found[("sub/note", 1, 1)]["log"] == "nodes/sub/1/attempt-1/nodes/note/1/attempt-1.log"
    assert found[("sub/think", 1, 1)]["harness"] == "claude-code" and found[("each[0]", 1, 1)]["model"] == "opus"
    assert ui.run_file(Run.at(folder), found[("sub/note", 1, 1)]["log"])["text"] == "inner\n"


def test_a_sub_workflow_attempt_that_starts_over_lists_the_attempts_of_both(repo, monkeypatch):
    fake = Fake("claude-code", talk(), talk(), talk(), submit({"summary": "ok"}))  # inner's first attempt never submits
    monkeypatch.setitem(harnesses.FACTORIES, "claude-code", fake.factory)
    Path("inner.yaml").write_text(dedent("""
        version: 1
        name: inner
        defaults: {harness: claude-code, retry: 0}
        start: {input: {}}
        nodes:
          think: {needs: [start], instruction: Think.}
        end: {output: {}}
    """).lstrip())
    Path("outer.yaml").write_text(dedent("""
        version: 1
        name: outer
        start: {input: {}}
        nodes:
          sub: {needs: [start], workflow: inner.yaml, retry: 1}
        end: {output: {}}
    """).lstrip())
    assert cli.main(["run", "outer.yaml"]) == 0
    (folder,) = (Path(".wf") / "runs").iterdir()
    found = ui.run_detail(Run.at(folder))
    assert [(a["node"], a["pass"], a["attempt"], a["instruction"]) for a in found["attempts"]] == [
        ("sub/think", 1, 1, "nodes/sub/1/attempt-1/nodes/think/1/instruction.md"),
        ("sub/think", 1, 1, "nodes/sub/1/attempt-2/nodes/think/1/instruction.md")]
    assert found["states"] == {"sub": "done", "sub/think": "done"}


def test_events_come_as_lines_and_then_only_the_new_ones(finished):
    first = ui.events_after(finished, None)
    texts = [e["text"] for e in first["events"]]
    assert texts[0] == "▶ think" and "  check → end" in texts
    assert first["next"] == (finished.dir / "events.jsonl").stat().st_size
    assert ui.events_after(finished, first["next"]) == {"next": first["next"], "events": []}
    finished.add_event({"event": "warning", "message": "a new one"})
    later = ui.events_after(finished, first["next"])
    assert [e["text"] for e in later["events"]] == ["⚠ a new one"]


def test_the_page_shows_files_of_the_passes_and_nothing_else(finished):
    found = ui.run_file(finished, "nodes/check/1/attempt-1.log")
    assert (found["text"], found["cut_bytes"]) == ("checked\n", 0)
    assert ui.run_file(finished, "nodes/think/1/instruction.md")["text"].startswith("Think about it.")
    for path in ("run.json", "nodes/../run.json", "../flow.yaml", "/etc/passwd", "nodes/check/1/../../../input.json",
                 "nodes/check/1/start-commit", "nodes/check/1/nope.log", ""):
        assert ui.run_file(finished, path) is None, path
    (finished.dir / "nodes" / "check" / "1" / "attempt-2.log").symlink_to(finished.dir / "input.json")
    assert ui.run_file(finished, "nodes/check/1/attempt-2.log") is None  # a link out of the passes


def test_a_long_file_comes_from_its_end_starting_at_a_whole_line(finished, monkeypatch):
    monkeypatch.setattr(ui, "FILE_BYTES", 10)
    log = finished.dir / "nodes" / "check" / "1" / "attempt-1.log"
    log.write_text("first line\nsecond\nthird\n")
    found = ui.run_file(finished, "nodes/check/1/attempt-1.log")
    assert (found["text"], found["cut_bytes"]) == ("third\n", 18)


def test_the_layout_puts_parallel_steps_side_by_side_in_file_order():
    found = diagram.layout(spec.parse(dedent("""
        version: 1
        name: fan
        start: {input: {}}
        nodes:
          a: {needs: [start], command: "true"}
          b: {needs: [a], command: "true"}
          c: {needs: [a], command: "true"}
          d: {needs: [b, c], command: "true"}
        end: {output: {}}
    """).lstrip()))
    places = {n["id"]: (n["row"], n["place"]) for n in found["nodes"]}
    assert places == {"start": (0, 0.0), "a": (1, 0.0), "b": (2, -0.5), "c": (2, 0.5), "d": (3, 0.0), "end": (4, 0.0)}
    assert found["rows"] == 5
    assert ("d", "end", "needs") in [(e["from"], e["to"], e["kind"]) for e in found["edges"]]  # the way out, drawn

    implement = diagram.layout(spec.load(EXAMPLES / "implement.yaml"))
    loop = next(e for e in implement["edges"] if e["kind"] == "loop")
    assert (loop["from"], loop["to"], loop["label"]) == ("review", "implement", "max 3")


def test_with_all_repos_the_page_lists_the_runs_of_each_repo_in_the_runs_folder(tmp_path, monkeypatch):
    runs_dir = tmp_path / "runs"
    for name in ("one", "two"):
        folder = tmp_path / name
        folder.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=folder, check=True)
        (folder / "flow.yaml").write_text("version: 1\nname: tiny\nstart: {input: {}}\nnodes:\n"
                                          "  a: {needs: [start], command: \"true\"}\nend: {output: {}}\n")
        monkeypatch.chdir(folder)
        assert cli.main(["run", "flow.yaml", "--runs-dir", str(runs_dir)]) == 0
    source = ui.Source(Path.cwd(), runs_dir, every_repo=True)
    keys = list(source.runs())
    assert len(keys) == 2 and all(key.count("/") == 1 for key in keys)
    assert {source.get(key).repo for key in keys} == {(tmp_path / "one").resolve(), (tmp_path / "two").resolve()}
    assert source.get("nope") is None

    assert source.get("../" + keys[0]) is None and source.get(keys[0].replace("/", "/../")) is None

    monkeypatch.delenv("WF_RUNS_DIR", raising=False)
    assert cli.main(["ui", "--all-repos"]) == 1  # no runs folder to read


def test_wf_ui_takes_its_port_and_its_runs_folder_from_config_toml_unless_a_flag_names_them(tmp_path, monkeypatch, wf_home):
    served = []
    monkeypatch.setattr(ui, "serve", lambda source, port, opened=None: served.append((source.folder, port)) or 0)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["ui"]) == 0
    (wf_home / "config.toml").write_text(f'runs_dir = "{tmp_path / "runs"}"\n[ui]\nport = 9001\n')
    assert cli.main(["ui", "--all-repos"]) == 0  # the runs folder of the file is enough
    assert cli.main(["ui", "--port", "0"]) == 0
    assert [port for _, port in served] == [8765, 9001, 0]
    assert served[1][0] == (tmp_path / "runs").resolve()


@pytest.mark.parametrize("port", ["-1", "65536", "70000", "http"])
def test_a_port_that_is_not_a_port_is_refused_with_a_message(port, capsys):
    with pytest.raises(SystemExit) as stop:  # bad arguments exit with 1, before any server starts
        cli.main(["ui", "--port", port])
    assert stop.value.code == 1
    assert f"argument --port: `{port}` isn't a whole number from 0 to 65535" in capsys.readouterr().err


# ---------- the server ----------


@pytest.fixture
def server(finished):
    running = ui.Server(ui.Source(Path.cwd()), 0)
    thread = threading.Thread(target=running.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    yield running
    running.shutdown()
    running.server_close()


def get(server: ui.Server, path: str, host: str | None = None, token: str | None = None) -> tuple[int, dict, bytes]:
    """A request as the page makes it: with the page's key, unless `token` says otherwise."""
    connection = http.client.HTTPConnection(ui.HOST, server.port, timeout=10)
    connection.request("GET", path, headers={"Host": host or f"{ui.HOST}:{server.port}",
                                             "X-WF-Token": server.token if token is None else token})
    response = connection.getresponse()
    body = response.read()
    connection.close()
    return response.status, dict(response.getheaders()), body


def test_the_server_serves_the_page_and_its_json_on_this_machine_only(server, finished):
    assert server.server_address[0] == "127.0.0.1"
    status, headers, body = get(server, "/")
    assert status == 200 and headers["Content-Type"] == "text/html; charset=utf-8" and b"page.js" in body
    assert "script-src 'self'" in headers["Content-Security-Policy"] and "unsafe-inline" not in headers["Content-Security-Policy"]
    for path in ("/page.js", "/page.css"):
        assert get(server, path)[0] == 200

    status, _, body = get(server, "/api/runs")
    assert status == 200 and json.loads(body)["runs"][0]["id"] == finished.id
    status, _, body = get(server, f"/api/run?id={finished.id}")
    assert status == 200 and json.loads(body)["state"]["run"] == finished.id
    status, _, body = get(server, f"/api/events?id={finished.id}&after=0")
    assert status == 200 and json.loads(body)["events"][0]["text"] == "▶ think"
    status, _, body = get(server, f"/api/file?id={finished.id}&path=nodes/check/1/attempt-1.log")
    assert status == 200 and json.loads(body)["text"] == "checked\n"


def test_the_server_gives_data_only_to_the_page_that_has_its_key(server, finished):
    assert server.url == f"http://127.0.0.1:{server.port}/#token={server.token}" and len(server.token) >= 32
    assert get(server, "/", token="")[0] == 200  # the page holds nothing of the runs
    for path in ("/api/runs", f"/api/run?id={finished.id}", f"/api/file?id={finished.id}&path=nodes/check/1/attempt-1.log"):
        status, _, body = get(server, path, token="")
        assert status == 401 and "key" in json.loads(body)["error"]
        assert get(server, path, token=server.token[:-1] + "x")[0] == 401
    assert ui.Server(ui.Source(Path.cwd()), 0).token != server.token  # a new key for each server


def test_the_server_refuses_other_hosts_unknown_runs_and_files_outside_the_passes(server, finished):
    assert get(server, "/api/runs", host=f"attacker.example:{server.port}")[0] == 403
    assert get(server, "/api/runs", host="127.0.0.1")[0] == 403
    assert get(server, "/api/runs", host=f"localhost:{server.port}")[0] == 200
    assert get(server, "/api/run?id=nope")[0] == 404
    assert get(server, "/api/run?id=../" + finished.id)[0] == 404
    assert get(server, f"/api/file?id={finished.id}&path=../run.json")[0] == 404
    assert get(server, "/nothing")[0] == 404


def test_a_port_in_use_gives_way_to_a_free_one(server, finished):
    said, opened = [], []

    def stop(url: str) -> None:
        opened.append(url)
        raise KeyboardInterrupt  # as Ctrl-C would, once the page is up

    assert ui.serve(ui.Source(Path.cwd()), server.port, say=said.append, opened=stop) == 0
    assert said[0].startswith(f"Port {server.port} is in use, so wf ui uses port ")
    assert opened[0].startswith("http://127.0.0.1:") and "#token=" in opened[0] and opened[0] != server.url
    assert said[1] == f"wf ui: {opened[0]} shows the runs in {finished.dir.parent}. Ctrl-C stops it."
