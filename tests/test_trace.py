"""The run's trace (trace.py) and `wf trace` (trace_view.py), end to end. A fake agent and real
commands run a workflow. The tests read trace.jsonl, blobs/ and what `wf trace` prints."""

import asyncio
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from textwrap import dedent

import pytest

from fakes import Fake, submit, talk
from workflow import cli, harnesses, nodes, runner, spec, templates, trace, trace_view
from workflow.harnesses.base import TurnEnd
from workflow.output_tool import REMINDER
from workflow.runs import Run
from workflow.schemas import EACH


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
        "outcome": "succeeded", "usage": {"tokens": 200, "cost_usd": 0.02, "input_tokens": 0, "output_tokens": 0,
                                          "cache_read_tokens": 0, "cache_write_tokens": 0, "reasoning_tokens": 0}}
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


def test_a_secret_item_is_hidden_whatever_its_for_each_and_only_in_its_node(repo):
    write("""
        version: 1
        name: items
        start: {input: {token: {type: string}, tokens: {type: array, items: {type: string}}}}
        nodes:
          deploy:
            needs: [start]
            for_each: "[start.token] | list"
            command: 'test -n "$TOKEN"'
            env: {TOKEN: "{{ item }}"}
            secret_env: [TOKEN]
          last:
            needs: [start]
            command: 'test -n "$TOKEN"'
            env: {TOKEN: "{{ start.tokens[-1] }}"}
            secret_env: [TOKEN]
          greet:
            needs: [start]
            for_each: '["alpha-name"]'
            command: "printf '%s' {{ item | quote }}"
        end: {output: {}}
    """)
    Path("in.json").write_text(json.dumps({"token": SECRET, "tokens": ["second-tok-9f2e", "third-tok-7a1c"]}))
    assert cli.main(["run", "flow.yaml", "--input", "in.json"]) == 0
    written = everything_written()
    for secret in (SECRET, "second-tok-9f2e", "third-tok-7a1c"):
        assert secret not in written
    assert of("run")[0]["inputs"] == {"token": trace.HIDDEN, "tokens": [trace.HIDDEN, trace.HIDDEN]}  # x[-1]: any element
    assert of("job", node="greet")[0]["item"] == "alpha-name"  # another node's items aren't secret
    assert of("job_end", node="greet")[0]["output"]["stdout"] == "alpha-name"


def test_a_short_secret_is_removed_wherever_it_shows(repo):
    write("""
        version: 1
        name: short
        start: {input: {pin: {type: string}}}
        nodes:
          deploy:
            needs: [start]
            command: 'printf "pin is %s" "$PIN"'
            env: {PIN: "{{ start.pin }}"}
            secret_env: [PIN]
          echo: {needs: [deploy], command: "printf '%s' {{ deploy.stdout | quote }}"}
        end: {output: {}}
    """)
    assert cli.main(["run", "flow.yaml", "pin=q7z"]) == 0
    assert "q7z" not in everything_written()
    assert of("job_end", node="echo")[0]["output"]["stdout"] == f"pin is {trace.HIDDEN}"


@pytest.mark.parametrize("source, note_shows", [
    ("{{ creds.token }}", True),
    ("{{ history.creds | tojson }}", False),  # every output the node has given
    ("{{ history | tojson }}", False),
])
def test_a_secret_that_a_sub_workflow_returns_is_hidden_inside_it_too(repo, source, note_shows):
    Path("creds.yaml").write_text(dedent("""
        version: 1
        name: creds
        start: {input: {}}
        nodes:
          fetch: {needs: [start], command: "printf 'tok-%s-%s' sub $((6*7))"}
        end: {output: {token: "{{ fetch.stdout }}", note: "{{ fetch.exit_code }}"}}
    """))
    write("""
        version: 1
        name: outer
        start: {input: {}}
        nodes:
          creds: {needs: [start], workflow: creds.yaml}
          use:
            needs: [creds]
            command: 'test -n "$TOKEN"'
            env: {TOKEN: "SOURCE"}
            secret_env: [TOKEN]
        end: {output: {}}
    """.replace("SOURCE", source))
    assert cli.main(["run", "flow.yaml"]) == 0
    assert "tok-sub-42" not in everything_written()
    (fetched,) = of("job_end", node="fetch")
    assert (fetched["where"], fetched["output"]["stdout"]) == ("creds/", trace.HIDDEN)
    assert of("render", field="end.output.token")[0]["result"] == trace.HIDDEN
    note = of("render", field="end.output.note")[0]["result"]
    assert note == (0 if note_shows else trace.HIDDEN)  # an output that feeds no secret shows


