"""validate.check(file): every rule in the spec's Validation section.

Each case is a small workflow file and the problems it should produce, as
(level, where, part of the message).
"""

import json
import re
from pathlib import Path
from textwrap import dedent, indent

import pytest

from workflow import harnesses, validate
from workflow.harnesses import cursor

DOCS = Path(__file__).parent.parent / "docs"
EXAMPLES = Path(__file__).parent.parent / "workflow" / "examples"


def flow(nodes: str, *, end: str = "{}", inputs: str = "task: {type: string}", top: str = "") -> str:
    """A workflow file around some nodes. `end` is the body of end.output."""
    end_block = f" {end}" if end.strip().startswith("{") else "\n" + indent(dedent(end).strip(), "    ")
    return (
        f"version: 1\nname: t\n{dedent(top)}defaults:\n  harness: claude-code\n"
        f"start:\n  input:\n{indent(dedent(inputs).strip(), '    ')}\n"
        f"nodes:\n{indent(dedent(nodes).strip(), '  ')}\n"
        f"end:\n  output:{end_block}\n"
    )


def check(tmp_path: Path, text: str, **others: str):
    for name, body in others.items():
        (tmp_path / f"{name}.yaml").write_text(body)
    path = tmp_path / "main.yaml"
    path.write_text(text)
    return validate.check(path)


def found(report) -> list[tuple[str, str, str]]:
    return [(p.level, p.where, p.message) for p in report.problems]


def assert_problems(report, expected: list[tuple[str, str, str]]) -> None:
    actual = found(report)
    assert len(actual) == len(expected), "\n".join(map(str, actual))
    for (level, where, message), (e_level, e_where, e_part) in zip(actual, expected):
        assert (level, where) == (e_level, e_where), actual
        assert e_part in message, f"{e_part!r} not in {message!r}"


# ---------- the examples ----------


def test_implement_example_is_valid_with_no_warnings():
    report = validate.check(EXAMPLES / "implement.yaml")
    assert found(report) == []


def test_feature_example_is_valid_and_warns_only_about_codex_cost():
    report = validate.check(EXAMPLES / "feature.yaml")
    assert report.errors == []
    assert [(p.level, p.where) for p in report.problems] == [("warning", "limits.cost_usd")]
    assert "codex" in report.problems[0].message


def test_the_spec_and_the_example_files_match():
    spec = (DOCS / "workflow-spec.md").read_text()
    blocks = re.findall(r"```yaml\n(.*?)```", spec, re.S)
    for name in ("implement", "feature"):
        copies = [b for b in blocks if b.startswith(f"version: 1\nname: {name}\n")]
        assert copies == [(EXAMPLES / f"{name}.yaml").read_text()], name


# ---------- 1. the file's shape ----------


def test_unknown_keys_and_missing_keys(tmp_path):
    report = check(tmp_path, "version: 1\nname: t\nstart: {}\nnodes:\n  a: {needs: [start], command: x, chek: false}\n")
    assert_problems(report, [("error", "nodes.a.chek", "unknown key"), ("error", "end", "missing required key")])


def test_a_node_needs_exactly_one_kind(tmp_path):
    report = check(tmp_path, flow("a: {needs: [start], instrction: hi}\nb: {needs: [start], command: x, human: y}"))
    assert_problems(report, [
        ("error", "nodes.a", "unknown keys: `instrction`"),
        ("error", "nodes.b", "has `command`, `human`"),
    ])


def test_duplicate_node_ids(tmp_path):
    report = check(tmp_path, flow("a: {needs: [start], command: x}\na: {needs: [start], command: y}"))
    assert_problems(report, [("error", "line 10", "duplicate key `a`")])


# ---------- 2. node ids ----------


def test_node_ids_are_lowercase_identifiers_and_not_reserved(tmp_path):
    report = check(tmp_path, flow("Bad-id: {needs: [start], command: x}\nhistory: {needs: [start], command: x}\n"
                                  "results: {needs: [start], command: x}"))
    assert_problems(report, [
        ("error", "nodes.Bad-id", "lowercase letter"),
        ("error", "nodes.history", "`history` is a reserved name"),
        ("error", "nodes.results", "`results` is a reserved name"),
    ])


# ---------- 4, 5. needs and routes ----------


def test_needs_and_route_targets_must_exist(tmp_path):
    report = check(tmp_path, flow("""
        a: {needs: [start, nope], command: x}
        b:
          needs: [a]
          command: x
          routes:
            - {when: "true", to: [c, ghost]}
            - {to: start}
            - {to: [c, end]}
            - {to: end}
        c: {command: x}
    """))
    assert_problems(report, [
        ("error", "nodes.a.needs", "unknown node `nope`"),
        ("error", "nodes.b.routes[0].to", "unknown node `ghost`"),
        ("error", "nodes.b.routes[1].to", "`start` can't be a route target"),
        ("error", "nodes.b.routes[2].to", "`end` can't be part of a list"),
    ])


def test_needs_cannot_form_a_cycle(tmp_path):
    report = check(tmp_path, flow("a: {needs: [start, b], command: x}\nb: {needs: [a], command: x}"))
    assert_problems(report, [("error", "nodes.a.needs", "a → b → a")])


def test_a_node_with_routes_cannot_be_needed(tmp_path):
    report = check(tmp_path, flow("""
        a: {needs: [start], command: x, routes: [{to: end}]}
        b: {needs: [a], command: x}
    """))
    assert_problems(report, [("error", "nodes.b.needs", "`a` has routes, so it can't be in needs")])


def test_every_node_must_be_able_to_run(tmp_path):
    report = check(tmp_path, flow("""
        a: {command: x}
        b: {command: x, routes: [{to: c}]}
        c: {command: x}
    """))
    assert_problems(report, [
        ("error", "nodes.a", "never runs"),
        ("error", "nodes.b", "never runs"),
        ("error", "nodes.c", "can't be reached from start"),
    ])


# ---------- 6. routes and loops ----------


