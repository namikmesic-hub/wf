"""The run's trace (trace.py) and `wf trace` (trace_view.py), end to end. A fake agent and real
commands run a workflow. The tests read trace.jsonl, blobs/ and what `wf trace` prints."""

import json
import subprocess
from pathlib import Path
from textwrap import dedent

import pytest

from fakes import Fake, submit, talk
from workflow import cli, harnesses, runner, templates, trace
from workflow.output_tool import REMINDER


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    subprocess.run(["git", "init", "-q"], check=True)
    monkeypatch.setattr(runner, "interactive", lambda: False)
    return tmp_path


def write(text: str) -> str:
    Path("flow.yaml").write_text(dedent(text))
    return "flow.yaml"


def run_dir() -> Path:
    (folder,) = (Path(".wf") / "runs").iterdir()
    return folder


def records() -> list[dict]:
    return [json.loads(line) for line in (run_dir() / "trace.jsonl").read_text().splitlines()]


def of(kind: str, **match) -> list[dict]:
    return [r for r in records() if r["kind"] == kind and all(r.get(k) == v for k, v in match.items())]


def everything_written() -> str:
    """trace.jsonl and every blob, as one text."""
    return "".join(path.read_text() for path in [run_dir() / "trace.jsonl", *(run_dir() / "blobs").glob("*")] if path.exists())


# ---------- what a trace holds ----------


LOOPED = """
version: 1
name: traced
defaults: {harness: claude-code, harnesses: {claude-code: {model: opus, effort: high}}}
start: {input: {task: {type: string, default: tidy up}}}
nodes:
  plan:
    needs: [start]
    instruction: "Plan this: {{ start.task }}"
    output: {steps: {type: array, items: {type: string}}}
  each:
    needs: [plan]
    for_each: plan.steps
    command: 'printf "%s" "$STEP"'
    env: {STEP: "{{ item }}"}
  check:
    needs: [each]
    command: "test {{ each | length }} -ge 3"
    check: false
    routes:
      - {when: "check.exit_code == 0", to: end}
      - {to: plan, max: 1}
      - {to: end}
end: {output: {steps: "{{ plan.steps }}"}}
"""


def test_a_runs_trace_holds_each_kind_of_record_and_its_ids_nest(repo, monkeypatch):
    fake = Fake("claude-code", talk(), submit({"steps": "a"}, {"steps": ["a", "b"]}), submit({"steps": ["a", "b", "c"]}))
    monkeypatch.setitem(harnesses.FACTORIES, "claude-code", fake.factory)
    assert cli.main(["run", write(LOOPED)]) == 0
    found = records()
    assert {r["kind"] for r in found} == {
        "run", "pass", "job", "render", "routes", "attempt", "turn", "message", "tool_call", "tool_result",
        "submit_result", "turn_end", "attempt_end", "job_end", "run_stopped"}
    ids = {r["id"] for r in found}
    for record in found:  # each parent is a record of its own, and each id starts with its parent's
        assert record["parent"] is None or (record["parent"] in ids and (record["parent"] == "run" or record["id"].startswith(record["parent"])))
    assert {r["field"] for r in of("render")} == {"instruction", "harness", "model", "effort", "for_each", "command", "env.STEP",
                                                   "routes[0].when", "end.output.steps"}

    first = of("render", id="plan#1", field="instruction")[0]
    assert (first["template"], first["reads"], first["result"]) == (
        "Plan this: {{ start.task }}", [{"path": "start.task", "value": "tidy up"}], "Plan this: tidy up")
    turns = of("turn", id="plan#1.a1.t2")[0]
    assert (turns["sends"], turns["text"]) == ("reminder", REMINDER)  # the first turn submitted nothing
    assert of("message", id="plan#1.a1.t1.m1")[0]["text"] == "Still looking."
    assert of("tool_call", id="plan#1.a1.t2.c1")[0] | {"time": 0} == {
        "time": 0, "kind": "tool_call", "id": "plan#1.a1.t2.c1", "parent": "plan#1.a1.t2", "node": "plan", "pass": 1,
        "attempt": 1, "turn": 2, "tool": "Read", "arguments": {"file_path": "notes.txt"}}
    assert of("tool_result", id="plan#1.a1.t2.c1")[0]["result"] == "the notes"
    refused, accepted = of("submit_result", node="plan", **{"pass": 1})
    assert (refused["accepted"], refused["arguments"], refused["problems"]) == (False, {"steps": "a"}, ["steps: 'a' is not of type 'array'"])
    assert (accepted["id"], accepted["accepted"], accepted["problems"]) == ("plan#1.a1.t2.s2", True, [])

    (attempt,) = of("attempt_end", id="plan#1.a1")
    assert {k: attempt[k] for k in ("harness", "model", "effort", "access", "web", "outcome", "usage")} == {
        "harness": "claude-code", "model": "opus", "effort": "high", "access": "read-only", "web": False,
        "outcome": "succeeded", "usage": {"tokens": 200, "cost_usd": 0.02}}
    assert attempt["session"] == {"start": "fresh", "id": "session-1"}
    assert attempt["workspace"]["path"] == str(repo.resolve())
    assert attempt["started"] <= attempt["ended"]

    item = of("job", id="each#1[1]")[0]
    assert (item["parent"], item["index"], item["item"]) == ("each#1", 1, "b")
    command = of("attempt_end", id="each#1[1].a1")[0]
    assert (command["command"], command["env"], command["exit_code"]) == ('printf "%s" "$STEP"', {"STEP": "b"}, 0)
    assert Path(command["stdout"]).read_text() == "b" and Path(command["stderr"]).read_text() == ""

    first_route, second_route = of("routes")
    assert (first_route["id"], first_route["taken"], first_route["to"]) == ("check#1", 1, ["plan"])
    assert first_route["routes"][0] | {"reads": None} == {
        "index": 0, "to": ["end"], "when": "check.exit_code == 0", "max": None, "taken_before": 0, "used_up": False,
        "checked": True, "result": False, "reads": None}
    assert first_route["routes"][0]["reads"] == [{"path": "check.exit_code", "value": 1}]
    assert [r["checked"] for r in first_route["routes"]] == [True, True, False]
    assert (second_route["id"], second_route["taken"]) == ("check#2", 0)
    assert of("render", id="each#1", field="for_each")[0]["reads"] == [{"path": "plan.steps", "value": ["a", "b"]}]
    assert of("run_stopped")[0] | {"time": 0} == {
        "time": 0, "kind": "run_stopped", "id": "run", "parent": None, "status": "succeeded",
        "output": {"steps": ["a", "b", "c"]}}


