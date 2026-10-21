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
    "{% for r in results.b %}{% if r.output %}{{ r.output.done }}{% endif %}{% endfor %}",
    "{% for r in results.b %}{% if not r.output %}x{% else %}{{ r.output.done }}{% endif %}{% endfor %}",
    "{% for r in results.b %}{% if r.output is not none %}{{ r.output.done }}{% endif %}{% endfor %}",
    "{% for r in results.b %}{{ r.output.done if r.output else 'x' }}{% endfor %}",
    "{% for r in results.b %}{{ r.output and r.output.done }}{% endfor %}",
    "{% for r in results.b if r.output %}{{ r.output.done }}{% endfor %}",
    "{% for r in results.b | selectattr('output') %}{{ r.output.done }}{% endfor %}",
    "{% for r in results.b | rejectattr('output', 'none') | list %}{{ r.output.done }}{% endfor %}",
])
def test_a_check_guards_the_path_it_reads_also_through_a_loop_variable(source):
    read = read_of(source)
    assert templates.guard("results", read.path[:3]) in read.guarded  # results.b[1].output or results.b[*].output


@pytest.mark.parametrize("source", [
    "{% for r in results.b %}{{ r.output.done }}{% endfor %}",
    "{% if results.b[0].output %}{{ results.b[1].output.done }}{% endif %}",  # another item
    "{% for r in results.b %}{% if r.index %}{{ r.output.done }}{% endif %}{% endfor %}",
    "{% for r in results.b if r %}{{ r.output.done }}{% endfor %}",
    "{% for r in results.b | selectattr('output', 'defined') %}{{ r.output.done }}{% endfor %}",  # set, but maybe none
    "{% for r in results.b %}{% if r.output %}x{% endif %}{{ r.output.done }}{% endfor %}",  # after the check
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
