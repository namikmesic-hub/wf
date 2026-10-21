"""wf metrics up, and the metrics stack that it starts (metrics/compose.yaml).

The tests marked docker start the stack from empty volumes, in a compose project of their own on free
ports, and remove its containers and volumes at the end. Each start needs the internet for Grafana's
ClickHouse plugin, and the first start also for the images. Where Docker doesn't answer, or where pytest
can't reach grafana.com, pytest skips them (conftest.py)."""

import base64
import contextlib
import http.client
import http.server
import io
import json
import os
import re
import shlex
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from workflow import cli, metrics

DASHBOARD = metrics.COMPOSE.parent / "grafana" / "dashboards" / "wf-runs.json"


# ---------- wf metrics up ----------


def fake_docker(tmp_path, monkeypatch, script: str):
    """A `docker` on PATH that adds its arguments to docker.log, then runs `script`."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(f'#!/bin/sh\necho "$*" >> {tmp_path / "docker.log"}\n{script}\n')
    docker.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return tmp_path / "docker.log"


@pytest.fixture
def otlp_port():
    """A server on this machine that answers each POST with 200, as the Collector answers OTLP: its address."""

    class Collector(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Collector)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def ports(collector: str, grafana: str = 'echo 127.0.0.1:3000') -> str:
    """A script for fake_docker that answers `docker compose port` for the Collector and for Grafana."""
    return f'case "$*" in *"port collector 4318") echo {collector} ;; *"port grafana 3000") {grafana} ;; esac'


def test_wf_metrics_up_starts_the_stack_and_prints_grafana_s_address(tmp_path, monkeypatch, capsys, otlp_port):
    log = fake_docker(tmp_path, monkeypatch, ports(otlp_port))
    assert cli.main(["metrics", "up"]) == 0
    compose = f"compose -f {metrics.COMPOSE}"
    assert log.read_text().splitlines() == [f"{compose} up -d --wait", f"{compose} port collector 4318",
                                            f"{compose} port grafana 3000"]
    assert capsys.readouterr().out == "Grafana: http://127.0.0.1:3000\n"


def test_wf_metrics_up_fails_when_the_collector_doesn_t_take_otlp(tmp_path, monkeypatch, capsys):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        closed = f"127.0.0.1:{probe.getsockname()[1]}"
    log = fake_docker(tmp_path, monkeypatch, ports(closed))
    monkeypatch.setattr(metrics, "READY_S", 0.5)
    assert cli.main(["metrics", "up"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    compose = shlex.join(["docker", "compose", "-f", str(metrics.COMPOSE)])
    assert f"the Collector doesn't take OTLP at {closed}. `{compose} logs collector` shows why" in captured.err
    assert "port grafana" not in log.read_text()


def test_wf_metrics_up_fails_when_docker_compose_fails_and_says_where_to_look(tmp_path, monkeypatch, capsys):
    fake_docker(tmp_path, monkeypatch, "echo 'Bind for 127.0.0.1:3000 failed: port is already allocated' >&2; exit 1")
    assert cli.main(["metrics", "up"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    compose = shlex.join(["docker", "compose", "-f", str(metrics.COMPOSE)])
    assert (f"`docker compose up` failed with exit code 1. `{compose} logs` shows what each service said, and "
            f"`{compose} down` stops them. To move a port that is in use, set WF_METRICS_GRAFANA_PORT, "
            "WF_METRICS_CLICKHOUSE_PORT, WF_METRICS_OTLP_GRPC_PORT or WF_METRICS_OTLP_HTTP_PORT to another port") in captured.err


def test_the_ports_that_wf_metrics_up_names_are_the_ports_of_the_compose_file():
    named = re.findall(r'"127\.0\.0\.1:\$\{(\w+):-\d+\}:\d+"', metrics.COMPOSE.read_text())
    assert sorted(named) == sorted(metrics.PORTS)


def test_wf_metrics_up_fails_when_docker_doesn_t_say_where_grafana_listens(tmp_path, monkeypatch, capsys, otlp_port):
    fake_docker(tmp_path, monkeypatch, ports(otlp_port, grafana='echo "no port" >&2; exit 1'))
    assert cli.main(["metrics", "up"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "the stack runs, but Docker didn't say where Grafana listens: no port" in captured.err


def test_wf_metrics_up_needs_docker_and_the_stack_s_files(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PATH", str(tmp_path))
    assert cli.main(["metrics", "up"]) == 1
    assert "needs Docker with Compose, and there's no `docker` on PATH" in capsys.readouterr().err
    fake_docker(tmp_path, monkeypatch, "exit 0")
    monkeypatch.setattr(metrics, "COMPOSE", tmp_path / "metrics" / "compose.yaml")
    assert cli.main(["metrics", "up"]) == 1
    assert "install wf from its checkout with `uv tool install --editable .`" in capsys.readouterr().err
    assert not (tmp_path / "docker.log").exists()


def test_no_service_starts_again_by_itself():
    # With a restart policy, docker compose up --wait can take a service that crashes at each start for one that runs
    assert not re.search(r"^\s+restart:", metrics.COMPOSE.read_text(), re.M)


PINNED = re.compile(r"[\w./-]+:\d+\.\d+\.\d+(\.\d+)?")  # a tag of 25.8 moves with each patch release


def test_each_image_and_plugin_names_its_version():
    assert not PINNED.fullmatch("clickhouse/clickhouse-server:25.8")
    text = metrics.COMPOSE.read_text()
    images = re.findall(r"^\s+image: (\S+)$", text, re.M)
    assert len(images) == 3
    assert all(PINNED.fullmatch(image) for image in images), images
    assert re.search(r"GF_PLUGINS_PREINSTALL_SYNC: grafana-clickhouse-datasource@\d+\.\d+\.\d+$", text, re.M)


# ---------- the stack, in Docker ----------


TABLES = ("runs", "attempts", "turns", "tool_calls", "routes", "metric_points")
DAY = timedelta(days=1)


@dataclass
class Stack:
    compose: list[str]
    grafana: str
    clickhouse: str
    otlp: str


@pytest.fixture(scope="module")
def stack():
    """The stack that `wf metrics up` starts, in a new compose project with new volumes, on ports that Docker picks."""
    project = f"wf-metrics-test-{uuid.uuid4().hex[:8]}"
    compose = ["docker", "compose", "-f", str(metrics.COMPOSE), "-p", project]
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("COMPOSE_PROJECT_NAME", project)
        for name in metrics.PORTS:
            patch.setenv(name, "0")
        try:
            printed = io.StringIO()
            with contextlib.redirect_stdout(printed):
                assert cli.main(["metrics", "up"]) == 0
            grafana = printed.getvalue().strip().removeprefix("Grafana: ")

            def address(service: str, port: int) -> str:
                found = subprocess.run([*compose, "port", service, str(port)], capture_output=True, text=True, check=True)
                return f"http://{found.stdout.strip()}"

            yield Stack(compose, grafana, address("clickhouse", 8123), address("collector", 4318))
        finally:
            removed = subprocess.run([*compose, "down", "--volumes", "--remove-orphans"], capture_output=True, text=True,
                                     timeout=300)
            assert removed.returncode == 0, f"docker compose down failed, and project {project} may still run: {removed.stderr}"


def status(url: str, body: bytes | None = None, headers: dict | None = None) -> int:
    """The status of one request, without following a redirect. A Host in headers replaces the host of the URL."""
    parts = urllib.parse.urlsplit(url)
    connection = http.client.HTTPConnection(parts.hostname, parts.port, timeout=30)
    try:
        target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        connection.request("GET" if body is None else "POST", target, body, headers or {})
        return connection.getresponse().status
    finally:
        connection.close()


def _request(url: str, body: bytes | None = None, headers: dict | None = None) -> bytes:
    request = urllib.request.Request(url, data=body, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read()
    except urllib.error.HTTPError as error:
        raise AssertionError(f"{url} answered {error.code}: {error.read().decode(errors='replace')}") from None


def clickhouse(stack: Stack, sql: str, rows: list[dict] | None = None) -> str:
    """A POST, because ClickHouse runs a GET read-only. The SQL is the body, or the query of the rows that are."""
    if rows is None:
        url, body = f"{stack.clickhouse}/", sql.encode()
    else:
        url, body = f"{stack.clickhouse}/?{urllib.parse.urlencode({'query': sql})}", "\n".join(map(json.dumps, rows)).encode()
    return _request(url, body, {"X-ClickHouse-User": "wf", "X-ClickHouse-Key": "wf"}).decode()


def grafana(stack: Stack, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    return json.loads(_request(stack.grafana + path, data, {"Content-Type": "application/json"}))


def eventually(check, what: str, seconds: float = 60):
    deadline, last = time.monotonic() + seconds, None
    while True:
        try:
            if check():
                return
        except (AssertionError, urllib.error.URLError, ConnectionError) as error:  # a table that isn't made yet
            last = error
        if time.monotonic() > deadline:
            raise AssertionError(f"{what} didn't happen in {seconds} s (last: {last})")
        time.sleep(0.5)


def send(url: str, payload: dict) -> None:
    """POST OTLP JSON, and wait for a Collector that doesn't listen yet. A refused payload fails at once."""
    deadline = time.monotonic() + 60
    while True:
        try:
            _request(url, json.dumps(payload).encode(), {"Content-Type": "application/json"})
            return
        except (urllib.error.URLError, ConnectionError):
            if time.monotonic() > deadline:
                raise
            time.sleep(0.5)


