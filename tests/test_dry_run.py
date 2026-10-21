"""Dry runs: a workflow driven by a script of agent results, answers and command results."""

import json
import shutil
import subprocess
from pathlib import Path
from textwrap import dedent

import pytest
import yaml

from workflow import cli, harnesses, runner

EXAMPLES = Path(__file__).parent.parent / "workflow" / "examples"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(runner, "interactive", lambda: False)
    for name in list(harnesses.FACTORIES):  # a dry run must never reach a real agent
        monkeypatch.setitem(harnesses.FACTORIES, name, lambda: pytest.fail("a dry run called a real harness"))
    git = lambda *a: subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *a], check=True, capture_output=True)
    git("init", "-q")
    Path("README").write_text("hi\n")
    git("add", "README")
    git("commit", "-qm", "first")
    for name in ("implement.yaml", "feature.yaml"):
        shutil.copy(EXAMPLES / name, name)
    return tmp_path


def script(data: dict) -> str:
    Path("script.yaml").write_text(yaml.safe_dump(data))
    return "script.yaml"


def only_run() -> Path:
    (folder,) = (Path(".wf") / "runs").iterdir()
    return folder


def output() -> dict:
    return json.loads((only_run() / "output.json").read_text())


LOOP = {
    "agents": {
        "explore": [{"plan": "add new.txt", "files": ["new.txt"]}],
        "implement": [{"_files": {"new.txt": "made"}, "summary": "first try", "notes": "wrote it"},
                      {"_files": {"new.txt": "made\n"}, "summary": "added the newline", "notes": "fixed"}],
        "review": [{"blocking_issues": ["no trailing newline"]}, {"blocking_issues": []}],
    },
    "commands": {"tests": [{"exit_code": 0, "stdout": "ok"}, {"exit_code": 0, "stdout": "ok"}]},
}


def test_a_dry_run_walks_the_review_loop_without_an_agent(repo, capsys):
    assert cli.main(["run", "implement.yaml", "task=Add new.txt", "--dry-run", script(LOOP)]) == 0
    run_id = only_run().name
    assert output() == {"branch": f"wf/{run_id}/implement", "summary": "added the newline", "passed": True}
    second = (only_run() / "nodes" / "implement" / "2" / "instruction.md").read_text()
    assert "- no trailing newline" in second  # the reviewer's feedback reached the second pass
    assert "dry run" in capsys.readouterr().out.lower()
    branches = subprocess.run(["git", "branch", "--list", "wf/*"], capture_output=True, text=True).stdout
    assert branches == ""  # a dry run that succeeds cleans up its branches too
    assert json.loads((only_run() / "run.json").read_text())["dry_run"] is True


def test_scripted_results_are_checked_before_the_run_starts(repo, capsys):
    bad = {"agents": {"review": [{"blocking_issue": []}], "nope": [{}]}}
    assert cli.main(["run", "implement.yaml", "task=x", "--dry-run", script(bad)]) == 1
    err = capsys.readouterr().err
    assert "agents.review[0]: 'blocking_issues' is a required property" in err
    assert "agents.nope: no node `nope`" in err
    assert not Path(".wf").exists()


def test_a_script_that_runs_out_fails_the_node_clearly(repo, capsys):
    short = {**LOOP, "agents": {**LOOP["agents"], "review": [{"blocking_issues": ["still wrong"]}]}}
    assert cli.main(["run", "implement.yaml", "task=x", "--dry-run", script(short)]) == 1
    assert "the script has no result left for review" in capsys.readouterr().err


def test_failures_retries_and_fallbacks_can_be_scripted(repo, capsys):
    write = Path("flow.yaml")
    write.write_text(dedent("""
        version: 1
        name: flaky
        defaults: {harness: [claude-code, codex], model: {claude-code: opus, codex: gpt}}
        start: {input: {}}
        nodes:
          work: {needs: [start], instruction: do it}
        end: {output: {summary: "{{ work.summary }}"}}
    """))
    steps = {"agents": {"work": [{"_fail": "crashed"}, {"_unavailable": "usage limit"}, {"summary": "on codex"}]}}
    assert cli.main(["run", "flow.yaml", "--dry-run", script(steps)]) == 0
    assert output() == {"summary": "on codex"}
    out = capsys.readouterr().out
    assert "attempt 1 failed: dry run: crashed" in out
    assert "claude-code is unavailable: dry run: usage limit; trying codex" in out


