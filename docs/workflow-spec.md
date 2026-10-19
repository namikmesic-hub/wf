# Workflow spec (v1 draft)

Status: draft, 2026-10-18. Nothing here is built yet.

A workflow is a YAML file describing a graph of steps. Most steps are tasks for a
coding agent (Claude Code, Codex or Cursor), given as a text instruction. Each step
declares the shape of its result as a schema. The runner turns that schema into a
tool, `submit_result`, and the agent finishes by calling it. Because results are
typed, later steps, conditions and fan-outs can rely on their fields, and
`wf validate` catches wiring mistakes before any agent runs.

## Terms

- **Workflow**: one YAML file. It has a `start` (its inputs), `nodes`, and an `end` (its outputs).
- **Node**: one step. There are four kinds: agent, command, human and workflow.
- **Harness**: the coding agent that runs an agent node: `claude-code`, `codex` or `cursor`.
- **Pass**: one run of a node. A node inside a loop has several passes.
- **Item**: one element of a `for_each` list. Each item runs separately.
- **Attempt**: one try at a pass or item. Retries and fallbacks add attempts.
- **Run**: one execution of a workflow, with its own folder under `.wf/runs/`.

## Example

`implement.yaml` makes a change, tests it, and loops until the tests pass and a reviewer
has no blocking issues (at most 3 extra rounds). A bigger example that uses this one as a
sub-workflow is in the appendix. Both are also saved as files in `workflow/examples/`
(`wf examples` copies them into a folder), and `wf validate` should pass on them.

```yaml
version: 1
name: implement
description: Make a change, test it, and loop until review passes.

defaults:
  harness: claude-code
  harnesses:
    claude-code: {model: claude-opus-5-5, effort: high}

start:
  input:
    task: {type: string, description: The change to make}
    test_command: {type: string, default: uv run pytest -q, description: Runs the tests}

nodes:
  explore:
    needs: [start]
    instruction: |
      Find the code involved in this task and plan the change. Don't edit anything.
      {{ start.task }}
    output:
      plan: {type: string}
      files: {type: array, items: {type: string}}

  implement:
    needs: [explore]
    access: edit
    workspace: new
    instruction: |
      {{ start.task }}

      Plan: {{ explore.plan }}
      Start with: {{ explore.files | join(", ") }}
      {% if history.implement %}

      This is attempt {{ history.implement | length + 1 }}. Your earlier work is committed on
      this branch, so run `git log` and `git diff {{ workspace.implement.base }}` first.
      Notes from earlier attempts:
      {% for attempt in history.implement %}
      - {{ attempt.notes }}
      {% endfor %}
      {% endif %}
      {% if tests and tests.exit_code != 0 %}

      The tests failed after your last attempt (exit code {{ tests.exit_code }}):
      {{ tests.stdout }}
      {{ tests.stderr }}
      {% endif %}
      {% if review and review.blocking_issues %}

      A reviewer found problems with your last attempt. Fix all of them:
      {% for issue in review.blocking_issues %}
      - {{ issue }}
      {% endfor %}
      {% endif %}
    output:
      summary: {type: string}
      notes: {type: string, description: "What you changed, what you tried and dropped, open concerns"}

  tests:
    needs: [implement]
    command: "{{ start.test_command }}"
    workspace: implement
    check: false

  review:
    needs: [tests]
    harness: codex
    workspace: implement
    instruction: |
      Review the changes on this branch (git diff {{ workspace.implement.base }}...HEAD).
      The task was: {{ start.task }}

      Tests exited with code {{ tests.exit_code }}:
      {{ tests.stdout }}
      {{ tests.stderr }}

      Earlier reviews: {{ history.review | tojson }}
      Only list problems that must be fixed, and check that earlier ones were fixed.
    output:
      blocking_issues: {type: array, items: {type: string}}
    routes:
      - when: tests.exit_code == 0 and not review.blocking_issues
        to: end
      - to: implement
        max: 3
      - to: end

end:
  output:
    branch: "{{ workspace.implement.branch }}"
    summary: "{{ implement.summary }}"
    passed:
      type: boolean
      value: "{{ tests.exit_code == 0 and not review.blocking_issues }}"
```

## Commands

