"""templates: sandboxed Jinja rendering, expressions, and the static reference finder."""

import pytest

from workflow import templates
from workflow.schemas import EACH
from workflow.templates import Ref, TemplateError

VARS = {
    "start": {"task": "Fix the bug", "n": 3},
    "explore": {"plan": "Do it", "files": ["a.py", "b c.py"]},
    "tests": None,
    "review": {"blocking_issues": []},
}


# ---------- rendering ----------


def test_render_fills_in_variables():
    assert templates.render("{{ start.task }}: {{ explore.files | join(', ') }}", VARS) == "Fix the bug: a.py, b c.py"


@pytest.mark.parametrize(
    "source, message",
    [
        ("{{ nope }}", "'nope' is undefined"),
        ("{{ start.taks }}", "has no attribute 'taks'"),
        ("{{ tests.exit_code }}", "has no attribute 'exit_code'"),  # a field of none
    ],
)
def test_unknown_names_and_fields_are_errors_not_empty_strings(source, message):
    with pytest.raises(TemplateError, match=message):
        templates.render(source, VARS)


def test_tags_on_their_own_line_leave_no_blank_lines():
    source = "a\n{% if review %}\n  b\n{% endif %}\nc\n"
    assert templates.render(source, VARS) == "a\n  b\nc\n"


def test_quote_filter_quotes_for_the_shell():
    assert templates.render("git add {{ explore.files | quote }}", VARS) == "git add a.py 'b c.py'"
    assert templates.render("echo {{ x | quote }}", {"x": "it's; rm -rf /"}) == "echo 'it'\"'\"'s; rm -rf /'"
    assert templates.render("echo {{ x | quote }}", {"x": None}) == "echo ''"


def test_tojson_is_plain_json():
    assert templates.render("{{ x | tojson }}", {"x": {"a": "<b>"}}) == '{"a": "<b>"}'


def test_output_fields_win_over_dict_methods():
    assert templates.render("{{ x.items | join(',') }}", {"x": {"items": ["p", "q"]}}) == "p,q"


def test_the_sandbox_blocks_internals_and_mutation():
    with pytest.raises(TemplateError):
        templates.render("{{ x.__class__.__mro__ }}", {"x": "s"})
    with pytest.raises(TemplateError):
        templates.render("{{ x.append(1) }}", {"x": []})


def test_a_single_expression_keeps_its_type():
    assert templates.render_value("{{ explore.files }}", VARS) == ["a.py", "b c.py"]
    assert templates.render_value("{{ start.n > 2 }}", VARS) is True
    assert templates.render_value(" {{ tests }} ", VARS) is None
    assert templates.render_value("{{ start.n }} files", VARS) == "3 files"
    assert templates.render_value("{{ start.n }}{{ start.n }}", VARS) == "33"


def test_evaluate_runs_an_expression():
    assert templates.evaluate("start.n == 3 and not review.blocking_issues", VARS) is True
    assert templates.evaluate("explore.files | length", VARS) == 2
    with pytest.raises(TemplateError, match="has no attribute"):
        templates.evaluate("tests.exit_code == 0", VARS)


def test_syntax_errors_name_the_line():
    with pytest.raises(TemplateError, match="line 2"):
        templates.render("ok\n{% if %}", VARS)


# ---------- finding references (for validate.py) ----------


def refs(source, **kw):
    return [(r.name, r.path) for r in templates.references(source, **kw)]


def find(source, name, **kw) -> Ref:
    return next(r for r in templates.references(source, **kw) if r.name == name)


def test_references_are_names_with_field_paths():
    assert refs("{{ explore.plan }} {{ start['task'] }} {{ plan.tasks[0].title }}") == [
        ("explore", ("plan",)),
        ("start", ("task",)),
        ("plan", ("tasks", 0, "title")),
    ]


def test_loop_variables_resolve_to_the_list_they_loop_over():
    source = "{% for t in plan.tasks %}{{ t.title }}{% endfor %}{% for b in build if b.passed %}{{ b.branch }}{% endfor %}"
    assert refs(source) == [
        ("plan", ("tasks",)),
        ("plan", ("tasks", EACH, "title")),
        ("build", ()),
        ("build", (EACH, "passed")),
        ("build", (EACH, "branch")),
    ]


