# wf workflow file reference

The condensed format. `wf validate` is the final word on what a file means;
`wf examples` copies two complete workflows to read or start from.

## Top level

```yaml
version: 1                     # always 1
name: my-flow                  # lowercase letters, digits, -; used in run ids and branch names
description: One line.
defaults:                      # every node inherits these; a node's own value wins
  harness: claude-code         # claude-code | codex, or a list to fall back through
  harnesses:
    claude-code: {model: claude-opus-5-5, effort: high}
    codex: {}                  # Codex: the default model from its model list
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
`budget_usd`. Commands also: `env` (template values), `check` (true: non-zero
exit fails the node; false: route on `exit_code`).

`harness: [codex, claude-code]` falls back in order: each harness gets 1 +
`retry` attempts, then the next takes over; a harness that is unavailable
(usage limit, not logged in) hands over at once.

`web: true` turns on web search. On Codex it also gives an `edit` agent network
access, which it has none of otherwise: a Codex step that installs packages
(`uv lock`, `npm install`) needs `web: true`. Dry runs can't catch a missing one.

Access: `read-only` reads and searches but changes nothing: on Claude Code it
gets a read-only `git` tool (diff, log, show, blame, status); on Codex it runs
commands, git included, in Codex's read-only sandbox. `edit` edits files and
runs commands in its workspace; `full` has no limits (git merge, rebase, pushes
need it).

A human node pauses the run. At a terminal wf asks the questions; otherwise the
run waits (`wf status <run-id>` shows them) until `wf answer <run-id>
<answers.json>` or `wf resume <run-id>` answers them.

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
result. It's named on the node's first pass and kept after that. An empty name,
and a branch that exists already and that the run didn't make, are refused. On a `for_each` node it
must use `item` or `index`, and no two nodes may be able to render the same name:
give it to the one node that makes the deliverable, and run the others in its
workspace (`workspace: <that node>`).

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
it got, and `wf resume <run-id> --limit cost_usd=20` continues it with a bigger
budget (`NAME=none` removes a limit; the new limit holds for the rest of the
run). It carries on where it stopped, sub-workflows included, and then writes
its end output as usual. `wf run <file> --limit ...` starts with a different
limit. Codex reports no dollar cost, so `cost_usd` counts Claude Code only: cap
Codex with `tokens` (validate warns about this).

One cap also holds on the agent sessions live at once, across all runs that use
the same `~/.wf` (spec: "Agent slots"):

| Condition | Effect | Source line | Conformance assertion |
|---|---|---|---|
| `max_agents: N` in `~/.wf/config.yaml`, or `WF_MAX_AGENTS=N`, which wins | At most N agent attempts hold a slot at once. | `workflow/capacity.py:60` | `test_two_runs_in_separate_processes_share_the_cap` |
| An attempt finds every slot taken | It waits. It keeps its retries, and its `timeout` starts once it has a slot. | `workflow/nodes.py:211` | `test_a_waiting_attempt_keeps_its_retries_and_its_timeout_starts_with_its_slot` |
| A run limit is reached while it waits | The attempt doesn't start, and the run pauses at the limit. | `workflow/nodes.py:410` | `test_a_limit_reached_while_waiting_ends_the_wait_and_the_attempt_never_starts` |

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
fails continues with `wf resume`, or goes with `wf discard <run-id>`. Its
`_fail` and `_tokens` show up in `wf report` like real ones: `--real` leaves
dry runs out.

## Commands

`wf init`, `examples`, `validate`, `graph`, `run [--detach] [--dry-run] [--limit
NAME=VALUE]`, `resume [--limit NAME=VALUE]`, `answer`, `wait`, `status`, `list` (newest first), `report [--real |
--dry-runs] [--version V]`, `discard [<run-id>... | --workflow NAME
[--dry-runs]]`. Exit codes: 0 succeeded, 1 failed or limit, 2 waiting for a
person, 3 (`wf wait`) nothing new, 130 Ctrl-C. The commands that make or find
runs take `--runs-dir DIR` (or `WF_RUNS_DIR`): run folders then go to
`DIR/<repo key>/` instead of `.wf/runs/`, and nothing is written in the repo.
