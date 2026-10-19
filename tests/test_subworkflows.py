"""Workflow nodes: a sub-workflow runs inside a node of its parent, end to end through `wf run`.

Each sub-workflow attempt is its own LangGraph thread in the run's checkpoints, so
its failure is the job's failure (retry, on_item_error, wf resume all apply), and a
question inside it is relayed to the top as the parent's own interrupt.
"""

import json
import subprocess
from pathlib import Path
from textwrap import dedent

import pytest

from fakes import Fake, edits
from workflow import cli, harnesses, runner


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(runner, "interactive", lambda: False)
    subprocess.run(["git", "init", "-q"], check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "first"], check=True)
    return tmp_path


def write(name: str, text: str) -> None:
    Path(name).write_text(dedent(text).lstrip())


def answers(monkeypatch, *lines: str) -> None:
    typed = iter(lines)
    monkeypatch.setattr(runner, "interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": next(typed))


def only_run() -> Path:
    (folder,) = (Path(".wf") / "runs").iterdir()
    return folder


def output() -> dict:
    return json.loads((only_run() / "output.json").read_text())


def status() -> str:
    return json.loads((only_run() / "status.json").read_text())["status"]


GREET = """
version: 1
name: greet
start:
  input:
    who: {type: string}
    loud: {type: boolean, default: false}
nodes:
  say:
    needs: [start]
    command: "test ! -e broken-{{ start.who }} && echo hi {{ start.who | quote }}{{ '!' if start.loud else '' }}"
end:
  output:
    said: "{{ say.stdout }}"
    run_id: "{{ run.id }}"
"""


def test_a_workflow_node_returns_the_sub_workflows_end_output(repo, capsys):
    write("greet.yaml", GREET)
    write("main.yaml", """
        version: 1
        name: main
        start: {input: {}}
        nodes:
          hello: {needs: [start], workflow: greet.yaml, with: {who: ada, loud: "{{ 1 == 1 }}"}}
        end:
          output:
            said: "{{ hello.said }}"
            same_run: {type: boolean, value: "{{ hello.run_id == run.id }}"}
    """)
    assert cli.main(["run", "main.yaml"]) == 0
    assert output() == {"said": "hi ada!\n", "same_run": True}
    sub = only_run() / "nodes" / "hello" / "1" / "attempt-1" / "nodes" / "say" / "1"
    assert (sub / "instruction.md").read_text() == "test ! -e broken-ada && echo hi ada!"
    assert (only_run() / "workflow" / "greet.yaml").exists()  # copied with the run
    out = capsys.readouterr().out
    assert "▶ hello/say" in out and "✓ hello/say" in out


BUILD = """
version: 1
name: build
start: {input: {}}
nodes:
  build:
    needs: [start]
    for_each: '["a", "b", "c"]'
    workflow: greet.yaml
    with: {who: "{{ item }}"}
    on_item_error: %s
end:
  output:
    said: {type: array, value: "{{ build | map(attribute='said') | list }}"}
    failed: {type: array, value: "{{ errors.build | map(attribute='item') | list }}"}
"""


def test_each_item_runs_its_own_sub_workflow(repo, capsys):
    write("greet.yaml", GREET)
    write("build.yaml", BUILD % "fail")
    assert cli.main(["run", "build.yaml"]) == 0
    assert output() == {"said": ["hi a\n", "hi b\n", "hi c\n"], "failed": []}
    assert "▶ build[1]/say" in capsys.readouterr().out


def test_a_failed_sub_workflow_is_an_item_error_with_continue(repo, capsys):
    write("greet.yaml", GREET)
    write("build.yaml", BUILD % "continue")
    Path("broken-b").touch()
    assert cli.main(["run", "build.yaml"]) == 0
    assert output() == {"said": ["hi a\n", "hi c\n"], "failed": ["b"]}


