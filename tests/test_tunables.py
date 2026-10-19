"""Tunables: a route's `max`, `timeout`, `retry`, `max_parallel` and `limits`
written as templates that read `start`, rendered once when the run starts.

Each test runs in a fresh git repo, with command nodes only, so these are cheap
and deterministic.
"""

import json
import subprocess
from pathlib import Path
from textwrap import dedent

import pytest

from workflow import cli, runner, runs, tunables


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    subprocess.run(["git", "init", "-q"], check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init"], check=True)
    monkeypatch.setattr(runner, "interactive", lambda: False)
    return tmp_path


def write(name: str, text: str) -> str:
    Path(name).write_text(dedent(text))
    return name


def only_run() -> Path:
    (folder,) = (Path(".wf") / "runs").iterdir()
    return folder


def output() -> dict:
    return json.loads((only_run() / "output.json").read_text())


def status() -> str:
    return json.loads((only_run() / "status.json").read_text())["status"]


def no_resolving(monkeypatch) -> None:
    """From here on, rendering a setting's template fails the test: the values must be the kept ones."""
    monkeypatch.setattr(tunables, "resolve", lambda *args: pytest.fail("the tunables' templates were rendered again"))


LOOP = """
version: 1
name: loop
start: {input: {max_rounds: {type: integer, default: 2}}}
nodes:
  try: {needs: [start], command: "false", check: false, routes: [{to: try, max: "{{ start.max_rounds }}"}, {to: end}]}
end: {output: {tries: {type: integer, value: "{{ history.try | length }}"}}}
"""


@pytest.mark.parametrize("given, tries", [([], 3), (["max_rounds=1"], 2), (["max_rounds=4"], 5)])
def test_a_route_max_from_an_input_caps_the_loop(repo, given, tries):
    write("loop.yaml", LOOP)
    assert cli.main(["run", "loop.yaml", *given]) == 0
    assert output() == {"tries": tries}
    routes = [json.loads(line) for line in (only_run() / "events.jsonl").read_text().splitlines() if '"route"' in line]
    assert [e["max"] for e in routes if e["to"] == ["try"]] == [tries - 1] * (tries - 1)
    assert json.loads((only_run() / "run.json").read_text())["tunables"] == {"nodes.try.routes[0].max": tries - 1}


SLOW = """
version: 1
name: slow
%s
start: {input: {wait: {type: string}}}
nodes:
  slow: {needs: [start], command: "sleep 30"%s}
end: {output: {}}
"""
TIMEOUT = 'timeout: "{{ start.wait }}"'


@pytest.mark.parametrize("text", [SLOW % ("", ", " + TIMEOUT), SLOW % ("defaults: {" + TIMEOUT + "}", "")])
def test_a_timeout_from_an_input_limits_each_attempt(repo, capsys, text):
    write("slow.yaml", text)
    assert cli.main(["run", "slow.yaml", "wait=1s"]) == 1
    assert "timed out after 1s" in capsys.readouterr().err


@pytest.mark.parametrize("retry, code", [(1, 0), (0, 1)])
def test_retry_from_an_input_gives_that_many_more_attempts(repo, capsys, retry, code):
    write("flaky.yaml", """
        version: 1
        name: flaky
        start: {input: {retry: {type: integer}}}
        nodes:
          flaky:
            needs: [start]
            retry: "{{ start.retry }}"
            command: "if [ -e once ]; then echo ok; else touch once; exit 3; fi"
        end: {output: {}}
    """)
    assert cli.main(["run", "flaky.yaml", f"retry={retry}"]) == code
    attempts = sorted(p.name for p in (only_run() / "nodes" / "flaky" / "1").glob("attempt-*"))
    assert len(attempts) == 1 + retry


@pytest.mark.parametrize("parallel, code", [(1, 0), (3, 1)])
def test_max_parallel_from_an_input_caps_the_items_running_at_once(repo, capsys, parallel, code):
    # Each item holds a lock for a while: items that overlap find it taken and fail
    write("items.yaml", """
        version: 1
        name: items
        start: {input: {parallel: {type: integer}}}
        nodes:
          each:
            needs: [start]
            for_each: "[1, 2, 3]"
            max_parallel: "{{ start.parallel }}"
            command: "mkdir lock && sleep 0.5 && rmdir lock"
        end: {output: {}}
    """)
    assert cli.main(["run", "items.yaml", f"parallel={parallel}"]) == code


def test_a_duration_limit_from_an_input_pauses_the_run(repo, capsys):
    write("limit.yaml", """
        version: 1
        name: limit
        limits: {duration: "{{ start.seconds }}s"}
        start: {input: {seconds: {type: integer}}}
        nodes:
          first: {needs: [start], command: "sleep 1.2"}
          second: {needs: [first], command: "touch second-ran"}
        end: {output: {}}
    """)
    assert cli.main(["run", "limit.yaml", "seconds=1"]) == 1
    assert status() == "limit"
    assert not Path("second-ran").exists()
    assert "limit 1s" in capsys.readouterr().err


# ---------- a bad value stops the run before it begins ----------


def tunable(setting: str) -> str:
    """A workflow whose `setting` (route.max, node.timeout, defaults.retry, limits.tokens, ...) reads the input `n`."""
    place, name = setting.split(".")
    value = f'{name}: "{{{{ start.n }}}}"'
    defaults = f"defaults: {{{value}}}\n" if place == "defaults" else ""
    limits = f"limits: {{{value}}}\n" if place == "limits" else ""
    node = f"    {value}\n" if place == "node" else ""
    route = value if place == "route" else "max: 1"
    return (f"version: 1\nname: tune\n{defaults}{limits}start: {{input: {{n: {{}}}}}}\nnodes:\n"
            f"  a:\n    needs: [start]\n    command: 'true'\n{node}"
            f"    routes: [{{to: a, {route}}}, {{to: end}}]\nend: {{output: {{}}}}\n")


@pytest.mark.parametrize("setting, value, where, message", [
    ("route.max", "0", "nodes.a.routes[0].max", "input `n` gives 0: Input should be greater than or equal to 1"),
    ("route.max", "2.5", "nodes.a.routes[0].max", "gives 2.5: Input should be a valid integer, got a number with a fractional part"),
    ("node.timeout", "soon", "nodes.a.timeout", 'input `n` gives "soon": expected a duration like 90s, 30m or 2h'),
    ("node.timeout", "{{ x }}", "nodes.a.timeout", 'gives "{{ x }}": expected a duration'),  # a value is never a template
    ("node.retry", "-1", "nodes.a.retry", "gives -1: Input should be greater than or equal to 0"),
    ("node.max_parallel", "0", "nodes.a.max_parallel", "gives 0: Input should be greater than or equal to 1"),
    ("defaults.timeout", "5", "defaults.timeout", "gives 5: expected a duration"),
    ("defaults.retry", "many", "defaults.retry", 'gives "many": Input should be a valid integer'),
    ("defaults.max_parallel", "0", "defaults.max_parallel", "gives 0: Input should be greater than or equal to 1"),
    ("limits.cost_usd", "0", "limits.cost_usd", "gives 0: Input should be greater than 0"),
    ("limits.tokens", "-5", "limits.tokens", "gives -5: Input should be greater than 0"),
    ("limits.duration", "2 hours", "limits.duration", 'gives "2 hours": expected a duration'),
    ("route.max", "null", "nodes.a.routes[0].max", "input `n` gives none, but this setting needs a value"),
])
def test_a_bad_value_stops_the_run_before_it_begins(repo, capsys, setting, value, where, message):
    write("tune.yaml", tunable(setting))
    assert cli.main(["validate", "tune.yaml"]) == 0  # n has no default: only the run knows the value
    assert cli.main(["run", "tune.yaml", f"n={value}"]) == 1
    err = capsys.readouterr().err
    assert f"wf run: {where}: " in err and message in err, err
    assert not Path(".wf/runs").exists()


@pytest.mark.parametrize("setting", ["route.max", "node.timeout", "node.retry", "node.max_parallel", "defaults.timeout",
                                     "defaults.retry", "defaults.max_parallel", "limits.cost_usd", "limits.tokens",
                                     "limits.duration"])
def test_a_good_value_runs(repo, setting):
    good = {"timeout": "5m", "duration": "1h"}.get(setting.rsplit(".", 1)[1], "1")
    write("tune.yaml", tunable(setting))
    assert cli.main(["run", "tune.yaml", f"n={good}"]) == 0


def test_every_bad_setting_is_named_at_once(repo, capsys):
    write("two.yaml", """
        version: 1
        name: two
        defaults: {retry: "{{ start.retry }}"}
        start: {input: {rounds: {type: integer}, retry: {type: integer}}}
        nodes:
          a: {needs: [start], command: "true", routes: [{to: a, max: "{{ start.rounds + start.retry }}"}, {to: end}]}
        end: {output: {}}
    """)
    assert cli.main(["run", "two.yaml", "rounds=0", "retry=-1"]) == 1
    assert capsys.readouterr().err.splitlines() == [
        "wf run: defaults.retry: input `retry` gives -1: Input should be greater than or equal to 0",
        "wf run: nodes.a.routes[0].max: inputs `rounds` and `retry` give -1: Input should be greater than or equal to 1",
    ]


def test_an_optional_input_left_out_reads_as_none_as_in_every_template(repo, capsys):
    write("opt.yaml", LOOP.replace("max_rounds: {type: integer, default: 2}", "max_rounds: {type: integer, optional: true}")
          .replace("start.max_rounds", "start.max_rounds or 1"))
    assert cli.main(["run", "opt.yaml"]) == 0
    assert output() == {"tries": 2}
    write("opt.yaml", LOOP.replace("default: 2", "optional: true"))
    assert cli.main(["run", "opt.yaml"]) == 1
    assert ("wf run: nodes.try.routes[0].max: input `max_rounds` gives none, but this setting needs a value"
            in capsys.readouterr().err)


# ---------- wf resume keeps the values the run started with ----------


HALFWAY = """
version: 1
name: halfway
start: {input: {max_rounds: {type: integer}}}
nodes:
  try:
    needs: [start]
    command: "echo x >> tries; test $(wc -l < tries) -ne 2 || test -e go"
    routes: [{to: try, max: "{{ start.max_rounds }}"}, {to: end}]
end: {output: {tries: {type: integer, value: "{{ history.try | length }}"}}}
"""


def test_a_resumed_run_keeps_the_values_it_started_with(repo, monkeypatch, capsys):
    write("halfway.yaml", HALFWAY)  # the 2nd pass fails until `go` exists
    assert cli.main(["run", "halfway.yaml", "max_rounds=3"]) == 1
    assert status() == "failed"
    Path("go").touch()
    no_resolving(monkeypatch)
    assert cli.main(["resume", only_run().name]) == 0
    assert output() == {"tries": 4}


def test_a_run_in_a_runs_folder_keeps_its_values_there(repo, monkeypatch, tmp_path_factory):
    elsewhere = tmp_path_factory.mktemp("runs")
    write("halfway.yaml", HALFWAY)
    assert cli.main(["run", "halfway.yaml", "max_rounds=3", "--runs-dir", str(elsewhere)]) == 1
    (run,) = runs.all_runs(Path.cwd(), elsewhere)
    assert json.loads((run.dir / "run.json").read_text())["tunables"] == {"nodes.try.routes[0].max": 3}
    Path("go").touch()
    no_resolving(monkeypatch)
    assert cli.main(["resume", run.id, "--runs-dir", str(elsewhere)]) == 0
    assert json.loads((run.dir / "output.json").read_text()) == {"tries": 4}
    assert not Path(".wf").exists()


def test_a_run_started_in_the_background_uses_the_values_it_was_started_with(repo, monkeypatch):
    write("loop.yaml", LOOP)
    monkeypatch.setattr("workflow.operate.detach", lambda run, args, lock: 0)  # leave starting it to us
    assert cli.main(["run", "loop.yaml", "max_rounds=1", "--detach"]) == 0
    no_resolving(monkeypatch)
    assert cli.main(["resume", only_run().name]) == 0
    assert output() == {"tries": 2}


# ---------- sub-workflows take theirs from `with` ----------


def parent(rounds: int) -> str:
    return f"""
        version: 1
        name: parent
        start: {{input: {{}}}}
        nodes:
          sub: {{needs: [start], workflow: halfway.yaml, with: {{max_rounds: {rounds}}}}}
        end: {{output: {{tries: "{{{{ sub.tries }}}}"}}}}
    """


def test_a_sub_workflow_takes_its_tunables_from_with(repo):
    write("halfway.yaml", HALFWAY)
    write("parent.yaml", parent(1))
    Path("go").touch()
    assert cli.main(["run", "parent.yaml"]) == 0
    assert output() == {"tries": 2}
    kept = only_run() / "nodes" / "sub" / "1" / "tunables.json"
    assert json.loads(kept.read_text()) == {"nodes.try.routes[0].max": 1}


def test_a_bad_value_from_with_fails_the_workflow_node(repo, capsys):
    write("halfway.yaml", HALFWAY)
    write("parent.yaml", parent(0))
    assert cli.main(["run", "parent.yaml"]) == 1
    assert ("sub: in halfway, nodes.try.routes[0].max: input `max_rounds` gives 0: "
            "Input should be greater than or equal to 1") in capsys.readouterr().err


def test_a_resumed_sub_workflow_keeps_the_values_it_started_with(repo, monkeypatch):
    write("halfway.yaml", HALFWAY)
    write("parent.yaml", parent(3))
    assert cli.main(["run", "parent.yaml"]) == 1  # the sub-workflow's 2nd pass fails, and so does its node
    Path("go").touch()
    no_resolving(monkeypatch)
    assert cli.main(["resume", only_run().name]) == 0
    assert output() == {"tries": 4}  # the sub-workflow started over, with the same max