def at(now: datetime, minutes: float) -> str:
    return (now - timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def wf_rows(now: datetime) -> dict[str, list[dict]]:
    """Rows for each wf table: three runs of two workflows, with fallbacks, a rate limit and a timeout.
    The review of r3 succeeded twice: its first attempt couldn't keep its output, and `wf retry` ran it again."""

    def run(run_id, workflow, status, began, took, cost, tokens, source="trace"):
        return {"run_id": run_id, "repo_key": "wf", "workflow": workflow, "status": status, "dry_run": False,
                "started": at(now, began), "ended": at(now, began - took / 60), "duration_s": took, "cost_usd": cost,
                "tokens": tokens, "source": source}

    def attempt(run_id, workflow, node, item, number, harness, model, began, took, outcome="succeeded", error="",
                error_class="", tokens=(0, 0, 0, 0, 0), cost=0.0, pass_=1, kind="agent", exit_code=None):
        given, written, read, cached, reasoning = tokens
        return {"run_id": run_id, "workflow": workflow, "node": node, "pass": pass_, "item": item, "attempt": number,
                "kind": kind, "harness": harness, "model": model, "effort": "high" if model else "",
                "access": "read-only" if kind == "agent" else "", "session_id": f"{run_id}-{node}-{pass_}-{item}-{number}",
                "session_start": "fresh", "started": at(now, began), "ended": at(now, began - took / 60), "duration_s": took,
                "outcome": outcome, "error": error, "error_class": error_class, "tokens": given + written + read + cached,
                "input_tokens": given, "output_tokens": written, "cache_read_tokens": read, "cache_write_tokens": cached,
                "reasoning_tokens": reasoning, "cost_usd": cost, "exit_code": exit_code}

    def call(run_id, workflow, node, item, number, call_id, tool, harness, model, began, took, is_error):
        return {"run_id": run_id, "workflow": workflow, "node": node, "pass": 1, "item": item, "attempt": number,
                "turn": 1, "call_id": call_id, "tool": tool, "harness": harness, "model": model, "started": at(now, began),
                "duration_ms": took, "is_error": is_error}

    def point(run_id, workflow, metric, node, pass_, item, minutes, value, **labels):
        return {"run_id": run_id, "workflow": workflow, "metric": metric, "node": node, "pass": pass_, "item": item,
                "time": at(now, minutes), "value": value, "labels": labels}

    opus, sol, flash = ("claude-code", "claude-opus-5-5"), ("codex", "gpt-6.1-sol"), ("cursor", "gemini-3.8-flash-high")
    return {
        "runs": [run("r1", "deliver-change", "succeeded", 180, 3600, 3.4, 268000),
                 run("r2", "deliver-change", "failed", 100, 1200, 0.0, 41000),
                 run("r3", "pr-review", "succeeded", 60, 1800, 0.6, 77000, source="events")],
        "attempts": [
            attempt("r1", "deliver-change", "implement", -1, 1, *opus, 178, 1680, tokens=(900, 20000, 150000, 25000, 4000), cost=3.0),
            attempt("r1", "deliver-change", "tests", -1, 1, "", "", 150, 120, kind="command", exit_code=0),
            attempt("r1", "deliver-change", "review", 0, 1, *sol, 148, 480, tokens=(30000, 8000, 3000, 0, 6000)),
            attempt("r1", "deliver-change", "review", 1, 1, *flash, 148, 120, outcome="failed",
                    error="Error: RetriableError: [resource_exhausted] Error", error_class="rate_limit"),
            attempt("r1", "deliver-change", "review", 1, 2, *opus, 146, 480, tokens=(500, 4000, 25000, 2000, 1000), cost=0.4),
            attempt("r1", "deliver-change", "synthesize", -1, 1, *opus, 138, 300, tokens=(300, 2000, 8000, 0, 500)),
            attempt("r1", "deliver-change", "synthesize", -1, 1, *opus, 125, 240, tokens=(300, 1500, 8000, 0, 400), pass_=2),
            attempt("r2", "deliver-change", "implement", -1, 1, *sol, 100, 1200, outcome="timed_out",
                    error="the node's timeout passed", error_class="timeout", tokens=(25000, 9000, 7000, 0, 3000)),
            attempt("r3", "pr-review", "review", 0, 1, *sol, 60, 600, tokens=(20000, 3000, 2000, 0, 1500)),
            attempt("r3", "pr-review", "review", 0, 2, *opus, 50, 1200, tokens=(400, 6000, 40000, 5600, 2000), cost=0.6),
        ],
        "turns": [{"run_id": "r1", "node": "implement", "pass": 1, "item": -1, "attempt": 1, "turn": turn,
                   "started": at(now, began), "ended": at(now, began - 10), "duration_s": 600, "status": "completed",
                   "message": "", "tokens": 98000, "input_tokens": 450, "output_tokens": 10000,
                   "cache_read_tokens": 75000, "cache_write_tokens": 12500, "reasoning_tokens": 2000, "cost_usd": 1.5}
                  for turn, began in ((1, 178), (2, 165))],
        "tool_calls": [call("r1", "deliver-change", "implement", -1, 1, "c1", "Bash", *opus, 170, 1500.0, False),
                       call("r1", "deliver-change", "implement", -1, 1, "c2", "Read", *opus, 169, 20.0, False),
                       call("r1", "deliver-change", "review", 0, 1, "c3", "exec_command", *sol, 147, 300.0, True),
                       call("r3", "pr-review", "review", 0, 2, "c4", "Grep", *opus, 45, 40.0, None)],
        "routes": [{"run_id": "r1", "node": "review", "pass": 1, "time": at(now, 140), "taken": 0, "to": ["synthesize"]}],
        "metric_points": [point("r1", "deliver-change", "review.problems", "review", 1, 0, 140, 3),
                          point("r1", "deliver-change", "review.problems", "review", 1, 1, 138, 2),
                          point("r3", "pr-review", "review.problems", "review", 1, 0, 30, 1),
                          point("r1", "deliver-change", "finding", "synthesize", 1, -1, 135, 1, id="f1", state="open"),
                          point("r1", "deliver-change", "finding", "synthesize", 1, -1, 135, 1, id="f2", state="open"),
                          point("r1", "deliver-change", "finding", "synthesize", 2, -1, 125, 1, id="f1", state="fixed"),
                          point("r1", "deliver-change", "finding", "synthesize", 2, -1, 125, 1, id="f2", state="open")],
    }


# What each panel shows over the last day for wf_rows, with each number to one decimal place
PANELS = {
    "Runs by state": [["failed", 1], ["succeeded", 2]],
    "Errors and retries per model": [["claude-code", "claude-opus-5-5", 5, 2, 0, 0, 0, 0, 0],
                                     ["codex", "gpt-6.1-sol", 3, 0, 1, 0, 0, 1, 0],
                                     ["cursor", "gemini-3.8-flash-high", 1, 0, 1, 1, 0, 0, 0]],
    "Tokens and cost per model": [["claude-code", "claude-opus-5-5", 5, 299500, 2400, 33500, 7900, 231000, 32600, 4.0],
                                  ["codex", "gpt-6.1-sol", 3, 107000, 75000, 20000, 10500, 12000, 0, 0.0],
                                  ["cursor", "gemini-3.8-flash-high", 1, 0, 0, 0, 0, 0, 0, 0.0]],
    "Tool calls per model": [["claude-code", "claude-opus-5-5", 3, 520.0, 1354.0, 0, 1560.0],
                             ["codex", "gpt-6.1-sol", 1, 300.0, 300.0, 1, 300.0]],
    "Node duration p50, p95, max": [["deliver-change", "implement", 2, 1440.0, 1656.0, 1680.0],
                                    ["deliver-change", "review", 3, 480.0, 480.0, 480.0],
                                    ["deliver-change", "synthesize", 2, 270.0, 297.0, 300.0],
                                    ["deliver-change", "tests", 1, 120.0, 120.0, 120.0],
                                    ["pr-review", "review", 2, 900.0, 1170.0, 1200.0]],
    "Review problems per model": [["deliver-change", "claude-code", "claude-opus-5-5", 1, 2.0, 2.0],
                                  ["deliver-change", "codex", "gpt-6.1-sol", 1, 3.0, 3.0],
                                  ["pr-review", "claude-code", "claude-opus-5-5", 1, 1.0, 1.0]],
    "Problem states per pass": [["deliver-change", 1, "open", 2], ["deliver-change", 2, "fixed", 1],
                                ["deliver-change", 2, "open", 1]],
}

EVERY_WORKFLOW = re.compile(r"\$__conditionalAll\((.+?), \$workflow\)")


def as_sent(sql: str, workflow: str | None) -> str:
    """A panel's SQL as Grafana's ClickHouse plugin sends it, for every workflow (None) or for one."""
    sql = EVERY_WORKFLOW.sub(lambda m: "1=1" if workflow is None else m[1].replace("${workflow:singlequote}", f"'{workflow}'"),
                             sql)
    assert "$workflow" not in sql and "${workflow" not in sql
    return sql


def rows(stack: Stack, query: dict, start: datetime, end: datetime, workflow: str | None = None) -> list[list]:
    """What Grafana's /api/ds/query gives for one query of a panel from start to end: the rows of each frame."""
    span = {"from": str(int(start.timestamp() * 1000)), "to": str(int(end.timestamp() * 1000))}
    sent = {**query, "rawSql": as_sent(query["rawSql"], workflow), "intervalMs": 600000, "maxDataPoints": 144}
    result = grafana(stack, "/api/ds/query", {**span, "queries": [sent]})["results"][query["refId"]]
    assert "error" not in result, result["error"]
    found = []
    for frame in result.get("frames", []):
        columns = frame["data"]["values"]
        found += [list(row) for row in zip(*columns)] if columns else []
    return found


def rounded(found: list[list]) -> list[list]:
    return sorted([round(value, 1) if isinstance(value, float) else value for value in row] for row in found)


@pytest.mark.docker
def test_the_stack_starts_empty_and_each_panel_shows_the_rows_of_wf_tables(stack):
    assert grafana(stack, "/api/health")["database"] == "ok"
    assert grafana(stack, "/api/plugins/grafana-clickhouse-datasource/settings")["info"]["version"] == "4.23.0"
    assert [plugin["id"] for plugin in grafana(stack, "/api/plugins?core=0")] == ["grafana-clickhouse-datasource"]
    logged = subprocess.run([*stack.compose, "logs", "grafana"], capture_output=True, text=True, check=True).stdout
    assert set(re.findall(r"\{ID:(\S+) Version:(\S*) URL:", logged)) == {("grafana-clickhouse-datasource", "4.23.0")}
    assert grafana(stack, "/api/dashboards/home")["dashboard"]["uid"] == "wf-runs"
    admin = base64.b64encode(b"admin:admin").decode()
    assert status(stack.grafana + "/api/admin/settings", headers={"Authorization": f"Basic {admin}"}) in (401, 403)
    keys = {"X-ClickHouse-User": "wf", "X-ClickHouse-Key": "wf"}
    assert status(f"{stack.clickhouse}/", b"SELECT 1", keys) == 200
    assert status(f"{stack.clickhouse}/?user=wf&password=wf", b"SELECT 1", {"Content-Type": "text/plain"}) == 403
    # A page on a DNS name that points to 127.0.0.1 sends its requests with that name in Host
    assert status(f"{stack.clickhouse}/", b"SELECT 1", {**keys, "Host": "attacker.example:8123"}) == 403
    assert status(f"{stack.grafana}/api/ds/query", b'{"queries": []}',
                  {"Content-Type": "application/json", "Host": "attacker.example:3000"}) == 301

    assert clickhouse(stack, "SELECT name, value FROM system.server_settings WHERE name IN "
                             "('mark_cache_size', 'max_server_memory_usage') ORDER BY name FORMAT TSV") == (
        "mark_cache_size\t268435456\nmax_server_memory_usage\t1073741824\n")
    assert clickhouse(stack, "SELECT value FROM system.settings WHERE name IN ('max_block_size', 'max_threads') "
                             "ORDER BY name FORMAT TSV") == "8192\n1\n"
    clickhouse(stack, "SYSTEM FLUSH LOGS")  # makes the table of each system log that the server's config.xml turns on
    assert clickhouse(stack, "SELECT count() FROM system.tables WHERE database = 'system' AND endsWith(name, '_log')") == "0\n"

    now = datetime.now(timezone.utc)
    for table in TABLES:
        assert clickhouse(stack, f"SELECT count() FROM wf.{table}") == "0\n"
        clickhouse(stack, f"SYSTEM STOP MERGES wf.{table}")  # so each old row stays until a query reads FINAL
    exported = wf_rows(now)
    for table, inserted in exported.items():
        clickhouse(stack, f"INSERT INTO wf.{table} FORMAT JSONEachRow", inserted)
    for table, inserted in exported.items():  # a second export of r1
        clickhouse(stack, f"INSERT INTO wf.{table} FORMAT JSONEachRow", [row for row in inserted if row["run_id"] == "r1"])
    r2 = next(row for row in exported["runs"] if row["run_id"] == "r2")
    clickhouse(stack, "INSERT INTO wf.runs FORMAT JSONEachRow",  # r2 while it ran, loaded before its last export
               [{**r2, "status": "running", "ended": None, "duration_s": None, "loaded_at": at(now, 60)}])
    for table, inserted in exported.items():
        again = sum(row["run_id"] == "r1" for row in inserted) + (table == "runs")
        assert clickhouse(stack, f"SELECT count() FROM wf.{table}") == f"{len(inserted) + again}\n"
        assert clickhouse(stack, f"SELECT count() FROM wf.{table} FINAL") == f"{len(inserted)}\n"

    dashboard = grafana(stack, "/api/dashboards/uid/wf-runs")["dashboard"]
    shipped = json.loads(DASHBOARD.read_text())
    assert [p["title"] for p in dashboard["panels"]] == [p["title"] for p in shipped["panels"]]
    assert sorted(p["title"] for p in dashboard["panels"]) == sorted([*PANELS, "Runs over time"])
    (variable,) = dashboard["templating"]["list"]
    listed = rows(stack, {"refId": "A", "datasource": variable["datasource"], "editorType": "sql", "format": 1,
                          "rawSql": variable["query"]["rawSql"]}, now - DAY, now)
    assert listed == [["deliver-change"], ["pr-review"]]
    for panel in dashboard["panels"]:
        (query,) = panel["targets"]
        title = panel["title"]
        assert EVERY_WORKFLOW.search(query["rawSql"]), f"{title} doesn't follow the workflow variable"
        found = rows(stack, query, now - DAY, now)
        if title == "Runs over time":
            assert sum(count or 0 for row in found for count in row[1:]) == 3, found
        else:
            assert rounded(found) == PANELS[title], title
        assert rows(stack, query, now - DAY, now, "deliver-change"), f"{title} shows no rows of deliver-change"
        assert rows(stack, query, now - DAY, now, "no-such-workflow") == [], f"{title} shows other workflows"
        assert rows(stack, query, now - 30 * DAY, now - DAY) == [], f"{title} shows rows from outside its time range"


# ---------- what the Collector keeps ----------


CODEX_KEPT = ["codex.sampling", "session_task.turn", "run_turn"]
CODEX_DROPPED = ["fs.get_metadata", "append_items"]
IDENTITY = {"user.email": "someone@example.com", "user.account_uuid": "4f9d1c1e", "user.account_id": "user-1",
            "organization.id": "org-1"}
LONG = "".join(f"{n:04d} " for n in range(1000))  # 5,000 bytes
WIDE = "€" * 400  # 1,200 bytes: the Collector cuts at a character boundary, so 510 bytes stay


def attributes(values: dict) -> list[dict]:
    return [{"key": key, "value": {"stringValue": value}} for key, value in values.items()]


def otlp(stack: Stack, now: datetime) -> dict[str, dict]:
    """One OTLP request for each signal, as Claude Code and Codex send them."""
    nanos = str(int(now.timestamp() * 1e9))

    def span(name: str, values: dict) -> dict:
        return {"traceId": uuid.uuid4().hex, "spanId": uuid.uuid4().hex[:16], "name": name, "kind": 1,
                "startTimeUnixNano": nanos, "endTimeUnixNano": nanos, "attributes": attributes(values)}

    def event(name: str, values: dict) -> dict:
        return {"timeUnixNano": nanos, "observedTimeUnixNano": nanos, "severityNumber": 9,
                "body": {"stringValue": name}, "attributes": attributes({"event.name": name, **values})}

    codex = {"attributes": attributes({"service.name": "codex-app-server", "service.version": "0.163.0"})}
    claude = {"attributes": attributes({"service.name": "claude-code", "service.version": "2.1.301", **IDENTITY})}
    return {
        "traces": {"resourceSpans": [
            {"resource": codex, "scopeSpans": [{"scope": {"name": "codex"}, "spans": [
                span(name, {"conversation.id": "c1"}) for name in CODEX_KEPT + CODEX_DROPPED]}]},
            {"resource": claude, "scopeSpans": [{"scope": {"name": "com.anthropic.claude_code"}, "spans": [
                span("claude_code.llm_request", {"session.id": "s1", "model": "claude-opus-5-5", **IDENTITY}),
                span("claude_code.tool", {"session.id": "s1", "tool_name": "Bash", "tool_input": LONG,
                                          "arguments": WIDE, "output": LONG, **IDENTITY})]}]},
        ]},
        "logs": {"resourceLogs": [
            {"resource": codex, "scopeLogs": [{"scope": {"name": "codex_otel.log_only"}, "logRecords": [
                event("codex.tool_result", {"conversation.id": "c1", "call_id": "long", "tool_name": "exec_command",
                                            "arguments": LONG, "output": WIDE, "user.email": IDENTITY["user.email"],
                                            "user.account_id": IDENTITY["user.account_id"]}),
                event("codex.tool_result", {"conversation.id": "c1", "call_id": "short", "tool_name": "exec_command",
                                            "arguments": '{"cmd": "ls"}', "output": "calc.py"})]}]},
            {"resource": claude, "scopeLogs": [{"scope": {"name": "com.anthropic.claude_code.events"}, "logRecords": [
                event("tool_result", {"session.id": "s1", "tool_name": "Bash", "tool_input": LONG, **IDENTITY}),
                event("api_request", {"session.id": "s1", "model": "claude-opus-5-5", "ttft_ms": "948", **IDENTITY})]}]},
        ]},
        "metrics": {"resourceMetrics": [
            {"resource": claude, "scopeMetrics": [{"scope": {"name": "com.anthropic.claude_code"}, "metrics": [
                {"name": "claude_code.token.usage", "unit": "tokens", "sum": {
                    "aggregationTemporality": 1, "isMonotonic": True, "dataPoints": [
                        {"startTimeUnixNano": nanos, "timeUnixNano": nanos, "asDouble": 213,
                         "attributes": attributes({"type": "output", "model": "claude-opus-5-5", "session.id": "s1",
                                                   **IDENTITY})}]}}]}]},
        ]},
    }


@pytest.mark.docker
def test_the_collector_keeps_three_codex_spans_and_cuts_tool_text_and_account_ids(stack):
    for signal, payload in otlp(stack, datetime.now(timezone.utc)).items():
        send(f"{stack.otlp}/v1/{signal}", payload)

    def count(table: str) -> int:
        return int(clickhouse(stack, f"SELECT count() FROM otel.{table}"))

    eventually(lambda: count("otel_traces") >= 5 and count("otel_logs") >= 4 and count("otel_metrics_sum") >= 1,
               "5 spans, 4 log records and 1 point in ClickHouse")

    def column(sql: str) -> list:
        return [json.loads(line)["v"] for line in clickhouse(stack, f"{sql} FORMAT JSONEachRow").splitlines()]

    # The filter ran on the one request that held all 7 spans, before the export, so a dropped span can't come later
    assert sorted(column("SELECT SpanName AS v FROM otel.otel_traces")) == sorted(
        CODEX_KEPT + ["claude_code.llm_request", "claude_code.tool"])
    keys = list(IDENTITY)
    for table, own in (("otel_traces", "SpanAttributes"), ("otel_logs", "LogAttributes"), ("otel_metrics_sum", "Attributes")):
        assert column(f"SELECT count() AS v FROM otel.{table} WHERE hasAny(mapKeys({own}), {keys}) "
                      f"OR hasAny(mapKeys(ResourceAttributes), {keys})") == [0], table
    assert column("SELECT Attributes['session.id'] AS v FROM otel.otel_metrics_sum") == ["s1"]

    cut, wide_cut = LONG[:512], "€" * 170
    assert len(cut.encode()) == 512 and len(wide_cut.encode()) == 510
    assert column("SELECT (LogAttributes['call_id'], LogAttributes['arguments'], LogAttributes['output']) AS v "
                  "FROM otel.otel_logs WHERE LogAttributes['event.name'] = 'codex.tool_result' ORDER BY v") == [
        ["long", cut, wide_cut], ["short", '{"cmd": "ls"}', "calc.py"]]
    assert column("SELECT LogAttributes['tool_input'] AS v FROM otel.otel_logs "
                  "WHERE LogAttributes['event.name'] = 'tool_result'") == [cut]
    assert column("SELECT (SpanAttributes['tool_input'], SpanAttributes['arguments'], SpanAttributes['output']) AS v "
                  "FROM otel.otel_traces WHERE SpanName = 'claude_code.tool'") == [[cut, wide_cut, cut]]