def test_questions_are_answered_from_the_script(repo, capsys):
    steps = {"agents": {"plan": [{"tasks": []}]}, "humans": {"approve": [{"approved": True}]}}
    assert cli.main(["run", "feature.yaml", "feature=nothing", "--dry-run", script(steps)]) == 0
    assert output() == {"pr_url": None}
    assert "approve: answered from the script" in capsys.readouterr().out


def test_scripted_spending_triggers_limits(repo):
    Path("flow.yaml").write_text(dedent("""
        version: 1
        name: costly
        defaults: {harness: claude-code}
        limits: {tokens: 1000}
        start: {input: {}}
        nodes:
          a: {needs: [start], instruction: x}
          b: {needs: [a], instruction: y}
        end: {output: {}}
    """))
    steps = {"agents": {"a": [{"summary": "big", "_tokens": 5000}], "b": [{"summary": "never"}]}}
    assert cli.main(["run", "flow.yaml", "--dry-run", script(steps)]) == 1
    assert json.loads((only_run() / "status.json").read_text())["status"] == "limit"


def test_a_paused_dry_run_resumes_with_the_rest_of_its_script(repo, capsys):
    steps = {"agents": {"plan": [{"tasks": [{"title": "A", "details": "a"}]}],
                        "explore": [{"plan": "p", "files": []}],
                        "implement": [{"summary": "did A", "notes": "n"}],
                        "review": [{"blocking_issues": []}],
                        "merge": [{"summary": "merged"}]},
             "commands": {"tests": [{"exit_code": 0, "stdout": "ok"}], "open_pr": [{"exit_code": 0, "stdout": "https://pr/1\n"}]}}
    assert cli.main(["run", "feature.yaml", "feature=one thing", "--dry-run", script(steps)]) == 2  # approve isn't scripted
    capsys.readouterr()
    cli.main(["status", only_run().name, "--json"])
    key = json.loads(capsys.readouterr().out)["questions"][0]["key"]
    Path("answers.json").write_text(json.dumps({key: {"approved": True}}))
    assert cli.main(["answer", only_run().name, "answers.json"]) == 0
    assert output() == {"pr_url": "https://pr/1"}


def test_a_node_whose_script_ran_out_fails_without_a_retry(repo, capsys):
    short = {**LOOP, "agents": {**LOOP["agents"], "review": [{"blocking_issues": ["still wrong"]}]}}
    assert cli.main(["run", "implement.yaml", "task=x", "--dry-run", script(short)]) == 1
    out = capsys.readouterr().out
    assert "review: attempt 2" not in out  # another attempt would find no entry either


def test_a_listed_command_that_runs_out_fails_instead_of_really_running(repo, capsys):
    steps = {**LOOP, "commands": {"tests": [{"exit_code": 1, "stdout": "1 failed"}]}}  # the loop needs a second one
    Path("implement.yaml").write_text(Path("implement.yaml").read_text().replace(
        "{{ start.test_command }}", "touch really-ran; {{ start.test_command }}"))
    assert cli.main(["run", "implement.yaml", "task=x", "--dry-run", script(steps)]) == 1
    assert "the script has no result left for tests" in capsys.readouterr().err
    assert not list(Path.home().glob(".wf/worktrees/*/implement/really-ran"))


def test_a_dry_run_says_which_commands_really_run(repo, capsys):
    steps = {"agents": LOOP["agents"]}  # no commands: tests really runs
    cli.main(["run", "implement.yaml", "task=x", "test_command=true", "--dry-run", script(steps)])
    assert "these commands aren't in the script, so they really run if they're reached: tests" in capsys.readouterr().out