def test_filters_that_keep_elements_keep_the_path():
    assert refs("{% for b in build | selectattr('passed') | list %}{{ b.summary }}{% endfor %}") == [
        ("build", ()),
        ("build", (EACH, "summary")),
    ]


@pytest.mark.parametrize("template", [
    "{{ results.b[1].output.done }}",
    "{{ results.b[0].output.done }}",
    "{% if results.b[1].output %}{{ results.b[1].output.done }}{% endif %}",
    "{% if not results.b[1].output %}x{% else %}{{ results.b[1].output.done }}{% endif %}",
    "{{ results.b[1].output.done if results.b[1].output else 'x' }}",
    "{{ results.b[1].output and results.b[1].output.done }}",
    "{% if results.b[1].output is not none %}{{ results.b[1].output.done }}{% endif %}",
    "{% if results.b[0].output %}{{ results.b[1].output.done }}{% endif %}",  # another item
    "{% if results.b[-1].output %}{{ results.b[-1].output.done }}{% endif %}",
    "{{ results.b[1].output }}",  # the output itself, and no field of it
])
def test_a_direct_read_is_unguarded_exactly_where_a_failed_item_fails_the_render(template):
    succeeded = {"index": 0, "item": "a", "output": {"done": True}, "harness": None, "model": None}
    failed = {"index": 1, "item": "b", "output": None, "harness": None, "model": None}
    assert "Undefined" not in templates.render(template, {"results": {"b": [succeeded, succeeded]}})
    broken = False
    for entries in ([succeeded, failed], [failed, succeeded], [failed, failed]):
        try:
            broken |= "Undefined" in templates.render(template, {"results": {"b": entries}})
        except TemplateError:
            broken = True
    reads = [r for r in templates.references(template) if r.name == "results" and len(r.path) > 3]
    assert any(templates.guard("results", r.path[:3]) not in r.guarded for r in reads) is broken


def test_a_read_through_a_name_that_the_template_defines_is_marked_local():
    source = ("{{ results.b[0].output.done }}{% for r in results.b %}{{ r.output.done }}{% endfor %}"
              "{% set first = results.b[0] %}{{ first.output.done }}{% with o = results.b[1].output %}{{ o.done }}{% endwith %}")
    assert [r.local for r in templates.references(source) if r.path and r.path[-1] == "done"] == [False, True, True, True]


def test_a_position_that_the_template_computes_is_kept_apart_from_a_loop():
    assert refs("{{ results.b[-1].item }} {{ results.b[index].item }} {{ results.b[start.pos + 1].item }}") == [
        ("results", ("b", -1, "item")), ("results", ("b", templates.At("index"), "item")), ("index", ()),
        ("results", ("b", templates.At("start.pos + 1"), "item")), ("start", ("pos",))]
    (read,) = [r for r in templates.references("{{ results.b[start.pos | int(0)].item }}") if r.name == "results"]
    assert templates.guard(read.name, read.path) == "results.b[start.pos | int(0)].item"


@pytest.mark.parametrize("source, guarded", [
    ("{% if results.b %}{{ results.b[0].index }}{% endif %}", True),
    ("{% if results.b is not none %}{{ results.b[0].index }}{% endif %}", False),  # an empty list isn't none
    ("{% if results.b != none %}{{ results.b[0].index }}{% endif %}", False),
])
def test_a_none_check_of_a_list_that_is_never_none_proves_nothing(source, guarded):
    assert ("results.b" in read_of(source, "index").guarded) is guarded


def test_method_calls_are_not_fields():
    assert refs("{{ start.task.upper() }}") == [("start", ("task",))]


def test_loop_variables_from_unknown_sources_keep_the_source_name_without_a_path():
    assert refs("{% for k, v in x.items() %}{{ v }}{% endfor %}") == [("x", ()), ("x", None)]


def test_locals_and_loop_are_not_references():
    assert refs("{% set n = 2 %}{% for i in range(n) %}{{ loop.index }}{% endfor %}") == [("range", ())]


