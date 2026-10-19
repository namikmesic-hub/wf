"""wf run and wf resume, end to end, on command and human workflows.

Each test runs in a fresh git repo. No agents run, so these are cheap and
deterministic; commands use small files as counters and switches.
"""

import json
import subprocess
from pathlib import Path
from textwrap import dedent

import pytest

from workflow import cli, runner


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


def run_dir(run_id: str) -> Path:
    return Path(".wf") / "runs" / run_id


def status(folder: Path) -> str:
    return json.loads((folder / "status.json").read_text())["status"]


def output(folder: Path) -> dict:
    return json.loads((folder / "output.json").read_text())


def events(folder: Path) -> list[dict]:
    return [json.loads(line) for line in (folder / "events.jsonl").read_text().splitlines()]


def answers(monkeypatch, *lines: str) -> None:
    """Make the run interactive, with these lines typed at the prompts."""
    typed = iter(lines)
    monkeypatch.setattr(runner, "interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": next(typed))


CHAIN = """
version: 1
name: chain
start:
  input:
    who: {type: string}
    times: {type: integer, default: 2}
nodes:
  hello: {needs: [start], command: "printf 'hello %s' {{ start.who | quote }}"}
  shout:
    needs: [hello]
    command: 'printf "%s!" "$TEXT" | tr a-z A-Z'
    env: {TEXT: "{{ hello.stdout }}"}
end:
  output:
    text: "{{ shout.stdout }}"
    times: "{{ start.times }}"
"""


# ---------- a basic run ----------


def test_a_run_produces_its_output_and_its_folder(repo, capsys):
    write("chain.yaml", CHAIN)
    assert cli.main(["run", "chain.yaml", "who=you"]) == 0
    folder = only_run()
    assert output(folder) == {"text": "HELLO YOU!", "times": 2}
    assert status(folder) == "succeeded"
    assert json.loads((folder / "input.json").read_text()) == {"who": "you", "times": 2}
    assert (folder / "workflow" / "chain.yaml").read_text() == dedent(CHAIN)
    assert (folder / "nodes" / "hello" / "1" / "instruction.md").read_text() == "printf 'hello %s' you"
    assert (folder / "nodes" / "hello" / "1" / "attempt-1.log").read_text() == "hello you"
    assert json.loads((folder / "nodes" / "shout" / "1" / "output.json").read_text())["exit_code"] == 0
    assert [e["event"] for e in events(folder)][-1] == "run_finished"
    assert ".wf/" in Path(".git/info/exclude").read_text().splitlines()
    out = capsys.readouterr().out
    assert '"text": "HELLO YOU!"' in out
    assert folder.name in out


def test_inputs_from_pairs_files_and_defaults(repo, capsys):
    write("chain.yaml", CHAIN)
    Path("in.json").write_text('{"who": "file", "times": 5}')
    assert cli.main(["run", "chain.yaml", "--input", "in.json", "times=7"]) == 0
    assert output(only_run()) == {"text": "HELLO FILE!", "times": 7}  # 7 parsed as JSON


def test_missing_and_unknown_inputs_stop_the_run(repo, capsys):
    write("chain.yaml", CHAIN)
    assert cli.main(["run", "chain.yaml"]) == 1
    assert "missing input `who`" in capsys.readouterr().err
    assert cli.main(["run", "chain.yaml", "who=x", "nope=1"]) == 1
    assert "unknown input `nope`" in capsys.readouterr().err
    assert cli.main(["run", "chain.yaml", "who=x", "times=many"]) == 1
    assert "times" in capsys.readouterr().err
    assert not Path(".wf").exists()


def test_missing_inputs_are_asked_at_a_terminal(repo, monkeypatch, capsys):
    write("chain.yaml", CHAIN)
    answers(monkeypatch, "typed")
    assert cli.main(["run", "chain.yaml"]) == 0
    assert output(only_run())["text"] == "HELLO TYPED!"


def test_an_invalid_workflow_does_not_start(repo, capsys):
    write("bad.yaml", "version: 1\nname: bad\nstart: {}\nnodes:\n  a: {command: x}\nend: {}\n")
    assert cli.main(["run", "bad.yaml"]) == 1
    assert "never runs" in capsys.readouterr().out
    assert not Path(".wf").exists()


# ---------- routes, loops, joins ----------