def test_the_last_route_has_no_when_and_no_max(tmp_path):
    report = check(tmp_path, flow("""
        a: {needs: [start], command: x, routes: [{when: "true", to: end}]}
        b: {needs: [start], command: x, routes: [{to: end, max: 2}]}
    """))
    assert_problems(report, [
        ("error", "nodes.a.routes[0]", "can't have `when`"),
        ("error", "nodes.b.routes[0]", "can't have `max`"),
    ])


def test_every_loop_needs_a_route_with_max(tmp_path):
    report = check(tmp_path, flow("""
        a: {needs: [start], command: x}
        b: {needs: [a], command: x, routes: [{when: "true", to: end}, {to: a}]}
        c: {needs: [start], command: x, routes: [{when: "true", to: c, max: 2}, {to: end}]}
    """))
    assert_problems(report, [("error", "nodes.b.routes", "a → b → a has no route with `max`")])


# ---------- 3. schemas ----------


def test_schema_problems_are_reported_where_they_are(tmp_path):
    report = check(tmp_path, flow(
        """
        a:
          needs: [start]
          instruction: hi
          output:
            x: {type: string, format: email}
            y: {$ref: nope}
        """,
        inputs="n: {type: integer, default: two}",
        top="types:\n  t: {self: {$ref: t}}\n",
        end="z: {type: strin, value: '{{ a.x }}'}",
    ))
    assert_problems(report, [
        ("error", "types.t", "refers to itself: t → t"),
        ("error", "start.input.n", "default doesn't match"),
        ("error", "nodes.a.output.x", "unsupported schema keyword `format`"),
        ("error", "nodes.a.output.y", "unknown type `nope`"),
        ("error", "end.output.z", "`strin` isn't a JSON Schema type"),
    ])


# ---------- 7, 8. templates and expressions ----------


def test_names_and_fields_in_templates_must_exist(tmp_path):
    report = check(tmp_path, flow("""
        a:
          needs: [start]
          instruction: "{{ start.taks }} {{ explroe }}"
          output: {plan: {type: string}}
        b:
          needs: [a]
          command: "echo {{ a.plna | quote }} {{ history.zzz | quote }} {{ run.nope }}"
          routes:
            - {when: "b.exit_cod == 0", to: end}
            - {to: end}
    """))
    assert_problems(report, [
        ("error", "nodes.a.instruction", "`start.taks`: no field `taks` (fields: task)"),
        ("error", "nodes.a.instruction", "unknown name `explroe`"),
        ("error", "nodes.b.command", "`a.plna`: no field `plna` (fields: plan)"),
        ("error", "nodes.b.command", "`history.zzz`: no field `zzz`"),
        ("error", "nodes.b.command", "`run.nope`: no field `nope`"),
        ("error", "nodes.b.routes[0].when", "`b.exit_cod`: no field `exit_cod`"),
    ])


def test_template_syntax_errors(tmp_path):
    report = check(tmp_path, flow('a: {needs: [start], instruction: "ok\\n{% if %}"}'))
    assert_problems(report, [("error", "nodes.a.instruction", "line 2")])


def test_item_and_index_only_in_for_each_nodes(tmp_path):
    report = check(tmp_path, flow("""
        a: {needs: [start], instruction: "{{ item }}"}
        b:
          needs: [start]
          for_each: "[1, 2]"
          command: "echo {{ index }} {{ item }}"
          routes: [{when: "item", to: end}, {to: end}]
    """))
    assert_problems(report, [
        ("error", "nodes.a.instruction", "`item` is only defined inside a for_each node"),
        ("error", "nodes.b.routes[0].when", "`item` is only defined inside a for_each node"),
    ])


def test_for_each_reads_a_list_and_items_are_checked(tmp_path):
    report = check(tmp_path, flow("""
        plan:
          needs: [start]
          instruction: plan
          output: {tasks: {type: array, maxItems: 3, items: {type: object, properties: {title: {type: string}}, required: [title], additionalProperties: false}}}
        a: {needs: [plan], for_each: start.task, command: x}
        b: {needs: [plan], for_each: plan.tasks, instruction: "{{ item.titel }}"}
    """))
    assert_problems(report, [
        ("error", "nodes.a.for_each", "must produce a list, but `start.task` is a string"),
        ("error", "nodes.b.instruction", "`item.titel`: no field `titel` (fields: title)"),
    ])


def test_results_lists_the_items_of_a_for_each_node_and_its_fields_are_checked(tmp_path):
    report = check(tmp_path, flow("""
        plan:
          needs: [start]
          instruction: plan
          output: {tasks: {type: array, maxItems: 3, items: {type: object, properties: {title: {type: string}}, required: [title], additionalProperties: false}}}
        build:
          needs: [plan]
          for_each: plan.tasks
          on_item_error: continue
          web: true
          instruction: "{{ item.title }}"
          output: {done: {type: boolean}}
        sum:
          needs: [build]
          instruction: >-
            {% for r in results.build %}{{ r.index }} {{ r.item.title }} {{ r.output.done if r.output else 'failed' }}
            {{ r.harness }} {{ r.model }}{% endfor %}
            {{ results.build[0].item.titel }} {{ results.build[0].output.dun }} {{ results.build[0].cost }} {{ results.plan }}
        echo:
          needs: [build]
          command: >-
            echo {{ results.build[0].index }} {{ results.build[0].output.done if results.build[0].output else false }}
            {{ results.build[0].item.title }}
        act: {needs: [build], access: full, instruction: "{{ results.build | tojson }}"}
    """))
    assert_problems(report, [
        ("error", "nodes.sum.instruction", "`results.build[0].item.titel`: no field `titel` (fields: title)"),
        ("error", "nodes.sum.instruction", "`results.build[0].output.dun`: no field `dun` (fields: done)"),
        ("error", "nodes.sum.instruction", "`results.build[0].cost`: no field `cost` (fields: index, item, output, harness, model)"),
        ("error", "nodes.sum.instruction", "`plan` isn't a for_each node, so `results.plan` doesn't exist"),
        ("warning", "nodes.echo.command", "output from `results` into the shell unquoted"),  # the item's title, not its index
        ("warning", "nodes.act.instruction", "reads output from `build`, which has `web: true`"),
    ])