def test_resume_reruns_only_the_failed_sub_workflow(repo, capsys):
    write("greet.yaml", GREET)
    write("build.yaml", BUILD % "fail")
    Path("broken-b").touch()
    assert cli.main(["run", "build.yaml"]) == 1
    assert status() == "failed"
    assert "item 1: sub-workflow greet failed: say: exited with code 1" in capsys.readouterr().err
    Path("broken-b").unlink()
    assert cli.main(["resume", only_run().name]) == 0
    assert output()["said"] == ["hi a\n", "hi b\n", "hi c\n"]
    item = only_run() / "nodes" / "build" / "1"
    assert sorted(p.name for p in (item / "1").iterdir()) == ["attempt-1", "attempt-2", "instruction.md", "output.json"]
    assert sorted(p.name for p in (item / "0").iterdir()) == ["attempt-1", "instruction.md", "output.json"]


ASKING = """
version: 1
name: asking
start: {input: {topic: {type: string}}}
nodes:
  approve:
    needs: [start]
    human: "Go ahead with {{ start.topic }}?"
    output: {ok: {type: boolean}}
end:
  output:
    ok: "{{ approve.ok }}"
"""


def test_a_question_inside_a_sub_workflow_pauses_the_run_and_resume_asks_it(repo, monkeypatch, capsys):
    write("asking.yaml", ASKING)
    write("main.yaml", """
        version: 1
        name: main
        start: {input: {}}
        nodes:
          check: {needs: [start], for_each: '["x", "y"]', workflow: asking.yaml, with: {topic: "{{ item }}"}}
        end:
          output:
            oks: {type: array, value: "{{ check | map(attribute='ok') | list }}"}
    """)
    assert cli.main(["run", "main.yaml"]) == 2
    assert status() == "waiting"
    capsys.readouterr()
    answers(monkeypatch, "yes", "no")
    assert cli.main(["resume", only_run().name]) == 0
    assert output() == {"oks": [True, False]}
    out = capsys.readouterr().out
    assert "Go ahead with x?" in out and "Go ahead with y?" in out


def test_a_question_inside_a_sub_workflow_is_asked_at_once_at_a_terminal(repo, monkeypatch):
    write("asking.yaml", ASKING)
    write("main.yaml", """
        version: 1
        name: main
        start: {input: {}}
        nodes:
          check: {needs: [start], workflow: asking.yaml, with: {topic: it}}
        end: {output: {ok: "{{ check.ok }}"}}
    """)
    answers(monkeypatch, "yes")
    assert cli.main(["run", "main.yaml"]) == 0
    assert output() == {"ok": True}


def test_the_sub_workflow_works_in_its_nodes_workspace(repo):
    write("where.yaml", """
        version: 1
        name: where
        start: {input: {}}
        nodes:
          here: {needs: [start], command: pwd}
        end: {output: {path: "{{ here.stdout }}", repo: "{{ run.base }}"}}
    """)
    write("main.yaml", """
        version: 1
        name: main
        start: {input: {}}
        nodes:
          inside: {needs: [start], workspace: temp, workflow: where.yaml}
        end: {output: {path: "{{ inside.path }}", temp: "{{ workspace.inside.path }}"}}
    """)
    assert cli.main(["run", "main.yaml"]) == 0
    out = output()
    assert Path(out["path"].strip()).resolve() == Path(out["temp"]).resolve()


def test_worktree_branches_inside_a_sub_workflow_are_named_by_their_path(repo, monkeypatch):
    fake = Fake("claude-code", edits({"f.txt": "a\n"}, {"summary": "a"}), edits({"f.txt": "b\n"}, {"summary": "b"}))
    monkeypatch.setitem(harnesses.FACTORIES, "claude-code", fake.factory)
    write("impl.yaml", """
        version: 1
        name: impl
        defaults: {harness: claude-code}
        start: {input: {task: {type: string}}}
        nodes:
          implement: {needs: [start], instruction: "{{ start.task }}", access: edit, workspace: new}
        end: {output: {branch: "{{ workspace.implement.branch }}"}}
    """)
    write("main.yaml", """
        version: 1
        name: main
        start: {input: {}}
        nodes:
          build: {needs: [start], for_each: '["a", "b"]', max_parallel: 1, workflow: impl.yaml, with: {task: "{{ item }}"}}
        end:
          output:
            branches: {type: array, value: "{{ build | map(attribute='branch') | list }}"}
    """)
    assert cli.main(["run", "main.yaml"]) == 0
    run_id = only_run().name
    assert output()["branches"] == [f"wf/{run_id}/build/0/implement", f"wf/{run_id}/build/1/implement"]
    shown = subprocess.run(["git", "show", f"wf/{run_id}/build/1/implement:f.txt"], capture_output=True, text=True).stdout
    assert shown == "b\n"