def test_a_loop_runs_until_its_route_says_stop(repo, capsys):
    write("loop.yaml", """
        version: 1
        name: loop
        start: {input: {}}
        nodes:
          bump: {needs: [start], command: "echo x >> count; wc -l < count | tr -d ' '"}
          check:
            needs: [bump]
            command: "test $(cat count | wc -l) -ge 3"
            check: false
            routes:
              - {when: "check.exit_code == 0", to: end}
              - {to: bump, max: 5}
              - {to: end}
        end:
          output:
            passes: {type: integer, value: "{{ history.bump | length }}"}
            last: "{{ bump.stdout }}"
    """)
    assert cli.main(["run", "loop.yaml"]) == 0
    folder = only_run()
    assert output(folder) == {"passes": 3, "last": "3\n"}
    routes = [(e["node"], e["to"], e["taken"]) for e in events(folder) if e["event"] == "route"]
    assert routes == [("check", ["bump"], 1), ("check", ["bump"], 2), ("check", ["end"], 1)]
    assert sorted(p.name for p in (folder / "nodes" / "bump").iterdir()) == ["1", "2", "3"]


def test_max_caps_a_loop(repo, capsys):
    write("loop.yaml", """
        version: 1
        name: loop
        start: {input: {}}
        nodes:
          try: {needs: [start], command: "false", check: false, routes: [{when: "try.exit_code != 0", to: try, max: 2}, {to: end}]}
        end: {output: {tries: {type: integer, value: "{{ history.try | length }}"}}}
    """)
    assert cli.main(["run", "loop.yaml"]) == 0
    assert output(only_run()) == {"tries": 3}


def test_a_skipped_branch_reads_as_none_and_does_not_block_the_join(repo, capsys):
    write("branch.yaml", """
        version: 1
        name: branch
        start: {input: {kind: {enum: [bug, feature]}}}
        nodes:
          classify:
            needs: [start]
            command: "true"
            routes: [{when: "start.kind == 'bug'", to: bugfix}, {to: feature}]
          bugfix: {command: echo fixed}
          feature: {command: echo built}
          report:
            needs: [bugfix, feature]
            command: "echo {{ 'bug' if bugfix else 'feature' }}"
        end:
          output:
            report: "{{ report.stdout }}"
            feature_ran: {type: boolean, value: "{{ feature is not none }}"}
    """)
    assert cli.main(["run", "branch.yaml", "kind=bug"]) == 0
    folder = only_run()
    assert output(folder) == {"report": "bug\n", "feature_ran": False}
    started = [e["node"] for e in events(folder) if e["event"] == "node_started"]
    assert started == ["classify", "bugfix", "report"]


# ---------- for_each ----------

ITEMS = """
version: 1
name: items
start: {input: {}}
nodes:
  each:
    needs: [start]
    for_each: "[1, 2, 3]"
    on_item_error: %s
    command: "echo run >> ran-{{ item }}; test ! -e broken-{{ item }} && echo item-{{ index }}"
end:
  output:
    results: {type: array, value: "{{ each | map(attribute='stdout') | list }}"}
    failed: {type: array, value: "{{ errors.each | map(attribute='index') | list }}"}
"""


def test_for_each_runs_every_item_in_list_order(repo, capsys):
    write("items.yaml", ITEMS % "fail")
    assert cli.main(["run", "items.yaml"]) == 0
    assert output(only_run()) == {"results": ["item-0\n", "item-1\n", "item-2\n"], "failed": []}


def test_items_that_fail_can_be_skipped(repo, capsys):
    write("items.yaml", ITEMS % "continue")
    Path("broken-2").touch()
    assert cli.main(["run", "items.yaml"]) == 0
    assert output(only_run()) == {"results": ["item-0\n", "item-2\n"], "failed": [1]}


def test_a_failed_item_stops_the_run_and_resume_reruns_only_that_item(repo, capsys):
    write("items.yaml", ITEMS % "fail")
    Path("broken-2").touch()
    assert cli.main(["run", "items.yaml"]) == 1
    folder = only_run()
    assert status(folder) == "failed"
    assert "item 1: exited with code 1" in capsys.readouterr().err

    Path("broken-2").unlink()
    assert cli.main(["resume", folder.name]) == 0
    assert status(folder) == "succeeded"
    assert output(folder)["results"] == ["item-0\n", "item-1\n", "item-2\n"]
    assert [Path(f"ran-{i}").read_text().count("run") for i in (1, 2, 3)] == [1, 2, 1]
    assert (folder / "nodes" / "each" / "1" / "1" / "attempt-2.log").exists()  # attempt numbers continue


