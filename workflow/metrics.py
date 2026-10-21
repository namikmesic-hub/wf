"""`wf metrics up`: the metrics stack on this machine, in Docker (metrics/compose.yaml).

An OpenTelemetry Collector takes OTLP from Claude Code and Codex, ClickHouse keeps it with wf's
own tables, and Grafana shows the dashboard of wf runs. The stack is part of wf's source, beside
this package, so `wf metrics up` needs wf installed from its checkout (`uv tool install --editable .`).
"""

import http.client
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

COMPOSE = Path(__file__).resolve().parent.parent / "metrics" / "compose.yaml"
PORTS = ("WF_METRICS_GRAFANA_PORT", "WF_METRICS_CLICKHOUSE_PORT", "WF_METRICS_OTLP_GRPC_PORT", "WF_METRICS_OTLP_HTTP_PORT")
READY_S = 60  # how long the Collector may take to answer OTLP after docker compose up


def up() -> int:
    """`docker compose up -d --wait` for the stack, then wait for the Collector to take OTLP, and print
    Grafana's address. Returns the exit code."""
    if shutil.which("docker") is None:
        print("wf metrics up: needs Docker with Compose, and there's no `docker` on PATH", file=sys.stderr)
        return 1
    if not COMPOSE.is_file():
        print(f"wf metrics up: there's no {COMPOSE}. The stack is part of wf's source: install wf from its "
              "checkout with `uv tool install --editable .`", file=sys.stderr)
        return 1
    compose = ["docker", "compose", "-f", str(COMPOSE)]
    started = subprocess.run([*compose, "up", "-d", "--wait"])
    if started.returncode != 0:
        print(f"wf metrics up: `docker compose up` failed with exit code {started.returncode}. "
              f"`{shlex.join(compose)} logs` shows what each service said, and `{shlex.join(compose)} down` stops them. "
              f"To move a port that is in use, set {', '.join(PORTS[:-1])} or {PORTS[-1]} to another port", file=sys.stderr)
        return 1
    collector, said = _address(compose, "collector", 4318)
    if not collector or not _takes_otlp(collector):
        print(f"wf metrics up: the Collector doesn't take OTLP{f' at {collector}' if collector else f': {said}'}. "
              f"`{shlex.join(compose)} logs collector` shows why", file=sys.stderr)
        return 1
    grafana, said = _address(compose, "grafana", 3000)
    if not grafana:
        print(f"wf metrics up: the stack runs, but Docker didn't say where Grafana listens: {said}", file=sys.stderr)
        return 1
    print(f"Grafana: http://{grafana}")
    return 0


def _address(compose: list[str], service: str, port: int) -> tuple[str, str]:
    """Where a port of a service listens on this machine, as host:port, or "" and what Docker said."""
    found = subprocess.run([*compose, "port", service, str(port)], capture_output=True, text=True)
    return (found.stdout.strip() if found.returncode == 0 else ""), found.stderr.strip()


def _takes_otlp(address: str) -> bool:
    """Whether the Collector answers an empty OTLP request within READY_S. It starts its receiver after its
    exporter, which makes its tables in ClickHouse, so a Collector that can't reach ClickHouse never answers."""
    host, port = address.rsplit(":", 1)
    deadline = time.monotonic() + READY_S
    while True:
        connection = http.client.HTTPConnection(host, int(port), timeout=5)
        try:
            connection.request("POST", "/v1/traces", b"{}", {"Content-Type": "application/json"})
            if connection.getresponse().status == 200:
                return True
        except (OSError, http.client.HTTPException):  # no answer yet
            pass
        finally:
            connection.close()
        if time.monotonic() > deadline:
            return False
        time.sleep(0.5)