@pytest.mark.parametrize("source, note_shows", [
    ("{{ results.creds[0].output.token }}", True),
    ("{{ results.creds | tojson }}", False),  # each item, with all of its output
    ("{{ results | tojson }}", False),
])
def test_a_secret_that_a_sub_workflow_item_returns_through_results_is_hidden_inside_it_too(repo, source, note_shows):
    Path("creds.yaml").write_text(dedent("""
        version: 1
        name: creds
        start: {input: {}}
        nodes:
          fetch: {needs: [start], command: "printf 'tok-%s-%s' sub $((6*7))"}
        end: {output: {token: "{{ fetch.stdout }}", note: "{{ fetch.exit_code }}"}}
    """))
    write("""
        version: 1
        name: outer
        start: {input: {}}
        nodes:
          creds: {needs: [start], for_each: "[1]", workflow: creds.yaml}
          use:
            needs: [creds]
            command: 'test -n "$TOKEN"'
            env: {TOKEN: "SOURCE"}
            secret_env: [TOKEN]
        end: {output: {}}
    """.replace("SOURCE", source))
    assert cli.main(["run", "flow.yaml"]) == 0
    assert "tok-sub-42" not in everything_written()
    (fetched,) = of("job_end", node="fetch")
    assert (fetched["where"], fetched["output"]["stdout"]) == ("creds[0]/", trace.HIDDEN)
    note = of("render", field="end.output.note")[0]["result"]
    assert note == (0 if note_shows else trace.HIDDEN)


@pytest.mark.parametrize("source, secret", [
    ("{{ results.fetch[0].output.stdout }}", "tok-9f2e-out"),  # the output that the item gave
    ("{{ results.fetch[0].item }}", "tok-9f2e"),  # the item, and the input that it comes from
    ("{{ results.fetch | tojson }}", "tok-9f2e"),
])
def test_a_secret_read_through_results_is_hidden_where_its_node_and_item_give_it(repo, source, secret):
    write("""
        version: 1
        name: through
        start: {input: {tokens: {type: array, maxItems: 2, items: {type: string}}}}
        nodes:
          fetch:
            needs: [start]
            for_each: start.tokens
            command: 'printf "%s-out" "$TOKEN"'
            env: {TOKEN: "{{ item }}"}
          use:
            needs: [fetch]
            command: 'test -n "$SECRET"'
            env: {SECRET: "SOURCE"}
            secret_env: [SECRET]
        end: {output: {}}
    """.replace("SOURCE", source))
    Path("in.json").write_text(json.dumps({"tokens": ["tok-9f2e"]}))
    assert cli.main(["run", "flow.yaml", "--input", "in.json"]) == 0
    assert secret not in everything_written()  # the job_end, job and run records were written before the read


@pytest.mark.parametrize("reader, source", [
    ("use", "{{ results.pick[0].item.output.stdout }}"),  # through pick, whose items are the entries of fetch
    ("pick", "{{ item.output.stdout }}"),
])
def test_a_secret_that_reaches_results_through_a_for_each_list_is_hidden_where_the_first_node_gives_it(repo, reader, source):
    nodes = {
        "fetch": {"needs": ["start"], "for_each": "start.tokens", "command": 'printf "%s-out" "$TOKEN"', "env": {"TOKEN": "{{ item }}"}},
        "pick": {"needs": ["fetch"], "for_each": "results.fetch", "command": "true"},
        "use": {"needs": ["pick"], "command": "true"},
    }
    nodes[reader] |= {"command": 'test -n "$SECRET"', "env": {"SECRET": source}, "secret_env": ["SECRET"]}
    tokens = {"type": "array", "maxItems": 2, "items": {"type": "string"}}
    write(json.dumps({"version": 1, "name": "chain", "start": {"input": {"tokens": tokens}}, "nodes": nodes, "end": {"output": {}}}))
    Path("in.json").write_text(json.dumps({"tokens": ["tok-9f2e"]}))
    assert cli.main(["run", "flow.yaml", "--input", "in.json"]) == 0
    assert "tok-9f2e-out" not in everything_written()  # also in the job_end records of fetch
    assert of("job_end", node="fetch")[0]["output"]["stdout"] == trace.HIDDEN