def test_an_empty_list_finishes_at_once(repo, capsys):
    write("empty.yaml", """
        version: 1
        name: empty
        start: {input: {}}
        nodes:
          each: {needs: [start], for_each: "[]", command: "exit 1"}
          after: {needs: [each], command: "echo {{ each | length }}"}
        end: {output: {n: "{{ after.stdout }}"}}
    """)
    assert cli.main(["run", "empty.yaml"]) == 0
    assert output(only_run()) == {"n": "0\n"}


# ---------- errors, retries, timeouts, limits ----------


def test_retry_gives_another_attempt(repo, capsys):
    write("retry.yaml", """
        version: 1
        name: retry
        start: {input: {}}
        nodes:
          flaky: {needs: [start], retry: 1, command: "if [ -e once ]; then echo ok; else touch once; exit 3; fi"}
        end: {output: {out: "{{ flaky.stdout }}"}}
    """)
    assert cli.main(["run", "retry.yaml"]) == 0
    folder = only_run()
    assert output(folder) == {"out": "ok\n"}
    assert sorted(p.name for p in (folder / "nodes" / "flaky" / "1").glob("attempt-*")) == ["attempt-1.log", "attempt-2.log"]


def test_a_timeout_fails_the_attempt(repo, capsys):
    write("slow.yaml", """
        version: 1
        name: slow
        start: {input: {}}
        nodes:
          slow: {needs: [start], timeout: 1s, command: "sleep 30"}
        end: {output: {}}
    """)
    assert cli.main(["run", "slow.yaml"]) == 1
    assert "timed out after 1s" in capsys.readouterr().err


def test_a_duration_limit_ends_the_run_with_status_limit(repo, capsys):
    write("limit.yaml", """
        version: 1
        name: limit
        limits: {duration: 1s}
        start: {input: {}}
        nodes:
          first: {needs: [start], command: "sleep 1.2"}
          second: {needs: [first], command: "touch second-ran"}
        end: {output: {}}
    """)
    assert cli.main(["run", "limit.yaml"]) == 1
    assert status(only_run()) == "limit"
    assert not Path("second-ran").exists()


def test_an_end_output_that_breaks_its_schema_fails_the_run(repo, capsys):
    write("end.yaml", """
        version: 1
        name: end
        start: {input: {}}
        nodes:
          a: {needs: [start], command: echo hi}
        end: {output: {n: {type: integer, value: "{{ a.stdout }}"}}}
    """)
    assert cli.main(["run", "end.yaml"]) == 1
    assert status(only_run()) == "failed"
    assert "n: 'hi\\n' is not of type 'integer'" in capsys.readouterr().err


# ---------- workspaces ----------


def test_temp_workspaces_are_outside_the_repo_and_shared_by_name(repo, capsys):
    write("temp.yaml", """
        version: 1
        name: temp
        start: {input: {}}
        nodes:
          make: {needs: [start], workspace: temp, command: "touch made; pwd"}
          look: {needs: [make], workspace: make, command: "ls"}
        end:
          output:
            where: "{{ make.stdout }}"
            seen: "{{ look.stdout }}"
    """)
    assert cli.main(["run", "temp.yaml"]) == 0
    result = output(only_run())
    assert result["seen"] == "made\n"
    assert not result["where"].startswith(str(repo))
    assert not Path(result["where"].strip()).exists()  # removed once the run succeeded


# ---------- human nodes ----------

ASK = """
version: 1
name: ask
start: {input: {}}
nodes:
  plan: {needs: [start], command: "echo the plan"}
  approve:
    needs: [plan]
    human: "Go ahead with {{ plan.stdout | trim }}?"
    output:
      ok: {type: boolean}
      size: {enum: [small, large]}
      note: {type: string, optional: true}
    routes: [{when: approve.ok, to: build}, {to: end}]
  build: {command: "echo building {{ approve.size | quote }}"}
end:
  output:
    built: {type: string, value: "{{ build.stdout if build else 'nothing' }}"}
"""


