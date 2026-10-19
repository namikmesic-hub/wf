# wf workflow file reference

The condensed format. `wf validate` is the final word on what a file means;
`wf examples` copies two complete workflows to read or start from.

## Top level

```yaml
version: 1                     # always 1
name: my-flow                  # lowercase letters, digits, -; used in run ids and branch names
description: One line.
defaults:                      # every node inherits these; a node's own value wins
  harness: claude-code         # claude-code | codex | cursor, or a list to fall back through
  harnesses:
    claude-code: {model: claude-opus-5-5, effort: high}
    codex: {}                  # Codex: the default model from its model list
    cursor: {model: gpt-5.6-sol-high}   # Cursor: a model is required, and its id carries the effort
  access: read-only            # read-only | edit | full
  timeout: 60m
  retry: 1
limits: {cost_usd: 20, tokens: 5000000, duration: 2h}   # whole run; see "Limits" below
types:                         # named schemas, used with $ref
  task: {title: {type: string}, details: {type: string}}
start:
  input:
    task: {type: string, description: What to do}
    test_command: {type: string, default: uv run pytest -q}
nodes: {...}
end:
  output:
    branch: "{{ workspace.implement.branch }}"           # short form: exactly one field reference
    passed: {type: boolean, value: "{{ tests.exit_code == 0 }}"}   # full form: schema + value
    summary: {type: string, value: "{{ implement.summary if implement else '' }}"}  # guarded (see Templates)
```

Unknown keys are errors.
A **field map** (`start.input`, `output`, `types` entries) maps field names to JSON Schema.
Every field is required unless `optional: true`, and a missing optional field reads as `none`.
Allowed keywords: type, properties, required, additionalProperties, items, enum, const, description, minimum, maximum, minLength, maxLength, minItems, maxItems, pattern, $ref (and `default` in start.input).
Nested objects are full JSON Schema or `$ref: <type>`.

In flow style (`{a: b, c: d}`) a comma ends a value: quote any description or
default with a comma or `: ` in it (`description: "The report, as written"`),
or write the field in block style.

## Nodes

Node ids: `^[a-z][a-z0-9_]*$`, not start, end, run, history, workspace, errors,
item, index, session. A node's kind is whichever of these keys it has:

| Kind | Key | Output |
|---|---|---|
| agent | `instruction` (template) | `output` field map; default `{summary: string}` |
| command | `command` (template, `bash -c`) | fixed: `exit_code`, `stdout`, `stderr` (last 20k chars), `log` |
| human | `human` (message template) | `output` field map, required |
| workflow | `workflow` (path), `with` (inputs) | the sub-workflow's end.output |

Every kind: `needs`, `routes`, `for_each`, `max_parallel` (4), `max_items` (20),
`on_item_error` (fail | continue), `workspace` (repo), `base`, `branch`, `timeout` (60m;
none for human and workflow), `retry` (1 for agents, else 0), `description`.

Agents also: `harness`, `model`, `effort` (maps by harness when `harness` is a
list), `access`, `web` (false), `session` (fresh | continue), `max_turns`,
`budget_usd`.
Commands also take `env` (template values) and `check` (true: a non-zero exit fails the node, false: route on `exit_code`).
`secret_env` names the `env` values that the run's trace never shows.

Agent nodes also take `project_instructions` (true).
`project_instructions: false`, on a node or in `defaults`, keeps the repo's own agent instruction files out of the node's sessions.
These are `CLAUDE.md`, `AGENTS.md`, Cursor's rules, the repo's skills, and Claude Code's project settings.
The instruction still reaches the agent.
Use it when a repo's files give agents a role that isn't the workflow's, as firstmate's `AGENTS.md` does.
A sub-workflow uses its own `defaults`, so set it in each sub-workflow too.
Some files stay, and `wf validate` warns about them.
On Codex, your global `~/.codex/AGENTS.md` and the skills in your home folder stay.
On Cursor, your account's User Rules, the skills in your home folder, and the repo's hooks stay.
On Cursor, semantic code search and lint diagnostics then don't cover the workspace.
The spec's "Instruction files" section has the rules for each harness.

Agent nodes also take `tools`, the wf tools the agent may use, on every harness.
For example `tools: [slack, granola, linear, notion, gmail]`.
An entry gets only the tool's read operations.
`{name: linear, write: true}` gets every operation the server offers, deleting and signing out included.
A node with `tools`, even `tools: []`, gets no other MCP server or connector, at any access level.
Each tool is set up once per machine with `wf tools <name> setup`, and `wf validate` warns about one that isn't.
For Slack and Linear, `setup --write` asks for the write scopes too, which a node with `write: true` needs.
docs/tools.md lists the reads.