def test_a_secret_item_from_a_literal_list_stays_hidden_in_results_after_a_new_process_continues_the_run(repo, capsys):
    write("""
        version: 1
        name: literal
        start: {input: {}}
        nodes:
          work:
            needs: [start]
            for_each: '["tok-lit-5e1d"]'
            command: 'test -n "$TOKEN"'
            env: {TOKEN: "{{ item }}"}
            secret_env: [TOKEN]
          approve: {needs: [work], human: "Go on?", output: {ok: {type: boolean}}}
          tell: {needs: [approve], command: "printf '%s' {{ results.work[0].item | quote }}"}
        end: {output: {}}
    """)
    assert cli.main(["run", "flow.yaml", "--no-input"]) == 2  # waits for approve
    capsys.readouterr()
    cli.main(["status", run_dir().name, "--json"])
    key = json.loads(capsys.readouterr().out)["questions"][0]["key"]
    Path("answers.json").write_text(json.dumps({key: {"ok": True}}))
    assert cli.main(["answer", run_dir().name, "answers.json"]) == 0  # a new process: work doesn't run again
    written = records()
    start = next(n for n, r in enumerate(written) if r["kind"] == "run" and r["action"] == "retried")
    assert "tok-lit-5e1d" not in json.dumps(written[start:])  # the for_each record of work holds the workflow's own text
    assert of("render", node="tell")[0]["reads"] == [{"path": "results.work[0].item", "value": trace.HIDDEN}]


def test_a_secret_item_that_failed_stays_hidden_in_errors_after_a_new_process_continues_the_run(repo, capsys):
    write("""
        version: 1
        name: literal
        start: {input: {}}
        nodes:
          work:
            needs: [start]
            for_each: '["tok-err-7c2a", "fine-2b9d"]'
            on_item_error: continue
            command: 'test "$TOKEN" = fine-2b9d'
            env: {TOKEN: "{{ item }}"}
            secret_env: [TOKEN]
          approve: {needs: [work], human: "Go on?", output: {ok: {type: boolean}}}
          tell: {needs: [approve], command: "printf '%s' {{ errors.work[0].item | quote }}"}
        end: {output: {}}
    """)
    assert cli.main(["run", "flow.yaml", "--no-input"]) == 2  # waits for approve, with one item of work failed
    capsys.readouterr()
    cli.main(["status", run_dir().name, "--json"])
    key = json.loads(capsys.readouterr().out)["questions"][0]["key"]
    Path("answers.json").write_text(json.dumps({key: {"ok": True}}))
    assert cli.main(["answer", run_dir().name, "answers.json"]) == 0  # a new process: work doesn't run again
    written = records()
    start = next(n for n, r in enumerate(written) if r["kind"] == "run" and r["action"] == "retried")
    assert "tok-err-7c2a" not in json.dumps(written[start:])
    assert of("render", node="tell")[0]["reads"] == [{"path": "errors.work[0].item", "value": trace.HIDDEN}]


def test_the_secret_paths_of_lists_that_read_each_others_results_come_to_an_end():
    workflow = spec.parse(dedent("""
        version: 1
        name: loop
        start: {input: {}}
        nodes:
          a: {needs: [start], for_each: "results.b[0].item.l", command: x}
          b: {needs: [start], for_each: "results.a[0].item.l", command: x}
          use: {needs: [a, b], command: x, env: {TOKEN: "{{ results.a[0].item.x }}"}, secret_env: [TOKEN]}
        end: {output: {}}
    """).lstrip())
    paths = trace.find(workflow, spec.load).paths  # each step of the loop makes the path longer: it is cut
    assert ("results", "b", 0, "item", "l", EACH, "x") in paths and max(map(len, paths)) <= 32


def test_a_secret_in_the_output_of_a_for_each_node_is_secret_where_history_and_results_hold_it():
    workflow = spec.parse(dedent("""
        version: 1
        name: items
        start: {input: {}}
        nodes:
          fetch: {needs: [start], for_each: "[1, 2]", command: x}
          use: {needs: [fetch], command: x, env: {TOKEN: "{{ fetch[-1].stdout }}"}, secret_env: [TOKEN]}
        end: {output: {}}
    """).lstrip())
    paths = trace.find(workflow, spec.load).paths
    assert {("fetch", EACH, "stdout"), ("history", "fetch", EACH, EACH, "stdout"),
            ("results", "fetch", EACH, "output", "stdout")} <= paths


