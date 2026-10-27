"""wf continue: a follow-up request for a run that succeeded, as a new pass of the same run.

The workflow's `follow_up` names the nodes that take the request, and `follow_up.request` brings it
into their templates. Without `follow_up`, the first agent node that can change files takes it, and
wf adds the request to its instruction. The run goes back to running on its own branches, with its sessions, fresh
route counters and a budget of its own.
"""

import io
import json
import random
import re
import subprocess
import sys
from pathlib import Path
from textwrap import dedent, indent

import pytest

from fakes import Fake, ends, submit
from workflow import cli, diagram, firstmate, harnesses, nodes, operate, output_tool, progress, report, runner, runs, scheduler, spec, state, trace_view, ui, validate, workspaces
from workflow.context import RunContext


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
    assert f"Continuing run {run.id} with follow-up 1" in out and "↪ follow-up 1 → work\n" in out
    # The nodes the request leads to read none until they run again, and the others keep their output
    assert run.output() == {"notes": "hello (check is none)\nadd a flag (check is none)\n", "said": "1 planned\n"}
    assert git("log", "--format=%s", branch).splitlines() == ["add a flag (check is none)", "hello (check is none)", "first"]
    assert not (workspaces.root() / run.id).exists()  # removed again when the follow-up succeeded
    assert sorted(p.name for p in (run.dir / "nodes" / "work").iterdir()) == ["1", "2"]
    assert sorted(p.name for p in (run.dir / "nodes" / "plan").iterdir()) == ["1"]
    (given,) = [e for e in events(run) if e["event"] == "follow_up"]
    assert given | {"time": None} == {"time": None, "event": "follow_up", "number": 1, "to": ["work"]}  # no request text
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
    assert run.status()["follow_up"] == 2


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
    assert {"path": "follow_up", "value": {"request": "add a flag", "number": 1}} in reads  # the latest request only
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


def test_a_session_continue_node_in_a_sub_workflow_continues_its_session_in_a_follow_up(repo, monkeypatch, capsys):
    fake = Fake("claude-code", *[submit({"summary": f"pass {n}"}) for n in range(1, 3)])
    monkeypatch.setitem(harnesses.FACTORIES, "claude-code", fake.factory)
    write("parent.yaml", """
        version: 1
        name: parent
        start: {input: {}}
        follow_up: {to: build}
        nodes:
          build: {needs: [start], workflow: child.yaml, with: {task: "{{ follow_up.request if follow_up else 'start' }}"}}
        end: {output: {}}
    """)
    write("child.yaml", """
        version: 1
        name: child
        defaults: {harness: claude-code, harnesses: {claude-code: {model: opus}}}
        start: {input: {task: {type: string}}}
        nodes:
          work: {needs: [start], session: continue, instruction: "{{ start.task }}"}
        end: {output: {}}
    """)
    assert cli.main(["run", "parent.yaml"]) == 0
    assert cli.main(["continue", only_run().id, "add a flag"]) == 0
    # The follow-up's new sub-workflow thread starts with the sessions that the last one ended with
    assert [s.fork_from for s in fake.specs] == [None, "session-1"]
    assert fake.sent[1].startswith("add a flag")


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


SPEND = """
version: 1
name: spend
defaults: {harness: claude-code, harnesses: {claude-code: {model: opus}}}
LIMITS
start: {input: {}}
follow_up: {to: work}
nodes:
  work: {needs: [start], instruction: "{{ follow_up.request if follow_up else 'start' }}"}
end: {output: {}}
"""


def spending(monkeypatch, *tokens: int, cost: float = 0.01) -> None:
    fake = Fake("claude-code", *[submit({"summary": f"pass {n}"}, tokens=t, cost=cost) for n, t in enumerate(tokens)])
    monkeypatch.setitem(harnesses.FACTORIES, "claude-code", fake.factory)


def test_a_follow_up_has_a_budget_of_its_own_from_the_command_line(repo, monkeypatch, capsys):
    spending(monkeypatch, 100, 100, 100)
    write("spend.yaml", SPEND.replace("LIMITS", "limits: {tokens: 150}"))
    assert cli.main(["run", "spend.yaml"]) == 0
    run = only_run()
    # 200 tokens in the whole run, but the file's limit counts only the 100 of the follow-up
    assert cli.main(["continue", run.id, "more"]) == 0

    capsys.readouterr()
    assert cli.main(["continue", run.id, "and more", "--limit", "tokens=50"]) == 1
    captured = capsys.readouterr()
    assert "Limits for this follow-up: no cost limit, tokens 50, no duration limit" in captured.out
    assert "used 100 tokens, limit 50" in captured.err
    assert status(capsys, run)["status"] == "limit"
    # 300 tokens in the whole run: a retry with room for the follow-up's own 100 continues it
    assert cli.main(["retry", run.id, "--limit", "tokens=150"]) == 0
    assert status(capsys, run)["follow_up"] == 2
    assert (run.info()["limits"], run.info()["limits_for"]) == ({"tokens": 150}, 2)


def test_limits_that_earlier_passes_got_dont_apply_to_a_follow_up(repo, monkeypatch, capsys):
    spending(monkeypatch, 100, 300)
    write("spend.yaml", SPEND.replace("LIMITS", ""))
    assert cli.main(["run", "spend.yaml", "--limit", "tokens=200"]) == 0
    assert cli.main(["continue", only_run().id, "more"]) == 0  # its 300 tokens pass the 200 that `wf run` gave


def test_a_follow_up_counts_cost_and_time_from_its_start(repo, monkeypatch, capsys):
    spending(monkeypatch, 100, 100, cost=1.0)
    write("spend.yaml", SPEND.replace("LIMITS", "limits: {cost_usd: 1.5}"))
    assert cli.main(["run", "spend.yaml"]) == 0
    assert cli.main(["continue", only_run().id, "more"]) == 0  # $2 in the run, $1 in the follow-up

    write("slow.yaml", """
        version: 1
        name: slow
        limits: {duration: 2s}
        start: {input: {}}
        follow_up: {to: wait}
        nodes:
          wait: {needs: [start], command: "sleep 1.2  # {% if follow_up %}{{ follow_up.number }}{% endif %}"}
        end: {output: {}}
    """)
    assert cli.main(["run", "slow.yaml"]) == 0
    (slow,) = [r for r in runs.all_runs(Path.cwd()) if r.info()["workflow"] == "slow"]
    assert cli.main(["continue", slow.id, "more"]) == 0  # 2.4 seconds in the run, 1.2 in the follow-up


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
    assert run.output() == {"seen": "one\ntwo\n"}  # its branch started again where the run left it


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
                                ("running", {}, f"stopped before it ended: continue it with wf retry {run.id}"),
                                ("stall_detected", {}, f"stopped before it ended: continue it with wf retry {run.id}")):
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


def test_a_workflow_without_follow_up_or_an_agent_that_changes_files_is_refused(repo, capsys):
    run = ran(capsys, FLOW.replace("follow_up: {to: work}\n", "").replace(
        "{{ follow_up.request if follow_up else start.task }}", "{{ start.task }}").replace(
        "{{ follow_up.number if follow_up else 0 }}", "0"))
    assert cli.main(["continue", run.id, "more"]) == 1
    error = capsys.readouterr().err
    assert ("workflow `flow` has no `follow_up`, which names the node that takes a follow-up request. And it has no "
            "agent node with `edit` or `full` access to take it by default. Add `follow_up: {to: <node>}` to name one") in error
    assert run.status()["status"] == "succeeded"


