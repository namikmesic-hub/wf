"""wf continue: a follow-up request for a run that succeeded, as a new pass of the same run.

The workflow's `follow_up` names the nodes that take the request, and `follow_up.request` brings it
into their templates. The run goes back to running on its own branches, with its sessions, fresh
route counters and a budget of its own.
"""

import io
import json
import subprocess
import sys
from pathlib import Path
from textwrap import dedent

import pytest

from fakes import Fake, submit
from workflow import cli, firstmate, harnesses, progress, report, runner, runs, spec, trace_view, validate, workspaces


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(runner, "interactive", lambda: False)
    subprocess.run(["git", "init", "-q"], check=True)
    git("commit", "-q", "--allow-empty", "-m", "first")
    return tmp_path


def git(*args: str) -> str:
    return subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def write(name: str, text: str) -> None:
    Path(name).write_text(dedent(text).lstrip())


def only_run() -> runs.Run:
    (run,) = runs.all_runs(Path.cwd())
    return run


def events(run: runs.Run) -> list[dict]:
    return [json.loads(line) for line in (run.dir / "events.jsonl").read_text().splitlines()]


def status(capsys, run: runs.Run) -> dict:
    capsys.readouterr()
    assert cli.main(["status", run.id, "--json"]) == 0
    return json.loads(capsys.readouterr().out)


FLOW = """
version: 1
name: flow
start: {input: {task: {type: string}}}
follow_up: {to: work}
nodes:
  plan: {needs: [start], command: "echo planned"}
  work:
    needs: [plan]
    workspace: new
    env:
      NOTE: "{{ follow_up.request if follow_up else start.task }} ({{ 'check is none' if check is none else 'check ran' }})"
    command: echo "$NOTE" >> notes && git add notes && git -c user.email=t@t -c user.name=t commit -qm "$NOTE" && cat notes
  check:
    needs: [work]
    workspace: work
    command: "echo {{ follow_up.number if follow_up else 0 }} {{ plan.stdout | trim | quote }}"
end:
  output:
    notes: {type: string, value: "{{ work.stdout }}"}
    said: {type: string, value: "{{ check.stdout }}"}
"""


def ran(capsys, text: str = FLOW) -> runs.Run:
    write("flow.yaml", text)
    assert cli.main(["run", "flow.yaml", "task=hello"]) == 0
    capsys.readouterr()
    return only_run()


# ---------- a follow-up ----------


def test_a_follow_up_runs_again_from_its_node_on_the_same_branch_with_the_request(repo, capsys):
    run = ran(capsys)
    branch = f"wf/{run.id}/work"
    assert not (workspaces.root() / run.id).exists()  # the run succeeded: its worktrees went, its branch stayed

    assert cli.main(["continue", run.id, "add a flag"]) == 0
    out = capsys.readouterr().out
    assert f"Continuing run {run.id} with follow-up 1" in out and "↪ follow-up 1 → work: add a flag" in out
    # The nodes the request leads to read none until they run again, and the others keep their output
    assert run.output() == {"notes": "hello (check is none)\nadd a flag (check is none)\n", "said": "1 planned\n"}
    assert git("log", "--format=%s", branch).splitlines() == ["add a flag (check is none)", "hello (check is none)", "first"]
    assert not (workspaces.root() / run.id).exists()  # removed again when the follow-up succeeded
    assert sorted(p.name for p in (run.dir / "nodes" / "work").iterdir()) == ["1", "2"]
    assert sorted(p.name for p in (run.dir / "nodes" / "plan").iterdir()) == ["1"]
    (given,) = [e for e in events(run) if e["event"] == "follow_up"]
    assert (given["number"], given["to"], given["request"]) == (1, ["work"], "add a flag")
    state = status(capsys, run)
    assert (state["status"], state["follow_up"], state["output"]["said"]) == ("succeeded", 1, "1 planned\n")
    assert state["progress"]["percent"] == 100