def test_a_sub_workflows_spending_counts_for_the_whole_run(repo, monkeypatch):
    from fakes import submit
    fake = Fake("claude-code", submit({"summary": "x"}, tokens=300, cost=0.03))
    monkeypatch.setitem(harnesses.FACTORIES, "claude-code", fake.factory)
    write("agent.yaml", """
        version: 1
        name: agent
        defaults: {harness: claude-code}
        start: {input: {}}
        nodes:
          think: {needs: [start], instruction: think}
        end: {output: {seen: {type: integer, value: "{{ run.tokens }}"}}}
    """)
    write("main.yaml", """
        version: 1
        name: main
        start: {input: {}}
        nodes:
          sub: {needs: [start], workflow: agent.yaml}
        end:
          output:
            tokens: {type: integer, value: "{{ run.tokens }}"}
            seen_inside: "{{ sub.seen }}"
    """)
    assert cli.main(["run", "main.yaml"]) == 0
    assert output() == {"tokens": 300, "seen_inside": 300}


def test_nested_sub_workflows_run_again_on_every_pass(repo):
    write("leaf.yaml", """
        version: 1
        name: leaf
        start: {input: {n: {type: integer}}}
        nodes:
          echo: {needs: [start], command: "echo {{ start.n }}"}
        end: {output: {said: "{{ echo.stdout }}"}}
    """)
    write("mid.yaml", """
        version: 1
        name: mid
        start: {input: {n: {type: integer}}}
        nodes:
          inner: {needs: [start], workflow: leaf.yaml, with: {n: "{{ start.n }}"}}
        end: {output: {said: "{{ inner.said }}"}}
    """)
    write("main.yaml", """
        version: 1
        name: main
        start: {input: {}}
        nodes:
          outer:
            needs: [start]
            workflow: mid.yaml
            with: {n: "{{ history.outer | length }}"}
            routes: [{when: "history.outer | length < 2", to: outer, max: 1}, {to: end}]
        end:
          output:
            said: {type: array, value: "{{ history.outer | map(attribute='said') | list }}"}
    """)
    assert cli.main(["run", "main.yaml"]) == 0
    assert output() == {"said": ["0\n", "1\n"]}  # the second pass ran the leaf again, with n=1


def test_answers_reach_the_right_question_when_a_sub_workflow_is_retried(repo, monkeypatch):
    write("flaky.yaml", """
        version: 1
        name: flaky
        start: {input: {}}
        nodes:
          approve: {needs: [start], human: "OK?", output: {ok: {type: boolean}}}
          check: {needs: [approve], command: "test -e marker || { touch marker; exit 1; }"}
        end: {output: {ok: "{{ approve.ok }}"}}
    """)
    write("main.yaml", """
        version: 1
        name: main
        start: {input: {}}
        nodes:
          w: {needs: [start], workflow: flaky.yaml, retry: 1}
        end: {output: {ok: "{{ w.ok }}"}}
    """)
    answers(monkeypatch, "yes", "no")  # attempt 1 is answered yes and fails; attempt 2 is answered no
    assert cli.main(["run", "main.yaml"]) == 0
    assert output() == {"ok": False}


def test_answers_reach_the_right_question_across_separate_resumes(repo, monkeypatch):
    write("flaky.yaml", """
        version: 1
        name: flaky
        start: {input: {}}
        nodes:
          approve: {needs: [start], human: "OK?", output: {ok: {type: boolean}}}
          check: {needs: [approve], command: "test -e marker || { touch marker; exit 1; }"}
        end: {output: {ok: "{{ approve.ok }}"}}
    """)
    write("main.yaml", """
        version: 1
        name: main
        start: {input: {}}
        nodes:
          w: {needs: [start], workflow: flaky.yaml, retry: 1}
        end: {output: {ok: "{{ w.ok }}"}}
    """)

    def one_answer(line):
        typed = iter([line])

        def read(prompt=""):
            try:
                return next(typed)
            except StopIteration:
                raise EOFError from None
        monkeypatch.setattr(runner, "interactive", lambda: True)
        monkeypatch.setattr("builtins.input", read)

    assert cli.main(["run", "main.yaml", "--no-input"]) == 2
    one_answer("yes")  # attempt 1: yes, then it fails; attempt 2 asks again and nobody answers
    assert cli.main(["resume", only_run().name]) == 2
    one_answer("no")
    assert cli.main(["resume", only_run().name]) == 0
    assert output() == {"ok": False}