A node with `tools` may not have a shell that reaches the Keychain, where every sign-in is stored, unless it sets `tools_with_shell: true`.

| Condition | Effect | Source line | Conformance assertion |
|---|---|---|---|
| A node with `tools` can run on Claude Code or Cursor at `edit` or `full`, or on Codex at `full`, and doesn't set `tools_with_shell: true` | `wf validate` errors, naming each such harness and the access level, so the run doesn't start. | `workflow/validate.py:631`, `workflow/harnesses/__init__.py:49` | `tests/test_tools.py::test_a_node_with_tools_and_a_shell_that_reaches_the_keychain_needs_the_opt_in`, `tests/test_tools.py::test_a_harness_list_names_each_harness_whose_shell_reaches_the_keychain` |
| A template picks such a harness at run time, which `wf validate` can't see, and the node doesn't set `tools_with_shell: true` | The run refuses that harness, and the node fails without a session. | `workflow/nodes.py:426` | `tests/test_tool_sessions.py::test_a_run_refuses_a_harness_whose_shell_reaches_the_keychain_without_the_opt_in` |
| The node sets `tools_with_shell: true` | `wf validate` warns, naming the node and its tools: its agent could read every stored sign-in. The run attaches the tools. | `workflow/validate.py:628` | `tests/test_tools.py::test_a_node_that_opts_in_gets_a_warning_naming_it_and_its_tools`, `tests/test_tool_sessions.py::test_a_run_lets_a_node_that_opted_in_have_tools_and_a_shell` |
| A node with `tools` is read-only on any harness, or on Codex at `edit` | It needs no opt-in: its commands can't reach the Keychain. | `workflow/harnesses/__init__.py:49` | `tests/test_tools.py::test_a_node_with_tools_and_a_shell_that_reaches_the_keychain_needs_the_opt_in` |
| A node has no tools, `tools: []` included | `wf validate` says nothing about its shell. | `workflow/validate.py:624` | `tests/test_tools.py::test_tools_with_shell_takes_a_boolean_and_matters_only_with_tools` |
| `tools_with_shell` isn't `true` or `false`, or is in `defaults` | `wf validate` errors: the field takes only a boolean, and only on the node, so that each such node says so itself. | `workflow/spec.py:193` | `tests/test_tools.py::test_tools_with_shell_takes_a_boolean_and_matters_only_with_tools`, `tests/test_tools.py::test_tools_with_shell_goes_on_the_node_not_in_defaults` |

`harness: [codex, claude-code]` falls back in order: each harness gets 1 +
`retry` attempts, then the next takes over; a harness that is unavailable
(usage limit, not logged in) hands over at once.

`web: true` turns on web search. On Codex it also gives an `edit` agent network
access, which it has none of otherwise: a Codex step that installs packages
(`uv lock`, `npm install`) needs `web: true`. Dry runs can't catch a missing one.

Access: `read-only` reads and searches but changes nothing.
On Claude Code and Cursor it gets a read-only `git` tool (diff, log, show, blame, status) and no shell.
On Codex it runs commands, git included, in Codex's read-only sandbox.
`edit` edits files and runs commands in its workspace.
`full` has no limits (git merge, rebase, pushes need it).

A human node pauses the run. At a terminal wf asks the questions; otherwise the
run waits (`wf status <run-id>` shows them) until `wf answer <run-id>
<answers.json>` or `wf retry <run-id>` answers them.

## When a node runs

- `start` finishes at once. A node that finishes makes pending either the targets
  of its chosen route (if it has `routes`) or every node that `needs` it.
- A pending node starts once all its `needs` are settled: not pending or
  running, and unreachable from anything pending or running. So a skipped
  branch doesn't block a join (it reads as `none`), and a join waits for a loop.
- Every node needs `needs` or a route pointing at it. First nodes: `needs: [start]`.
- A node with `routes` can't be in anyone's `needs`; route to the next node instead.

## Routes

```yaml
routes:
  - when: tests.exit_code == 0 and not review.blocking_issues
    to: end
  - to: implement
    max: 3          # taken at most 3 times per run: with the first pass, implement runs up to 4 times
  - to: end         # the last route: no when, no max
```

First route whose `when` is true (or absent) and whose `max` isn't used up
wins. `to` is a node, `end`, or a list of nodes. In `when`, the node's own name
is the output it just produced. Every loop must pass through a route with `max`.