def test_a_branch_that_is_gone_or_checked_out_elsewhere_changes_nothing(repo, capsys):
    run = ran(capsys)
    branch = f"wf/{run.id}/work"
    git("checkout", "-q", branch)  # you look at the result in your own checkout
    assert cli.main(["continue", run.id, "more"]) == 1
    error = capsys.readouterr().err
    assert "can't bring back the run's workspaces, so the run stays as it was: `work` worked in" in error
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

    async def broken(ctx, given):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(runner, "_give", broken)
    assert cli.main(["continue", run.id, "more", "--limit", "tokens=50"]) == 1
    assert capsys.readouterr().err == (f"wf continue: can't give run {run.id} the follow-up, so the run stays as it was: "
                                       "No space left on device\n")
    assert run.info() == before and run.status()["status"] == "succeeded" and "follow_up" not in run.status()
    assert not (workspaces.root() / run.id).exists()
    monkeypatch.setattr(runner, "_give", give)
    assert cli.main(["continue", run.id, "more"]) == 0
    assert status(capsys, run)["follow_up"] == 1


def test_a_follow_up_that_never_started_is_taken_in_by_wf_retry(repo, monkeypatch, capsys):
    run = ran(capsys)
    drive = runner._retry
    monkeypatch.setattr(runner, "_retry", lambda *args: 0)  # as if wf stopped after it wrote the request
    assert cli.main(["continue", run.id, "more"]) == 0
    monkeypatch.setattr(runner, "_retry", drive)
    # Its state says the follow-up runs, never that it succeeded with the old output
    state = operate.state(run)
    assert (state["status"], state["follow_up"], "output" in state) == ("running", 1, False)
    assert firstmate.invoke({"operation": "result.terminal", "input": {"content": json.dumps(state)}})["result"] == {"value": False}
    capsys.readouterr()
    assert cli.main(["continue", run.id, "other"]) == 1
    assert capsys.readouterr().err == (f"wf continue: run {run.id} stopped before it ended: "
                                       f"continue it with wf retry {run.id}\n")
    assert cli.main(["retry", run.id]) == 0
    assert run.output()["notes"].splitlines()[-1] == "more (check is none)"
    assert [r["action"] for r in trace_view.records(run) if r["kind"] == "run"] == ["started", "continued"]


def test_a_follow_up_that_stopped_before_its_request_was_written_leaves_the_run_as_it_was(repo, capsys):
    run = ran(capsys)
    run.set_status("running", follow_up=1)  # as wf continue leaves it when it is killed before its checkpoint write
    assert cli.main(["retry", run.id]) == 0
    assert run.status()["status"] == "succeeded" and "follow_up" not in run.status()
    assert cli.main(["continue", run.id, "more"]) == 0
    assert run.status()["follow_up"] == 1


def test_a_run_whose_end_isnt_in_its_checkpoints_is_refused(repo, capsys):
    run = ran(capsys)
    run.checkpoints.unlink()
    assert cli.main(["continue", run.id, "more"]) == 1
    assert capsys.readouterr().err == (f"wf continue: run {run.id} has no checkpoint of its end to continue from: "
                                       f"wf retry {run.id} says where it stands\n")


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


# ---------- a workflow without follow_up ----------


def editing(nodes_yaml: str, top: str = "") -> spec.Workflow:
    text = f"version: 1\nname: edits\nstart: {{input: {{}}}}\n{top}nodes:\n{indent(dedent(nodes_yaml).strip(), '  ')}\nend: {{output: {{}}}}\n"
    return spec.parse(text)


@pytest.mark.parametrize("nodes_yaml, top, found", [
    # Read-only agents and commands don't take it, and an agent with `edit` or `full` access does
    ("""
     plan: {needs: [start], instruction: plan}
     test: {needs: [plan], command: "true"}
     merge: {needs: [test], instruction: merge, access: full}
     """, "", ("merge",)),
    # The access that `defaults` gives counts, for agent nodes only
    ("""
     plan: {needs: [start], instruction: plan}
     build: {needs: [plan], instruction: build}
     """, "defaults: {access: edit}\n", ("plan",)),
    ("""
     test: {needs: [start], command: "true"}
     ask: {needs: [test], human: "ok?", output: {ok: {type: boolean}}}
     implement: {needs: [ask], instruction: implement}
     """, "defaults: {access: edit}\n", ("implement",)),
    # Of two nodes at the same step, the first in the file takes it
    ("""
     plan: {needs: [start], instruction: plan}
     docs: {needs: [plan], instruction: docs, access: edit}
     code: {needs: [plan], instruction: code, access: edit}
     """, "", ("docs",)),
    # The earliest step goes first, whatever the order in the file: a join runs after the longest path to it
    ("""
     polish: {needs: [setup, implement], instruction: polish, access: edit}
     setup: {needs: [start], command: "true"}
     plan: {needs: [start], instruction: plan}
     implement: {needs: [plan], instruction: implement, access: edit}
     """, "", ("implement",)),
    # A route that goes back doesn't move a node to a later step
    ("""
     fix: {instruction: fix, access: full}
     implement: {needs: [start], instruction: implement, access: edit}
     check:
       needs: [implement]
       command: "true"
       routes: [{when: "check.exit_code != 0", to: fix, max: 1}, {to: implement, max: 2}, {to: end}]
     """, "", ("implement",)),
    # `needs` decides the order, also for a node that comes first in the file, and that a route leads back to
    ("""
     implement: {needs: [start, plan], instruction: implement, access: edit}
     check: {needs: [implement], command: "true", routes: [{to: plan, max: 1}, {to: end}]}
     plan: {needs: [start], instruction: plan, access: edit}
     """, "", ("plan",)),
    # A route that a run might not take doesn't change the order: it is the same for each run
    ("""
     classify: {needs: [start], command: "true", routes: [{when: "classify.exit_code == 0", to: bugfix}, {to: prepare}]}
     bugfix: {instruction: fix, access: edit}
     prepare: {command: "true", routes: [{to: feature}]}
     feature: {instruction: build, access: edit}
     """, "", ("bugfix",)),
    # A route target counts as any node
    ("""
     plan: {needs: [start], instruction: plan}
     check: {needs: [plan], command: "true", routes: [{when: "check.exit_code != 0", to: fix}, {to: end}]}
     fix: {instruction: fix, access: edit}
     """, "", ("fix",)),
    # Without such a node, no node takes it
    ("""
     plan: {needs: [start], instruction: plan}
     test: {needs: [plan], command: "true"}
     """, "", ()),
    # A node that start doesn't lead to, in a file that validate refuses, doesn't take it
    ("""
     orphan: {instruction: work, access: edit}
     work: {needs: [start], instruction: work, access: edit}
     """, "", ("work",)),
    # Nor does any node of a file with a node named `start`
    ("""
     start: {instruction: work, access: edit}
     """, "", ()),
    ("""
     a: {needs: [start], instruction: a, access: edit}
     start: {needs: [a], command: x}
     """, "", ()),
    # `follow_up` wins
    ("""
     plan: {needs: [start], instruction: plan}
     implement: {needs: [plan], instruction: implement, access: edit}
     """, "follow_up: {to: plan}\n", ("plan",)),
])
def test_without_follow_up_the_first_agent_that_can_change_files_takes_the_request(nodes_yaml, top, found):
    workflow = editing(nodes_yaml, top)
    assert scheduler.follow_up_targets(workflow) == found
    if not found:
        assert "wf_follow_up" not in diagram.mermaid(workflow)


def test_the_node_that_takes_a_request_is_found_in_a_workflow_longer_than_the_limit_on_recursion():
    count = sys.getrecursionlimit() + 100
    chain = "".join(f"c{i}: {{needs: [{f'c{i - 1}' if i else 'start'}], command: 'true'}}\n" for i in range(count))
    workflow = editing(chain + f"edit: {{needs: [c{count - 1}], instruction: x, access: edit}}\n")
    assert scheduler.follow_up_targets(workflow) == ("edit",)
    assert "  wf_follow_up -.-> n_edit\n" in diagram.mermaid(workflow)


@pytest.mark.parametrize("nodes_yaml, error", [
    ("start: {instruction: work, access: edit}", "nodes.start: `start` is a reserved name"),
    ("a: {needs: [start], instruction: a, access: edit}\nstart: {needs: [a], command: x}", "nodes.start: `start` is a reserved name"),
])
def test_validate_reports_a_node_named_start_with_an_agent_that_changes_files(repo, nodes_yaml, error):
    write("bad.yaml", f"version: 1\nname: bad\nstart: {{input: {{}}}}\nnodes:\n{indent(nodes_yaml, '  ')}\nend: {{output: {{}}}}\n")
    report = validate.check("bad.yaml")
    assert any(str(p) == f"bad.yaml: {error}" for p in report.errors) and report.notes == []