def test_a_secret_in_an_agents_result_is_hidden_from_its_harness_record_of_the_call(repo, monkeypatch):
    result = {"token": SECRET}

    async def reports_the_call_first(session):  # as Claude Code can: the call's message before submit_result runs
        session.spec.trace.tool_call("t1", "submit_result", result)
        session.spec.trace.tool_call("t2", "submit_result", {"tool": "submit_result", "arguments": result})  # Codex's shape
        session.spec.tool.submit(result)
        return TurnEnd("done")

    monkeypatch.setitem(harnesses.FACTORIES, "claude-code", Fake("claude-code", reports_the_call_first).factory)
    write("""
        version: 1
        name: login
        defaults: {harness: claude-code, harnesses: {claude-code: {model: opus}}}
        start: {input: {}}
        nodes:
          login: {needs: [start], instruction: Get a token, output: {token: {type: string}}}
          use:
            needs: [login]
            command: 'test -n "$TOKEN"'
            env: {TOKEN: "{{ login.token }}"}
            secret_env: [TOKEN]
        end: {output: {}}
    """)
    assert cli.main(["run", "flow.yaml"]) == 0
    assert SECRET not in everything_written()
    assert [r["arguments"] for r in of("tool_call")] == [{"token": trace.HIDDEN},
                                                        {"tool": "submit_result", "arguments": {"token": trace.HIDDEN}}]


def test_an_attempt_that_fails_before_its_session_has_its_settings(repo, monkeypatch):
    class Away:
        name = "claude-code"

        async def open(self, run_dir):
            raise harnesses.Unavailable("not logged in")

        async def close(self):
            pass

    monkeypatch.setitem(harnesses.FACTORIES, "claude-code", Away)
    assert cli.main(["run", write(LOOPED)]) == 1
    (began,) = of("attempt", id="plan#1.a1")
    assert {k: began[k] for k in ("harness", "model", "effort", "access", "web")} == {
        "harness": "claude-code", "model": "opus", "effort": "high", "access": "read-only", "web": False}
    assert began["workspace"]["path"] == str(repo.resolve()) and began["session"] == {"start": "fresh"}
    assert of("attempt_end", id="plan#1.a1")[0]["outcome"] == "unavailable"


def test_writing_the_trace_survives_text_that_utf8_cannot_hold(tmp_path):
    written = trace.Trace(tmp_path)
    for text in ("a lone \ud800 surrogate", "long \udfff " + "x" * trace.CAP):
        written.write({"kind": "message", "id": "m", "parent": None, "text": text})
    short, long = [json.loads(line) for line in (tmp_path / trace.FILE).read_text().splitlines()]
    assert short["text"] == "a lone \ud800 surrogate"
    assert trace_view.full(Run("r", tmp_path, tmp_path), long["text"]).startswith("long \\udfff x")


def test_wf_trace_reads_only_blobs_of_the_run(tmp_path):
    folder = tmp_path / "run"
    (folder / "blobs").mkdir(parents=True)
    (tmp_path / "private.txt").write_text("not the run's")
    run = Run("r", folder, tmp_path)
    for name in ("blobs/../../private.txt", f"blobs/{'a' * 64}.txt/../../../private.txt"):
        reference = {"$blob": name, "size": 1, "start": ""}
        assert trace_view.full(run, reference) == reference
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / f"{'b' * 64}.txt").write_text("not the run's either")
    (folder / "blobs").rmdir()
    (folder / "blobs").symlink_to(elsewhere)
    reference = {"$blob": f"blobs/{'b' * 64}.txt", "size": 1, "start": ""}
    assert trace_view.full(run, reference) == reference
    lookalike = {"$blob": "blobs/x.txt", "size": 1, "start": ""}  # a value that only looks like a reference
    written = trace.Trace(tmp_path / "other")
    (tmp_path / "other").mkdir()
    written.write({"kind": "message", "id": "m", "parent": None, "text": lookalike})
    (line,) = (tmp_path / "other" / trace.FILE).read_text().splitlines()
    assert trace_view.full(Run("o", tmp_path / "other", tmp_path), json.loads(line)["text"]) == lookalike


def test_a_command_whose_stream_file_cant_be_opened_never_starts(tmp_path):
    log = tmp_path / "attempt-1.log"
    log.with_suffix(".stderr").mkdir()  # a folder in the way
    with pytest.raises(IsADirectoryError):
        asyncio.run(nodes.run_shell("touch ran", tmp_path, dict(os.environ), log))
    time.sleep(0.5)  # a command that had started would have run by now
    assert not (tmp_path / "ran").exists()


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