def test_each_follow_up_has_its_number_and_wf_list_says_it(repo, capsys):
    run = ran(capsys)
    assert cli.main(["continue", run.id, "two"]) == 0
    assert cli.main(["continue", run.id, "three"]) == 0
    assert run.output()["notes"].splitlines()[-1] == "three (check is none)"
    assert run.output()["said"] == "2 planned\n"
    capsys.readouterr()
    assert cli.main(["list"]) == 0
    assert capsys.readouterr().out.strip() == f"{run.id} [flow]: succeeded (follow-up 2)"
    assert runs.Run.at(run.dir).follow_up() == 2


def test_the_trace_shows_each_follow_up_as_a_new_pass_of_the_run(repo, capsys):
    run = ran(capsys)
    assert cli.main(["continue", run.id, "add a flag"]) == 0
    records = trace_view.records(run)
    starts = [(r["action"], r.get("follow_up"), r.get("request")) for r in records if r["kind"] == "run"]
    assert starts == [("started", None, None), ("continued", 1, "add a flag")]
    assert [r["kind"] for r in records].count("run_stopped") == 2
    assert {(r["node"], r["pass"]) for r in records if r["kind"] == "pass"} == {
        ("plan", 1), ("work", 1), ("check", 1), ("work", 2), ("check", 2)}
    reads = [read for r in records if r["kind"] == "render" and r.get("pass") == 2 for read in r["reads"]]
    assert {"path": "follow_up.request", "value": "add a flag"} in reads
    text = trace_view.text(run, records, None)
    assert 'run continued: flow, follow-up 1: "add a flag"' in text


def test_a_session_continue_node_continues_its_last_session_in_a_follow_up(repo, monkeypatch, capsys):
    fake = Fake("claude-code", *[submit({"summary": f"pass {n}"}) for n in range(1, 5)])
    monkeypatch.setitem(harnesses.FACTORIES, "claude-code", fake.factory)
    write("talk.yaml", """
        version: 1
        name: talk
        defaults: {harness: claude-code, harnesses: {claude-code: {model: opus}}}
        start: {input: {}}
        follow_up: {to: work}
        nodes:
          work:
            needs: [start]
            session: continue
            instruction: "{% if follow_up %}Follow-up: {{ follow_up.request }}{% elif session.continued %}Fix it{% else %}The whole task{% endif %}"
          check:
            needs: [work]
            command: "echo try >> tries; test $(wc -l < tries) -ge 3"
            check: false
            routes: [{when: "check.exit_code != 0", to: work, max: 2}, {to: end}]
        end: {output: {}}
    """)
    assert cli.main(["run", "talk.yaml"]) == 0
    assert cli.main(["continue", only_run().id, "add a flag"]) == 0
    # Two passes in a row continued before it: a follow-up still continues the session
    assert [s.fork_from for s in fake.specs] == [None, "session-1", "session-2", "session-3"]
    assert fake.sent[3].startswith("Follow-up: add a flag")


def test_routes_count_from_zero_again_in_a_follow_up(repo, capsys):
    write("loop.yaml", """
        version: 1
        name: loop
        start: {input: {}}
        follow_up: {to: work}
        nodes:
          work: {needs: [start], command: "echo {% if follow_up %}{{ follow_up.request | quote }}{% endif %}"}
          check:
            needs: [work]
            command: "true"
            routes: [{to: work, max: 1}, {to: end}]
        end: {output: {}}
    """)
    assert cli.main(["run", "loop.yaml"]) == 0
    run = only_run()
    assert cli.main(["continue", run.id, "again"]) == 0
    routes = [(e["route"], e["taken"]) for e in events(run) if e["event"] == "route"]
    assert routes == [(0, 1), (1, 1), (0, 1), (1, 1)]  # the loop went round once in each pass
    assert len(list((run.dir / "nodes" / "work").iterdir())) == 4