def test_discard_deletes_a_failed_dry_run_with_its_worktree_and_branches(repo, capsys):
    short = {**LOOP, "agents": {**LOOP["agents"], "review": [{"blocking_issues": ["x"]}]}}
    assert cli.main(["run", "implement.yaml", "task=x", "--dry-run", script(short)]) == 1
    run_id = only_run().name
    assert subprocess.run(["git", "branch", "--list", "wf/*"], capture_output=True, text=True).stdout
    capsys.readouterr()
    assert cli.main(["discard", run_id]) == 0
    assert capsys.readouterr().out.strip() == f"Discarded run {run_id}"
    assert not list((Path(".wf") / "runs").iterdir())
    assert subprocess.run(["git", "branch", "--list", "wf/*"], capture_output=True, text=True).stdout == ""
    assert "implement" not in subprocess.run(["git", "worktree", "list"], capture_output=True, text=True).stdout


def test_uncommitted_changes_are_noted_when_new_worktrees_will_miss_them(repo, capsys):
    Path("repro.sh").write_text("exit 1\n")  # not committed: a `workspace: new` node won't see it
    cli.main(["run", "implement.yaml", "task=x", "--dry-run", script(LOOP)])
    (note,) = [line for line in capsys.readouterr().err.splitlines() if line.startswith("Note:")]
    assert "repro.sh" in note and "won't be in the `workspace: new` worktrees" in note
    assert "implement.yaml" not in note and "script.yaml" not in note  # wf reads those itself


EACH = """
version: 1
name: each
start: {input: {}}
nodes:
  each: {needs: [start], for_each: '["a", "b", "c"]', command: "touch ran-{{ index }}"}
end: {output: {}}
"""


def test_a_command_listed_for_some_items_fails_for_the_others_instead_of_really_running(repo, capsys):
    Path("each.yaml").write_text(dedent(EACH))
    steps = {"commands": {"each[0]": [{"exit_code": 0}], "each[1]": [{"exit_code": 0}]}}
    assert cli.main(["run", "each.yaml", "--dry-run", script(steps)]) == 1
    out = capsys.readouterr()
    assert "really run" not in out.out
    assert "the script has no entry for each[2]" in out.err
    assert not list(Path().glob("ran-*"))


def test_the_commands_that_really_run_are_named_with_their_workflow(repo, capsys):
    steps = {"agents": {"plan": [{"tasks": []}]}, "humans": {"approve": [{"approved": False}]}}
    cli.main(["run", "feature.yaml", "feature=x", "--dry-run", script(steps)])
    assert "so they really run if they're reached: open_pr, tests (in implement)" in capsys.readouterr().out


def test_a_script_can_carry_the_inputs_of_its_path(repo):
    steps = {**LOOP, "inputs": {"task": "Task from the script"}}
    assert cli.main(["run", "implement.yaml", "--dry-run", script(steps)]) == 0
    assert "Task from the script" in (only_run() / "nodes" / "explore" / "1" / "instruction.md").read_text()
    shutil.rmtree(".wf/runs")
    assert cli.main(["run", "implement.yaml", "task=From the command line", "--dry-run", script(steps)]) == 0
    assert "From the command line" in (only_run() / "nodes" / "explore" / "1" / "instruction.md").read_text()


def test_each_items_log_shows_the_harness_model_and_effort_its_templates_rendered(repo):
    Path("seats.yaml").write_text(dedent("""
        version: 1
        name: seats
        types:
          seat:
            harness: {enum: [claude-code, codex]}
            model: {type: string}
            effort: {type: string}
        start: {input: {seats: {type: array, maxItems: 3, items: {$ref: seat}}}}
        nodes:
          review:
            needs: [start]
            for_each: start.seats
            harness: "{{ item.harness }}"
            model: "{{ item.model }}"
            effort: "{{ item.effort }}"
            instruction: review
        end: {output: {}}
    """))
    seats = [{"harness": "claude-code", "model": "claude-opus-5-5", "effort": "high"},
             {"harness": "codex", "model": "gpt-6", "effort": "low"}]
    steps = {"inputs": {"seats": seats}, "agents": {"review": [{"summary": "a"}, {"summary": "b"}]}}
    assert cli.main(["run", "seats.yaml", "--dry-run", script(steps)]) == 0
    for index, seat in enumerate(seats):
        log = only_run() / "nodes" / "review" / "1" / str(index) / "attempt-1.log"
        (session,) = [line for line in map(json.loads, log.read_text().splitlines()) if line["kind"] == "dry_run"]
        assert {key: session[key] for key in seat} == seat