PLAIN = """
version: 1
name: plain
defaults: {harness: claude-code, harnesses: {claude-code: {model: opus}}}
start: {input: {}}
nodes:
  plan: {needs: [start], instruction: Plan the change}
  implement: {needs: [plan], access: edit, workspace: new, instruction: "Build {{ plan.summary }}"}
  check:
    needs: [implement]
    workspace: implement
    command: "true"
    routes: [{to: implement, max: 1}, {to: end}]
end: {output: {}}
"""


def planned(monkeypatch, capsys, text: str = PLAIN) -> tuple[runs.Run, Fake]:
    """A run of `text` that succeeded: plan once, then implement twice, as check sends it back once."""
    fake = Fake("claude-code", submit({"summary": "the plan"}), *[submit({"summary": f"pass {n}"}) for n in range(1, 5)])
    monkeypatch.setitem(harnesses.FACTORIES, "claude-code", fake.factory)
    before = {run.id for run in runs.all_runs(Path.cwd())}
    write("plain.yaml", text)
    assert cli.main(["run", "plain.yaml"]) == 0
    capsys.readouterr()
    (run,) = [run for run in runs.all_runs(Path.cwd()) if run.id not in before]
    return run, fake


def test_a_follow_up_goes_to_the_first_agent_that_can_change_files_with_the_request_in_its_instruction(repo, monkeypatch, capsys):
    run, fake = planned(monkeypatch, capsys)
    assert len(fake.sent) == 3
    assert "Follow-up request" not in "".join(fake.sent)

    assert cli.main(["continue", run.id, "add a flag"]) == 0
    (given,) = [e for e in events(run) if e["event"] == "follow_up"]
    assert given["to"] == ["implement"]
    # The first pass in the follow-up finds the request in a section of its own, and a later pass doesn't
    first, later = fake.sent[3:]
    assert first == "\n\n".join(["Build the plan", nodes.FOLLOW_UP, nodes.AFTER_A_PASS, "add a flag", nodes.DONT_COMMIT,
                                  output_tool.FINISH])
    assert later == "\n\n".join(["Build the plan", nodes.DONT_COMMIT, output_tool.FINISH])
    assert (run.dir / "nodes" / "implement" / "3" / "instruction.md").read_text() == first
    assert sorted(p.name for p in (run.dir / "nodes" / "plan").iterdir()) == ["1"]
    # A second follow-up brings its own request, on each attempt of the first pass and not on a later pass
    fake.turns += [ends("failed", "it broke"), *[submit({"summary": f"pass {n}"}) for n in (5, 6)]]
    assert cli.main(["continue", run.id, "and a test"]) == 0
    assert [text.count("## Follow-up request") for text in fake.sent[5:]] == [1, 1, 0]
    assert "and a test" in fake.sent[5] and "add a flag" not in fake.sent[5]


LOOP_NODES = {
    "plan": '{needs: [start], command: "echo planned"}',
    "implement": '{needs: [plan], access: edit, instruction: "Build {{ plan.stdout | trim }}"}',
    "check": '{needs: [implement], command: "true", routes: [{to: plan, max: 1}, {to: end}]}',
}


@pytest.mark.parametrize("order, implement", [
    (("plan", "implement", "check"), LOOP_NODES["implement"]),
    # The file names implement first, so a walk from start in file order meets it before plan
    (("implement", "check", "plan"), LOOP_NODES["implement"].replace("needs: [plan]", "needs: [start, plan]")),
])
def test_a_follow_up_keeps_the_outputs_before_its_node_also_of_a_node_that_a_loop_leads_back_to(repo, monkeypatch, capsys,
                                                                                                order, implement):
    fake = Fake("claude-code", *[submit({"summary": f"pass {n}"}) for n in range(1, 5)])
    monkeypatch.setitem(harnesses.FACTORIES, "claude-code", fake.factory)
    given = {**LOOP_NODES, "implement": implement}
    write("loop.yaml", "version: 1\nname: loop\ndefaults: {harness: claude-code, harnesses: {claude-code: {model: opus}}}\n"
                       "start: {input: {}}\nnodes:\n" + "".join(f"  {name}: {given[name]}\n" for name in order) +
                       "end: {output: {}}\n")
    assert validate.check("loop.yaml").problems == []
    assert cli.main(["run", "loop.yaml"]) == 0
    run = only_run()
    assert cli.main(["continue", run.id, "add a flag"]) == 0
    # implement reads what plan gave last, as on a pass of the run from implement, and plan runs again in the loop
    first, later = fake.sent[2:]
    assert first.startswith(f"Build planned\n\n{nodes.FOLLOW_UP}\n\n{nodes.AFTER_A_PASS}\n\nadd a flag\n\n")
    assert later.startswith("Build planned\n\nWhen you're done")
    assert sorted(p.name for p in (run.dir / "nodes" / "plan").iterdir()) == ["1", "2", "3"]


def test_a_follow_up_keeps_the_output_of_the_route_that_the_run_took_before_its_node(repo, monkeypatch, capsys):
    fake = Fake("claude-code", *[submit({"summary": f"pass {n}"}) for n in range(1, 5)])
    monkeypatch.setitem(harnesses.FACTORIES, "claude-code", fake.factory)
    write("routed.yaml", """
        version: 1
        name: routed
        defaults: {harness: claude-code, harnesses: {claude-code: {model: opus}}}
        start: {input: {kind: {type: string}}}
        nodes:
          classify:
            needs: [start]
            command: "echo {{ start.kind | quote }}"
            routes: [{when: "classify.stdout | trim == 'a'", to: plan_a}, {to: plan_b}]
          plan_a: {command: "echo plan a"}
          plan_b: {command: "echo plan b"}
          implement:
            needs: [plan_a, plan_b]
            access: edit
            max_parallel: 1
            for_each: "[plan_a.stdout] if plan_a else ([plan_b.stdout] if plan_b else [])"
            instruction: "Build {{ item | trim }}"
          check: {needs: [implement], command: "true", routes: [{to: classify, max: 1}, {to: end}]}
        end: {output: {}}
    """)
    assert validate.check("routed.yaml").problems == []
    assert cli.main(["run", "routed.yaml", "kind=b"]) == 0
    run = only_run()
    assert cli.main(["continue", run.id, "add a flag"]) == 0
    # plan_b starts before implement, so it keeps its output, and implement's first pass has its item and the request
    first, later = fake.sent[2:]
    assert first.startswith(f"Build plan b\n\n{nodes.FOLLOW_UP}\n\n{nodes.AFTER_A_PASS}\n\nadd a flag\n\n")
    assert later.startswith("Build plan b\n\nWhen you're done")
    assert sorted(p.name for p in (run.dir / "nodes" / "plan_b").iterdir()) == ["1", "2", "3"]


def test_a_follow_up_keeps_the_output_of_a_planner_that_a_route_chose_before_its_node(repo, monkeypatch, capsys):
    fake = Fake("claude-code", *[submit({"summary": f"pass {n}"}) for n in range(1, 3)])
    monkeypatch.setitem(harnesses.FACTORIES, "claude-code", fake.factory)
    write("planned.yaml", """
        version: 1
        name: planned
        defaults: {harness: claude-code, harnesses: {claude-code: {model: opus}}}
        start: {input: {size: {type: string}}}
        nodes:
          triage:
            needs: [start]
            command: "echo {{ start.size | quote }}"
            routes: [{when: "triage.stdout | trim == 'big'", to: plan}, {to: implement}]
          plan: {command: "echo step one", routes: [{to: implement}]}
          implement:
            access: edit
            max_parallel: 1
            for_each: "[plan.stdout] if plan else []"
            instruction: "Build {{ item | trim }}"
          review:
            needs: [implement]
            command: "echo done"
            routes: [{when: "review.stdout | trim == 'again'", to: plan, max: 1}, {to: end}]
        end: {output: {}}
    """)
    assert validate.check("planned.yaml").problems == []
    assert cli.main(["run", "planned.yaml", "size=big"]) == 0
    run = only_run()
    assert cli.main(["continue", run.id, "add a flag"]) == 0
    # implement ran after plan in the run, so its pass in the follow-up has plan's item and the request
    (sent,) = fake.sent[1:]
    assert sent.startswith(f"Build step one\n\n{nodes.FOLLOW_UP}\n\n{nodes.AFTER_A_PASS}\n\nadd a flag\n\n")
    assert sorted(p.name for p in (run.dir / "nodes" / "plan").iterdir()) == ["1"]