def test_a_follow_up_has_a_budget_of_its_own_from_the_command_line(repo, monkeypatch, capsys):
    fake = Fake("claude-code", *[submit({"summary": f"pass {n}"}, tokens=100) for n in range(1, 4)])
    monkeypatch.setitem(harnesses.FACTORIES, "claude-code", fake.factory)
    write("spend.yaml", """
        version: 1
        name: spend
        defaults: {harness: claude-code, harnesses: {claude-code: {model: opus}}}
        limits: {tokens: 150}
        start: {input: {}}
        follow_up: {to: work}
        nodes:
          work: {needs: [start], instruction: "{{ follow_up.request if follow_up else 'start' }}"}
        end: {output: {}}
    """)
    assert cli.main(["run", "spend.yaml"]) == 0
    run = only_run()
    # 200 tokens in the whole run, but the file's limit counts only the 100 of the follow-up
    assert cli.main(["continue", run.id, "more"]) == 0
    assert "limits" not in run.info()

    capsys.readouterr()
    assert cli.main(["continue", run.id, "and more", "--limit", "tokens=50"]) == 1
    captured = capsys.readouterr()
    assert "Limits for this follow-up: no cost limit, tokens 50, no duration limit" in captured.out
    assert "used 100 tokens, limit 50" in captured.err
    assert run.info()["limits"] == {"tokens": 50}
    assert status(capsys, run)["status"] == "limit"
    assert cli.main(["retry", run.id, "--limit", "tokens=none"]) == 0
    assert status(capsys, run)["follow_up"] == 2


def test_a_dry_run_takes_the_next_entries_of_its_script_and_makes_its_branches_again(repo, capsys):
    write("edit.yaml", """
        version: 1
        name: edit
        defaults: {harness: claude-code}
        start: {input: {}}
        follow_up: {to: work}
        nodes:
          work:
            needs: [start]
            workspace: new
            access: edit
            instruction: "{{ follow_up.request if follow_up else 'start' }}"
          look: {needs: [work], workspace: work, command: "cat *.txt"}
        end: {output: {seen: "{{ look.stdout }}"}}
    """)
    write("edit.dry.yaml", """
        agents:
          work:
            - {_files: {a.txt: "one\\n"}, summary: first}
            - {_files: {b.txt: "two\\n"}, summary: second}
    """)
    assert cli.main(["run", "edit.yaml", "--dry-run", "edit.dry.yaml"]) == 0
    run = only_run()
    assert not workspaces.branch_exists(Path.cwd(), f"wf/{run.id}/work")  # a dry run that succeeds deletes them
    assert cli.main(["continue", run.id, "more"]) == 0
    assert run.output() == {"seen": "two\n"}  # its branch started again from the base: only the new file


# ---------- what wf continue refuses ----------


ASK = """
version: 1
name: ask
start: {input: {}}
follow_up: {to: approve}
nodes:
  approve: {needs: [start], human: "Ship it? {{ follow_up.request if follow_up else '' }}", output: {ok: {type: boolean}}}
end: {output: {ok: "{{ approve.ok }}"}}
"""

FAILS = """
version: 1
name: fails
start: {input: {}}
follow_up: {to: work}
nodes:
  work: {needs: [start], command: "echo {% if follow_up %}{{ follow_up.request | quote }}{% endif %}; exit 1"}
end: {output: {}}
"""


@pytest.mark.parametrize("text, code, why", [
    (ASK, 2, "waits for a person: answer it with wf answer {run} <answers.json>"),
    (FAILS, 1, "failed: continue it with wf retry {run}"),
])
def test_a_run_that_waits_or_failed_is_refused_and_stays_as_it_was(repo, capsys, text, code, why):
    write("flow.yaml", text)
    assert cli.main(["run", "flow.yaml", "--no-input"]) == code
    run = only_run()
    before = run.status()
    capsys.readouterr()
    assert cli.main(["continue", run.id, "more"]) == 1
    assert capsys.readouterr().err == f"wf continue: run {run.id} {why.format(run=run.id)}\n"
    assert run.status() == before and "follow_up" not in run.info()


def test_a_run_at_a_limit_or_one_that_stopped_is_refused(repo, capsys):
    run = ran(capsys)
    for state, details, why in (("limit", {"limit": "tokens"}, f"is paused at a limit: continue it with wf retry {run.id} --limit tokens=<more>"),
                                ("running", {}, f"stopped before it ended: continue it with wf retry {run.id}")):
        run.set_status(state, **details)
        assert cli.main(["continue", run.id, "more"]) == 1
        assert capsys.readouterr().err == f"wf continue: run {run.id} {why}\n"