def test_a_for_each_list_from_results_gives_its_items_the_schema_of_the_entries(tmp_path):
    report = check(tmp_path, flow("""
        plan:
          needs: [start]
          instruction: plan
          output: {tasks: {type: array, maxItems: 3, items: {type: object, properties: {title: {type: string}}, required: [title], additionalProperties: false}}}
        a: {needs: [plan], for_each: plan.tasks, instruction: "{{ item.title }}"}
        b: {needs: [a], for_each: results.a, instruction: "{{ item.item.title }} {{ item.output.summary }}"}
        c: {needs: [b], for_each: results.b, instruction: "{{ item.item.item.titel }}"}
        d: {needs: [c], instruction: "{{ results.c[0].item.item.item.title }} {{ results.c[0].item.item.output.summry }}"}
        each: {needs: [start], for_each: start.values, command: echo ok}
    """, inputs="task: {type: string}\nvalues: {type: array, maxItems: 5, items: true}"))
    assert_problems(report, [  # and no warning that results.a has no maxItems: a node's max_items bounds its results
        ("error", "nodes.c.instruction", "`item.item.item.titel`: no field `titel` (fields: title)"),
        ("error", "nodes.d.instruction", "`results.c[0].item.item.output.summry`: no field `summry` (fields: summary)"),
    ])


def test_a_filtered_for_each_list_of_results_gives_its_items_the_schema_of_the_entries(tmp_path):
    report = check(tmp_path, flow("""
        build: {needs: [start], for_each: '[1, 2]', on_item_error: continue, instruction: x, output: {done: {type: boolean}}}
        listed: {needs: [build], for_each: "results.build | list", instruction: "{{ item.output.done }} {{ item.output.dun }}"}
        kept: {needs: [build], for_each: "results.build | selectattr('output') | list", instruction: "{{ item.output.dnoe }}"}
    """))
    assert_problems(report, [  # and no warning about item.output.done: a read through item isn't checked
        ("error", "nodes.listed.instruction", "`item.output.dun`: no field `dun` (fields: done)"),
        ("error", "nodes.kept.instruction", "`item.output.dnoe`: no field `dnoe` (fields: done)"),
    ])


def test_a_read_of_results_that_fails_when_an_item_failed_or_the_node_didnt_run_warns(tmp_path):
    # Each read is the only one in its template, as wf validate warns once for each template and entry or node
    after = {  # these run after build and all
        "bare_index": "{{ results.build[0].output.done }}",
        "by_if": "{% if results.build[1].output %}{{ results.build[1].output.done }}{% endif %}",
        "by_else": "{% if not results.build[1].output %}failed{% else %}{{ results.build[1].output.done }}{% endif %}",
        "by_value": "{{ results.build[1].output.done if results.build[1].output else 'failed' }}",
        "by_and": "{{ results.build[1].output and results.build[1].output.done }}",
        "by_not_none": "{% if results.build[1].output is not none %}{{ results.build[1].output.done }}{% endif %}",
        "by_last": "{% if results.build[-1].output %}{{ results.build[-1].output.done }}{% endif %}",
        "other_item": "{% if results.build[0].output %}{{ results.build[1].output.done }}{% endif %}",
        "on_fail": "{{ results.all[0].output.done }}",  # on_item_error: fail leaves no output none
        "whole": "{{ results.build[0].output }}",  # the output itself, and no field of it
        "in_loop": "{% for r in results.build %}{{ r.output.done }}{% endfor %}",  # through a name: not checked
        "by_set": "{% set first = results.build[0] %}{{ first.output.done }}",
        "by_with": "{% with outs = results.build | map(attribute='output') | list %}{{ outs[0].done }}{% endwith %}",
        "by_select": "{% set ok = results.build | selectattr('output') | list %}{{ ok[1].output.done }}",
        "by_map": "{{ results.build | map(attribute='output.done') | list }}",  # through a filter: not checked
    }
    beside = {  # these run beside build, which might not have run yet
        "early": "{{ results.build[0].index }}",
        "early_last": "{{ results.build[-1].index }}",
        "early_none": "{% if results.build is not none %}{{ results.build[1].index }}{% endif %}",  # never none
        "if_ran": "{% if build %}{{ results.build[1].index }}{% endif %}",
        "if_any": "{% if results.build %}{{ results.build[1].index }}{% endif %}",
        "in_loop_early": "{% for r in results.build %}{{ r.index }}{% endfor %}",
    }
    nodes = ["side: {needs: [start], command: x}",
             "build: {needs: [start], for_each: '[1, 2]', on_item_error: continue, instruction: x, output: {done: {type: boolean}}}",
             "all: {needs: [start], for_each: '[1, 2]', instruction: x, output: {done: {type: boolean}}}"]
    nodes += [f"{name}: {{needs: [build, all], instruction: {json.dumps(text)}}}" for name, text in after.items()]
    nodes += [f"{name}: {{needs: [side], instruction: {json.dumps(text)}}}" for name, text in beside.items()]
    nodes += ["pair: {needs: [side], for_each: '[0, 1]', instruction: '{{ results.build[index].index }}'}",  # by its position
              "pick: {needs: [build], for_each: results.build, instruction: '{{ item.output.done }}'}"]  # through item: not checked
    report = check(tmp_path, flow("\n".join(nodes)))
    assert_problems(report, [
        ("warning", "nodes.bare_index.instruction", "reads `results.build[0].output.done`, but with `on_item_error: continue` "
                                                    "an item of `build` that failed has the output none: check that the output "
                                                    "is set first"),
        ("warning", "nodes.other_item.instruction", "reads `results.build[1].output.done`, but with `on_item_error: continue`"),
        ("warning", "nodes.early.instruction", "reads `results.build[0].index`, but `build` might not have run yet; "
                                               "check first with {% if build %}"),
        ("warning", "nodes.early_last.instruction", "reads `results.build[-1].index`, but `build` might not have run yet"),
        ("warning", "nodes.early_none.instruction", "reads `results.build[1].index`, but `build` might not have run yet"),
        ("warning", "nodes.pair.instruction", "reads `results.build[index].index`, but `build` might not have run yet"),
    ])


