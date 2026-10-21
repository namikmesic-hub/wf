"""`wf metrics up`: the metrics stack on this machine, in Docker (metrics/compose.yaml).

An OpenTelemetry Collector takes OTLP from Claude Code and Codex, ClickHouse keeps it with wf's
own tables, and Grafana shows the dashboard of wf runs. The stack is part of wf's source, beside
this package, so `wf metrics up` needs wf installed from its checkout (`uv tool install --editable .`).
"""

import shlex
import shutil
import subprocess
import sys
from pathlib import Path

COMPOSE = Path(__file__).resolve().parent.parent / "metrics" / "compose.yaml"
PORTS = ("WF_METRICS_GRAFANA_PORT", "WF_METRICS_CLICKHOUSE_PORT", "WF_METRICS_OTLP_GRPC_PORT", "WF_METRICS_OTLP_HTTP_PORT")


def up() -> int:
    """`docker compose up -d --wait` for the stack, then print Grafana's address. Returns the exit code."""
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
    found = subprocess.run([*compose, "port", "grafana", "3000"], capture_output=True, text=True)
    address = found.stdout.strip()
    if found.returncode != 0 or not address:
        print(f"wf metrics up: the stack runs, but Docker didn't say where Grafana listens: {found.stderr.strip()}",
              file=sys.stderr)
        return 1
    print(f"Grafana: http://{address}")
    return 0