def test_a_run_that_still_runs_is_refused(repo, capsys):
    run = ran(capsys)
    with run.driving():
        assert cli.main(["continue", run.id, "more"]) == 1
    error = capsys.readouterr().err
    assert error.startswith(f"wf continue: run {run.id} is already running (process ")
    assert f"only a run that succeeded takes a follow-up. Follow it with: wf wait {run.id}" in error


def test_a_workflow_without_follow_up_is_refused(repo, capsys):
    run = ran(capsys, FLOW.replace("follow_up: {to: work}\n", "").replace(
        "{{ follow_up.request if follow_up else start.task }}", "{{ start.task }}").replace(
        "{{ follow_up.number if follow_up else 0 }}", "0"))
    assert cli.main(["continue", run.id, "more"]) == 1
    assert "workflow `flow` has no `follow_up`, which names the node that takes a follow-up request" in capsys.readouterr().err
    assert run.status()["status"] == "succeeded"


def test_a_branch_that_is_gone_or_checked_out_elsewhere_changes_nothing(repo, capsys):
    run = ran(capsys)
    branch = f"wf/{run.id}/work"
    git("checkout", "-q", branch)  # you look at the result in your own checkout
    assert cli.main(["continue", run.id, "more"]) == 1
    error = capsys.readouterr().err
    assert "can't bring back a worktree of the run, so the run stays as it was: `work` worked in" in error
    assert not (workspaces.root() / run.id).exists()
    assert run.status()["status"] == "succeeded" and "follow_up" not in run.info()

    git("checkout", "-q", "-")
    git("branch", "-q", "-D", branch)
    assert cli.main(["continue", run.id, "more"]) == 1
    assert f"and its branch `{branch}` is gone" in capsys.readouterr().err
    assert cli.main(["retry", run.id]) == 0  # still the run that succeeded
    assert "already succeeded. Give it a follow-up with: wf continue" in capsys.readouterr().out


def test_a_follow_up_that_cant_be_written_into_the_checkpoints_changes_nothing(repo, monkeypatch, capsys):
    run = ran(capsys)
    before = run.info()
    give = runner._give

    async def broken(ctx, request):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(runner, "_give", broken)
    assert cli.main(["continue", run.id, "more", "--limit", "tokens=50"]) == 1
    assert capsys.readouterr().err == (f"wf continue: can't give run {run.id} the follow-up, so the run stays as it was: "
                                       "No space left on device\n")
    assert run.info() == before and run.status()["status"] == "succeeded"
    assert not (workspaces.root() / run.id).exists()
    monkeypatch.setattr(runner, "_give", give)
    assert cli.main(["continue", run.id, "more"]) == 0
    assert status(capsys, run)["follow_up"] == 1


def test_a_follow_up_that_never_started_is_taken_in_by_wf_retry(repo, monkeypatch, capsys):
    run = ran(capsys)
    drive = runner._retry
    monkeypatch.setattr(runner, "_retry", lambda *args: 0)  # as if wf stopped after it kept the request
    assert cli.main(["continue", run.id, "more"]) == 0
    monkeypatch.setattr(runner, "_retry", drive)
    capsys.readouterr()
    assert cli.main(["continue", run.id, "other"]) == 1
    assert capsys.readouterr().err == (f"wf continue: run {run.id} has a follow-up that hasn't started yet: "
                                       f"start it with wf retry {run.id}\n")
    assert cli.main(["retry", run.id]) == 0
    assert run.output()["notes"].splitlines()[-1] == "more (check is none)"
    assert [r["action"] for r in trace_view.records(run) if r["kind"] == "run"] == ["started", "continued"]


@pytest.mark.parametrize("args, stdin, error", [
    ([], "", "give the request, or --file, but not both"),
    (["more", "--file", "-"], "", "give the request, or --file, but not both"),
    (["  "], "", "the request is empty"),
    (["--file", "-"], "x" * 100_001, "the request is longer than 100,000 characters"),
])
def test_the_request_is_checked_before_the_run_is_read(repo, monkeypatch, capsys, args, stdin, error):
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    assert cli.main(["continue", "no-such-run", *args]) == 1
    assert capsys.readouterr().err == f"wf continue: {error}\n"