# ---------- 9. harnesses ----------


def test_harness_lists_need_model_maps_and_cursor_needs_a_model(tmp_path):
    report = check(tmp_path, flow("""
        a: {needs: [start], instruction: x, harness: [claude-code, codex], model: opus}
        b: {needs: [start], instruction: x, harness: cursor}
        c: {needs: [start], instruction: x, harness: cursor, model: composer-2.5}
        d: {needs: [start], instruction: x, harness: gemini}
        e: {needs: [start], for_each: '["codex", "cursor"]', instruction: x, harness: "{{ item }}"}
    """))
    assert_problems(report, [
        ("error", "nodes.a.model", "`harness` is a list, so `model` must be a map"),
        ("error", "nodes.b", "can run on cursor, which needs a `model`"),
        ("error", "nodes.d.harness", "unknown harness `gemini`"),
        ("error", "nodes.e", "can run on cursor, which needs a `model`"),
    ])


def test_templates_in_model_and_effort_are_checked_where_they_are_set(tmp_path):
    top = "defaults:\n  harnesses:\n    codex: {model: '{{ item.model }}'}\n"
    seats = "seats: {type: array, maxItems: 2, items: {type: object, properties: {model: {type: string}}, additionalProperties: false}}"
    report = check(tmp_path, flow("""
        a: {needs: [start], for_each: start.seats, instruction: x, model: "{{ item.modle }}"}
        b: {needs: [start], instruction: x, effort: {claude-code: "{{ start.effort }}"}}
        c: {needs: [start], instruction: x, harness: codex}
        d: {needs: [start], for_each: start.seats, instruction: x, harness: codex}
    """, inputs=seats, top=top).replace("defaults:\n  harness: claude-code\n", "", 1).replace(
        "  harnesses:", "  harness: claude-code\n  harnesses:"))
    assert_problems(report, [
        ("error", "nodes.a.model", "`item.modle`: no field `modle` (fields: model)"),
        ("error", "nodes.b.effort.claude-code", "`start.effort`: no field `effort`"),
        ("error", "defaults.harnesses.codex.model", "`item` is only defined inside a for_each node"),
    ])



def test_model_and_effort_are_checked_for_the_harnesses_a_node_can_run_on(tmp_path):
    report = check(tmp_path, dedent("""
        version: 1
        name: t
        defaults:
          harness: claude-code
          effort: "{{ nope }}"
          harnesses: {cursor: {model: "{{ cursor_only }}"}}
        start:
          input:
            seats:
              type: array
              maxItems: 2
              items: {type: object, properties: {harness: {enum: [claude-code, codex]}, model: {type: string}}}
        nodes:
          a: {needs: [start], for_each: start.seats, instruction: x, harness: "{{ item.harness }}", model: "{{ item.model }}", effort: high}
          b: {needs: [start], instruction: x, model: {claude-code: opus, codex: "{{ start.nope }}"}, effort: low}
          c: {needs: [start], instruction: x}
        end: {output: {}}
    """))
    assert_problems(report, [  # `a` can't run on cursor, so its default isn't checked there
        ("error", "nodes.b.model.codex", "`start.nope`: no field `nope`"),
        ("error", "defaults.effort", "unknown name `nope`"),
    ])


def test_model_and_effort_cannot_read_the_session(tmp_path):
    report = check(tmp_path, flow("""
        a: {needs: [start], instruction: x, model: "{{ 'opus' if session.continued else 'sonnet' }}"}
        b: {needs: [start], instruction: x, effort: {claude-code: "{{ session.continued }}"}}
    """))
    assert_problems(report, [
        ("error", "nodes.a.model", "`session` isn't available here"),
        ("error", "nodes.b.effort.claude-code", "`session` isn't available here"),
    ])

# ---------- 10. end outputs ----------


def test_short_form_end_outputs_are_single_field_references(tmp_path):
    report = check(tmp_path, flow(
        "a: {needs: [start], command: x}",
        end="""
            ok: "{{ a.stdout }}"
            branch: "{{ workspace.a.branch }}"
            two: "{{ a.stdout }} and more"
            filtered: "{{ a.stdout | trim }}"
            whole: "{{ a }}"
            nofield: "{{ a.nope }}"
        """,
    ))
    assert_problems(report, [
        ("error", "end.output.two", "single field reference"),
        ("error", "end.output.filtered", "single field reference"),
        ("error", "end.output.whole", "single field reference"),
        ("error", "end.output.nofield", "`a.nope`: no field `nope`"),
    ])


# ---------- 11. sub-workflows ----------

SUB = """
version: 1
name: sub
start:
  input:
    task: {type: string}
    count: {type: integer, optional: true}
    cmd: {type: string, default: "true"}
nodes:
  run_it: {needs: [start], command: "{{ start.cmd }} {{ start.task | quote }}"}
end:
  output: {code: "{{ run_it.exit_code }}"}
"""


def test_sub_workflows_must_exist_and_with_must_fit_their_inputs(tmp_path):
    report = check(
        tmp_path,
        flow("""
            a: {needs: [start], workflow: missing.yaml}
            b: {needs: [start], workflow: sub.yaml, with: {count: "{{ start.task }}", extra: 1}}
            c: {needs: [b], command: "exit {{ b.code }}"}
            d: {needs: [b], command: "exit {{ b.nope }}"}
        """),
        sub=SUB,
    )
    assert_problems(report, [
        ("error", "nodes.a.workflow", "file not found: missing.yaml"),
        ("error", "nodes.b.with", "missing required input `task`"),
        ("error", "nodes.b.with.count", "expects integer, but this gives string"),
        ("error", "nodes.b.with.extra", "sub has no input `extra`"),
        ("error", "nodes.d.command", "`b.nope`: no field `nope` (fields: code)"),
    ])