def test_results_numbers_each_seat_by_its_position_with_the_harness_and_model_it_ran_on(repo):
    Path("seats.yaml").write_text(dedent("""
        version: 1
        name: seats
        defaults: {harness: claude-code, harnesses: {claude-code: {model: claude-opus-5-5}, codex: {model: gpt-6}}}
        start: {input: {}}
        nodes:
          check:
            needs: [start]
            for_each: '["claude-code", "codex", "claude-code"]'
            harness: "{{ item }}"
            retry: 0
            on_item_error: continue
            instruction: check it
            output: {problems: {type: array, items: {type: string}}}
          tally:
            needs: [check]
            instruction: |
              {% for seat in results.check %}
              {{ seat.index }} on {{ seat.harness }} {{ seat.model }}: {{ seat.output.problems | join(', ') if seat.output else 'failed' }}
              {% endfor %}
        end: {output: {checks: {type: integer, value: "{{ check | length }}"}}}
    """))
    steps = {"agents": {"check[0]": [{"problems": ["a"]}], "check[1]": [{"_fail": "crashed"}],
                        "check[2]": [{"problems": ["c", "d"]}], "tally": [{"summary": "done"}]}}
    assert cli.main(["run", "seats.yaml", "--dry-run", script(steps)]) == 0
    assert output() == {"checks": 2}  # check lists only the seats that succeeded
    asked = (only_run() / "nodes" / "tally" / "1" / "instruction.md").read_text()
    assert asked.split("\n\n")[0] == ("0 on claude-code claude-opus-5-5: a\n"
                                      "1 on codex gpt-6: failed\n"  # the harness and model of the attempt that failed
                                      "2 on claude-code claude-opus-5-5: c, d")


def test_results_names_the_last_attempt_of_each_item_after_a_fallback_and_lists_only_the_latest_pass(repo):
    Path("seats.yaml").write_text(dedent("""
        version: 1
        name: seats
        defaults: {harness: [claude-code, codex], model: {claude-code: claude-opus-5-5, codex: gpt-6}}
        start: {input: {}}
        nodes:
          check:
            needs: [start]
            for_each: '["a", "b", "c"]'
            retry: 0
            on_item_error: continue
            instruction: check it
            routes:
              - {when: "history.check | length < 2", to: check, max: 1}
              - {to: end}
        end:
          output:
            harnesses: {type: array, value: "{{ results.check | map(attribute='harness') | list }}"}
            models: {type: array, value: "{{ results.check | map(attribute='model') | list }}"}
            outputs: {type: array, value: "{{ results.check | map(attribute='output') | list }}"}
    """))
    steps = {"agents": {"check[0]": [{"summary": "a1"}, {"summary": "a2"}],
                        "check[1]": [{"summary": "b1"}, {"_unavailable": "usage limit"}, {"_fail": "crashed"}],
                        "check[2]": [{"_unavailable": "usage limit"}, {"summary": "c1"},
                                     {"_unavailable": "usage limit"}, {"summary": "c2"}]}}
    assert cli.main(["run", "seats.yaml", "--dry-run", script(steps)]) == 0
    assert output() == {"harnesses": ["claude-code", "codex", "codex"],  # not the first attempt's claude-code
                        "models": ["claude-opus-5-5", "gpt-6", "gpt-6"],
                        "outputs": [{"summary": "a2"}, None, {"summary": "c2"}]}  # the second pass only


def test_a_fallback_after_failed_attempts_is_shown(repo, capsys):
    Path("flow.yaml").write_text(dedent("""
        version: 1
        name: flaky
        defaults: {harness: [claude-code, codex], model: {claude-code: opus, codex: gpt}}
        start: {input: {}}
        nodes:
          work: {needs: [start], instruction: do it}
        end: {output: {summary: "{{ work.summary }}"}}
    """))
    steps = {"agents": {"work": [{"_fail": "one"}, {"_fail": "two"}, {"summary": "on codex"}]}}
    assert cli.main(["run", "flow.yaml", "--dry-run", script(steps)]) == 0
    assert "↻ work: attempt 3, falling back to codex" in capsys.readouterr().out