def test_the_request_can_come_from_stdin(repo, monkeypatch, capsys):
    run = ran(capsys)
    monkeypatch.setattr(sys, "stdin", io.StringIO("from stdin"))
    assert cli.main(["continue", run.id, "--file", "-"]) == 0
    assert run.output()["notes"].splitlines()[-1] == "from stdin (check is none)"


# ---------- an operator without a terminal ----------


def test_a_detached_follow_up_prints_json_and_wait_reports_it(repo, capsys):
    run = ran(capsys)
    assert cli.main(["continue", run.id, "in the background", "--detach", "--json"]) == 0
    started = json.loads(capsys.readouterr().out)
    assert (started["run"], started["dir"], started["follow_up"]) == (run.id, str(run.dir), 1)
    assert cli.main(["wait", run.id, "--timeout", "30"]) == 0
    state = json.loads(capsys.readouterr().out)
    assert (state["status"], state["follow_up"]) == ("succeeded", 1)
    assert state["output"]["notes"].splitlines()[-1] == "in the background (check is none)"


def test_firstmate_sees_the_follow_up_as_a_new_pass_of_the_same_run(repo, monkeypatch, capsys):
    monkeypatch.setattr(firstmate, "POLL_SECONDS", 1)
    run = ran(capsys)

    def poll(source: str) -> dict:
        reply = firstmate.invoke({"operation": "source.poll", "input": {"source_id": source, "config_ref": f"wf-run:{run.dir}"}})
        return json.loads(reply["result"]["output"])

    assert "follow_up" not in poll("wf-run")
    assert cli.main(["continue", run.id, "more"]) == 0
    state = poll("wf-run-1")
    assert (state["status"], state["follow_up"]) == ("succeeded", 1)
    assert firstmate.invoke({"operation": "result.terminal", "input": {"content": json.dumps(state)}})["result"] == {"value": True}
    slim = json.loads(firstmate._bounded({**state, "nodes": ["x" * 40_000]}, "wf status"))
    assert (slim["status"], slim["follow_up"]) == ("succeeded", 1)  # cut down to fit, it still says which pass


# ---------- watching and reporting ----------


def test_wf_status_counts_a_follow_up_as_a_new_lap_from_its_node():
    workflow = spec.parse(dedent("""
        version: 1
        name: lap
        start: {input: {}}
        follow_up: {to: implement}
        nodes:
          plan: {needs: [start], command: "true"}
          implement: {needs: [plan], command: "true"}
          review:
            needs: [implement]
            command: "true"
            routes: [{to: implement, max: 1}, {to: end}]
        end: {output: {}}
    """).lstrip())
    at = "2026-10-20T10:00:00+00:00"
    passes = [{"time": at, "event": kind, "node": node, "pass": number}
              for node, number in (("plan", 1), ("implement", 1), ("review", 1), ("implement", 2), ("review", 2))
              for kind in ("node_started", "node_finished")]
    passes[6:6] = [{"time": at, "event": "route", "node": "review", "route": 0, "to": ["implement"], "taken": 1}]
    given = {"time": at, "event": "follow_up", "number": 1, "to": ["implement"], "request": "more"}
    found = progress.estimate(workflow, [*passes, given], "running")
    assert {n: i["state"] for n, i in found["nodes"].items()} == {"plan": "done", "implement": "waiting", "review": "waiting"}
    assert [loop["taken"] for loop in found["loops"]] == [0]
    assert (found["step"], found["steps"]) == (2, 3)


def test_wf_report_counts_follow_ups_and_not_the_time_the_run_waited_for_them(repo, capsys):
    run = ran(capsys)
    assert cli.main(["continue", run.id, "more"]) == 0
    logged = events(run)
    later = next(i for i, e in enumerate(logged) if e["event"] == "follow_up")
    times = ["2026-10-20T10:00:00+00:00"] * later + ["2026-10-21T10:00:00+00:00"] * (len(logged) - later)
    (run.dir / "events.jsonl").write_text("".join(json.dumps({**e, "time": t}) + "\n" for e, t in zip(logged, times)))
    (summary,) = report.build([run])["runs"]
    assert (summary["follow_ups"], summary["duration_s"]) == (1, 0)