def test_a_workflow_cannot_include_itself(tmp_path):
    loop = flow("a: {needs: [start], workflow: other.yaml, with: {task: x}}")
    other = flow("b: {needs: [start], workflow: main.yaml, with: {task: x}}")
    report = check(tmp_path, loop, other=other)
    assert_problems(report, [("error", "nodes.b.workflow", "includes itself: main.yaml → other.yaml → main.yaml")])
    assert report.problems[0].file.name == "other.yaml"


def test_errors_in_a_sub_workflow_are_reported_with_its_file(tmp_path):
    report = check(tmp_path, flow("a: {needs: [start], workflow: bad.yaml, with: {task: x}}"), bad=flow("z: {command: x}"))
    assert [(p.file.name, p.where) for p in report.problems] == [("bad.yaml", "nodes.z")]


# ---------- workspaces ----------


def test_workspace_values(tmp_path):
    report = check(tmp_path, flow("""
        a: {needs: [start], command: x, workspace: elsewhere}
        b: {needs: [start], command: x, workspace: b}
        c: {needs: [start], for_each: "[1]", command: x}
        d: {needs: [c], command: x, workspace: c}
    """))
    assert_problems(report, [
        ("error", "nodes.a.workspace", "unknown workspace `elsewhere`"),
        ("error", "nodes.b.workspace", "can't use its own workspace"),
        ("error", "nodes.d.workspace", "`c` is a for_each node"),
    ])


def test_branch_names_a_new_worktree_once_per_item(tmp_path):
    report = check(tmp_path, flow("""
        a: {needs: [start], command: x, branch: fm/a}
        b: {needs: [start], command: x, workspace: new, for_each: '["x", "y"]', branch: fm/b}
        c: {needs: [start], command: x, workspace: new, for_each: '["x", "y"]', branch: "c/{{ index }}"}
        d: {needs: [start], command: x, workspace: new, branch: "d/{{ workspace.d.path }}"}
        e: {needs: [start], command: x, workspace: new, branch: "wf/{{ start.task }}"}
        f: {needs: [start], command: x, workspace: new, branch: "f/{{ nope }}"}
        g: {needs: [start], command: x, workspace: new, branch: ""}
        h: {needs: [start], command: x, workspace: new, branch: "  "}
    """))
    assert_problems(report, [
        ("error", "nodes.f.branch", "unknown name `nope`"),
        ("error", "nodes.a.branch", "but this node's workspace is `repo`"),
        ("error", "nodes.b.branch", "must use `item` or `index`"),
        ("error", "nodes.d.branch", "can't read its workspace"),
        ("error", "nodes.e.branch", "where wf names branches itself"),
        ("error", "nodes.g.branch", "`branch` is empty"),
        ("error", "nodes.h.branch", "`branch` is empty"),
    ])


@pytest.mark.parametrize("name", ["wf", "WF/{{ start.task }}", " wf "])
def test_branch_cannot_be_wf_or_under_it_in_any_case(tmp_path, name):
    report = check(tmp_path, flow(f'a: {{needs: [start], command: x, workspace: new, branch: "{name}"}}'))
    assert_problems(report, [("error", "nodes.a.branch", "wf and wf/... are where wf names branches itself")])


def test_two_nodes_that_could_name_the_same_branch_are_refused(tmp_path):
    report = check(tmp_path, flow("""
        a: {needs: [start], command: x, branch: "fix/{{ start.task }}"}
        b: {needs: [start], command: x, branch: "fix/{{ start.task }}"}
        c: {needs: [start], command: x, branch: "review/{{ start.task }}"}
        d: {needs: [start], command: x, branch: "review/{{ start.task }}/2"}
        e: {needs: [start], command: x, branch: pick/one}
        f: {needs: [start], command: x, for_each: '["one", "two"]', branch: "pick/{{ item }}"}
        g: {needs: [start], command: x, branch: " pick/one "}
    """).replace("defaults:\n", "defaults:\n  workspace: new\n"))
    assert_problems(report, [
        ("error", "nodes.b.branch", "`a` and `b` could name the same branch"),
        ("error", "nodes.d.branch", "`c` and `d` could name the same branch"),
        ("error", "nodes.f.branch", "`e` and `f` could name the same branch"),
        ("error", "nodes.g.branch", "`e` and `g` could name the same branch"),  # names are stripped
    ])


def test_branches_git_cannot_have_side_by_side_are_refused(tmp_path):
    report = check(tmp_path, flow("""
        a: {needs: [start], command: x, branch: fm/task}
        b: {needs: [start], command: x, branch: fm/task/review}
        c: {needs: [start], command: x, branch: FIX/one}
        d: {needs: [start], command: x, branch: fix/one}
        e: {needs: [start], command: x, branch: "pick {{ start.task }}"}
        f: {needs: [start], command: x, branch: pick}
    """).replace("defaults:\n", "defaults:\n  workspace: new\n"))
    assert_problems(report, [
        ("error", "nodes.b.branch", "`a` and `b` could name the same branch, or one under the other"),
        ("error", "nodes.d.branch", "`c` and `d` could name the same branch"),  # on any machine: git ignores case on macOS
        ("error", "nodes.f.branch", "`e` and `f` could name the same branch"),  # an empty task renders `pick`
    ])


def test_fixed_text_between_expressions_tells_branches_apart(tmp_path):
    report = check(tmp_path, flow("""
        author: {needs: [start], command: x, for_each: '["a", "b"]', branch: "fm/{{ start.task }}/author/{{ index }}"}
        review: {needs: [start], command: x, for_each: '["a", "b"]', branch: "fm/{{ start.task }}/review/{{ index }}"}
    """).replace("defaults:\n", "defaults:\n  workspace: new\n"))
    assert_problems(report, [])


SUB_BRANCH = flow(
    'impl: {needs: [start], command: x, workspace: new, branch: "{{ start.branch }}"}',
    inputs="branch: {type: string}",
)


