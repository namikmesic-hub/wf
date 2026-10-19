# Workflow patterns

Shapes that work, each with when to use it and the mistake it invites. Combine
them: a fan-out's per-item work is usually a review loop in a sub-workflow.

## Typed task

One agent, one contract. The base of everything else.

```yaml
nodes:
  diagnose:
    needs: [start]
    instruction: |
      Find the cause of this bug. Don't change any files.
      {{ start.report }}
    output:
      cause: {type: string, description: One or two sentences}
      files: {type: array, maxItems: 10, items: {type: string}, description: Files involved}
      confidence: {enum: [low, medium, high]}
```

Use enums and `maxItems` wherever the next step branches or loops on the value.
Mistake: a free-text field the next step must parse; give it structure instead.

## Review loop

Make, check with a command, judge with a second agent, go round until clean.
The complete file is `implement.yaml` from `wf examples`.

```yaml
  implement:
    needs: [explore]
    access: edit
    workspace: new
    instruction: |
      {{ start.task }}
      {% if review and review.blocking_issues %}
      Fix every one of these:
      {% for issue in review.blocking_issues %}
      - {{ issue }}
      {% endfor %}
      {% endif %}
    output: {summary: {type: string}, notes: {type: string, description: What you tried and dropped}}
  tests: {needs: [implement], command: "{{ start.test_command }}", workspace: implement, check: false}
  review:
    needs: [tests]
    harness: codex                    # a different reviewer than the author
    workspace: implement
    instruction: Review `git diff {{ workspace.implement.base }}...HEAD`. Tests exited {{ tests.exit_code }}.
    output: {blocking_issues: {type: array, items: {type: string}}}
    routes:
      - {when: "tests.exit_code == 0 and not review.blocking_issues", to: end}
      - {to: implement, max: 3}
      - {to: end}
```

Feed every pass what the last one learned: the reviewer's issues, the test
output, `history.implement` notes, and a pointer to `git log`/`git diff` (the
worktree keeps earlier passes' commits). Mistakes: a reviewer that may only
say "looks fine" (ask for blocking issues as a list, nothing else); `max` set
high to hide a reviewer that never converges.

## Plan, approve, fan out, merge

Split work, let a person approve the plan, build each part in its own worktree
in parallel, merge what passed. The complete file is `feature.yaml` from
`wf examples`.

```yaml
  plan: {needs: [start], instruction: "Split into independent tasks: {{ start.feature }}",
         output: {tasks: {type: array, maxItems: 5, items: {$ref: task}}}}
  approve:
    needs: [plan]
    human: "Build these?{% for t in plan.tasks %}\n- {{ t.title }}{% endfor %}"
    output: {approved: {type: boolean}}
    routes: [{when: approve.approved, to: build}, {to: end}]
  build: {for_each: plan.tasks, workflow: implement.yaml, with: {task: "{{ item.title }}: {{ item.details }}"},
          max_parallel: 3, on_item_error: continue,
          routes: [{when: "build | selectattr('passed') | list", to: merge}, {to: end}]}
  merge: {access: full, workspace: new, instruction: "Merge: {% for b in build if b.passed %}{{ b.branch }} {% endfor %}"}
```

Mistakes: tasks that touch the same files (they conflict at merge; ask the
planner for independent tasks); no `maxItems` on the list (a long plan fails
the node).

## Router

Classify, then take one of several branches; a join after them runs once.

```yaml
  classify:
    needs: [start]
    output: {kind: {enum: [bug, feature, question]}}
    routes:
      - {when: "classify.kind == 'bug'", to: fix}
      - {when: "classify.kind == 'feature'", to: build}
      - {to: answer}
  fix: {instruction: ...}
  build: {instruction: ...}
  answer: {instruction: ...}
  report:
    needs: [fix, build, answer]       # runs after whichever one ran
    instruction: "{% if fix %}{{ fix.summary }}{% elif build %}{{ build.summary }}{% elif answer %}{{ answer.summary }}{% endif %}"
```

The branches read as `none` when skipped, so guard each one by name, even the
last (`{% elif answer %}`, not a bare `{% else %}`). Mistake: reading
`fix.summary` unguarded in the join (validate warns about it).

## Best of N

The same task on several harnesses or models, then a judge picks.

```yaml
  attempt:
    needs: [start]
    for_each: '["claude-code", "codex"]'
    harness: "{{ item }}"
    model: {claude-code: claude-opus-5-5, codex: gpt-6-sol}
    workspace: new
    access: edit
    instruction: "{{ start.task }}"
  judge:
    needs: [attempt]
    instruction: "Pick the best: {% for a in attempt %}{{ workspace.attempt[loop.index0].branch }}: {{ a.summary }}\n{% endfor %}"
    output: {winner: {type: integer, minimum: 0}, why: {type: string}}
```

Use it when quality varies a lot between runs and a judge can compare.

## Gate first

Run a cheap deterministic check before spending agent time, and route on it.

```yaml
  lint: {needs: [start], command: "make lint", check: false,
         routes: [{when: "lint.exit_code == 0", to: review}, {to: fix_lint}]}
```

## Ask only when it matters

A person is the slowest step. Put human nodes where a wrong choice is expensive
(approving a plan, merging, spending), and let a route skip them otherwise:

```yaml
  routes:
    - {when: "plan.tasks | length <= 2", to: build}   # small plans go straight on
    - {to: approve}
```