def test_a_limit_reached_in_a_sub_workflow_is_reported_once(repo, capsys):
    Path("inner.yaml").write_text(dedent("""
        version: 1
        name: inner
        defaults: {harness: claude-code}
        start: {input: {}}
        nodes:
          big: {needs: [start], instruction: x}
        end: {output: {}}
    """))
    Path("outer.yaml").write_text(dedent("""
        version: 1
        name: outer
        limits: {tokens: 1000}
        start: {input: {}}
        nodes:
          sub: {needs: [start], workflow: inner.yaml}
        end: {output: {}}
    """))
    steps = {"agents": {"big": [{"summary": "s", "_tokens": 5000}]}}
    assert cli.main(["run", "outer.yaml", "--dry-run", script(steps)]) == 1
    assert capsys.readouterr().out.count("limit reached:") == 1


def test_discard_by_workflow_can_keep_real_runs(repo, capsys):
    Path("each.yaml").write_text(dedent(EACH))
    assert cli.main(["run", "each.yaml"]) == 0
    real = only_run().name
    assert cli.main(["run", "each.yaml", "--dry-run", script({"commands": {"each": [{"exit_code": 0}] * 3}})]) == 0
    assert cli.main(["discard", "--workflow", "each", "--dry-runs"]) == 0
    assert only_run().name == real


def test_script_entries_are_checked_for_their_kind(repo, capsys):
    bad = {"agents": {"review": [{"blocking_issues": [], "_tokens": "lots"}]},
           "humans": {"approve": [{"approved": True, "_tokens": 10}]},
           "commands": {"each[0]": [{"exit_code": 0}]}}
    Path("each.yaml").write_text(dedent(EACH))
    assert cli.main(["run", "feature.yaml", "feature=x", "--dry-run", script(bad)]) == 1
    err = capsys.readouterr().err
    assert "agents.review[0]: `_tokens` must be a number" in err
    assert "humans.approve[0]: `_tokens` isn't a dry-run key (only agent entries take `_` keys)" in err
    assert "commands.each[0]: no node `each`" in err  # an indexed key names its node without the index


def test_a_failed_attempt_spends_what_the_script_says(repo):
    Path("flow.yaml").write_text(dedent("""
        version: 1
        name: costly
        defaults: {harness: claude-code}
        limits: {tokens: 1000}
        start: {input: {}}
        nodes:
          a: {needs: [start], instruction: x, retry: 2}
        end: {output: {}}
    """))
    steps = {"agents": {"a": [{"_fail": "crashed", "_tokens": 5000}, {"summary": "never"}]}}
    assert cli.main(["run", "flow.yaml", "--dry-run", script(steps)]) == 1
    assert json.loads((only_run() / "status.json").read_text())["status"] == "limit"


ASK_EACH = """
version: 1
name: ask-each
start: {input: {}}
nodes:
  ask: {needs: [start], for_each: '["a", "b"]', human: "Ok {{ item }}?", output: {ok: {type: boolean}}}
end: {output: {oks: {type: array, value: "{{ ask | map(attribute='ok') | list }}"}}}
"""


def test_questions_sharing_a_key_wait_for_a_person_if_the_script_is_short(repo, capsys):
    Path("ask.yaml").write_text(dedent(ASK_EACH))
    assert cli.main(["run", "ask.yaml", "--dry-run", script({"humans": {"ask": [{"ok": True}]}})]) == 2
    assert json.loads((only_run() / "status.json").read_text())["status"] == "waiting"


def test_a_retried_dry_run_answers_from_its_script(repo, monkeypatch):
    Path("ask.yaml").write_text(dedent(ASK_EACH))
    real = runner._scripted_answers
    calls = []

    def crash_once(interrupts, script):  # the process dies just as the questions come in
        calls.append(1)
        if len(calls) == 1:
            raise KeyboardInterrupt
        return real(interrupts, script)

    monkeypatch.setattr(runner, "_scripted_answers", crash_once)
    assert cli.main(["run", "ask.yaml", "--dry-run", script({"humans": {"ask": [{"ok": True}, {"ok": False}]}})]) == 130
    assert cli.main(["retry", only_run().name, "--no-input"]) == 0
    assert output() == {"oks": [True, False]}