def test_branches_named_in_a_sub_workflow_are_checked_against_the_parent(tmp_path):
    report = check(tmp_path, flow("""
        ship: {needs: [start], command: x, workspace: new, branch: fm/task}
        same: {needs: [start], workflow: sub.yaml, with: {branch: fm/task}}
        apart: {needs: [start], workflow: sub.yaml, with: {branch: "review/{{ start.task }}"}}
        each: {needs: [start], workflow: sub.yaml, for_each: '["a", "b"]', with: {branch: "fix/{{ item }}"}}
        every: {needs: [start], workflow: sub.yaml, for_each: '["a", "b"]', with: {branch: "all/{{ start.task }}"}}
    """), sub=SUB_BRANCH)
    assert_problems(report, [
        ("error", "nodes.every.with", "`every` runs sub.yaml once per item, and its `impl` would name the same branch"),
        ("error", "nodes.same", "`ship` and `same/impl` could name the same branch"),
    ])


# ---------- warnings ----------

BRANCHES = """
classify:
  needs: [start]
  instruction: classify
  output: {kind: {enum: [bug, feature]}}
  routes:
    - {when: "classify.kind == 'bug'", to: bugfix}
    - {to: feature}
bugfix: {instruction: fix}
feature: {instruction: build}
"""


def test_reading_a_node_that_might_not_have_run_warns_unless_checked(tmp_path):
    report = check(tmp_path, flow(BRANCHES + dedent("""
        open_pr:
          needs: [bugfix, feature]
          instruction: |
            {{ bugfix.summary }}
            {% if feature %}{{ feature.summary }}{% endif %}
            {{ classify.kind }}
    """)))
    assert_problems(report, [("warning", "nodes.open_pr.instruction", "`bugfix` might not have run")])


def test_joins_and_loops_do_not_warn(tmp_path):
    report = check(tmp_path, flow("""
        a: {needs: [start], command: x}
        b: {needs: [start], command: x}
        both: {needs: [a, b], command: "exit {{ a.exit_code + b.exit_code }}"}
        fix: {needs: [both], instruction: "{% if check %}{{ check.stdout }}{% endif %}"}
        check: {needs: [fix], command: x, routes: [{when: "check.exit_code != 0", to: fix, max: 2}, {to: end}]}
    """, end='out: "{{ check.stdout }}"'))
    assert found(report) == []


def test_reading_its_own_output_warns_but_not_its_workspace(tmp_path):
    report = check(tmp_path, flow("""
        a:
          needs: [start]
          instruction: "{{ a.summary }} {{ workspace.a.path }}"
          routes: [{when: "true", to: a, max: 1}, {to: end}]
    """))
    assert_problems(report, [("warning", "nodes.a.instruction", "`a` might not have run")])


def test_unquoted_agent_output_in_a_command_warns(tmp_path):
    report = check(tmp_path, flow("""
        a: {needs: [start], instruction: x, output: {branch: {type: string}, files: {type: array, items: {type: string}}}}
        raw: {needs: [a], command: "git checkout {{ a.branch }}"}
        quoted: {needs: [a], command: "git checkout {{ a.branch | quote }}"}
        env: {needs: [a], command: 'git checkout "$B"', env: {B: "{{ a.branch }}"}}
        loop: {needs: [a], command: "{% for f in a.files %}rm {{ f }}{% endfor %}"}
        input: {needs: [a], command: "{{ start.task }}"}
    """))
    assert_problems(report, [
        ("warning", "nodes.raw.command", "output from `a` into the shell unquoted"),
        ("warning", "nodes.loop.command", "output from `a` into the shell unquoted"),
    ])


def test_a_branch_named_from_agent_output_is_untrusted_in_a_command(tmp_path):
    report = check(tmp_path, flow("""
        plan: {needs: [start], instruction: x, output: {slug: {type: string}}}
        work: {needs: [plan], command: x, workspace: new, branch: "fm/{{ plan.slug }}"}
        mine: {needs: [start], command: x, workspace: new, branch: "own/{{ start.task }}"}
        raw: {needs: [work], command: "echo shipping {{ workspace.work.branch }}"}
        shared: {needs: [work], command: x, workspace: work}
        through: {needs: [shared], command: "echo {{ workspace.shared.branch }}"}
        quoted: {needs: [work], command: "echo {{ workspace.work.branch | quote }}"}
        path: {needs: [work], command: "cd {{ workspace.work.path }}"}
        input: {needs: [mine], command: "echo {{ workspace.mine.branch }}"}
    """))
    assert_problems(report, [
        ("warning", "nodes.raw.command", "output from `workspace.work` into the shell unquoted"),
        ("warning", "nodes.through.command", "output from `workspace.shared` into the shell unquoted"),
    ])


def test_an_input_filled_from_agent_output_is_untrusted_in_the_sub_workflow(tmp_path):
    report = check(
        tmp_path,
        flow("""
            a: {needs: [start], instruction: x}
            b: {needs: [a], workflow: sub.yaml, with: {task: "{{ start.task }}", cmd: "{{ a.summary }}"}}
        """),
        sub=SUB,
    )
    assert_problems(report, [("warning", "nodes.run_it.command", "output from `start.cmd` into the shell unquoted")])
    assert report.problems[0].file.name == "sub.yaml"


def test_a_full_access_agent_reading_web_output_warns(tmp_path):
    report = check(tmp_path, flow("""
        search: {needs: [start], instruction: look it up, web: true}
        act: {needs: [search], instruction: "{{ search.summary }}", access: full}
        safe: {needs: [search], instruction: "{{ search.summary }}"}
    """))
    assert_problems(report, [("warning", "nodes.act.instruction", "reads output from `search`, which has `web: true`")])


def test_edit_nodes_that_could_run_together_in_one_workspace_warn(tmp_path):
    report = check(tmp_path, flow("""
        a: {needs: [start], instruction: x, access: edit}
        b: {needs: [start], instruction: x, access: edit}
        c: {needs: [a, b], instruction: x, access: edit}
        d: {needs: [start], instruction: x, access: edit, workspace: new}
        e: {needs: [c], for_each: "[1, 2]", instruction: x, access: edit}
    """))
    assert_problems(report, [
        ("warning", "nodes.b", "`a` and `b` both edit the repo workspace"),
        ("warning", "nodes.e", "items run in parallel in the same workspace"),
    ])


