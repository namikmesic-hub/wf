"""The example files in docs/metrics: a ClickHouse in Docker that you start yourself, the dashboard
for Grafana, and the configuration of an OpenTelemetry Collector. wf runs none of them.

The test of compose.yaml needs Docker with Compose, and pytest skips it without them. It starts no
container: `docker compose config` only reads the file."""

import asyncio
import http.server
import json
import re
import shutil
import subprocess
import threading
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import yaml

from workflow import cli, config
from workflow.harnesses import codex

ROOT = Path(__file__).parent.parent
EXAMPLES = ROOT / "docs" / "metrics"
COMPOSE = EXAMPLES / "compose.yaml"
DASHBOARD = EXAMPLES / "grafana" / "wf-runs.json"
COLLECTOR = EXAMPLES / "collector" / "config.yaml"
README = ROOT / "README.md"


def compose_answers() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return subprocess.run(["docker", "compose", "version"], capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def readme_url() -> str:
    """The url under [metrics] that the README gives for the example."""
    (url,) = re.findall(r'^url = "(http://[^"]+)"$', README.read_text(encoding="utf-8"), re.M)
    return url


@pytest.mark.skipif(not compose_answers(), reason="needs Docker with Compose")
def test_the_example_compose_file_parses_and_runs_clickhouse_alone_on_127_0_0_1(monkeypatch):
    monkeypatch.delenv("COMPOSE_PROJECT_NAME", raising=False)
    done = subprocess.run(["docker", "compose", "-f", str(COMPOSE), "config", "--format", "json"],
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    found = json.loads(done.stdout)
    assert found["name"] == "wf-clickhouse"  # not wf-metrics, the project of the stack of an older wf
    ((name, service),) = found["services"].items()
    assert name == "clickhouse"
    assert re.fullmatch(r"clickhouse/clickhouse-server:\d+\.\d+\.\d+(\.\d+)?", service["image"])  # 25.8 moves with each patch
    assert [(port["host_ip"], port["target"], port["published"]) for port in service["ports"]] == [("127.0.0.1", 8123, "8123")]
    assert {"CLICKHOUSE_USER", "CLICKHOUSE_PASSWORD", "CLICKHOUSE_DB"} <= set(service["environment"])
    mounts = {mount["target"]: mount for mount in service["volumes"]}
    data = mounts.pop("/var/lib/clickhouse")
    assert data["type"] == "volume" and data["source"] in found["volumes"]
    assert sorted(mounts) == ["/etc/clickhouse-server/config.d/http-headers.xml", "/etc/clickhouse-server/config.d/low-memory.xml",
                              "/etc/clickhouse-server/users.d/low-memory-profile.xml"]  # no SQL that makes tables
    assert all(mount["read_only"] and Path(mount["source"]).is_file() for mount in mounts.values())


def test_the_readme_gives_the_commands_and_the_url_of_the_example(wf_home):
    text = README.read_text(encoding="utf-8")
    assert "docker compose -f docs/metrics/compose.yaml up -d" in text
    assert "docker compose -f docs/metrics/compose.yaml down" in text
    assert "`docker compose -p wf-metrics down` stops that stack" in text  # the stack that an older wf started
    assert "A `COMPOSE_PROJECT_NAME` in your shell replaces that name" in text and "`-p wf-clickhouse`" in text
    service = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]["clickhouse"]
    login = service["environment"]
    url = urlsplit(readme_url())
    assert (url.username, url.password, url.path) == (login["CLICKHOUSE_USER"], login["CLICKHOUSE_PASSWORD"],
                                                      f"/{login['CLICKHOUSE_DB']}")
    assert f"{url.hostname}:{url.port}:8123" in service["ports"]
    (wf_home / "config.toml").write_text(f'[metrics]\nenabled = true\nurl = "{readme_url()}"\n')
    (wf_home / "config.toml").chmod(0o600)
    loaded = config.load().metrics
    assert loaded.enabled and loaded.url.problems == []


def test_the_dashboard_reads_the_database_of_the_example_through_one_datasource():
    dashboard = json.loads(DASHBOARD.read_text(encoding="utf-8"))
    database = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]["clickhouse"]["environment"]["CLICKHOUSE_DB"]
    queries = [panel["targets"][0] for panel in dashboard["panels"]] + [variable["query"] for variable in dashboard["templating"]["list"]]
    assert {target["datasource"]["uid"] for target in [*dashboard["panels"], *dashboard["templating"]["list"]]} == {"clickhouse"}
    assert all(set(re.findall(r"\bFROM (\w+)\.", query["rawSql"])) == {database} for query in queries)