def test_a_large_value_is_stored_once_and_referenced(repo, capsys):
    big = "x" * 3000 + "end"
    Path("big.json").write_text(json.dumps({"big": big}))
    write("""
        version: 1
        name: big
        start: {input: {big: {type: string}}}
        nodes:
          first: {needs: [start], command: 'printf "%s" "$BIG" | wc -c', env: {BIG: "{{ start.big }}"}}
          second: {needs: [first], command: 'printf "%s" "$BIG" | wc -c', env: {BIG: "{{ start.big }}"}}
        end: {output: {size: "{{ second.stdout }}"}}
    """)
    assert cli.main(["run", "flow.yaml", "--input", "big.json"]) == 0
    reads = [r["reads"][0]["value"] for r in of("render", field="env.BIG")]
    assert len(reads) == 2 and reads[0] == reads[1]
    ref = reads[0]
    assert set(ref) == {"$blob", "size", "start"} and ref["size"] == 3003 and ref["start"] == "x" * trace.PREVIEW
    assert (run_dir() / ref["$blob"]).read_text() == big
    assert [p for p in (run_dir() / "blobs").iterdir() if p.read_text() == big] == [run_dir() / ref["$blob"]]  # once
    assert all(len(line) < 2 * trace.CAP for line in (run_dir() / "trace.jsonl").read_text().splitlines())
    capsys.readouterr()
    assert cli.main(["trace", run_dir().name, "--var", "start.big", "--json"]) == 0
    assert [m["value"] for m in json.loads(capsys.readouterr().out)["values"]] == [big, big]  # read back whole


SECRET = "tok-3f9a1c2b4d5e-only-the-command-sees-it"