## Templates and expressions

Sandboxed Jinja.
Templates (`{{ }}`, `{% %}`): instruction, human, command, env, with, base, branch, harness, model, effort, end values.
Expressions (no braces): `when`, `for_each`.
A template that is exactly one `{{ ... }}` keeps its type (a list stays a list).

| Name | Value |
|---|---|
| `start.<field>` | the input |
| `<node>` | its latest output, or `none` if it hasn't run; a list of item outputs for for_each |
| `history.<node>` | every output, oldest first (in its own instruction: earlier passes only) |
| `item`, `index` | the current item and its position (for_each node templates only) |
| `workspace.<node>` | `{path, branch, base}` |
| `errors.<node>` | failed items `{index, item, error}` (on_item_error: continue) |
| `run` | `{id, dir, cost_usd, tokens, elapsed_s, base}` |
| `session.continued` | true while rendering a continued session's instruction |

Reading a field of a node that hasn't run fails the run, so guard every node
some path skips, each by its own name (a guard on `review` doesn't cover
`tests`): `{% if review %}...{% endif %}` in text, `review.x if review else none`
in a single value. Validate warns about each unguarded read; at `end` it fails
the run on that path. Put agent output into a command through `env` and
`"$NAME"`, or `| quote`. Filters: Jinja's, plus `quote` and plain `tojson`. A
`{% %}` tag alone on its line leaves no blank line; one at the end of a line of
text takes that line's newline with it.

## for_each

`for_each: plan.tasks` runs the node once per item, `max_parallel` at a time;
its output is the list of item outputs in order. With `on_item_error:
continue`, failed items are left out of that list, so positions shift: their
item and error are in `errors.<node>`, and an item's output should say which
item it was (a name, not a position). Give the producing schema a
`maxItems`. `harness: "{{ item }}"` over `["claude-code", "codex"]` is best-of-N.
To run several steps per item, make the node a workflow node.

## Workspaces