def test_guards_come_from_if_and_and_conditional_expressions():
    source = "{% if tests and tests.exit_code != 0 %}{{ tests.stdout }}{% endif %}{{ review.x }}"
    assert find(source, "tests").guarded == frozenset()  # the check itself
    guarded = [r for r in templates.references(source) if r.name == "tests" and r.path]
    assert all("tests" in r.guarded for r in guarded)
    assert "review" not in find(source, "review").guarded
    cond = templates.references("{{ open_pr.stdout | trim if open_pr else none }}")
    assert [(r.path, "open_pr" in r.guarded) for r in cond] == [((), False), (("stdout",), True)]
    negated = templates.references("{% if not review %}none{% else %}{{ review.x }}{% endif %}")
    assert any(r.path == ("x",) and "review" in r.guarded for r in negated)
    is_none = templates.references("{% if review is not none %}{{ review.x }}{% endif %}")
    assert any(r.path == ("x",) and "review" in r.guarded for r in is_none)


def read_of(source: str, last: str = "done") -> Ref:
    (read,) = [r for r in templates.references(source) if r.path and r.path[-1] == last]
    return read


@pytest.mark.parametrize("source", [
    "{% if results.b[1].output %}{{ results.b[1].output.done }}{% endif %}",
    "{% if not results.b[1].output %}x{% else %}{{ results.b[1].output.done }}{% endif %}",
    "{% if results.b[1].output is not none %}{{ results.b[1].output.done }}{% endif %}",
    "{{ results.b[1].output.done if results.b[1].output else 'x' }}",
    "{{ results.b[1].output and results.b[1].output.done }}",
    "{% if results.b[-1].output %}{{ results.b[-1].output.done }}{% endif %}",
    "{% if results.b[start.i].output %}{{ results.b[start.i].output.done }}{% endif %}",
])
def test_a_check_guards_the_path_it_reads(source):
    read = read_of(source)
    assert templates.guard("results", read.path[:3]) in read.guarded  # results.b[1].output, results.b[start.i].output


@pytest.mark.parametrize("source", [
    "{{ results.b[1].output.done }}",
    "{% if results.b[0].output %}{{ results.b[1].output.done }}{% endif %}",  # another item
    "{% if results.b[1].index %}{{ results.b[1].output.done }}{% endif %}",
    "{% if results.b[1].output %}x{% endif %}{{ results.b[1].output.done }}",  # after the check
    "{% if results.b[start.i].output %}{{ results.b[start.j].output.done }}{% endif %}",
])
def test_a_check_of_something_else_leaves_the_path_unguarded(source):
    read = read_of(source)
    assert templates.guard("results", read.path[:3]) not in read.guarded


def test_output_and_quoting_are_tracked_for_commands():
    out = find("git checkout {{ implement.branch | quote }}", "implement")
    assert (out.output, out.quoted) == (True, True)
    raw = find("echo {{ implement.summary }}", "implement")
    assert (raw.output, raw.quoted) == (True, False)
    tested = find("{% if implement.ok %}x{% endif %}", "implement")
    assert tested.output is False
    looped = templates.references("{% for f in explore.files %}rm {{ f }}{% endfor %}")
    assert [(r.path, r.output) for r in looped] == [(("files",), False), (("files", EACH), True)]


def test_expressions_have_no_output():
    ref = find("tests.exit_code == 0", "tests", expression=True)
    assert (ref.path, ref.output) == (("exit_code",), False)


def test_includes_are_refused():
    with pytest.raises(TemplateError, match="include"):
        templates.references("{% include 'x.txt' %}")


def test_pure_path_and_constant():
    assert templates.pure_path("{{ workspace.implement.branch }}") == ("workspace", ("implement", "branch"))
    assert templates.pure_path("{{ a.b | trim }}") is None
    assert templates.pure_path("x {{ a.b }}") is None
    assert templates.pure_path("plan.tasks", expression=True) == ("plan", ("tasks",))
    assert templates.constant('["claude-code", "codex"]') == (True, ["claude-code", "codex"])
    assert templates.constant("plan.tasks") == (False, None)