def test_wf_graph_shows_where_a_follow_up_comes_in(repo, capsys):
    write("flow.yaml", FLOW)
    assert cli.main(["graph", "flow.yaml"]) == 0
    assert '  wf_follow_up(["follow-up"])\n  wf_follow_up -.-> n_work\n' in capsys.readouterr().out


# ---------- validation ----------


def problems(text: str) -> list[str]:
    write("check.yaml", text)
    return [str(p).removeprefix("check.yaml: ") for p in validate.check("check.yaml").problems]


REVIEW = """
version: 1
name: review
start: {input: {}}
follow_up: {to: implement}
nodes:
  implement: {needs: [start], command: "echo {% if follow_up %}{{ follow_up.request | quote }}{% endif %}"}
  tests: {needs: [implement], command: "true"}
  review:
    needs: [tests]
    command: "echo {{ tests.exit_code }}"
    routes: [{to: implement, max: 1}, {to: end}]
end: {output: {passed: {type: boolean, value: "{{ tests.exit_code == 0 }}"}}}
"""


def test_a_workflow_with_follow_up_validates(repo):
    assert problems(REVIEW) == []
    assert spec.load("check.yaml").follow_up.targets == ("implement",)


@pytest.mark.parametrize("before, after, problem", [
    ("follow_up: {to: implement}", "follow_up: {to: implment}", "error: follow_up.to: unknown node `implment` (did you mean `implement`?)"),
    ("follow_up: {to: implement}", "follow_up: {to: end}", "follow_up.to: a follow-up starts at nodes of the workflow, not at `end`"),
    ("follow_up: {to: implement}", "follow_up: {to: [implement, start]}", "follow_up.to: a follow-up starts at nodes of the workflow, not at `start`"),
    ("follow_up: {to: implement}", "follow_up: {to: implement, when: x}", "follow_up.when: unknown key"),
    ("  tests: {needs", "  follow_up: {needs: [start], command: 'true'}\n  tests: {needs", "nodes.follow_up: `follow_up` is a reserved name"),
])
def test_follow_up_names_nodes_of_the_workflow(repo, before, after, problem):
    found = problems(REVIEW.replace(before, after))
    assert any(problem.removeprefix("error: ") in p for p in found), found


def test_a_template_reads_follow_up_only_in_a_workflow_that_has_it_and_checks_it_first(repo):
    without = problems(REVIEW.replace("follow_up: {to: implement}\n", ""))
    assert any("reads `follow_up`, but the workflow has no `follow_up` key, so it is always none" in p for p in without)
    unguarded = problems(REVIEW.replace("{% if follow_up %}{{ follow_up.request | quote }}{% endif %}", "{{ follow_up.request | quote }}"))
    assert unguarded == ["warning: nodes.implement.command: reads `follow_up.request`, but `follow_up` is none until "
                         "`wf continue` gives the run a request; check first with {% if follow_up %}...{% endif %}"]
    unread = problems(REVIEW.replace("{% if follow_up %}{{ follow_up.request | quote }}{% endif %}", "x"))
    assert unread == ["warning: follow_up: no template reads `follow_up`, so a follow-up request reaches no node; put "
                      "{{ follow_up.request }} in the instruction of a node it starts, inside {% if follow_up %}...{% endif %}"]
    unquoted = problems(REVIEW.replace("{{ follow_up.request | quote }}", "{{ follow_up.request }}"))
    assert any("puts output from `follow_up` into the shell unquoted" in p for p in unquoted)


def test_a_template_that_reads_a_node_a_follow_up_clears_gets_a_warning(repo):
    found = problems(REVIEW.replace("follow_up: {to: implement}", "follow_up: {to: review}"))
    why = "might not have run yet in a follow-up, which clears the outputs of the nodes that `follow_up.to` can lead to"
    assert [p.split(":", 2)[1].strip() for p in found if why in p] == ["nodes.review.command", "end.output.passed.value"]