def test_the_request_goes_to_the_first_pass_with_a_job_when_a_for_each_pass_has_no_items(repo, monkeypatch, capsys):
    fake = Fake("claude-code", *[submit({"summary": f"pass {n}"}) for n in range(1, 4)])
    monkeypatch.setitem(harnesses.FACTORIES, "claude-code", fake.factory)
    write("fix.yaml", """
        version: 1
        name: fix
        defaults: {harness: claude-code, harnesses: {claude-code: {model: opus}}}
        start: {input: {}}
        nodes:
          fix:
            needs: [start]
            access: edit
            max_parallel: 1
            for_each: "[check.stdout] if check else []"
            instruction: "Fix {{ item | trim }}"
          check: {needs: [fix], command: "echo broken", routes: [{to: fix, max: 1}, {to: end}]}
        end: {output: {}}
    """)
    assert validate.check("fix.yaml").problems == []
    assert cli.main(["run", "fix.yaml"]) == 0
    run = only_run()
    assert len(fake.sent) == 1
    assert cli.main(["continue", run.id, "add a flag"]) == 0
    # The first pass of fix in the follow-up has no items, so the next pass, after check, gets the request
    (sent,) = fake.sent[1:]
    assert sent.startswith(f"Fix broken\n\n{nodes.FOLLOW_UP}\n\n{nodes.AFTER_A_PASS}\n\nadd a flag\n\n")
    assert sorted(p.name for p in (run.dir / "nodes" / "fix").iterdir()) == ["2", "4"]


def test_a_follow_up_goes_to_the_first_agent_in_the_order_of_the_graph_and_says_when_it_never_ran(repo, monkeypatch, capsys):
    fake = Fake("claude-code", *[submit({"summary": f"pass {n}"}) for n in range(1, 4)])
    monkeypatch.setitem(harnesses.FACTORIES, "claude-code", fake.factory)
    write("branch.yaml", """
        version: 1
        name: branch
        defaults: {harness: claude-code, harnesses: {claude-code: {model: opus}}}
        start: {input: {kind: {type: string}}}
        nodes:
          classify:
            needs: [start]
            command: "echo {{ start.kind | quote }}"
            routes: [{when: "classify.stdout | trim == 'bug'", to: bugfix}, {to: feature}]
          bugfix: {access: edit, instruction: Fix the bug}
          feature: {access: edit, instruction: Build the feature}
        end: {output: {}}
    """)
    assert validate.check("branch.yaml").notes == [
        "`wf continue` sends a follow-up request to `bugfix`, the first agent node with `edit` or `full` access. "
        "Name another node with `follow_up: {to: <node>}`"]
    assert cli.main(["run", "branch.yaml", "kind=feature"]) == 0
    run = only_run()
    # The request goes to bugfix in each run, also in one that took the route to feature
    assert cli.main(["continue", run.id, "add a flag"]) == 0
    (given,) = [e for e in events(run) if e["event"] == "follow_up"]
    assert given["to"] == ["bugfix"]
    assert fake.sent[1].startswith(f"Fix the bug\n\n{nodes.FOLLOW_UP}\n\n{nodes.FIRST_PASS}\n\nadd a flag\n\n")
    assert sorted(p.name for p in (run.dir / "nodes" / "feature").iterdir()) == ["1"]
    # The node that takes the request didn't run either in this run
    write("tests.yaml", """
        version: 1
        name: tests
        defaults: {harness: claude-code, harnesses: {claude-code: {model: opus}}}
        start: {input: {}}
        nodes:
          tests: {needs: [start], command: "true", check: false, routes: [{when: "tests.exit_code != 0", to: fix}, {to: end}]}
          fix: {access: edit, instruction: "Fix {{ tests.stdout }}"}
        end: {output: {}}
    """)
    assert cli.main(["run", "tests.yaml"]) == 0
    (other,) = [r for r in runs.all_runs(Path.cwd()) if r.id != run.id]
    assert cli.main(["continue", other.id, "add a test"]) == 0
    assert fake.sent[2].startswith(f"Fix\n\n{nodes.FOLLOW_UP}\n\n{nodes.FIRST_PASS}\n\nadd a test\n\n")


def test_an_instruction_that_reads_follow_up_itself_is_left_as_it_is(repo, monkeypatch, capsys):
    text = PLAIN.replace('"Build {{ plan.summary }}"',
                         '"{% if follow_up %}Now: {{ follow_up.request }}{% else %}Build {{ plan.summary }}{% endif %}"')
    write("plain.yaml", text)
    assert validate.check("plain.yaml").problems == []  # a node takes follow-ups, so a template can read `follow_up`
    run, fake = planned(monkeypatch, capsys, text)
    assert cli.main(["continue", run.id, "add a flag"]) == 0
    assert [sent.split("\n\n")[0] for sent in fake.sent[3:]] == ["Now: add a flag", "Now: add a flag"]
    assert "Follow-up request" not in "".join(fake.sent)


def test_a_workflows_own_follow_up_wins_and_its_nodes_get_no_section(repo, monkeypatch, capsys):
    text = PLAIN.replace("start: {input: {}}\n", "start: {input: {}}\nfollow_up: {to: check}\n").replace(
        'command: "true"', 'command: "echo {% if follow_up %}{{ follow_up.request | quote }}{% endif %}"')
    run, fake = planned(monkeypatch, capsys, text)
    assert cli.main(["continue", run.id, "add a flag"]) == 0
    (given,) = [e for e in events(run) if e["event"] == "follow_up"]
    assert given["to"] == ["check"]
    # check sends the run back to implement once, as in each pass, and wf adds nothing to its instruction
    assert fake.sent[3] == "\n\n".join(["Build the plan", nodes.DONT_COMMIT, output_tool.FINISH])
    assert len(fake.sent) == 4
    # A named node whose instruction doesn't read `follow_up` gets nothing either, and validate still warns
    named = PLAIN.replace("start: {input: {}}\n", "start: {input: {}}\nfollow_up: {to: implement}\n")
    write("named.yaml", named)
    assert [p.where for p in validate.check("named.yaml").problems] == ["follow_up"]
    assert validate.check("named.yaml").notes == []
    run, fake = planned(monkeypatch, capsys, named)
    assert cli.main(["continue", run.id, "add a flag"]) == 0
    assert "Follow-up request" not in "".join(fake.sent)