def test_for_each_over_a_list_without_max_items_warns(tmp_path):
    report = check(tmp_path, flow("""
        plan: {needs: [start], instruction: x, output: {tasks: {type: array, items: {type: string}}}}
        build: {needs: [plan], for_each: plan.tasks, command: "echo {{ item | quote }}"}
    """))
    assert_problems(report, [("warning", "nodes.build.for_each", "`plan.tasks` has no `maxItems`")])


def test_cost_limits_with_harnesses_that_do_not_report_cost_warn(tmp_path):
    report = check(tmp_path, flow(
        "a: {needs: [start], instruction: x, harness: codex}",
        top="limits: {cost_usd: 5}\n",
        end='spent: {type: number, value: "{{ run.cost_usd }}"}',
    ))
    assert_problems(report, [("warning", "limits.cost_usd", "codex doesn't report cost in dollars")])


def test_cursor_reports_tokens_but_no_cost_and_takes_no_effort(tmp_path, fake_keyring):
    fake_keyring.set_password(*cursor.KEYCHAIN, "key_test")
    report = check(tmp_path, flow(
        "a: {needs: [start], instruction: x, harness: cursor, model: gpt-5.6-sol-max-fast, effort: high}",
        top="limits: {cost_usd: 5, tokens: 100000}\n",
        end='used: {type: integer, value: "{{ run.tokens }}"}',
    ))
    assert_problems(report, [
        ("warning", "nodes.a", "cursor doesn't use `effort`: a Cursor model id carries its own"),
        ("warning", "limits.cost_usd", "cursor doesn't report cost in dollars"),
    ])


def test_a_token_limit_warns_when_cursor_has_no_api_key_and_runs_through_acp(tmp_path, fake_keyring):
    text = flow("a: {needs: [start], instruction: x, harness: cursor, model: composer-2.5}", top="limits: {tokens: 100000}\n")
    assert_problems(check(tmp_path, text), [("warning", "limits.tokens", "or with no Cursor API key set")])
    fake_keyring.set_password(*cursor.KEYCHAIN, "key_test")
    assert check(tmp_path, text).problems == []  # with a key, read-only runs on the SDK, which counts tokens


def test_a_template_reading_run_tokens_warns_when_cursor_runs_an_edit_node_through_acp(tmp_path):
    report = check(tmp_path, flow(
        "a: {needs: [start], instruction: x, access: edit, harness: [claude-code, cursor], model: {claude-code: opus, cursor: composer-2.5}}",
        end='used: {type: integer, value: "{{ run.tokens }}"}',
    ))
    assert_problems(report, [("warning", "end.output.used.value", "cursor doesn't report tokens through cursor-agent's ACP")])


def test_a_token_limit_warns_when_every_cursor_node_runs_through_acp(tmp_path, monkeypatch):
    monkeypatch.setenv("WF_CURSOR_RUNTIME", "acp")
    report = check(tmp_path, flow("a: {needs: [start], instruction: x, harness: cursor, model: composer-2.5}",
                                  top="limits: {tokens: 100000}\n"))
    assert_problems(report, [("warning", "limits.tokens", "every node when cursor.runtime is acp")])


def test_project_instructions_takes_a_boolean_on_a_node_or_in_defaults(tmp_path):
    report = check(tmp_path, flow('a: {needs: [start], instruction: x, project_instructions: "no"}'))
    assert_problems(report, [("error", "nodes.a.project_instructions", "Input should be a valid boolean")])
    text = flow("a: {needs: [start], instruction: x}").replace("defaults:\n", "defaults:\n  project_instructions: false\n")
    assert_problems(check(tmp_path, text), [])  # Claude Code keeps every instruction file out


def test_project_instructions_off_warns_once_per_harness_that_keeps_a_file(tmp_path):
    codex = ("on codex, `project_instructions: false` doesn't keep out your global ~/.codex/AGENTS.md, "
             "or the skills in your home folder (~/.agents/skills, ~/.codex/skills)")
    report = check(tmp_path, flow("""
        a: {needs: [start], instruction: x, harness: codex, project_instructions: false}
        b: {needs: [start], instruction: x, harness: codex}
        c: {needs: [start], instruction: x, harness: codex, project_instructions: true}
        """))
    assert_problems(report, [("warning", "nodes.a.project_instructions", codex)])  # b and c load them: nothing to say
    text = flow("""
        a: {needs: [start], instruction: x, harness: codex}
        b: {needs: [start], instruction: x, harness: [claude-code, codex], model: {claude-code: opus, codex: gpt-6}}
        c: {needs: [start], instruction: x}
        """).replace("defaults:\n", "defaults:\n  project_instructions: false\n")
    assert_problems(check(tmp_path, text), [("warning", "defaults.project_instructions", codex)])


def test_project_instructions_off_warns_about_every_harness_a_template_could_pick(tmp_path):
    report = check(tmp_path, flow(
        'a: {needs: [start], instruction: x, access: edit, harness: "{{ start.task }}", project_instructions: false, '
        'model: {cursor: composer-2.5}}'))
    assert [(p.level, p.where) for p in report.problems] == [("warning", "nodes.a.project_instructions")] * len(
        harnesses.KEPT_INSTRUCTIONS)
    assert [p.message.split(",")[0] for p in report.problems] == ["on codex", "on cursor through cursor-agent"]
    assert "your Cursor account's User Rules, the skills in your home folder" in report.problems[1].message


def test_project_instructions_off_on_cursors_sdk_keeps_nothing_out_and_gets_no_warning(tmp_path, monkeypatch, fake_keyring):
    fake_keyring.set_password(*cursor.KEYCHAIN, "key_test")
    text = flow("a: {needs: [start], instruction: x, harness: cursor, model: composer-2.5, project_instructions: false}")
    assert check(tmp_path, text).problems == []  # read-only runs on the SDK, which loads no settings layer
    monkeypatch.setenv("WF_CURSOR_RUNTIME", "acp")
    assert [p.message.split(",")[0] for p in check(tmp_path, text).problems] == ["on cursor through cursor-agent"]


