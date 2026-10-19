"""wf ui: the JSON the page reads, the files it may show, and the server on 127.0.0.1 (ui.py)."""

import http.client
import json
import subprocess
import threading
from pathlib import Path
from textwrap import dedent

import pytest

from fakes import Fake, submit
from workflow import cli, diagram, harnesses, report, runner, spec, ui
from workflow.runs import Run

EXAMPLES = Path(__file__).parent.parent / "workflow" / "examples"


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
    (finished.dir / "nodes" / "think" / "1" / "attempt-1.log").write_text('{"kind": "sent", "text": "Think about it."}\n')
    think = next(a for a in ui.run_detail(finished)["attempts"] if a["node"] == "think")
    assert (think["log"], think["harness"], think["started"] is not None) == ("nodes/think/1/attempt-1.log", "claude-code", True)
    assert (check["node"], check["log"], check["output"]) == ("check", "nodes/check/1/attempt-1.log", "nodes/check/1/output.json")


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

    monkeypatch.delenv("WF_RUNS_DIR", raising=False)
    assert cli.main(["ui", "--all-repos"]) == 1  # no runs folder to read


# ---------- the server ----------


@pytest.fixture
def server(finished):
    running = ui.Server(ui.Source(Path.cwd()), 0)
    thread = threading.Thread(target=running.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    yield running
    running.shutdown()
    running.server_close()


def get(server: ui.Server, path: str, host: str | None = None) -> tuple[int, dict, bytes]:
    connection = http.client.HTTPConnection(ui.HOST, server.port, timeout=10)
    connection.request("GET", path, headers={"Host": host or f"{ui.HOST}:{server.port}"})
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


def test_the_server_refuses_other_hosts_unknown_runs_and_files_outside_the_passes(server, finished):
    assert get(server, "/api/runs", host=f"attacker.example:{server.port}")[0] == 403
    assert get(server, "/api/runs", host="127.0.0.1")[0] == 403
    assert get(server, "/api/runs", host=f"localhost:{server.port}")[0] == 200
    assert get(server, "/api/run?id=nope")[0] == 404
    assert get(server, f"/api/file?id={finished.id}&path=../run.json")[0] == 404
    assert get(server, "/nothing")[0] == 404


def test_a_port_in_use_gives_way_to_a_free_one(server, finished):
    said, opened = [], []

    def stop(url: str) -> None:
        opened.append(url)
        raise KeyboardInterrupt  # as Ctrl-C would, once the page is up

    assert ui.serve(ui.Source(Path.cwd()), server.port, say=said.append, opened=stop) == 0
    assert said[0].startswith(f"Port {server.port} is in use, so wf ui uses port ")
    assert opened[0].startswith("http://127.0.0.1:") and opened[0] != server.url
    assert said[1] == f"wf ui: {opened[0]} shows the runs in {finished.dir.parent}. Ctrl-C stops it."