def test_a_secret_never_enters_the_trace(repo, monkeypatch, capsys):
    async def echoes(session):  # an agent that saw the secret in its instruction, and repeats it
        session.spec.trace.text(f"The deploy used {SECRET}.")
        session.spec.trace.tool_call("c1", "Bash", {"command": f"echo {SECRET}"})
        session.spec.trace.tool_result("c1", f"{SECRET}\n")
        return await submit({"summary": f"deployed with {SECRET}"})(session)

    fake = Fake("claude-code", echoes)
    monkeypatch.setitem(harnesses.FACTORIES, "claude-code", fake.factory)
    write("""
        version: 1
        name: secretive
        defaults: {harness: claude-code}
        start: {input: {token: {type: string}, who: {type: string, default: ops}}}
        nodes:
          deploy:
            needs: [start]
            command: 'printf "deployed with %s" "$TOKEN"'
            env: {TOKEN: "{{ start.token }}", PLAIN: "{{ start.who }}"}
            secret_env: [TOKEN]
          approve:
            needs: [deploy]
            human: "It says: {{ deploy.stdout }}"
            output: {ok: {type: boolean}}
          report:
            needs: [approve]
            instruction: "Report on this: {{ deploy.stdout }} for {{ start | tojson }}"
        end: {output: {said: "{{ report.summary }}"}}
    """)
    assert cli.main(["run", "flow.yaml", f"token={SECRET}", "--no-input"]) == 2  # waits for approve
    capsys.readouterr()
    cli.main(["status", run_dir().name, "--json"])
    key = json.loads(capsys.readouterr().out)["questions"][0]["key"]
    Path("answers.json").write_text(json.dumps({key: {"ok": True}}))
    assert cli.main(["answer", run_dir().name, "answers.json"]) == 0  # a new trace writer: it learns the secret again

    assert SECRET in fake.sent[0]  # the agent really had it
    written = everything_written()
    assert SECRET not in written
    assert of("run", action="started")[0]["inputs"] == {"token": trace.HIDDEN, "who": "ops"}
    (env,) = of("attempt_end", node="deploy")
    assert env["env"] == {"TOKEN": trace.HIDDEN, "PLAIN": "ops"}
    assert of("render", field="env.TOKEN")[0]["reads"] == [{"path": "start.token", "value": trace.HIDDEN}]
    assert of("tool_call", node="report")[0]["arguments"] == {"command": f"echo {trace.HIDDEN}"}
    assert of("job_end", node="report")[0]["output"] == {"summary": f"deployed with {trace.HIDDEN}"}
    assert of("run_stopped")[-1]["output"] == {"said": f"deployed with {trace.HIDDEN}"}


def test_a_sub_workflows_records_nest_under_the_attempt_that_runs_it(repo, capsys):
    Path("step.yaml").write_text(dedent("""
        version: 1
        name: step
        start: {input: {word: {type: string}}}
        nodes:
          say: {needs: [start], command: "printf '%s' {{ start.word | quote }}"}
        end: {output: {said: "{{ say.stdout }}"}}
    """))
    write("""
        version: 1
        name: outer
        start: {input: {}}
        nodes:
          each:
            needs: [start]
            for_each: '["one", "two"]'
            workflow: step.yaml
            with: {word: "{{ item }}"}
        end: {output: {said: {type: array, items: {type: object}, value: "{{ each }}"}}}
    """)
    assert cli.main(["run", "flow.yaml"]) == 0
    (say,) = of("render", field="command", where="each[1]/")
    assert (say["id"], say["parent"], say["node"]) == ("each#1[1].a1/say#1", "each#1[1].a1", "say")
    assert say["reads"] == [{"path": "start.word", "value": "two"}]
    assert of("render", id="each#1[1]", field="with.word")[0]["result"] == "two"
    assert {r["id"] for r in of("render", field="end.output.said")} == {"each#1[0].a1/end", "each#1[1].a1/end", "end"}
    capsys.readouterr()
    assert cli.main(["trace", run_dir().name, "--node", "each[1]/say", "--var", "start.word"]) == 0
    assert capsys.readouterr().out.split() == ["each#1[1].a1/say#1", "command", "start.word", "=", '"two"']


# ---------- wf trace ----------


COUNTED = """
version: 1
name: counted
start: {input: {}}
nodes:
  count:
    needs: [start]
    command: "echo x >> tries; wc -l < tries | tr -d ' '"
  check:
    needs: [count]
    command: "test {{ count.stdout | int }} -ge 3"
    check: false
    routes:
      - {when: "check.exit_code != 0", to: count, max: 5}
      - {to: end}
end: {output: {count: "{{ count.stdout }}"}}
"""