def test_a_human_node_asks_at_a_terminal(repo, monkeypatch, capsys):
    write("ask.yaml", ASK)
    answers(monkeypatch, "maybe", "yes", "2", "")
    assert cli.main(["run", "ask.yaml"]) == 0
    assert output(only_run()) == {"built": "building large\n"}
    out = capsys.readouterr().out
    assert "Go ahead with the plan?" in out
    assert "answer yes or no" in out


def test_without_a_terminal_a_human_node_pauses_and_resume_asks_later(repo, monkeypatch, capsys):
    write("ask.yaml", ASK)
    assert cli.main(["run", "ask.yaml"]) == 2
    folder = only_run()
    assert status(folder) == "waiting"
    assert f"wf resume {folder.name}" in capsys.readouterr().out

    assert cli.main(["resume", folder.name]) == 2  # still no terminal: still waiting

    answers(monkeypatch, "no", "1", "not now")
    assert cli.main(["resume", folder.name]) == 0
    assert output(folder) == {"built": "nothing"}
    assert json.loads((folder / "nodes" / "approve" / "1" / "output.json").read_text()) == {
        "ok": False, "size": "small", "note": "not now",
    }


def test_no_input_pauses_even_at_a_terminal(repo, monkeypatch, capsys):
    write("ask.yaml", ASK)
    answers(monkeypatch)
    assert cli.main(["run", "ask.yaml", "--no-input"]) == 2


NOTE = """
version: 1
name: note
start: {input: {}}
nodes:
  approve:
    needs: [start]
    human: Ship it?
    output: {ok: {type: boolean}, note: {type: string, optional: true}}
  after: {needs: [approve], command: "echo {{ (approve.note or 'no note') | quote }}"}
end:
  output:
    note: "{{ approve.note }}"
    said: "{{ after.stdout }}"
"""


def test_an_optional_field_left_out_reads_as_none_later_and_in_the_end_output(repo, monkeypatch):
    write("note.yaml", NOTE)
    answers(monkeypatch, "yes", "")  # no note
    assert cli.main(["run", "note.yaml"]) == 0
    folder = only_run()
    assert output(folder) == {"note": None, "said": "no note\n"}
    assert json.loads((folder / "nodes" / "approve" / "1" / "output.json").read_text()) == {"ok": True, "note": None}


@pytest.mark.parametrize("required, error", [("[ok]", None), ("[ok, note]", "answer: note: None is not of type 'string'")])
def test_an_end_output_schema_that_requires_a_left_out_field_still_fails(repo, monkeypatch, capsys, required, error):
    write("note.yaml", NOTE.replace("    said: \"{{ after.stdout }}\"\n", f"""    answer:
      type: object
      properties: {{ok: {{type: boolean}}, note: {{type: string}}}}
      required: {required}
      additionalProperties: false
      value: "{{{{ approve }}}}"
"""))
    answers(monkeypatch, "yes", "")
    assert cli.main(["run", "note.yaml"]) == (1 if error else 0)
    if error:
        assert error in capsys.readouterr().err
    else:
        assert output(only_run()) == {"note": None, "answer": {"ok": True, "note": None}}


# ---------- resume ----------


def test_resume_of_a_finished_run_says_so(repo, capsys):
    write("chain.yaml", CHAIN)
    cli.main(["run", "chain.yaml", "who=a"])
    capsys.readouterr()
    assert cli.main(["resume", only_run().name]) == 0
    assert "already succeeded" in capsys.readouterr().out


def test_resume_of_an_unknown_run(repo, capsys):
    assert cli.main(["resume", "nope"]) == 1
    assert "no run `nope`" in capsys.readouterr().err


def test_resume_uses_the_copied_workflow(repo, capsys):
    write("items.yaml", ITEMS % "fail")
    Path("broken-1").touch()
    assert cli.main(["run", "items.yaml"]) == 1
    Path("items.yaml").write_text("garbage: [")
    Path("broken-1").unlink()
    assert cli.main(["resume", only_run().name]) == 0


# ---------- stopping at a prompt, and limits on resume ----------


@pytest.mark.parametrize("error, code", [(EOFError, 2), (KeyboardInterrupt, 130)])
def test_no_answer_at_a_prompt_leaves_the_run_waiting(repo, monkeypatch, capsys, error, code):
    write("ask.yaml", ASK)
    monkeypatch.setattr(runner, "interactive", lambda: True)

    def no_answer(prompt=""):
        raise error

    monkeypatch.setattr("builtins.input", no_answer)
    assert cli.main(["run", "ask.yaml"]) == code
    folder = only_run()
    assert status(folder) == "waiting"
    assert f"wf resume {folder.name}" in capsys.readouterr().out