def test_validate_says_where_a_follow_up_goes_and_doesnt_warn(repo, capsys):
    write("plain.yaml", PLAIN)
    assert validate.check("plain.yaml").problems == []
    assert cli.main(["validate", "plain.yaml"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "plain.yaml: note: `wf continue` sends a follow-up request to `implement`, the first agent node with `edit` "
        "or `full` access. Name another node with `follow_up: {to: <node>}`",
        "plain.yaml: valid",
    ]
    write("read.yaml", PLAIN.replace("access: edit, ", ""))
    assert validate.check("read.yaml").problems == []
    assert validate.check("read.yaml").notes == [
        "`wf continue` refuses the runs of this workflow, because it has no agent node with `edit` or `full` access. "
        "Name a node that takes a follow-up request with `follow_up: {to: <node>}`"]
    # A file with errors gets no note
    write("broken.yaml", PLAIN.replace("needs: [plan], access", "needs: [plann], access"))
    assert validate.check("broken.yaml").errors and validate.check("broken.yaml").notes == []
    # A sub-workflow's own default isn't the run's: only the file that runs gets a note
    write("parent.yaml", """
        version: 1
        name: parent
        start: {input: {}}
        nodes:
          build: {needs: [start], workflow: plain.yaml}
        end: {output: {}}
    """)
    assert validate.check("parent.yaml").notes == [
        "`wf continue` refuses the runs of this workflow, because it has no agent node with `edit` or `full` access. "
        "Name a node that takes a follow-up request with `follow_up: {to: <node>}`"]


def test_wf_retry_of_a_run_that_succeeded_says_how_to_give_it_a_follow_up(repo, monkeypatch, capsys):
    run, _ = planned(monkeypatch, capsys)
    assert cli.main(["retry", run.id]) == 0
    assert f"Give it a follow-up with: wf continue {run.id} <request>" in capsys.readouterr().out
    write("flow.yaml", FLOW.replace("follow_up: {to: work}\n", "").replace(
        "{{ follow_up.request if follow_up else start.task }}", "{{ start.task }}").replace(
        "{{ follow_up.number if follow_up else 0 }}", "0"))
    assert cli.main(["run", "flow.yaml", "task=hello"]) == 0
    (other,) = [r for r in runs.all_runs(Path.cwd()) if r.id != run.id]
    capsys.readouterr()
    assert cli.main(["retry", other.id]) == 0
    assert "Give it a follow-up" not in capsys.readouterr().out


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


@pytest.mark.parametrize("has_stall", [False, True])
def test_firstmate_sees_the_follow_up_as_a_new_pass_of_the_same_run(repo, monkeypatch, capsys, has_stall):
    monkeypatch.setattr(firstmate, "POLL_SECONDS", 1)
    run = ran(capsys)
    run.add_event({"event": "stall_detected", "node": "work", "pass": 1, "attempt": 1})

    def poll(source: str) -> dict:
        reply = firstmate.invoke({"operation": "source.poll", "input": {"source_id": source, "config_ref": f"wf-run:{run.dir}"}})
        return json.loads(reply["result"]["output"])

    assert poll("wf-run")["stall"]["pass"] == 1
    assert "follow_up" not in poll("wf-run")
    for number in (1, 2):
        assert cli.main(["continue", run.id, "more"]) == 0
        if has_stall:
            run.add_event({"event": "stall_detected", "node": "work", "pass": number + 1, "attempt": 1})
            notice = poll(f"wf-run-{number}")
            assert (notice["status"], notice["follow_up"], notice["stall"]["pass"]) == ("stall_detected", number, number + 1)
        state = poll(f"wf-run-{number}")
        assert (state["status"], state["follow_up"]) == ("succeeded", number)
    assert firstmate.invoke({"operation": "result.terminal", "input": {"content": json.dumps(state)}})["result"] == {"value": True}
    slim = json.loads(firstmate._bounded({**state, "nodes": ["x" * 40_000]}, "wf status"))
    assert (slim["status"], slim["follow_up"]) == ("succeeded", 2)  # cut down to fit, it still says which pass


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


def test_wf_ui_reads_only_the_follow_up_into_the_view_of_a_run_it_has_shown(repo, capsys, monkeypatch):
    run = ran(capsys)
    assert ui.run_detail(run)["metrics"]["run"]["follow_ups"] == 0
    assert cli.main(["continue", run.id, "more"]) == 0
    logged = events(run)
    later = next(i for i, e in enumerate(logged) if e["event"] == "follow_up")
    parsed, real_parse = [], runs.parse_events
    monkeypatch.setattr(runs, "parse_events", lambda lines: parsed.append(real_parse(lines)) or parsed[-1])
    found = ui.run_detail(run)
    assert [len(new) for new in parsed] == [len(logged) - later]  # the events of the follow-up
    assert found["metrics"]["run"]["follow_ups"] == 1
    assert found["metrics"]["nodes"] == report.build([run])["nodes"]
    assert found["states"] == {"plan": "done", "work": "done", "check": "done"}
    assert found["progress"] == progress.of(run)
    times = ["2026-10-20T10:00:00+00:00"] * later + ["2026-10-21T10:00:00+00:00"] * (len(logged) - later)
    (run.dir / "events.jsonl").write_text("".join(json.dumps({**e, "time": t}) + "\n" for e, t in zip(logged, times)))
    assert ui.run_detail(run)["metrics"]["run"]["duration_s"] == 0  # the day the run waited for the follow-up doesn't count


def test_wf_graph_shows_where_a_follow_up_comes_in(repo, capsys):
    write("flow.yaml", FLOW)
    assert cli.main(["graph", "flow.yaml"]) == 0
    assert '  wf_follow_up(["follow-up"])\n  wf_follow_up -.-> n_work\n' in capsys.readouterr().out
    # Without `follow_up`, the arrow goes to the node that takes the request, also in a file that validate refuses
    write("plain.yaml", PLAIN.replace("{to: end}", "{to: ending}"))
    assert cli.main(["graph", "plain.yaml"]) == 0
    assert '  wf_follow_up(["follow-up"])\n  wf_follow_up -.-> n_implement\n' in capsys.readouterr().out


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
    assert "nodes.implement.command: unknown name `follow_up`" in without
    unguarded = problems(REVIEW.replace("{% if follow_up %}{{ follow_up.request | quote }}{% endif %}", "{{ follow_up.request | quote }}"))
    assert unguarded == ["warning: nodes.implement.command: reads `follow_up.request`, but `follow_up` is none until "
                         "`wf continue` gives the run a request; check first with {% if follow_up %}...{% endif %}, "
                         "or in a single value `follow_up.request if follow_up else none`"]
    unread = problems(REVIEW.replace("{% if follow_up %}{{ follow_up.request | quote }}{% endif %}", "x"))
    assert unread == ["warning: follow_up: no node that a follow-up runs again reads `follow_up`, so its request reaches "
                      "no node; put {{ follow_up.request }} in the instruction of a node that `follow_up.to` starts, inside "
                      "{% if follow_up %}...{% endif %}"]
    # A node that a follow-up doesn't run again never gets the request
    before = problems("""
        version: 1
        name: before
        start: {input: {}}
        follow_up: {to: work}
        nodes:
          plan: {needs: [start], command: "echo {% if follow_up %}{{ follow_up.request | quote }}{% endif %}"}
          work: {needs: [plan], command: "true"}
        end: {output: {}}
    """)
    assert any("no node that a follow-up runs again reads `follow_up`" in p for p in before)
    unquoted = problems(REVIEW.replace("{{ follow_up.request | quote }}", "{{ follow_up.request }}"))
    assert any("puts output from `follow_up` into the shell unquoted" in p for p in unquoted)


def test_a_template_that_reads_a_node_a_follow_up_clears_gets_a_warning(repo):
    at_review = REVIEW.replace("follow_up: {to: implement}", "follow_up: {to: review}")
    why = "might not have run yet in a follow-up, which clears the outputs of the nodes that `follow_up.to` can lead to"
    # The route back to implement has no `when`, so the follow-up takes it once, and tests runs again before the end
    assert [p.split(":", 2)[1].strip() for p in problems(at_review) if why in p] == ["nodes.review.command"]
    found = problems(at_review.replace("{to: implement, max: 1}", '{when: "review.exit_code != 0", to: implement, max: 1}'))
    assert [p.split(":", 2)[1].strip() for p in found if why in p] == ["nodes.review.command", "end.output.passed.value"]


def test_a_template_that_reads_results_of_a_node_a_follow_up_clears_gets_a_warning(repo):
    found = problems("""
        version: 1
        name: again
        start: {input: {}}
        follow_up: {to: check}
        nodes:
          implement: {needs: [start], command: "true"}
          tests: {needs: [implement], for_each: "[1]", command: "true"}
          check:
            needs: [tests]
            command: "echo {{ results.tests[0].output.exit_code }} {% if follow_up %}{{ follow_up.request | quote }}{% endif %}"
            routes: [{to: implement, max: 1}, {to: end}]
        end: {output: {}}
    """)
    why = "might not have run yet in a follow-up, which clears the outputs of the nodes that `follow_up.to` can lead to"
    assert [p.split(":", 2)[1].strip() for p in found if why in p] == ["nodes.check.command"]  # results.tests is [] then


# ---------- the workspaces that come back ----------


SHARE = """
version: 1
name: share
start: {input: {}}
follow_up: {to: publish}
nodes:
  build:
    needs: [start]
    workspace: new
    command: echo built > out && git add out && git -c user.email=t@t -c user.name=t commit -qm built
  scratch: {needs: [build], workspace: temp, command: "echo note > note"}
  publish:
    needs: [scratch]
    workspace: build
    env: {SCRATCH: "{{ workspace.scratch.path }}", BUILD: "{{ workspace.build.path }}"}
    command: 'cat out && test -d "$BUILD" && echo {% if follow_up %}{{ follow_up.request | quote }}{% endif %}'
  reuse: {needs: [publish], workspace: scratch, command: "ls -A | wc -l"}
end:
  output:
    said: {type: string, value: "{{ publish.stdout }}"}
    files: {type: string, value: "{{ reuse.stdout | trim }}"}
"""


def test_a_follow_up_finds_the_workspaces_of_nodes_it_doesnt_run_again(repo, capsys):
    write("share.yaml", SHARE)
    assert cli.main(["run", "share.yaml"]) == 0
    run = only_run()
    assert run.output() == {"said": "built\n\n", "files": "1"}
    assert cli.main(["continue", run.id, "again"]) == 0
    # publish works in build's worktree and reads its path, though build doesn't run again,
    # and the temp folder that reuse shares with scratch comes back empty
    assert run.output() == {"said": "built\nagain\n", "files": "0"}


def test_a_workspace_that_cant_come_back_changes_nothing(repo, capsys):
    write("share.yaml", SHARE)
    assert cli.main(["run", "share.yaml"]) == 0
    run = only_run()
    workspaces.temp_root(run.id).parent.mkdir(parents=True, exist_ok=True)
    workspaces.temp_root(run.id).write_text("not a folder")
    capsys.readouterr()
    assert cli.main(["continue", run.id, "again"]) == 1
    assert capsys.readouterr().err.startswith("wf continue: can't bring back the run's workspaces, so the run stays as it "
                                              f"was: can't make {workspaces.temp_root(run.id)}/scratch/1: ")
    assert run.status()["status"] == "succeeded" and not (workspaces.root() / run.id).exists()


def test_a_sub_workflows_worktree_comes_back_on_its_branch_and_a_gone_one_is_refused(repo, capsys):
    write("parent.yaml", """
        version: 1
        name: parent
        start: {input: {}}
        follow_up: {to: build}
        nodes:
          build: {needs: [start], workflow: child.yaml, with: {note: "{{ follow_up.request if follow_up else 'first' }}"}}
        end: {output: {}}
    """)
    write("child.yaml", """
        version: 1
        name: child
        start: {input: {note: {type: string}}}
        nodes:
          make:
            needs: [start]
            workspace: new
            branch: feature-pr
            env: {NOTE: "{{ start.note }}"}
            command: echo "$NOTE" >> notes && git add notes && git -c user.email=t@t -c user.name=t commit -qm "$NOTE"
        end: {output: {}}
    """)
    assert cli.main(["run", "parent.yaml"]) == 0
    run = only_run()
    assert cli.main(["continue", run.id, "second"]) == 0
    assert git("log", "--format=%s", "feature-pr").splitlines() == ["second", "first", "first"]
    git("branch", "-q", "-D", "feature-pr")
    capsys.readouterr()
    assert cli.main(["continue", run.id, "third"]) == 1
    assert "`build/make` worked in" in (error := capsys.readouterr().err) and "its branch `feature-pr` is gone" in error


def test_a_follow_up_that_works_in_your_checkout_needs_the_branch_the_run_worked_on(repo, capsys):
    write("here.yaml", """
        version: 1
        name: here
        start: {input: {}}
        follow_up: {to: look}
        nodes:
          look: {needs: [start], command: "git rev-parse --abbrev-ref HEAD  # {% if follow_up %}{{ follow_up.number }}{% endif %}"}
        end: {output: {branch: {type: string, value: "{{ look.stdout | trim }}"}}}
    """)
    branch = git("rev-parse", "--abbrev-ref", "HEAD")
    assert cli.main(["run", "here.yaml"]) == 0
    run = only_run()
    git("checkout", "-q", "-b", "other")
    capsys.readouterr()
    assert cli.main(["continue", run.id, "more"]) == 1
    assert capsys.readouterr().err == (f"wf continue: run {run.id} worked on branch `{branch}` in {Path.cwd().resolve()}, "
                                       f"and that checkout is on `other` now: check out `{branch}` there first\n")
    git("checkout", "-q", branch)
    assert cli.main(["continue", run.id, "more"]) == 0
    assert run.output() == {"branch": branch}


# ---------- entries, state, limits and the trace ----------


def test_a_follow_up_can_start_at_several_nodes(repo, capsys):
    write("two.yaml", """
        version: 1
        name: two
        start: {input: {}}
        follow_up: {to: [left, right]}
        nodes:
          left: {needs: [start], command: "echo L{% if follow_up %}{{ follow_up.number }}{% endif %}"}
          right: {needs: [start], command: "echo R{% if follow_up %}{{ follow_up.number }}{% endif %}"}
          join: {needs: [left, right], env: {BOTH: "{{ left.stdout | trim }}{{ right.stdout | trim }}"}, command: 'echo "$BOTH"'}
        end: {output: {both: {type: string, value: "{{ join.stdout | trim }}"}}}
    """)
    assert [str(p) for p in validate.check("two.yaml").problems] == []  # both entries run before the join, also in a follow-up
    assert cli.main(["run", "two.yaml"]) == 0
    run = only_run()
    assert cli.main(["continue", run.id, "more"]) == 0
    assert run.output() == {"both": "L1R1"}


def test_an_entry_that_only_a_route_leads_from_isnt_certain_before_another_entry(repo):
    found = problems("""
        version: 1
        name: race
        start: {input: {}}
        follow_up: {to: [a, b]}
        nodes:
          a: {needs: [start], env: {B: "{{ b.stdout }}"}, command: "echo {% if follow_up %}{{ follow_up.number }}{% endif %}"}
          b: {needs: [start], command: "true", routes: [{to: a, max: 1}, {to: end}]}
        end: {output: {}}
    """)
    # Both entries start at once in a follow-up: a doesn't need b, so b may not have run when a starts
    assert any(p.startswith("warning: nodes.a.env.B: reads `b.stdout`, but `b` might not have run yet in a follow-up")
               for p in found), found


def test_an_entry_that_the_scheduler_starts_first_to_break_a_wait_reads_no_later_entry(repo):
    text = """
        version: 1
        name: cycle
        start: {input: {}}
        follow_up: {to: [a, b]}
        nodes:
          c: {needs: [start], command: "true"}
          d: {needs: [start], command: "true"}
          a: {needs: [c], command: "echo {% if follow_up %}{{ b.stdout | quote }}{% endif %}", routes: [{to: d, max: 1}, {to: end}]}
          b: {needs: [d], command: "echo {{ a.stdout | quote }}", routes: [{to: c, max: 1}, {to: end}]}
        end: {output: {}}
    """
    found = problems(text)
    # a and b each wait for a need that the other leads to, so the scheduler starts a, the first in the file
    assert [p.split(":", 2)[1].strip() for p in found if "might not have run yet in a follow-up" in p] == ["nodes.a.command"]
    assert [job["node"] for job in following_up().starts_now(0.0)] == ["a"]


def test_an_entry_waits_for_a_later_entry_it_needs_when_no_wait_can_lock(repo):
    write("later.yaml", """
        version: 1
        name: later
        start: {input: {}}
        follow_up: {to: [a, b]}
        nodes:
          a: {needs: [b], command: "echo {{ b.stdout | trim | quote }}{% if follow_up %} {{ follow_up.number }}{% endif %}"}
          b: {needs: [start], command: "echo B"}
        end: {output: {said: {type: string, value: "{{ a.stdout | trim }}"}}}
    """)
    # b waits for no node, so the scheduler never starts a first to break a wait: file order doesn't matter
    assert [str(p) for p in validate.check("later.yaml").problems] == []
    assert cli.main(["run", "later.yaml"]) == 0
    run = only_run()
    assert cli.main(["continue", run.id, "more"]) == 0
    assert run.output() == {"said": "B 1"}


def test_a_node_that_an_entry_leads_to_isnt_run_before_a_join_that_the_scheduler_can_start_first(repo):
    found = problems("""
        version: 1
        name: lock
        start: {input: {}}
        follow_up: {to: [a, b]}
        nodes:
          b: {needs: [start], command: "true"}
          a: {needs: [start], command: "true"}
          join: {needs: [x, b], command: "echo {% if follow_up %}{{ x.stdout | quote }}{% endif %}", routes: [{to: c, max: 1}, {to: end}]}
          c: {needs: [start], command: "true"}
          x: {needs: [a, c], command: "true"}
        end: {output: {}}
    """)
    # x waits for c, which join leads to, and join waits for x. With both pending, the scheduler starts join, the first
    assert [p.split(":", 2)[1].strip() for p in found if "might not have run yet in a follow-up" in p] == ["nodes.join.command"]
    progress = following_up()
    jobs = progress.starts_now(0.0)
    assert [job["node"] for job in jobs] == ["b", "a"]
    for job in jobs:
        progress.record({"node": job["node"], "pass_no": job["pass_no"], "index": None, "ok": True, "output": {"stdout": ""},
                         "error": None, "attempts": 1, "workspace": None, "usage": {}})
    (join,) = progress.starts_now(0.0)
    assert join["node"] == "join" and join["vars"]["x"] is None


def test_a_node_that_a_join_waits_for_runs_first_when_both_become_pending_together(repo, capsys):
    write("siblings.yaml", """
        version: 1
        name: siblings
        start: {input: {}}
        follow_up: {to: a}
        nodes:
          a: {needs: [start], command: "echo A{% if follow_up %}{{ follow_up.number }}{% endif %}"}
          x: {needs: [a, c], command: "echo X"}
          join: {needs: [a, x], command: "echo {{ x.stdout | trim | quote }}", routes: [{to: c, max: 1}, {to: end}]}
          c: {needs: [start], command: "echo C"}
        end: {output: {said: {type: string, value: "{{ join.stdout | trim }}"}}}
    """)
    # a makes x and join pending at once, in file order. Both wait, so the scheduler starts x, the first
    assert [str(p) for p in validate.check("siblings.yaml").problems] == []
    assert cli.main(["run", "siblings.yaml"]) == 0
    run = only_run()
    assert cli.main(["continue", run.id, "more"]) == 0
    assert run.output() == {"said": "X"}


def test_each_read_that_validate_doesnt_warn_about_finds_an_output_however_a_follow_up_runs(repo, monkeypatch):
    rng = random.Random(20261020)
    monkeypatch.setattr(state.templates, "watched", lambda evaluate, when, variables: (rng.random() < 0.5, set()))
    monkeypatch.setattr(state.Progress, "_for_each", lambda self, node, pass_no, expression, variables: [0] * rng.randint(0, 1))
    checked = 0
    while checked < 150:
        text = random_workflow(rng)
        write("check.yaml", text)
        found = validate.check("check.yaml").problems
        if any(p.level == "error" for p in found):
            continue
        warned = {(m[1], m[2]) for p in found if (m := re.match(r"nodes\.(\w+)\.command: reads `(\w+)\.stdout`", f"{p.where}: {p.message}"))}
        workflow = spec.load("check.yaml")
        progress = state.Progress({}, workflow, scheduler.Topology.of(workflow), lambda event: None)
        progress.begin({}, {"id": "x", "dir": "x", "base": None, "branch": None, "repo": "."})
        drive(progress, rng)
        for number in (1, 2):  # a run that succeeded, and its follow-ups
            progress.s["status"], progress.s["follow_up"] = "succeeded", {"request": "more", "number": number, "limits": {}, "pending": True}
            progress.follow_up(0.0)
            for job in drive(progress, rng):
                unread = [t for t in workflow.nodes if t != job["node"] and (job["node"], t) not in warned and job["vars"][t] is None]
                assert not unread, (text, job["node"], unread)
        checked += 1


def test_validate_ends_for_an_empty_list_that_leads_back_to_itself_many_times(repo, capsys):
    others = "".join(f"  n{i}: {{needs: [start], command: 'true'}}\n" for i in range(39))
    write("loop.yaml", f"""
version: 1
name: loop
start: {{input: {{}}}}
follow_up: {{to: loop}}
nodes:
  loop: {{needs: [start], for_each: '[]', routes: [{{to: loop, max: 1000}}, {{to: end}}], command: 'echo {{% if follow_up %}}{{{{ follow_up.number }}}}{{% endif %}}'}}
{others}end: {{output: {{}}}}
""")
    assert [str(p) for p in validate.check("loop.yaml").problems] == []
    assert cli.main(["run", "loop.yaml"]) == 0
    assert cli.main(["continue", only_run().id, "more"]) == 0


def random_workflow(rng: random.Random) -> str:
    """A workflow of a few nodes with random needs, routes and lists, each command reading every other node."""
    names = [f"n{i}" for i in range(rng.randint(3, 6))]
    routed = {n for n in names if rng.random() < 0.35}
    lines = []
    for name in rng.sample(names, len(names)):
        others = [n for n in names if n != name and n not in routed]  # a node with routes can't be needed
        needs = rng.sample(others, k=min(len(others), rng.choice([0, 1, 1, 2])))
        reads = " ".join(f"{{{{ {n}.stdout | quote }}}}" for n in names if n != name)
        node = f"needs: [{', '.join(needs or ['start'])}], command: \"echo {reads}\""
        if rng.random() < 0.2:
            node += ", for_each: '[0]'"
        if name in routed:
            routes = [f"{{to: {rng.choice([n for n in names if n != name] + ['end'])}, "
                      + rng.choice(["max: 1}", "when: 'true'}"]) for _ in range(rng.choice([1, 2]))]
            node += f", routes: [{', '.join(routes)}, {{to: {rng.choice(names + ['end'])}}}]"
        lines.append(f"  {name}: {{{node}}}")
    return ("version: 1\nname: random\nstart: {input: {}}\n"
            f"follow_up: {{to: [{', '.join(rng.sample(names, rng.choice([1, 2])))}]}}\nnodes:\n" + "\n".join(lines) + "\nend: {output: {}}\n")


def drive(progress: state.Progress, rng: random.Random) -> list[dict]:
    """Run the pass as graph.schedule does: each step records its jobs' results in order. A job fails at
    random, and `wf retry` runs it again before the new jobs. Returns the jobs that started."""
    started = jobs = progress.starts_now(0.0)
    for _ in range(200):  # a loop without max may go on for long
        if not jobs:
            break
        for job in jobs:
            ok = rng.random() < 0.8
            progress.record({"node": job["node"], "pass_no": job["pass_no"], "index": job["index"], "ok": ok,
                             "output": {"stdout": ""} if ok else None, "error": None if ok else "failed", "attempts": 1,
                             "workspace": None, "usage": {}})
        jobs = (progress.retry() if progress.stopped else []) + progress.starts_now(0.0)
        started = started + jobs
    return started


def following_up() -> state.Progress:
    """The state of a run of check.yaml that succeeded, as its first follow-up starts."""
    workflow = spec.load("check.yaml")
    saved = {"run": {"id": "x", "dir": "x", "base": None, "branch": None, "repo": "."}, "status": "succeeded", "failure": None,
             "outputs": {n: {"stdout": ""} for n in ("start", *workflow.nodes)},
             "errors": {}, "taken": {}, "history": {}, "pending": [], "active": {}, "output": {}, "sessions": {}, "passes": {},
             "usage": {"cost_usd": 0.0, "tokens": 0}, "follow_up": {"request": "more", "number": 1, "limits": {}, "pending": True}}
    progress = state.Progress(saved, workflow, scheduler.Topology.of(workflow), lambda event: None)
    progress.follow_up(0.0)
    return progress


def test_a_node_after_an_entry_counts_as_run_before_a_join_that_waits_for_it(repo, capsys):
    write("join.yaml", """
        version: 1
        name: join
        start: {input: {}}
        follow_up: {to: [a, b]}
        nodes:
          a: {needs: [start], command: "echo A{% if follow_up %}{{ follow_up.number }}{% endif %}"}
          b: {needs: [start], command: "echo B"}
          x: {needs: [a], env: {A: "{{ a.stdout | trim }}"}, command: 'echo "x$A"'}
          join: {needs: [x, b], env: {X: "{{ x.stdout | trim }}"}, command: 'echo "$X"'}
        end: {output: {said: {type: string, value: "{{ join.stdout | trim }}"}}}
    """)
    assert [str(p) for p in validate.check("join.yaml").problems] == []
    assert cli.main(["run", "join.yaml"]) == 0
    run = only_run()
    assert cli.main(["continue", run.id, "more"]) == 0
    assert run.output() == {"said": "xA1"}


def test_taking_in_a_follow_up_clears_what_its_nodes_did_and_keeps_the_rest():
    workflow = spec.parse(dedent("""
        version: 1
        name: two
        start: {input: {}}
        follow_up: {to: [a, b]}
        nodes:
          plan: {needs: [start], command: "true"}
          a: {needs: [plan], command: "true"}
          b: {needs: [plan], for_each: "[1, 2]", on_item_error: continue, command: "true"}
          join: {needs: [a, b], command: "true", routes: [{to: a, max: 1}, {to: end}]}
        end: {output: {}}
    """).lstrip())
    saved = {"run": {"id": "x", "dir": "x", "base": None, "branch": None, "repo": "."}, "status": "succeeded", "failure": None,
             "outputs": {"start": {}, "plan": {"exit_code": 0}, "a": {"exit_code": 0}, "b": [{"exit_code": 0}], "join": {}},
             "errors": {"b": [{"index": 1, "item": 2, "error": "exited with code 1"}]}, "taken": {"join:0": 1},
             "results": {"b": [{"index": 0, "item": 1, "output": {"exit_code": 0}, "harness": None, "model": None},
                               {"index": 1, "item": 2, "output": None, "harness": None, "model": None}]},
             "history": {"a": [{}, {}]}, "pending": [], "active": {}, "output": {"done": True},
             "sessions": {"a": {"harness": "codex", "model": None, "id": "s1", "cost_usd": 0.0, "streak": 2},
                          "sub": {"sessions": {"inner": {"harness": "codex", "model": None, "id": "s2", "cost_usd": 0.0, "streak": 2}}}},
             "usage": {"cost_usd": 1.5, "tokens": 300}, "follow_up": {"request": "more", "number": 1, "limits": {}, "pending": True}}
    events = []
    progress = state.Progress(saved, workflow, scheduler.Topology.of(workflow), events.append)
    progress.follow_up(42.0)
    taken = progress.update()
    assert (taken["pending"], set(taken["outputs"]), taken["errors"], taken["results"]) == (["a", "b"], {"start", "plan"}, {}, {})
    assert (taken["taken"], taken["sessions"]["a"]["streak"], taken["history"]) == ({}, 0, {"a": [{}, {}]})
    assert taken["sessions"]["sub"] == {"sessions": {"inner": {**saved["sessions"]["sub"]["sessions"]["inner"], "streak": 0}}}
    assert (taken["status"], taken["output"]) == ("running", None)
    assert taken["follow_up"] == {"request": "more", "number": 1, "limits": {}, "to": ["a", "b"],
                                  "from": {"cost_usd": 1.5, "tokens": 300, "elapsed_s": 42.0}}
    assert events == [{"event": "follow_up", "number": 1, "to": ["a", "b"]}]
    assert progress.vars()["follow_up"] == {"request": "more", "number": 1}


def test_a_job_that_runs_again_before_the_first_step_counts_only_the_follow_ups_time():
    workflow = spec.parse("version: 1\nname: t\nlimits: {duration: 1m}\nstart: {input: {}}\nnodes:\n"
                          "  a: {needs: [start], command: 'true'}\nend: {output: {}}\n")
    ctx = RunContext(workflow, runs.Run("t", Path("."), Path(".")), home={})
    used = {"cost_usd": 0.0, "tokens": 0}
    ctx.resume({"elapsed_s": 125.0, "usage": used, "follow_up": {"from": {**used, "elapsed_s": 120.0}}})
    assert ctx.limit_check() is None  # 5 seconds of the follow-up, not the run's 125
    ctx.resume({"elapsed_s": 125.0, "usage": used})
    assert ctx.limit_check()[0] == "duration"


def test_a_secret_request_stays_out_of_the_trace_and_the_events(repo, capsys):
    write("secret.yaml", """
        version: 1
        name: secret
        start: {input: {}}
        follow_up: {to: use}
        nodes:
          use:
            needs: [start]
            env: {TOKEN: "{{ follow_up.request if follow_up else 'none' }}"}
            secret_env: [TOKEN]
            command: 'test -n "$TOKEN"'
        end: {output: {}}
    """)
    assert cli.main(["run", "secret.yaml"]) == 0
    run = only_run()
    assert cli.main(["continue", run.id, "SECRET-FOLLOWUP-TOKEN"]) == 0
    assert "SECRET-FOLLOWUP-TOKEN" not in capsys.readouterr().out
    for name in ("trace.jsonl", "events.jsonl", "status.json", "run.json"):
        assert "SECRET-FOLLOWUP-TOKEN" not in (run.dir / name).read_text()
    (continued,) = [r for r in trace_view.records(run) if r.get("action") == "continued"]
    assert continued["request"] == "[secret]"


def test_wf_status_says_a_node_that_a_follow_up_skips_is_skipped():
    workflow = spec.parse(dedent("""
        version: 1
        name: skip
        start: {input: {}}
        follow_up: {to: check}
        nodes:
          check: {needs: [start], command: "true", routes: [{when: "check.exit_code != 0", to: fix}, {to: ship}]}
          fix: {command: "true", routes: [{to: ship}]}
          ship: {command: "true"}
        end: {output: {}}
    """).lstrip())
    at = "2026-10-20T10:00:00+00:00"

    def lap(node: str, number: int, to: str | None = None, index: int = 0) -> list[dict]:
        found = [{"time": at, "event": kind, "node": node, "pass": number} for kind in ("node_started", "node_finished")]
        return found + ([{"time": at, "event": "route", "node": node, "route": index, "to": [to], "taken": 1}] if to else [])

    events = [*lap("check", 1, "fix"), *lap("fix", 1, "ship"), *lap("ship", 1),
              {"time": at, "event": "follow_up", "number": 1, "to": ["check"]}, *lap("check", 2, "ship", 1),
              {"time": at, "event": "node_started", "node": "ship", "pass": 2}]
    found = progress.estimate(workflow, events, "running")
    assert {n: i["state"] for n, i in found["nodes"].items()} == {"check": "done", "fix": "skipped", "ship": "running"}


def test_what_earlier_runs_teach_is_learnt_again_once_a_follow_up_adds_to_them(repo, capsys):
    run = ran(capsys)
    workflow = spec.load(run.workflow_file)
    first = progress.learn(workflow, [run])
    assert progress.learn(workflow, [run]) is first  # kept while the run doesn't change
    assert cli.main(["continue", run.id, "more"]) == 0
    assert progress.learn(workflow, [run]) is not first