| Command | What it does |
|---|---|
| `wf init <name>` | Creates `<name>.yaml` with a `start`, an `end` and an empty `nodes` section. Refuses to overwrite an existing file. |
| `wf validate <file>` | Checks the file and every sub-workflow it uses (see [Validation](#validation)). Exits with code 1 on errors. |
| `wf graph <file>` | Prints the workflow as a Mermaid diagram. |
| `wf run <file> [name=value ...]` | Validates, then runs the workflow. Inputs come from `name=value` pairs (a value that parses as JSON is used as JSON), from `--input file.json`, or from prompts for anything missing. Prints progress, then the end output. |
| `wf resume <run-id> [--limit NAME=VALUE]` | Continues a paused or failed run, or one a limit stopped (with a bigger limit). Finished nodes and items aren't rerun. |

`wf run` exit codes: 0 succeeded, 1 failed or stopped by a limit, 2 paused waiting for a person
(only with `--no-input` or when there's no terminal).

### Setup

Each harness needs its own login, and only the harnesses a workflow uses need to be set up.

- **Claude Code**: sign in once with `claude`, then `/login`. Runs use your subscription
  (or `ANTHROPIC_API_KEY`, as in `claude_code_chat/auth.py`).
- **Codex**: the `codex` CLI, signed in with `codex login`. `wf` is tested against one Codex
  version and warns when the installed one differs, because the parts of Codex it uses are
  marked experimental.
- **Cursor**: the `cursor-sdk` package (an optional dependency) and an API key from the
  Cursor dashboard in `CURSOR_API_KEY`. Cursor's browser login isn't enough. The runner
  passes the key to the SDK directly and removes it from the environment agents see, so
  their shell commands can't read it.

`wf init my-flow` writes:

```yaml
version: 1
name: my-flow
description: ""

defaults:
  harness: claude-code
  harnesses:
    claude-code: {model: claude-opus-5-5, effort: high}

start:
  input:
    task: {type: string, description: What this workflow should do}

# Add nodes here. Each node needs `needs: [...]` or a route that points to it.
nodes: {}

end:
  output: {}
```

## File format

### Top-level keys

| Key | Required | Meaning |
|---|---|---|
| `version` | yes | Spec version. Always `1` for now. |
| `name` | yes | Used in run ids and branch names. Lowercase letters, digits and `-`. |
| `description` | no | One line, shown in progress output and by `wf graph`. |
| `defaults` | no | Settings every node inherits ([defaults](#defaults)). |
| `limits` | no | Hard stops for the whole run ([limits](#limits)). |
| `types` | no | Named schemas that other schemas reuse with `$ref`. |
| `start` | yes | The workflow's inputs. |
| `nodes` | yes | The steps: a map from node id to node. |
| `end` | yes | The workflow's outputs. |

Unknown keys anywhere in the file are errors, so typos fail early.

### Schemas

Wherever the file declares data (`start.input`, a node's `output`, entries in `types`),
it uses a **field map**: each key is a field name, each value is a JSON Schema for that field.

```yaml
output:
  root_cause: {type: string, description: One or two sentences}
  files: {type: array, items: {type: string}}
  severity: {enum: [low, medium, high]}
  notes: {type: string, optional: true}
```

becomes

```json
{
  "type": "object",
  "properties": {"root_cause": {...}, "files": {...}, "severity": {...}, "notes": {...}},
  "required": ["root_cause", "files", "severity"],
  "additionalProperties": false
}
```

- Every field is required unless it has `optional: true`.
  That key is removed from the final schema.
  A missing optional field reads as `none` in templates and end outputs, at any depth: in nested objects, list items and `additionalProperties` values.
  A node's `output.json` records it as null.
  To let an agent or a person give null, use `type: [string, "null"]`.
- A value a template makes for `with` or for an end output's schema can hold that `none`.
  There, `none` in any field the schema doesn't require counts as left out.
  So a `with` input that has a `default` gets the default.
  And an output with a missing optional field passes through to a schema that declares the field or leaves it out.
- In `start.input`, a field can have a `default`, used when that input isn't given. A field
  with a default is optional.
- Write `description`s. For agent nodes they're part of the `submit_result` tool, so the
  agent reads them.
- Only the top level and `types` entries use the field-map shorthand. Nested objects are
  written as full JSON Schema, or as `$ref` to a type.
- `$ref: <name>` uses an entry from `types`. Types are inlined when a tool is built, so
  every harness gets a plain schema. Types can use other types, but not themselves,
  directly or indirectly.
- Supported keywords: `type`, `properties`, `required`, `additionalProperties`, `items`,
  `enum`, `const`, `description`, `minimum`, `maximum`, `minLength`, `maxLength`,
  `minItems`, `maxItems`, `pattern`, `$ref`, and `default` (only in `start.input`).
  Anything else is an error. This keeps schemas usable by every harness.

```yaml
types:
  task:
    title: {type: string}
    details: {type: string, description: What to build and how to check it}
```

### start and end

`start.input` is a field map of the workflow's inputs. Templates read them as `start.<field>`.

`end.output` maps each output field to a value, in one of two forms:

```yaml
end:
  output:
    branch: "{{ workspace.implement.branch }}"    # short form: one field reference
    passed:                                        # full form: a schema plus a value
      type: boolean
      value: "{{ tests.exit_code == 0 }}"
```

- Short form: a string holding exactly one reference, to a node output field, a `start`
  field, or a `workspace.<node>` or `run` field. Its type is copied from the schema or
  variable it points to.
- Full form: a JSON Schema plus `value`.
- A value made of a single `{{ ... }}` keeps its type, so a list stays a list. Anything
  else renders to a string.
- The result is checked against the schema. If it doesn't match, the run fails.
- When the workflow is used as a sub-workflow, this is what the parent node gets as its output.

### Nodes

Node ids start with a lowercase letter, followed by lowercase letters, digits or `_`. They
become template variables, so `-` isn't allowed. These names are reserved: `start`, `end`,
`run`, `history`, `workspace`, `errors`, `item`, `index`, `session`.

A node's kind is set by which of these keys it has, and it must have exactly one:
`instruction` (agent), `command`, `human` or `workflow`.

Fields every kind accepts:

| Field | Default | Meaning |
|---|---|---|
| `needs` | none | Nodes that must finish first. `start` is allowed. See [When a node runs](#when-a-node-runs). |
| `routes` | none | Where to go next, by condition. See [Routes](#routes). |
| `for_each` | none | Run once per item of a list. See [for_each](#for_each). |
| `max_parallel` | 4 | How many items run at the same time. One cap on agent sessions across all runs is separate: see [Agent slots](#agent-slots). |
| `max_items` | 20 | A longer list fails the node. |
| `on_item_error` | `fail` | `fail` or `continue`. |
| `workspace` | `repo` | Where the node runs. See [Workspaces](#workspaces). |
| `base` | the workflow's base commit | Git ref that a `new` workspace branches from. |
| `timeout` | `60m` (none for human and workflow nodes) | Per attempt. Written like `90s`, `30m`, `2h`. An agent attempt's timeout starts once it has an [agent slot](#agent-slots). A workflow node's steps have their own timeouts. |
| `retry` | 1 for agent nodes, 0 for others | Extra attempts after a failure. |
| `description` | none | Shown in progress output and by `wf graph`. |

#### Agent nodes

| Field | Default | Meaning |
|---|---|---|
| `instruction` | required | The task, as a template. Sent as the user message. |
| `output` | `{summary: {type: string}}` | The result's field map. Becomes the `submit_result` tool. |
| `harness` | `defaults.harness` | `claude-code`, `codex` or `cursor`, or a list to fall back through. See [Errors](#errors-retries-and-fallbacks). |
| `model`, `effort` | `defaults.harnesses.<harness>` | A string, or a map by harness when `harness` is a list. Each string is a template, so a `for_each` item can choose its own: `model: "{{ item.model }}"`. A value that renders to `none`, empty text or anything but a string fails the node (or the item) before a session starts. |
| `access` | `read-only` | `read-only`, `edit` or `full`. |
| `web` | `false` | Allow web search and page fetching. |
| `session` | `fresh` | `fresh` starts a new agent session for every pass: the code carries over through the workspace, and the instruction carries the feedback. `continue` carries the conversation over too. See [Sessions](#sessions). |
| `max_turns`, `budget_usd` | none | Passed to harnesses that support them. |

Access levels:
- `read-only`: read and search files, and run commands that don't change anything, such
  as `git diff` or `git log`. Each adapter enforces this with the harness's sandbox, or by
  allowing only known read-only commands.
- `edit`: read and edit files, and run any shell command in the workspace.
- `full`: no restrictions. For steps that need things outside the workspace.

Each harness adapter maps these levels to its own permission settings (see
[Harness adapters](#harness-adapters)). Nodes never stop to ask for permission: anything
outside the level is refused.

With `edit`, agents can't write to `.git` under Codex's sandbox, and probably not under
Cursor's either. So the runner makes the commits (see [Workspaces](#workspaces)), and a
node that runs git commands that write, such as `git merge` or `git rebase`, needs
`access: full`.

Agents run with the harness's normal coding setup: its own system prompt, plus the repo's
instruction files (`CLAUDE.md`, `AGENTS.md`). Where the harness allows it, user-level
settings and memory aren't loaded, so a workflow behaves the same on every machine. Codex
is the exception: it always loads your global `~/.codex/AGENTS.md`.

#### Command nodes

| Field | Default | Meaning |
|---|---|---|
| `command` | required | A template, run with `bash -c` in the workspace. |
| `env` | none | Extra environment variables. Values are templates. |
| `check` | `true` | A non-zero exit fails the node. Set `false` to route on `exit_code` instead. |

The output is fixed: `exit_code` (integer), `stdout` and `stderr` (the last 20,000
characters of each), and `log` (path to the full output in the run folder).

Values from agent outputs can contain anything, including shell syntax. Pass them through
`env` and use `"$NAME"` in the command, or quote them:
`git checkout {{ implement.branch | quote }}`. `wf validate` warns when a `command` uses
node outputs without one of these.

The same goes for `start` inputs that a parent workflow fills from agent output through
`with`. An input you type yourself is trusted, which is why `implement.yaml` can run
`{{ start.test_command }}` as a command. If `feature.yaml` passed an agent's output as
`test_command`, `wf validate` would warn.

#### Human nodes

```yaml
approve:
  needs: [plan]
  human: |
    Build these tasks?
    {% for t in plan.tasks %}
    - {{ t.title }}
    {% endfor %}
  output:
    approved: {type: boolean}
    notes: {type: string, optional: true}
```

`human` is the message (a template). `output` is required, and a person fills it in.

- In a terminal, `wf run` shows the message and asks for each field: yes/no for booleans,
  a numbered choice for `enum`, a line of text for strings and numbers, and `$EDITOR` with
  JSON for arrays and objects.
- With `--no-input`, or without a terminal, the run saves its state and pauses (exit
  code 2). `wf resume <run-id>` asks the questions later.
- Nodes that are already running finish before the run pauses.

#### Workflow nodes

```yaml
build:
  for_each: plan.tasks
  workflow: implement.yaml
  with:
    task: "{{ item.title }}: {{ item.details }}"
```

- `workflow`: path to the sub-workflow, relative to this file.
- `with`: values for the sub-workflow's `start.input` fields. They're templates, and a
  single `{{ ... }}` keeps its type.
- The node's output is the sub-workflow's `end.output`.
- The sub-workflow uses its own `defaults` and gets its own route counters. Its `limits`
  are ignored: the top-level run's limits, `run.cost_usd` and `run.tokens` cover everything.
- The node's `workspace` becomes the sub-workflow's `repo`.

### defaults

```yaml
defaults:
  harness: claude-code
  harnesses:
    claude-code: {model: claude-opus-5-5, effort: high}
    codex: {}                       # the default model from Codex's model list
    cursor: {model: composer-2.5}   # Cursor requires a model
  access: read-only
  workspace: repo
  max_parallel: 4
  timeout: 60m
  retry: 1
```

Any node field can go here except `needs`, `routes`, `for_each` and the kind keys. A
node's own value wins. `harnesses` sets `model`, `effort`, `max_turns` and `budget_usd`
for each harness.

The runner always passes the model and effort to the harness explicitly, so your personal
harness config never decides them. When nothing is set, it uses the harness's own default:
for Codex, the default model in the app server's model list. Cursor has no default, so any
node that can run on Cursor needs a `model`.

### limits

```yaml
limits:
  cost_usd: 60         # stop starting new attempts once the run has spent this much
  tokens: 20000000     # the same, counted in tokens
  duration: 4h         # the same, for wall-clock time
```

When a limit is reached, running attempts finish, nothing new starts, and the run pauses
with status `limit`. It isn't over: `wf resume <run-id> --limit cost_usd=100` continues it
with a bigger budget (`--limit NAME=none` removes a limit). The new limit replaces the file's
for the rest of the run, so later resumes keep it. The run carries on where it stopped: a
retry the limit held back runs, and a sub-workflow the limit stopped continues its own steps
rather than starting over. `wf resume` without a big enough limit leaves the run as it is
and says which limit is still reached. `wf run <file> --limit ...` starts a run with a
different limit.

- **Tokens** are input plus output tokens, as each harness reports them (cached input
  included). All three harnesses report them, so `tokens` is the limit that works everywhere.
- **Cost** only counts harnesses that report it in dollars: Claude Code always does, Cursor
  only if it's enabled for your account, and Codex never does. A harness that doesn't report
  cost adds nothing, and `wf validate` warns about it.

## Templates and expressions

Text is evaluated with sandboxed Jinja, in two ways:

- **Templates** use `{{ }}` and `{% %}`. Fields: `instruction`, `human`, `command`, `env`,
  `with`, `base`, `harness`, `model`, `effort`, and end `value`s.
- **Expressions** are written without braces. Fields: `when` and `for_each`.
  Example: `review.approved and tests.exit_code == 0`.

Variables:

| Name | Value |
|---|---|
| `start` | The workflow's input. |
| `<node>` | That node's latest output, or `none` if it hasn't run. For a `for_each` node, the list of item outputs from its latest pass (only items that succeeded, in list order). |
| `history.<node>` | Every output of that node, oldest first: a list of lists for `for_each` nodes, empty if it hasn't run. When a node renders its own instruction, its history holds only earlier passes. |
| `item`, `index` | The current item and its position (from 0). Only inside a `for_each` node. |
| `workspace.<node>` | `{path, branch, base}` for that node's workspace (`branch` and `base` are `none` for `temp`). A list for `for_each` nodes. |
| `errors.<node>` | For `for_each` nodes with `on_item_error: continue`: a list of `{index, item, error}` for the items that failed. |
| `run` | `{id, dir, cost_usd, tokens, elapsed_s, base}`. `base` is the commit the repo was at when the run started. |
| `session` | `{continued}`: true while rendering a pass that continues an earlier session (see [Sessions](#sessions)). Not in `model` and `effort`, which are chosen before the session starts. |

Rules:
- An unknown name or field is an error, never an empty string. The same goes for reading
  a field of `none`, so check nodes that might not have run: `{% if review %}`.
- Tags on a line of their own don't leave blank lines behind (`trim_blocks` and
  `lstrip_blocks` are on).
- One extra filter, `quote`, quotes a value for the shell. Jinja's built-in filters such as
  `tojson`, `join` and `length` work as usual.

## Execution

### When a node runs

1. When the run starts, `start` finishes at once with the input.
2. When a node finishes:
   - if it has `routes`, the targets of the chosen route become **pending**;
   - otherwise every node that lists it in `needs` becomes pending.

   A node that is already pending isn't added twice.
3. A pending node starts once all of its `needs` are **settled**. A need is settled when
   it isn't pending or running, and nothing else that is pending or running could still
   lead to it. "Could lead to" means a path along `needs` and route edges, ignoring
   conditions and leaving out paths through the waiting node itself.
4. The run ends when nothing is pending or running. Then `end.output` is computed.

So every node must either have `needs` or be the target of a route, otherwise it can never
run. Nodes that run first use `needs: [start]`.

What these rules give you:
- **Skipped branches don't block joins.** `classify` routes to either `bugfix` or `feature`,
  and `open_pr` has `needs: [bugfix, feature]`. Only one of them runs. The other is settled,
  and reads as `none`.
- **Joins wait for loops to finish.** `open_pr` has `needs: [implement]`, and `review` can
  route back to `implement`. After the first `implement`, `open_pr` is pending, but `review`
  could still lead back to `implement`, so `open_pr` waits. It runs once, after the loop ends,
  with the final result.
- **`to: end` ends that path, not the whole run.** Other branches still running carry on.

If the pending nodes end up all waiting on each other (only possible with routes that go
backwards), the one that became pending first starts. Ties go by order in the file.

### Routes

```yaml
routes:
  - when: not review.blocking_issues
    to: end
  - to: implement
    max: 3
  - to: end
```

- Routes are checked in order after the node succeeds. The first one whose `when` is true
  (or that has no `when`) and whose `max` isn't used up is taken.
- `to` is a node id, `end`, or a list of node ids that all become pending.
- `max` is how many times the route can be taken in one run of this workflow. Once used
  up, the route is skipped. In the example, `implement` runs at most 4 times.
- The last route must have no `when` and no `max`, so there's always a way out.
- In `when`, the node's own name is the output it just produced.
- A node with `routes` can't appear in another node's `needs`. Reach the next nodes through
  its routes instead.
- Every loop must pass through a route with `max`. `wf validate` rejects loops that could
  run forever.

On a `for_each` node, routes are checked once, after all items finish, and the node's name
is the list of item outputs.

### for_each

- `for_each` is an expression that must produce a list. It's evaluated when the node starts.
- Each item runs separately, with `item` and `index` set, up to `max_parallel` at a time.
- The node's output is the list of item outputs, in list order.
- An empty list finishes the node at once with `[]`.
- A list longer than `max_items` fails the node. Put `maxItems` on the schema that produces
  the list, so the agent learns the limit when `submit_result` rejects a longer one.
- Retries apply per item. If an item still fails: with `on_item_error: fail`, the node
  fails, but finished items are kept, so `wf resume` reruns only the failed ones. With
  `continue`, the node succeeds with the items that worked, and `errors.<node>` lists the rest.
- `harness` is a template, so `for_each: ["claude-code", "codex", "cursor"]` with
  `harness: "{{ item }}"` runs the same task on each harness (best-of-N).
- To run several steps per item, for example implement, test and review, use a workflow node.

### Errors, retries and fallbacks

An attempt fails when the harness errors or crashes, the `timeout` passes, the agent never
makes a valid `submit_result` call, a command exits non-zero with `check: true`, or a
sub-workflow fails.

- `retry: N` gives N more attempts after a failure.
- `harness: [claude-code, codex]` tries harnesses in order, and each gets `1 + retry`
  attempts. If a harness reports a usage limit or can't start, the runner moves to the next
  harness straight away.
- An agent attempt that waits for an [agent slot](#agent-slots) hasn't started yet: the
  wait isn't a failure and uses no retry.
- When a node, or an item with `on_item_error: fail`, runs out of attempts, the run stops:
  running attempts finish, nothing new starts, and the run's status becomes `failed`.
  `wf resume` continues from there.

### Sessions

An attempt runs in one agent session. Reminders to call `submit_result` are extra messages
in that same session (see [The output tool](#the-output-tool)). What a node's next pass
gets depends on `session`:

- **`fresh` (default)**: every pass starts a new session. The code carries over through the
  workspace, and the instruction carries the feedback, for example through
  `history.<node>` and the reviewer's output. A fresh session isn't anchored to its earlier
  approach, a retry is the same instruction on the same commit, and each pass's whole input
  is its `instruction.md` plus a commit.
- **`continue`**: the 2nd and later passes carry the conversation over as well.
  - The runner records each pass's session id in its pass folder.
  - The next pass *forks* that session: Claude Code with `fork_session`, Codex with
    `thread/fork`. The earlier session is never changed, so a retry forks from the same
    point again. Cursor can't fork, so it resumes the previous agent instead.
  - The rendered instruction is sent as the next message. `session.continued` is true while
    it renders, so the template can send just the new feedback instead of the whole task.
    The runner adds that a new `submit_result` call is needed.
  - The pass starts fresh instead, and an event says why, when: the harness or model differs
    from the previous pass, the session can't be found, it's a Cursor retry after a failed
    continued attempt, or the two passes before it both continued.

### Agent slots

`max_parallel` caps the items of one node in one run. Several runs on one machine can still
open more agent sessions at once than the machine, or an account's quota, holds. One cap
holds across every wf run of the user:

```yaml
# ~/.wf/config.yaml (WF_HOME moves ~/.wf)
max_agents: 4
```

The `WF_MAX_AGENTS` environment variable sets the same cap, and wins over the file. With
neither, there is no cap. The cap is a whole number of 1 or more; `wf run` and `wf resume`
refuse to start with anything else. `config.yaml` holds wf's own settings, and a name in it
that wf doesn't know stops them too, so a misspelt `max_agents` can't quietly mean no cap.

- Every agent attempt takes a slot before its session opens, and gives it back when the
  attempt ends. A retry or a fallback takes a slot again. Command, human and workflow nodes
  take no slot (the agent nodes inside a workflow node do), and neither does a dry run.
- An attempt that finds every slot taken waits until one is free. Waiting isn't failing: it
  uses no retry, and the node's `timeout` starts once the attempt has its slot. The run's
  `duration` limit counts the wait. A limit reached during the wait ends it: the attempt
  doesn't start, and the run pauses at the limit as usual.
- The wait shows in `events.jsonl` (`slot_waiting` when it starts, then `slot_taken` with
  `waited_s`) and in `wf status`: the run is `running`, and `agents_waiting` lists the
  attempts that wait, with their node, attempt number and start time.
- A slot is a file in `~/.wf/agent-slots/`, locked by the process whose attempt holds it.
  The operating system releases the lock when that process ends, so a crashed or killed
  run never keeps a slot.
- Each `wf run` or `wf resume` process reads the cap when it starts. Give every process the
  same cap (the file does that). With different caps, the total stays within the biggest.

## The output tool

Each agent attempt gets one tool, `submit_result`. Its input schema is the node's
`output` schema.

- The runner adds this to the end of the instruction:

  > When you're done, call the `submit_result` tool with your result. The task isn't
  > complete until that call succeeds. If it returns errors, fix them and call it again.

- When the agent calls it, the runner checks the arguments against the schema (JSON Schema
  2020-12). If they don't match, the tool returns each problem with its field path as a tool
  error, so the agent can fix them and try again. The problems are spelled out in the reply's
  text, because Codex shows the model only the text, not the error flag.
- The first valid call is the result. The tool replies "Result recorded. You're done.",
  later calls return an error, and the runner ends the session once the agent's turn finishes.
- If the agent's turn ends without a valid call, the runner sends "You haven't called
  submit_result yet. Call it now with your result." as another message in the same
  session. It does this up to 2 times. If there's still no result, the attempt fails.

`submit_result` is a Python function in the runner, and every harness can call one
directly. No separate server is needed:
- **Claude Code**: an SDK MCP server inside the runner process, like
  `claude_code_chat/native_tools.py`.
- **Codex**: a dynamic tool, declared when the thread starts. The app server sends each call
  to the runner as an `item/tool/call` request on the same connection.
- **Cursor**: a custom tool (`LocalAgentOptions.custom_tools`), which the SDK calls in the
  runner's process.

Each call arrives tied to its session (a process, a thread id or an agent id), so a result
can only reach the attempt it belongs to.

The runner uses this tool instead of each harness's own structured-output feature because
it works the same everywhere, and the agent gets validation errors back while it still has
the context to fix them.

## Workspaces

`workspace` says where a node runs:

| Value | Meaning |
|---|---|
| `repo` (default) | The workflow's repo. At the top level, the folder `wf run` was started in. In a sub-workflow, the parent node's workspace. |
| `new` | A new git worktree on a new branch, created from `base`. By default `base` is the commit the workflow's repo was at when this workflow run started. |
| `<node id>` | The same workspace as that node's latest pass, for testing or reviewing an agent's changes. It can't be a `for_each` node. |
| `temp` | An empty temporary folder, for nodes that don't touch code. |

- Branch names are `wf/<run-id>/` followed by the path of node ids and item indexes, for
  example `wf/<run-id>/build/2/implement`.
- A node keeps the same worktree across passes, so a loop keeps working on one branch.
  Each item gets its own.
- Worktrees live outside the repo, under `~/.wf/worktrees/`, so agents searching the repo
  don't find copies of it.
- After an `edit` or `full` node finishes in a worktree the runner created, the runner
  commits anything left uncommitted (`wf: <node> (<run-id>)`). It never commits in your own
  checkout. The runner tells `edit` agents not to commit, because some harness sandboxes
  keep `.git` read-only.
- The commit skips git hooks, so its message is only that line, with no trailers. It is
  signed when your git config signs commits (`commit.gpgsign`), with your signing key.
  Nobody is at a terminal to unlock that key, so use one that needs no passphrase, PIN or
  touch, for example a key file set as the repo's `user.signingkey`. A commit that can't be
  signed, or isn't signed within 30 seconds, fails the node with an error that says so.
- When a run succeeds, its worktrees are removed and its branches are kept. A failed run
  keeps its worktrees for `wf resume`.
- `wf validate` warns when two `edit` nodes could run at the same time in the same workspace.

## Run folder

```
.wf/runs/<run-id>/              <run-id> is like 20261018-1432-implement-k3f9
  workflow/                     copies of the workflow files as they were run
  input.json
  output.json                   the end output, once the run succeeds
  status.json                   running | waiting | succeeded | failed | limit
  events.jsonl                  one line per event: node started, finished, route taken, ...
  checkpoints.sqlite            LangGraph checkpoints, used by wf resume
  nodes/<node>/<pass>/          one folder per pass ...
    [<index>/]                  ... and per item
      instruction.md            the rendered instruction (or command, or message)
      output.json
      attempt-<n>.log           harness transcript or command output
```

- `.wf/` is added to the repo's `.git/info/exclude`, so it never shows up in `git status`.
- While a run is `running`, `agents_waiting` in `status.json` lists the attempts waiting
  for an [agent slot](#agent-slots).
- `wf resume` uses the copies in `workflow/`, so editing the YAML doesn't change a run
  that's already in progress.

## Validation

`wf validate`, and `wf run` before it starts, check the following.

Errors:
1. The file matches this spec: required keys are present and there are no unknown keys.
2. Node ids are valid, unique and not reserved. Each node has exactly one kind.
3. Schemas are valid and use only supported keywords. Every `$ref` exists. No type refers
   to itself.
4. Every `needs` entry and route target exists, and `start` isn't a route target. `needs`
   has no cycles. No node needs a node that has `routes`.
5. Every node has `needs` or is a route target, and can be reached from `start`.
6. The last route of each node has no `when` and no `max`. Every loop includes a route
   with `max`.
7. Templates and expressions parse. Every name exists. Field paths exist in the schemas
   they read from, including `item.<field>` checked against the list's item schema when
   that can be worked out.
8. `item` and `index` appear only in `for_each` nodes. `for_each` reads a list.
9. `model` and `effort` are maps when `harness` is a list, and don't read `session`.
   A node that can run on `cursor` has a `model`.
10. Each short-form end output is a single field reference.
11. Each sub-workflow exists and is valid, no workflow includes itself (directly or
    indirectly), and `with` sets every required input with a matching type.

Warnings:
- A template reads a node that might not have run at that point, outside an `{% if %}`
  that checks it.
- A `command` uses node outputs without `env` or `| quote`, or uses a `start` input that a
  parent workflow fills from node output.
- An agent with `access: full` reads output from a node with `web: true`. Web pages can
  contain instructions aimed at agents, and this passes them to one with no restrictions.
- Two `edit` nodes could run at the same time in the same workspace.
- A `for_each` reads a list whose schema has no `maxItems`.
- The workflow uses `run.cost_usd` or `limits.cost_usd` with a harness that doesn't
  report cost.

## Implementation notes (Python, LangGraph)

Package layout, one concept per file, in reading order:

```
workflow/
  spec.py           Pydantic models of the YAML file: parsing and unknown-key errors
  schemas.py        field maps to JSON Schema, $ref inlining, checking results
  templates.py      sandboxed Jinja: variables, the quote filter, keeping native types
  validate.py       the checks in Validation
  workspaces.py     git worktrees, auto-commit, cleanup
  output_tool.py    submit_result: one validator that every harness adapter wraps
  harnesses/
    base.py         the Harness and Session interfaces
    claude_code.py  Claude Agent SDK
    codex.py        codex app-server, JSON-RPC over stdio
    cursor.py       cursor-sdk
  settings.py       ~/.wf/config.yaml: wf's own settings, and the names it knows
  capacity.py       the agent slots: one cap on live agent sessions across all runs
  nodes.py          runs one attempt of each node kind, including the reminder loop
  scheduler.py      the rules in "When a node runs": pending, settled, routes, end
  graph.py          LangGraph wiring
  runs.py           run folder, events, resume
  cli.py            init, validate, graph, run, resume
```

The `wf` command is a `[project.scripts]` entry in `pyproject.toml`:
`wf = "workflow.cli:main"`.

Dependencies to add to `pyproject.toml`: `jinja2`, `pyyaml`, `jsonschema` and
`langgraph-checkpoint-sqlite`, plus `cursor-sdk` as an optional extra. (`pyyaml` and
`jsonschema` are already installed through LangChain, but the package should list what it
imports.) Codex needs no Python package: the runner talks to the `codex` CLI's app server.

### Harness adapters

Details and sources are in `docs/research/harness-adapters.md`.

Each harness implements two small interfaces:

```python
class Harness(Protocol):
    name: str
    async def open(self, run_dir: Path) -> None            # once per run
    async def session(self, spec: SessionSpec) -> Session  # new, or forked from spec.fork_from
    async def close(self) -> None

class Session(Protocol):
    id: str
    async def send(self, text: str) -> TurnEnd   # one turn: done, unavailable or failed
    async def close(self) -> None                # also interrupts a running turn
```

`SessionSpec` holds the working folder, access level, `web`, model, effort, the attempt's
`submit_result` tool and the session to fork from. `TurnEnd` says how the turn ended and
what it used. `unavailable` means a usage limit, a login problem or a harness that can't
start, and sends the runner to the next harness. The attempt loop is written once, in
`nodes.py`, for every harness: send the instruction, then up to 2 reminders, and stop once
`submit_result` has a result. Each adapter only maps settings and events.

| | Claude Code | Codex | Cursor |
|---|---|---|---|
| Runs as | `claude-agent-sdk`: one process per session | one `codex app-server --disable hooks --disable apps` per run, JSON-RPC over stdio, one thread per session | `cursor-sdk`: one bridge per run, with its state in the run folder, one agent per session |
| `submit_result` | SDK MCP server | dynamic tool (`experimentalApi`) | custom tool |
| `read-only` | Read, Grep, Glob, and Bash limited to read-only git commands | sandbox `read-only` | read and search tools, plus a runner tool that runs `git diff`, `log`, `show` or `blame`; no shell |
| `edit` | Read, Edit, Write, Bash with `permission_mode="dontAsk"` | sandbox `workspace-write` | default tools, sandbox on |
| `full` | `permission_mode="bypassPermissions"` | sandbox `danger-full-access` | sandbox off |
| `web: false` | no WebSearch or WebFetch | `web_search: "disabled"` (the default is `cached`) | `webSearch` and `webFetch` disallowed |
| Repo instructions | `setting_sources=["project"]`, `claude_code` system prompt | `AGENTS.md`, always loaded | `setting_sources=["project"]` |
| Usage limit | a `RateLimitEvent` that isn't `allowed` | `codexErrorInfo: "usageLimitExceeded"` with `willRetry: false` | `RateLimitError`, or an error status whose message mentions a usage or rate limit |
| Usage | tokens and dollars | tokens | tokens, and dollars where enabled |
| `session: continue` | fork with `fork_session` | fork with `thread/fork` | resume (no fork) |

Notes:
- Every setting is passed on every session. Codex fills any gap from your
  `~/.codex/config.toml`.
- Codex threads are `ephemeral` (not saved) unless the node uses `session: continue`.
- A resumed or forked Claude session reports cost including its earlier spend. The runner
  subtracts that, so `run.cost_usd` doesn't count it twice.
- Codex's app server answers each dynamic tool call only when the runner replies, with no
  timeout of its own. So the runner always replies, even on errors.
- Codex's app-server login is for local use on your own account. Don't run `wf` as a hosted
  service on someone else's ChatGPT login.

How it maps onto LangGraph:
- **It's not one LangGraph node per YAML node.** The settle rule needs to know what's
  pending across the whole graph, and static edges can't express that. A multi-source edge
  (`add_edge(["a", "b"], "c")`) waits for every source, so it would wait forever on a
  skipped branch. Instead the graph has three nodes:
  - `schedule` applies the rules in `scheduler.py`. It returns one `Send("execute", ...)`
    for each node or item that's ready, or goes to `finish` when nothing is pending.
  - `execute` runs the attempts for one node or item. It never raises: failures come back
    as data, so one failure doesn't cancel other tasks running in the same step. The one
    exception is `interrupt()` in human nodes, which LangGraph handles itself.
  - `finish` computes the end output.

  `execute` leads back to `schedule`.
- State holds each node's outputs and history, the pending set, route counters, item
  errors and cost.
- Human nodes call `interrupt()`, and `wf resume` continues with `Command(resume=answers)`.
- An `AsyncSqliteSaver` in the run folder keeps checkpoints. LangGraph also saves the
  results of tasks that finished within a step, so a resumed run doesn't redo them.
- Progress events go through `get_stream_writer()`, as in `deep_research/researcher.py`.
  The CLI prints them, and `runs.py` appends them to `events.jsonl`.
- A workflow node runs the sub-workflow's compiled graph inside `execute`, as a subgraph.
- **Trade-off:** LangGraph runs in steps. `schedule` only runs again after every task
  started in that step has finished, so a fast branch waits for a slow parallel one before
  its next node starts. That's acceptable for v1. If it becomes a problem, `schedule` and
  `execute` can be replaced by a plain asyncio loop without changing anything else in
  this spec.

## Out of scope for v1

- Triggers and schedules (cron, webhooks). Runs start with `wf run`.
- "First result wins" races.
- Agents writing new workflows during a run.
- Variables and assignments in YAML.
- Routing on failure (an `on_error: <node>` route).
- Command nodes that return JSON as structured output.
- Sharing `types` between files.
- Resuming a run with an edited version of its workflow.
- A separate cap on agent sessions per harness (Claude Code, Codex and Cursor have separate
  quotas). The [agent slots](#agent-slots) cap all harnesses together.

## Open questions

None right now. How to drive Codex and Cursor, and whether loops should continue sessions,
were answered in `docs/research/harness-adapters.md`. Its last section lists what to check
while building.

## Appendix: plan, build in parallel, merge

`feature.yaml` plans a feature, asks a person to approve the plan, builds each task in
parallel with `implement.yaml` (each in its own worktree, with its own review loop), merges
the branches that passed, and opens a pull request.

```yaml
version: 1
name: feature
description: Plan a feature, build each part in parallel, merge, open a PR.

defaults:
  harness: claude-code
  harnesses:
    claude-code: {model: claude-opus-5-5, effort: high}

limits:
  cost_usd: 60
  tokens: 20000000     # the review step runs on Codex, which reports no dollar cost

types:
  task:
    title: {type: string}
    details: {type: string, description: What to build and how to check it}

start:
  input:
    feature: {type: string, description: The feature to build}

nodes:
  plan:
    needs: [start]
    instruction: |
      Split this feature into independent tasks that change different files:
      {{ start.feature }}
    output:
      tasks: {type: array, maxItems: 5, items: {$ref: task}}

  approve:
    needs: [plan]
    human: |
      Build these tasks?
      {% for t in plan.tasks %}
      - {{ t.title }}
      {% endfor %}
    output:
      approved: {type: boolean}
    routes:
      - when: approve.approved
        to: build
      - to: end

  build:
    for_each: plan.tasks
    workflow: implement.yaml
    with:
      task: "{{ item.title }}: {{ item.details }}"
    max_parallel: 3
    on_item_error: continue
    routes:
      - when: build | selectattr("passed") | list
        to: merge
      - to: end

  merge:
    access: full
    workspace: new
    instruction: |
      Merge these branches into the current branch and resolve any conflicts:
      {% for b in build if b.passed %}
      - {{ b.branch }}: {{ b.summary }}
      {% endfor %}
    output:
      summary: {type: string}

  open_pr:
    needs: [merge]
    command: git push -u origin HEAD && gh pr create --fill
    workspace: merge

end:
  output:
    pr_url:
      type: [string, "null"]
      value: "{{ open_pr.stdout | trim if open_pr else none }}"
```

`build` and `merge` have no `needs`: each runs only when a route points to it. If the plan
is rejected, or no task passes its tests and review, the run ends early and `pr_url` is null.
`merge` needs `access: full` because it runs `git merge`, which writes to `.git`.