def test_resume_does_not_retry_past_a_limit(repo, capsys):
    write("limit.yaml", """
        version: 1
        name: limit
        limits: {duration: 1s}
        start: {input: {}}
        nodes:
          first: {needs: [start], command: "sleep 1.2; echo x >> ran; exit 1"}
        end: {output: {}}
    """)
    assert cli.main(["run", "limit.yaml"]) == 1
    folder = only_run()
    assert status(folder) == "failed"
    assert cli.main(["resume", folder.name]) == 1
    assert status(folder) == "limit"
    assert Path("ran").read_text() == "x\n"  # not run again
    assert cli.main(["resume", folder.name, "--limit", "duration=1h"]) == 1  # a bigger budget: it retries
    assert Path("ran").read_text() == "x\nx\n"
    assert status(folder) == "failed"


def test_items_that_queued_for_max_parallel_run_again_after_a_question(repo, monkeypatch, capsys):
    # The items wait for each other (max_parallel 1), then a question, then the same node again
    write("again.yaml", """
        version: 1
        name: again
        start: {input: {}}
        nodes:
          each: {needs: [start], for_each: "[1, 2, 3]", max_parallel: 1, command: "sleep 0.1; echo {{ item }}"}
          ask:
            needs: [each]
            human: Again?
            output: {again: {type: boolean}}
            routes: [{when: ask.again, to: each, max: 1}, {to: end}]
        end:
          output:
            passes: {type: integer, value: "{{ history.each | length }}"}
    """)
    answers(monkeypatch, "yes", "no")
    assert cli.main(["run", "again.yaml"]) == 0
    assert output(only_run()) == {"passes": 2}


def test_a_crash_inside_the_graph_is_recorded_not_left_running(repo, monkeypatch, capsys):
    monkeypatch.setattr(runner, "RECURSION_LIMIT", 3)
    write("loop.yaml", """
        version: 1
        name: loop
        start: {input: {}}
        nodes:
          a: {needs: [start], command: "true"}
          b: {needs: [a], command: "true"}
          c: {needs: [b], command: "true"}
        end: {output: {}}
    """)
    assert cli.main(["run", "loop.yaml"]) == 1
    assert status(only_run()) == "failed"
    assert "GraphRecursionError" in capsys.readouterr().err


@pytest.mark.parametrize("bad", ["../../etc", "a/b", ".."])
def test_resume_refuses_run_ids_that_are_paths(repo, capsys, bad):
    assert cli.main(["resume", bad]) == 1
    assert "isn't a run id" in capsys.readouterr().err


def test_a_routes_when_sees_the_pass_that_just_finished_in_history(repo, capsys):
    write("count.yaml", """
        version: 1
        name: count
        start: {input: {}}
        nodes:
          bump:
            needs: [start]
            command: "echo x"
            routes: [{when: "history.bump | length < 3", to: bump, max: 10}, {to: end}]
        end: {output: {passes: {type: integer, value: "{{ history.bump | length }}"}}}
    """)
    assert cli.main(["run", "count.yaml"]) == 0
    assert json.loads((only_run() / "output.json").read_text()) == {"passes": 3}


def test_an_optional_input_with_a_default_gets_its_default(repo):
    write("opt.yaml", """
        version: 1
        name: opt
        start: {input: {mode: {type: string, optional: true, default: fast}}}
        nodes:
          say: {needs: [start], command: "echo {{ start.mode }}"}
        end: {output: {said: "{{ say.stdout }}"}}
    """)
    assert cli.main(["run", "opt.yaml"]) == 0
    assert json.loads((only_run() / "output.json").read_text()) == {"said": "fast\n"}


def test_an_optional_input_left_out_reads_as_none(repo):
    write("opt.yaml", """
        version: 1
        name: opt
        start: {input: {mode: {type: string, optional: true}}}
        nodes:
          say: {needs: [start], command: "echo {{ start.mode or 'normal' }}"}
        end: {output: {said: "{{ say.stdout }}", mode: "{{ start.mode }}"}}
    """)
    assert cli.main(["run", "opt.yaml"]) == 0
    assert output(only_run()) == {"said": "normal\n", "mode": None}
    assert json.loads((only_run() / "input.json").read_text()) == {}  # the input as given