def test_wf_trace_var_shows_a_value_across_the_passes_of_a_loop(repo, capsys):
    assert cli.main(["run", write(COUNTED)]) == 0
    run_id = run_dir().name
    capsys.readouterr()
    assert cli.main(["trace", run_id, "--var", "count.stdout"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        'check#1  command           count.stdout = "1\\n"',
        'check#2  command           count.stdout = "2\\n"',
        'check#3  command           count.stdout = "3\\n"',
        'end      end.output.count  count.stdout = "3\\n"',
    ]
    assert cli.main(["trace", run_id, "--var", "count", "--field", "command", "--json"]) == 0
    values = json.loads(capsys.readouterr().out)["values"]
    assert [(v["pass"], v["path"], v["value"]) for v in values] == [(1, "count.stdout", "1\n"), (2, "count.stdout", "2\n"),
                                                                       (3, "count.stdout", "3\n")]


def test_wf_trace_says_why_a_route_was_taken(repo, capsys):
    assert cli.main(["run", write(COUNTED)]) == 0
    capsys.readouterr()
    assert cli.main(["trace", run_dir().name, "--node", "check", "--pass", "2", "--field", "routes"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "  check#2 routes → count (route 0)",
        "      route 0 → count when check.exit_code != 0, max 5, taken 1 so far: true",
        "          check.exit_code = 1",
        "      route 1 → end: not checked",
    ]
    assert cli.main(["trace", run_dir().name, "--node", "check", "--pass", "3", "--field", "routes"]) == 0
    assert "route 1 → end: true" in capsys.readouterr().out
    assert cli.main(["trace", run_dir().name, "--field", "routes", "--var", "check.exit_code"]) == 0
    assert [line.split()[-1] for line in capsys.readouterr().out.splitlines()] == ["1", "1", "0"]


def test_wf_trace_shows_a_field_pass_by_pass_and_the_whole_run(repo, capsys):
    assert cli.main(["run", write(COUNTED)]) == 0
    capsys.readouterr()
    assert cli.main(["trace", run_dir().name, "--field", "command", "--node", "check"]) == 0
    shown = capsys.readouterr().out
    assert shown.startswith("command of check:\n    test {{ count.stdout | int }} -ge 3\n  check#1\n")
    assert '    → "test 2 -ge 3"' in shown
    assert cli.main(["trace", run_dir().name]) == 0
    outline = capsys.readouterr().out
    assert outline.startswith("run started: counted, input {}\ncount#1\n")
    assert "  check#3.a1 succeeded after" in outline and "exit code 0, stdout " in outline
    assert outline.rstrip().endswith('run stopped: succeeded {"output": {"count": "3\\n"}}')


def test_wf_trace_refuses_a_bad_path_and_a_run_without_a_trace(repo, capsys):
    assert cli.main(["run", write(COUNTED)]) == 0
    assert cli.main(["trace", run_dir().name, "--var", "count..stdout"]) == 1
    assert "can't read `count..stdout`" in capsys.readouterr().err
    (run_dir() / "trace.jsonl").unlink()
    assert cli.main(["trace", run_dir().name]) == 1
    assert "has no trace" in capsys.readouterr().err


# ---------- what a template read ----------


@pytest.mark.parametrize("source, call, expected", [
    ("{{ start.task }} {% for t in plan.tasks %}{{ t.title }}{% endfor %}", templates.render,
     [("start.task", "do"), ("plan.tasks[0].title", "A"), ("plan.tasks[1].title", "B")]),
    ("{% if review %}{{ review.x }}{% endif %}{% if tests and tests.exit_code %}{{ tests.stdout }}{% endif %}", templates.render,
     [("review", None), ("tests.exit_code", 1), ("tests.stdout", "f")]),
    ("{% if session.continued %}{{ history | tojson }}{% endif %}", templates.render,  # a branch that didn't run reads nothing
     [("session.continued", False)]),
    ("history.implement | selectattr('ok') | list", templates.evaluate,
     [("history.implement[0].ok", True), ("history.implement[1].ok", False)]),
    ("{{ plan.tasks }}", templates.render_value, [("plan.tasks", [{"title": "A"}, {"title": "B"}])]),
    ("{{ plan.tasks[-1].title }}", templates.render, [("plan.tasks[1].title", "B")]),
])
def test_a_render_reads_each_path_it_used_with_its_value(source, call, expected):
    variables = {"start": {"task": "do"}, "plan": {"tasks": [{"title": "A"}, {"title": "B"}]}, "review": None,
                 "tests": {"exit_code": 1, "stdout": "f"}, "session": {"continued": False},
                 "history": {"implement": [{"ok": True}, {"ok": False}]}}
    _, reads = templates.watched(call, source, variables)
    assert [(templates.path_text(r.path), r.value) for r in reads] == expected