def test_an_optional_field_left_out_passes_through_with_and_reads_as_none_inside(repo, monkeypatch):
    from fakes import submit
    monkeypatch.setitem(harnesses.FACTORIES, "claude-code", Fake("claude-code", submit({"seat": {"name": "argon"}})).factory)
    write("use.yaml", """
        version: 1
        name: use
        types: {seat: {name: {type: string}, effort: {type: string, optional: true}}}
        start: {input: {seat: {$ref: seat}}}
        nodes:
          say: {needs: [start], command: "echo {{ (start.seat.effort or 'no effort') | quote }}"}
        end: {output: {said: "{{ say.stdout }}"}}
    """)
    write("main.yaml", """
        version: 1
        name: main
        defaults: {harness: claude-code}
        types: {seat: {name: {type: string}, effort: {type: string, optional: true}}}
        start: {input: {}}
        nodes:
          pick: {needs: [start], instruction: pick a seat, output: {seat: {$ref: seat}}}
          use: {needs: [pick], workflow: use.yaml, with: {seat: "{{ pick.seat }}"}}
        end: {output: {said: "{{ use.said }}", effort: "{{ pick.seat.effort }}"}}
    """)
    assert cli.main(["run", "main.yaml"]) == 0
    assert output() == {"said": "no effort\n", "effort": None}
    assert json.loads((only_run() / "nodes" / "pick" / "1" / "output.json").read_text()) == {"seat": {"name": "argon", "effort": None}}


def test_a_left_out_optional_field_fits_a_narrower_input_and_takes_its_default(repo, monkeypatch):
    from fakes import submit
    monkeypatch.setitem(harnesses.FACTORIES, "claude-code", Fake("claude-code", submit({"seat": {"name": "argon"}})).factory)
    write("use.yaml", """
        version: 1
        name: use
        start:
          input:
            seat: {type: object, properties: {name: {type: string}}, required: [name], additionalProperties: false}
            effort: {type: string, default: high}
        nodes:
          say: {needs: [start], command: "echo {{ start.seat.name | quote }} {{ start.effort | quote }}"}
        end: {output: {said: "{{ say.stdout }}"}}
    """)
    write("main.yaml", """
        version: 1
        name: main
        defaults: {harness: claude-code}
        types: {seat: {name: {type: string}, effort: {type: string, optional: true}}}
        start: {input: {}}
        nodes:
          pick: {needs: [start], instruction: pick a seat, output: {seat: {$ref: seat}}}
          use: {needs: [pick], workflow: use.yaml, with: {seat: "{{ pick.seat }}", effort: "{{ pick.seat.effort }}"}}
        end: {output: {said: "{{ use.said }}"}}
    """)
    assert cli.main(["run", "main.yaml"]) == 0
    assert output() == {"said": "argon high\n"}


def test_an_optional_field_a_sub_workflows_end_output_leaves_out_reads_as_none_in_the_parent(repo):
    write("child.yaml", """
        version: 1
        name: child
        start: {input: {}}
        nodes:
          say: {needs: [start], command: echo hi}
        end:
          output:
            result:
              type: object
              properties: {said: {type: string}, note: {type: string}}
              required: [said]
              additionalProperties: false
              value: "{{ {'said': say.stdout} }}"
    """)
    write("main.yaml", """
        version: 1
        name: main
        start: {input: {}}
        nodes:
          child: {needs: [start], workflow: child.yaml}
          after: {needs: [child], command: "echo {{ (child.result.note or 'no note') | quote }}"}
        end: {output: {said: "{{ after.stdout }}", note: "{{ child.result.note }}"}}
    """)
    assert cli.main(["run", "main.yaml"]) == 0
    assert output() == {"said": "no note\n", "note": None}