def test_each_run_record_has_a_run_stopped_record_whatever_wf_retry_does(repo, capsys):
    write("""
        version: 1
        name: asked
        start: {input: {}}
        nodes:
          approve: {needs: [start], human: "Go?", output: {ok: {type: boolean}}}
        end: {output: {ok: "{{ approve.ok }}"}}
    """)
    assert cli.main(["run", "flow.yaml", "--no-input"]) == 2
    run_id = run_dir().name
    assert cli.main(["retry", run_id, "--no-input"]) == 2  # nobody answers: it stays waiting
    capsys.readouterr()
    cli.main(["status", run_id, "--json"])
    key = json.loads(capsys.readouterr().out)["questions"][0]["key"]
    Path("answers.json").write_text(json.dumps({key: {"ok": True}}))
    assert cli.main(["answer", run_id, "answers.json"]) == 0
    assert cli.main(["retry", run_id]) == 0  # it ended: nothing to drive
    assert [(r["kind"], r.get("action") or r.get("status")) for r in records() if r["id"] == "run"] == [
        ("run", "started"), ("run_stopped", "waiting"), ("run", "retried"), ("run_stopped", "succeeded")]


def test_wf_trace_reads_blobs_back_in_json_and_shows_a_missing_one_as_its_reference(repo, capsys):
    long = "printf '%s' " + "y" * 1200
    write(f"""
        version: 1
        name: long
        start: {{input: {{}}}}
        nodes:
          say: {{needs: [start], command: "{long}"}}
        end: {{output: {{}}}}
    """)
    assert cli.main(["run", "flow.yaml"]) == 0
    capsys.readouterr()
    assert cli.main(["trace", run_dir().name, "--node", "say", "--field", "command", "--json"]) == 0
    (render,) = json.loads(capsys.readouterr().out)["records"]
    assert (render["template"], render["result"]) == (long, long)
    for blob in (run_dir() / "blobs").iterdir():
        blob.unlink()
    assert cli.main(["trace", run_dir().name, "--field", "command"]) == 0
    assert "characters in blobs/" in capsys.readouterr().out


def test_wf_trace_prints_text_that_utf8_cannot_hold_escaped(repo, monkeypatch):
    assert cli.main(["run", write(COUNTED)]) == 0
    trace.Trace(run_dir()).write({"kind": "render", "id": "count#1", "parent": "run", "node": "count", "pass": 1,
                                  "field": "command", "template": "x", "reads": [{"path": "count.stdout", "value": "a \ud800"}],
                                  "result": "a \ud800"})
    for more in ([], ["--field", "command"], ["--var", "count.stdout"], ["--json"], ["--var", "count.stdout", "--json"]):
        out = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")  # a strict UTF-8 stdout, as a pipe has
        monkeypatch.setattr(sys, "stdout", out)
        assert cli.main(["trace", run_dir().name, *more]) == 0
        out.flush()
        shown = out.buffer.getvalue().decode("utf-8")
        assert "a \\ud800" in shown
        if "--json" in more:
            assert "a \ud800" in json.dumps(json.loads(shown), ensure_ascii=False)  # the JSON escape reads back as it was


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
     [("review", None), ("tests", {"exit_code": 1, "stdout": "f"}), ("tests.exit_code", 1), ("tests.stdout", "f")]),
    ("{{ tests.exit_code }} {{ tests.stdout }}", templates.render,  # only a way to its fields: tests isn't listed
     [("tests.exit_code", 1), ("tests.stdout", "f")]),
    ("{{ plan.tasks | length }} {{ plan.tasks[0].title }}", templates.render,  # the length needs the whole list
     [("plan.tasks", [{"title": "A"}, {"title": "B"}]), ("plan.tasks[0].title", "A")]),
    ("{{ history.implement[i] }}", templates.render, [("i", 1), ("history.implement[1]", {"ok": False})]),
    ("{% if session.continued %}{{ history | tojson }}{% endif %}", templates.render,  # a branch that didn't run reads nothing
     [("session.continued", False)]),
    ("history.implement | selectattr('ok') | list", templates.evaluate,
     [("history.implement", [{"ok": True}, {"ok": False}]), ("history.implement[0].ok", True), ("history.implement[1].ok", False)]),
    ("{{ plan.tasks }}", templates.render_value, [("plan.tasks", [{"title": "A"}, {"title": "B"}])]),
    ("{{ plan.tasks[-1].title }}", templates.render, [("plan.tasks[1].title", "B")]),
])
def test_a_render_reads_each_path_it_used_with_its_value(source, call, expected):
    variables = {"start": {"task": "do"}, "plan": {"tasks": [{"title": "A"}, {"title": "B"}]}, "review": None,
                 "tests": {"exit_code": 1, "stdout": "f"}, "session": {"continued": False},
                 "history": {"implement": [{"ok": True}, {"ok": False}]}, "i": 1}
    _, reads = templates.watched(call, source, variables)
    assert [(templates.path_text(r.path), r.value) for r in reads] == expected