def test_the_collector_takes_the_login_of_clickhouse_from_its_environment():
    exporter = yaml.safe_load(COLLECTOR.read_text(encoding="utf-8"))["exporters"]["clickhouse"]
    assert (exporter["endpoint"], exporter["username"], exporter["password"]) == (
        "${env:CLICKHOUSE_ENDPOINT}", "${env:CLICKHOUSE_USER}", "${env:CLICKHOUSE_PASSWORD}")
    assert "otel/opentelemetry-collector-contrib:0.162.0" in COLLECTOR.read_text(encoding="utf-8")  # its exporter is there


def test_wf_runs_no_docker_and_has_no_metrics_command(capsys):
    for source in (ROOT / "workflow").rglob("*.py"):
        assert not re.search(r"""["']docker["']""", source.read_text(encoding="utf-8")), source
    with pytest.raises(SystemExit):
        cli.main(["metrics", "up"])
    assert "invalid choice: 'metrics'" in capsys.readouterr().err


# ---------- what the Collector keeps ----------


CODEX_KEPT = ["codex.sampling", "session_task.turn", "run_turn"]


class _Collector(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        self.server.posts.append((self.path, self.rfile.read(int(self.headers.get("Content-Length", 0)))))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args):
        pass


@pytest.fixture
def otlp_server():
    """A server on this machine that answers each POST with 200, as the Collector answers OTLP.
    Its attribute posts lists the path and the body of each POST that it got."""
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Collector)
    server.posts = []
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


@pytest.mark.skipif(shutil.which("codex") is None, reason="codex isn't installed")
def test_the_codex_that_wf_starts_names_its_service_as_the_collector_s_filter_expects(tmp_path, monkeypatch, otlp_server):
    """No model call, no quota: wf's adapter starts the app server and stops it, and the server sends its spans."""
    (condition,) = yaml.safe_load(COLLECTOR.read_text())["processors"]["filter/codex-spans"]["trace_conditions"]
    service = re.search(r'IsMatch\(resource\.attributes\["service\.name"\], "(.+?)"\)', condition)[1]
    assert json.loads(re.search(r"ContainsValue\((\[.+?\]), span\.name\)", condition)[1]) == CODEX_KEPT
    url = f"http://127.0.0.1:{otlp_server.server_address[1]}/v1"
    flags = [arg for key, signal in (("exporter", "logs"), ("trace_exporter", "traces"), ("metrics_exporter", "metrics"))
             for arg in ("-c", f'otel.{key}={{otlp-http={{endpoint="{url}/{signal}",protocol="json"}}}}')]
    (tmp_path / "codex-home").mkdir()
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))  # none of your login, config or sessions

    async def open_and_close():
        harness = codex.Codex(command=[codex.program(), *codex.APP_SERVER, *flags])
        await harness.open(tmp_path)
        await harness.close()

    asyncio.run(open_and_close())
    spans = [({a["key"]: a["value"].get("stringValue") for a in resource["resource"]["attributes"]}["service.name"], span["name"])
             for path, body in otlp_server.posts if path == "/v1/traces"
             for resource in json.loads(body)["resourceSpans"] for scope in resource["scopeSpans"] for span in scope["spans"]]
    assert spans, "the app server sent no spans"
    assert all(re.search(service, name) for name, _ in spans), spans
    assert {span for _, span in spans} - set(CODEX_KEPT), "the filter would keep each of them"