def test_discarding_the_same_run_twice_on_one_line(repo, capsys):
    Path("each.yaml").write_text(dedent(EACH))
    assert cli.main(["run", "each.yaml"]) == 0
    run_id = only_run().name
    assert cli.main(["discard", run_id, run_id]) == 0
    assert not list((Path(".wf") / "runs").iterdir())


def test_uncommitted_changes_are_noted_for_a_default_new_workspace(repo, capsys):
    Path("flow.yaml").write_text(dedent("""
        version: 1
        name: flow
        defaults: {harness: claude-code, workspace: new, access: edit}
        start: {input: {}}
        nodes:
          a: {needs: [start], instruction: x}
        end: {output: {}}
    """))
    Path("notes.txt").write_text("not committed\n")
    cli.main(["run", "flow.yaml", "--dry-run", script({"agents": {"a": [{"summary": "s"}]}})])
    assert "notes.txt" in capsys.readouterr().err


def test_questions_from_items_are_named_by_item_and_scripts_can_key_them(repo, capsys):
    Path("ask.yaml").write_text(dedent(ASK_EACH))
    assert cli.main(["run", "ask.yaml", "--dry-run", script({"humans": {"ask[1]": [{"ok": False}]}})]) == 2
    capsys.readouterr()
    cli.main(["status", only_run().name, "--json"])
    assert [q["node"] for q in json.loads(capsys.readouterr().out)["questions"]] == ["ask[0]", "ask[1]"]
    shutil.rmtree(".wf/runs")
    steps = {"humans": {"ask[0]": [{"ok": True}], "ask[1]": [{"ok": False}]}}
    assert cli.main(["run", "ask.yaml", "--dry-run", script(steps)]) == 0
    assert output() == {"oks": [True, False]}


def test_retrying_a_dry_run_whose_script_is_gone_says_so(repo, capsys):
    Path("ask.yaml").write_text(dedent(ASK_EACH))
    cli.main(["run", "ask.yaml", "--dry-run", script({"humans": {"ask": [{"ok": True}]}})])
    shutil.rmtree(only_run() / "dry-run")
    capsys.readouterr()
    assert cli.main(["retry", only_run().name]) == 1
    assert "can't read the dry run's script" in capsys.readouterr().err


def test_a_sub_workflows_spending_is_counted_once_in_the_report(repo, capsys):
    Path("inner.yaml").write_text(dedent("""
        version: 1
        name: inner
        defaults: {harness: claude-code}
        start: {input: {}}
        nodes:
          x: {needs: [start], instruction: x}
        end: {output: {}}
    """))
    Path("outer.yaml").write_text(dedent("""
        version: 1
        name: outer
        start: {input: {}}
        nodes:
          sub: {needs: [start], workflow: inner.yaml}
        end: {output: {}}
    """))
    assert cli.main(["run", "outer.yaml", "--dry-run", script({"agents": {"x": [{"summary": "s", "_tokens": 100}]}})]) == 0
    capsys.readouterr()
    assert cli.main(["report", "--workflow", "outer", "--json"]) == 0
    found = json.loads(capsys.readouterr().out)
    assert found["runs"][0]["tokens"] == 100
    assert (found["nodes"]["sub"]["tokens"], found["nodes"]["sub/x"]["tokens"]) == (100, 100)
    assert not any(note.startswith("sub ") for note in found["notes"])  # sub/x says where it went


def test_the_uncommitted_note_names_files_right_from_a_subfolder(repo, capsys, monkeypatch):
    Path("README").write_text("changed\n")  # tracked and modified: " M README", the first line git prints
    Path("sub").mkdir()
    shutil.copy("implement.yaml", "sub/implement.yaml")
    monkeypatch.chdir("sub")
    cli.main(["run", "implement.yaml", "task=x", "--dry-run", script(LOOP)])
    (note,) = [line for line in capsys.readouterr().err.splitlines() if line.startswith("Note:")]
    assert "../README" in note
    assert "implement.yaml" not in note.replace("../implement.yaml", "")  # the workflow file wf reads itself