`repo` (the folder wf runs in, as it is, uncommitted changes included; in a
sub-workflow, its node's workspace), `new` (a git worktree on branch
`wf/<run-id>/<path>`, from `base` or the commit the run started at), `<node id>`
(that node's latest workspace), `temp` (an empty folder).

`branch` names a `new` worktree's branch instead, as a template:
`branch: "{{ start.branch }}"` lets the caller pick the branch that carries the
result. Give it to the one node that makes the deliverable, and run the others
in its workspace (`workspace: <that node>`). Each rule below names the test that
fails when the code stops doing what the row says.

| Condition | Effect | Source line | Conformance assertion |
|---|---|---|---|
| A `workspace: new` node has `branch` | Its worktree's branch is the rendered name, stripped, instead of `wf/<run-id>/...` | `workflow/nodes.py:190` | `tests/test_workspaces.py::test_branch_names_the_worktrees_branch` |
| `branch` is on a node whose workspace isn't `new` | `wf validate` error | `workflow/validate.py:730` | `tests/test_validate.py::test_branch_names_a_new_worktree_once_per_item` |
| A later pass of the node | The name that the first pass gave stays | `workflow/nodes.py:188` | `tests/test_workspaces.py::test_a_loop_keeps_the_branch_its_first_pass_named` |
| `wf retry` continues the run, and its worktree is gone | It takes up the branch that `branches.json` records for the node | `workflow/nodes.py:207` | `tests/test_workspaces.py::test_a_retried_run_keeps_the_branch_it_made` |
| A `for_each` item, or an item of a `for_each` node that runs a sub-workflow, on a later pass | The name renders again; a name that differs from the one its index had fails the item | `workflow/nodes.py:189` | `tests/test_workspaces.py::test_an_item_keeps_its_branch_only_while_its_index_holds_the_same_item`, `tests/test_subworkflows.py::test_a_sub_workflow_item_fails_when_its_index_names_another_branch_on_a_later_pass` |
| The rendered name is empty, or only spaces | The node fails | `workflow/workspaces.py:119` | `tests/test_workspaces.py::test_a_branch_name_git_or_wf_would_refuse_fails_the_node` |
| The name is `wf`, or under `wf/`, in any case | The node fails | `workflow/workspaces.py:121` | `tests/test_workspaces.py::test_a_branch_name_git_or_wf_would_refuse_fails_the_node` |
| git doesn't allow the name as a branch name | The node fails | `workflow/workspaces.py:123` | `tests/test_workspaces.py::test_a_branch_name_git_or_wf_would_refuse_fails_the_node` |
| The fixed text of `branch` is empty or only spaces | `wf validate` error | `workflow/validate.py:741` | `tests/test_validate.py::test_branch_names_a_new_worktree_once_per_item` |
| The fixed text is `wf`, or starts with `wf/`, in any case | `wf validate` error | `workflow/validate.py:744` | `tests/test_validate.py::test_branch_cannot_be_wf_or_under_it_in_any_case` |
| The branch exists, and this run didn't make it for this node | The node fails | `workflow/nodes.py:221` | `tests/test_workspaces.py::test_a_branch_that_already_exists_is_refused` |
| A branch exists that git can't have beside the name: `fix/a` beside `fix/a/b` | The node fails | `workflow/workspaces.py:133` | `tests/test_workspaces.py::test_a_branch_git_cannot_have_beside_an_existing_one_is_refused` |
| `core.ignorecase` is on, and a branch exists with the name in another case | The node fails | `workflow/workspaces.py:132` | `tests/test_workspaces.py::test_where_git_ignores_case_a_name_in_other_case_is_the_same_branch` |
| Another node or item of the run has that name, or one that git can't have beside it | The node fails | `workflow/nodes.py:216` | `tests/test_workspaces.py::test_each_item_names_its_own_branch_and_two_items_cannot_share_one` |
| Two `branch` fields, in a file or the sub-workflows it runs, could name the same branch or one under the other | `wf validate` error | `workflow/validate.py:751` | `tests/test_validate.py::test_branches_git_cannot_have_side_by_side_are_refused`, `tests/test_validate.py::test_two_nodes_that_could_name_the_same_branch_are_refused` |
| Two `branch` fields differ only in case | `wf validate` error on every machine, also where git doesn't ignore case | `workflow/validate.py:992` | `tests/test_validate.py::test_branches_git_cannot_have_side_by_side_are_refused` |
| A sub-workflow's `branch` reads `{{ start.<input> }}` | `wf validate` compares it with the value that `with` gives | `workflow/validate.py:769` | `tests/test_validate.py::test_branches_named_in_a_sub_workflow_are_checked_against_the_parent` |
| A `for_each` node's `branch` doesn't use `item` or `index` | `wf validate` error | `workflow/validate.py:737` | `tests/test_validate.py::test_branch_names_a_new_worktree_once_per_item` |
| A `for_each` workflow node doesn't give `item` or `index` to a `branch` in its sub-workflow | `wf validate` error | `workflow/validate.py:782` | `tests/test_validate.py::test_branches_named_in_a_sub_workflow_are_checked_against_the_parent` |
| The run made the branch but stopped before it recorded it | The next pass records it and goes on | `workflow/nodes.py:212` | `tests/test_workspaces.py::test_a_run_that_stopped_before_recording_its_branch_takes_it_up_again` |
| `wf discard --branches`, and a worktree of the run has the named branch checked out | Deletes the branch, also when `branches.json` has no record of it | `workflow/runs.py:358` | `tests/test_workspaces.py::test_discard_deletes_the_branch_a_run_made_without_recording_it` |
| `wf discard --branches`, and the named branch isn't on the commit that the run left it on | The branch stays, and discard says why | `workflow/runs.py:359` | `tests/test_workspaces.py::test_discard_deletes_a_named_branch_only_while_it_is_the_runs_work` |
| A dry run succeeds | Its named branches are deleted | `workflow/runner.py:478` | `tests/test_workspaces.py::test_discard_keeps_or_deletes_named_branches_and_a_dry_run_deletes_them` |
| A `command` writes `workspace.<node>.branch` unquoted, and that node's `branch` reads node output | `wf validate` warning | `workflow/validate.py:485` | `tests/test_validate.py::test_a_branch_named_from_agent_output_is_untrusted_in_a_command` |

A `new` worktree has only what's committed: an uncommitted script or fixture a
step needs isn't there (`wf run` lists such files when it starts). Worktrees and
temp folders live outside the repo, in `~/.wf/` (`WF_HOME` moves it). The runner
commits what `edit`/`full` agents leave in worktrees it made (`edit` agents are
told not to commit), keeps worktrees across a node's passes, and removes them
when a run succeeds (branches stay). `wf discard <run-id>` deletes a run you
won't continue: folder, worktrees, temp folders (`--branches`: branches too).

## Limits

`limits` cap the whole run, sub-workflows included. Reaching one starts nothing
new and pauses the run with status `limit`: `wf report <run-id>` shows how far
it got, and `wf retry <run-id> --limit cost_usd=20` continues it with a bigger
budget (`NAME=none` removes a limit; the new limit holds for the rest of the
run). It carries on where it stopped, sub-workflows included, and then writes
its end output as usual. `wf run <file> --limit ...` starts with a different
limit.
Codex reports no dollar cost, so `cost_usd` counts Claude Code only: cap Codex with `tokens`.
Cursor reports neither, so only `duration` caps it (validate warns about both).

One cap also holds on the agent sessions live at once, across all runs that use
the same `~/.wf` (spec: "Agent slots"):

| Condition | Effect | Source line | Conformance assertion |
|---|---|---|---|
| `max_agents: N` in `~/.wf/config.yaml`, or `WF_MAX_AGENTS=N`, which wins | At most N agent attempts hold a slot at once. | `workflow/capacity.py:60` | `test_two_runs_in_separate_processes_share_the_cap` |
| An attempt finds every slot taken | It waits. It keeps its retries, and its `timeout` starts once it has a slot. | `workflow/nodes.py:288` | `test_a_waiting_attempt_keeps_its_retries_and_its_timeout_starts_with_its_slot` |
| A run limit is reached while it waits | The attempt doesn't start, and the run pauses at the limit. | `workflow/nodes.py:574` | `test_a_limit_reached_while_waiting_ends_the_wait_and_the_attempt_never_starts` |

## Sessions

`fresh` (default): each pass starts a new agent session; feedback reaches it
through the instruction (`history.<node>`, a reviewer's output). `continue`:
later passes fork the previous session and `session.continued` is true, so the
instruction can send only the new feedback; it falls back to fresh when the
harness or model changes or two passes in a row already continued.

## Dry runs

`wf run <file> --dry-run <script.yaml> [name=value ...]`. One list per node,
one entry per attempt, consumed in order:

```yaml
inputs: {task: Add a README}               # start inputs for this path (name=value on the command line wins)
agents:
  review:                                  # node id, or a path: build[1]/implement
    - {blocking_issues: [missing test]}    # the result it submits
    - {blocking_issues: []}
  implement:
    - {_files: {src/x.py: "..."}, summary: s, notes: n}   # writes files first
    - {_fail: crashed}                     # this attempt fails
    - {_unavailable: usage limit}          # this harness is out; the next one runs
    - {summary: s, notes: n, _tokens: 50000, _cost_usd: 1.5}   # pretend spending
humans:
  approve:
    - {approved: true}
commands:                                  # commands the script doesn't mention really run
  tests:
    - {exit_code: 1, stdout: "1 failed"}
```

A bare node id matches that node everywhere: in every item, and in every
sub-workflow that has a node by that name. A path matches one place. Items
running in parallel take a bare id's entries in whatever order they ask, so
key them by path when each item needs its own.

Results are checked against each node's contract before the run starts. An
agent node with no entries, or any listed node whose entries run out (or that
is listed by path for other items but not this one), fails at once, without
retrying. A command the script doesn't mention at all **really runs**, in its
real workspace: wf names those before it starts, so list every command that
deploys, pushes or costs money. Keep scripts in `<name>.dry/` next to the
workflow: they are its tests.

A dry run that succeeds removes its worktrees and branches; one that pauses or
fails continues with `wf retry`, or goes with `wf discard <run-id>`. Its
`_fail` and `_tokens` show up in `wf report` like real ones: `--real` leaves
dry runs out.

## Commands

`wf init`, `examples`, `validate`, `graph`, `run [--detach] [--dry-run] [--limit
NAME=VALUE]`, `retry [--limit NAME=VALUE]`, `answer`, `wait`, `status`, `list` (newest first), `report [--real |
--dry-runs] [--version V]`, `trace <run-id> [--node N] [--pass P] [--field F] [--var PATH]`
(what one run read, decided and did: improving.md), `discard [<run-id>... | --workflow NAME
[--dry-runs]]`. Exit codes: 0 succeeded, 1 failed or limit, 2 waiting for a
person, 3 (`wf wait`) nothing new, 130 Ctrl-C. The commands that make or find
runs take `--runs-dir DIR` (or `WF_RUNS_DIR`): run folders then go to
`DIR/<repo key>/` instead of `.wf/runs/`. Then the repo's working tree and
`.git/info/exclude` stay as they were: only `workspace: new` adds branches and
worktree records under `.git` (a runs folder inside the repo goes into the
exclude, as `.wf/` does). A blank value counts as no setting, and the
commands wf prints for the run include `--runs-dir`.