# ---------- malformed files are reported, never crash ----------


@pytest.mark.parametrize(
    "text, where, part",
    [
        (flow("a: {needs: [start], instruction: x, output: {f: {type: {a: b}}}}"), "nodes.a.output.f", "isn't a JSON Schema type"),
        (flow("a: {needs: [start], for_each: start.task, command: x}", inputs="task: {type: 5}"), "start.input.task", "isn't a JSON Schema type"),
        (flow("a: {needs: [start], for_each: start.task, command: x}", inputs="task: {enum: 5}"), "start.input.task", "invalid schema"),
        ("version: 1\nname: t\nstart: {}\nnodes:\n  ? [a, b]\n  : {command: x}\nend: {}\n", "line 5", "keys must be simple values"),
        (flow("a: {needs: [start], command: x}", inputs="x: {$ref: node, default: {}}", top="types:\n  node: {child: {$ref: node}}\n"),
         "types.node", "refers to itself"),
        (flow("a: {needs: [start], command: x}", end='s: {type: boolean, value: "{{ session.continued }}"}'),
         "end.output.s.value", "`session` isn't available in end.output"),
    ],
)
def test_malformed_files_are_reported_not_crashed_on(tmp_path, text, where, part):
    report = check(tmp_path, text)
    assert any(p.where == where and part in p.message for p in report.problems), found(report)


def test_an_empty_enum_is_not_a_crash(tmp_path):
    report = check(tmp_path, flow("a: {needs: [start], for_each: start.task, command: x}", inputs="task: {enum: []}"))
    assert report.errors == []


def test_is_defined_does_not_count_as_a_check_for_none(tmp_path):
    report = check(tmp_path, flow(BRANCHES + dedent("""
        report:
          needs: [bugfix, feature]
          instruction: "{% if bugfix is defined %}{{ bugfix.summary }}{% endif %}"
    """)))
    assert_problems(report, [("warning", "nodes.report.instruction", "`bugfix` might not have run")])


def test_numbers_derived_from_output_are_safe_in_commands(tmp_path):
    report = check(tmp_path, flow("""
        plan: {needs: [start], instruction: x, output: {tasks: {type: array, maxItems: 3, items: {type: string}}}}
        count: {needs: [plan], command: "echo {{ plan.tasks | length }} tasks"}
    """))
    assert found(report) == []


def test_a_workflow_node_in_a_new_worktree_cannot_run_one_that_makes_worktrees(tmp_path):
    inner = flow("impl: {needs: [start], instruction: x, access: edit, workspace: new}")
    report = check(tmp_path, flow("w: {needs: [start], workflow: inner.yaml, with: {task: x}, workspace: new}"), inner=inner)
    assert_problems(report, [("error", "nodes.w.workspace", "creates worktrees of its own")])


def test_for_each_reading_item_is_an_error_not_a_crash(tmp_path):
    report = check(tmp_path, flow("a: {needs: [start], for_each: item.subtasks, command: x}"))
    assert_problems(report, [("error", "nodes.a.for_each", "`item` is only defined inside a for_each node")])


def test_templates_in_defaults_env_are_checked(tmp_path):
    report = check(tmp_path, flow("a: {needs: [start], command: x}", top="defaults:\n  env: {X: '{{ nope }}'}\n").replace(
        "defaults:\n  harness: claude-code\n", "", 1).replace("defaults:\n  env:", "defaults:\n  harness: claude-code\n  env:"))
    assert_problems(report, [("error", "nodes.a.env.X", "unknown name `nope`")])


def test_editors_sharing_a_temp_workspace_warn(tmp_path):
    report = check(tmp_path, flow("""
        scratch: {needs: [start], command: x, workspace: temp}
        b: {needs: [scratch], instruction: x, access: edit, workspace: scratch}
        c: {needs: [scratch], instruction: x, access: edit, workspace: scratch}
        d: {needs: [start], for_each: "[1, 2]", instruction: x, access: edit, workspace: temp}
    """))
    assert_problems(report, [("warning", "nodes.c", "`b` and `c` both edit the `scratch` workspace")])


def test_an_unknown_type_reached_through_another_type_is_reported(tmp_path):
    report = check(tmp_path, flow("a: {needs: [start], command: x}", top="types:\n  a: {x: {$ref: b}}\n  b: {y: {$ref: nope}}\n"))
    assert_problems(report, [("error", "types.b.y", "unknown type `nope`")])


def test_field_paths_through_boolean_sub_schemas_do_not_crash(tmp_path):
    report = check(tmp_path, flow("""
        a: {needs: [start], instruction: x, output: {xs: {type: array, items: true}}}
        b: {needs: [a], instruction: "{{ a.xs[0].name }}"}
    """))
    assert report.errors == []


def test_a_file_that_is_not_utf8_is_reported(tmp_path):
    path = tmp_path / "main.yaml"
    path.write_bytes(flow("a: {needs: [start], command: x}").replace("x}", "caf\xe9}").encode("latin-1"))
    report = validate.check(path)
    assert [p.message for p in report.errors][0].startswith("the file isn't valid UTF-8")


def test_secret_env_names_only_what_env_sets(tmp_path):
    report = check(tmp_path, flow("""
        a: {needs: [start], command: x, env: {TOKEN: "{{ start.task }}"}, secret_env: [TOKEN, TOKN]}
        b: {needs: [start], command: y, secret_env: [TOKEN]}
    """, top="defaults:\n  secret_env: [API_KEY]\n").replace(
        "defaults:\n  harness: claude-code\n", "", 1).replace("defaults:\n  secret_env:", "defaults:\n  harness: claude-code\n  secret_env:"))
    assert_problems(report, [
        ("error", "defaults.secret_env", "`API_KEY` isn't set in any command node's `env`"),
        ("error", "nodes.a.secret_env", "`TOKN` isn't set in `env` (did you mean `TOKEN`?)"),
        ("error", "nodes.b.secret_env", "`TOKEN` isn't set in `env`"),
    ])
