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
- **Run**: one execution of a workflow, with its own folder under `.wf/runs/` (or in a runs
  folder, see [Run folder](#run-folder)).

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
| `wf retry <run-id> [--limit NAME=VALUE]` | Continues a failed or paused run from its last checkpoint, or one a limit stopped (with a bigger limit). Finished nodes and items aren't rerun. |
| `wf trace <run-id> [--node N] [--pass P] [--field F] [--var PATH] [--json]` | Shows what one run read, decided and did, from its trace: how each template rendered, why each route was taken, each attempt, turn and tool call. See [The trace](#the-trace). |
| `wf status <run-id>` | Says where a run stands, and how far it is on its way from `start` to `end`, as an estimate (see [Watching runs](#watching-runs)). `--json` gives the same as JSON. |
| `wf sessions [run-id]` | Lists the agent sessions that run now, in one run or in each run of the repo, with the last tool call of each. |
| `wf watch <run-id>` | Prints the run's steps and tool calls as they come, until the run needs attention. |
| `wf ui` | Serves a web page on `127.0.0.1` that shows the runs: the graph, the progress, the metrics of `wf report`, the events and the transcripts. |
| `wf tools list` | Lists the tools agent nodes can use (see [docs/tools.md](tools.md)), how each signs in, and whether it's set up. |
| `wf tools <name> setup [--write]` | Signs in to a tool once, in the browser or with a pasted token, and keeps the secret in the macOS Keychain. It asks Slack and Linear for read scopes only, unless `--write` is given. |
| `wf tools <name> doctor`, `wf tools doctor` | Checks that a tool's sign-in is there and still good, and makes one harmless read call. Prints pass or fail with the next step. |

`wf run` exit codes: 0 succeeded, 1 failed or stopped by a limit, 2 paused waiting for a person
(only with `--no-input` or when there's no terminal).

Every command that makes or finds runs (`run`, `retry`, `answer`, `wait`, `status`, `list`,
`report`, `discard`, `sessions`, `watch`, `ui`) takes `--runs-dir <dir>`, to keep run folders outside the repo (see
[Run folder](#run-folder)). The commands that wf prints for a run in a runs folder, like
`wf retry <run-id>`, include the option, so they find the run whether or not
`WF_RUNS_DIR` is set where you type them.

### Setup

Each harness needs its own login, and only the harnesses a workflow uses need to be set up.

- **Claude Code**: sign in once with `claude`, then `/login`. Runs use your subscription
  (or `ANTHROPIC_API_KEY`, as in `claude_code_chat/auth.py`).
- **Codex**: the `codex` CLI, signed in with `codex login`. `wf` is tested against one Codex
  version and warns when the installed one differs, because the parts of Codex it uses are
  marked experimental.
- **Cursor**: `cursor-agent`, signed in with `cursor-agent login`.
  Runs use that login, not an API key: the runner removes `CURSOR_API_KEY` from the environment Cursor and its agents see.
  Each cursor-agent process gets a settings folder of its own, outside the repo, so your `~/.cursor` settings don't apply and aren't changed.
  `wf` is tested against one cursor-agent version and warns when the installed one differs, because it reads the messages Cursor writes.
- **Tools** (Slack, Granola, Linear, Notion, Gmail): only the ones a workflow's nodes list.
  `wf tools <name> setup` signs in once per machine.
  [docs/tools.md](tools.md) has the steps.

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
| `max_parallel` | 4 | How many items run at the same time. One cap on agent sessions across runs is separate: see [Agent slots](#agent-slots). |
| `max_items` | 20 | A longer list fails the node. |
| `on_item_error` | `fail` | `fail` or `continue`. |
| `workspace` | `repo` | Where the node runs. See [Workspaces](#workspaces). |
| `base` | the workflow's base commit | Git ref that a `new` workspace branches from. |
| `branch` | `wf/<run-id>/...` | The name of a `new` workspace's branch, as a template. See [Workspaces](#workspaces). |
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
| `tools` | none | wf tools the agent may use: `[slack, linear]`. Each gets only its read operations, unless the entry is `{name: ..., write: true}`, which gets every operation the server offers. A node with `tools`, even `tools: []`, gets no other MCP server or connector, at any access level. See [docs/tools.md](tools.md). |
| `tools_with_shell` | `false` | Lets a node with `tools` run where its agent's commands can reach the Keychain, where every sign-in is stored. See [Tools and a shell](#tools-and-a-shell). |
| `project_instructions` | `true` | `false` keeps the repo's own agent instruction files out of the node's sessions: `CLAUDE.md`, `AGENTS.md`, Cursor's rules, the repo's skills and Claude Code's project settings. The instruction still reaches the agent. Can go in `defaults`. See [Instruction files](#instruction-files). |

Access levels:
- `read-only`: read and search files, and run commands that don't change anything, such
  as `git diff` or `git log`. Each adapter enforces this with the harness's sandbox, or by
  allowing only known read-only commands.
- `edit`: read and edit files, and run any shell command in the workspace.
- `full`: no restrictions. For steps that need things outside the workspace.

Each harness adapter maps these levels to its own permission settings (see
[Harness adapters](#harness-adapters)). Nodes never stop to ask for permission: anything
outside the level is refused.

With `edit`, agents can't write to `.git` under Codex's sandbox.
So the runner makes the commits (see [Workspaces](#workspaces)).
A node that runs git commands that write, such as `git merge` or `git rebase`, needs `access: full`.

##### Instruction files

Agents run with the harness's normal coding setup: its own system prompt, plus the repo's
instruction files (`CLAUDE.md`, `AGENTS.md`). Where the harness allows it, user-level
settings and memory aren't loaded, so a workflow behaves the same on every machine.
Codex and Cursor are the exceptions.
Codex always loads your global `~/.codex/AGENTS.md` and the skills in your home folder, such as `~/.agents/skills`.
Cursor loads your Cursor account's User Rules and the skills in your home folder, such as `~/.claude/skills` and `~/.agents/skills`.

A repo's instruction files can give an agent a role that isn't the workflow's.
For example, firstmate's `AGENTS.md` tells each agent that loads it that it is the first mate.
`project_instructions: false` keeps the repo's files out of a node's sessions, and the node's instruction still reaches the agent.
The default, `true`, loads them as before.
A sub-workflow uses its own `defaults`, so a workflow that sets `false` in its `defaults` sets it in each sub-workflow too.
The tests named below are in `tests/test_claude_code.py`, `tests/test_codex.py` and `tests/test_cursor.py` for the rows of those harnesses.
The tests of the other rows are in `tests/test_validate.py`, unless another file is named.

| Condition | Effect | Source line | Conformance assertion |
|---|---|---|---|
| A node sets `project_instructions`, or `defaults` does | The node's own value wins, then `defaults`, then `true`. Only `true` or `false` is accepted. | `workflow/spec.py:194`, `workflow/spec.py:298`, `workflow/nodes.py:459` | `tests/test_agents.py::test_project_instructions_reach_the_session_from_the_node_or_defaults`, `test_project_instructions_takes_a_boolean_on_a_node_or_in_defaults` |
| `false` on Claude Code | No setting source loads: no `CLAUDE.md` or `AGENTS.md` at any level, no `.claude/rules`, no `.claude/settings.json`, and no skills in `.claude/skills`. Claude Code's own system prompt stays. | `workflow/harnesses/claude_code.py:137` | `test_without_project_instructions_no_setting_source_loads`, `test_a_real_node_without_project_instructions_does_not_get_the_repos_claude_md` (`-m harness`) |
| `false` on Codex | The thread gets `project_doc_max_bytes: 0`, so no `AGENTS.md` and no fallback file such as `CLAUDE.md`. Each skill that `skills/list` finds in the repo is switched off in the thread's config. | `workflow/harnesses/codex.py:171`, `workflow/harnesses/codex.py:206` | `test_without_project_instructions_a_thread_gets_no_repo_agents_md_and_no_repo_skill` |
| `false` on Codex, and `skills/list` doesn't answer | Codex is unavailable for the attempt, so the next harness in the node's list runs. | `workflow/harnesses/codex.py:212` | `test_a_codex_that_does_not_list_its_skills_is_unavailable_to_a_node_without_project_instructions` |
| `false` on Cursor | cursor-agent starts in a new empty folder in the system's temp folder, because it reads rules, `AGENTS.md`, `CLAUDE.md` and skills from the folder it starts in, the folders above it, and the repo it is in. `session/new` still gives the workspace, where the agent reads, edits and runs commands. The folder goes with the session. | `workflow/harnesses/cursor.py:291`, `workflow/harnesses/cursor.py:440` | `test_without_project_instructions_cursor_agent_starts_in_an_empty_folder_and_works_in_the_workspace`, `test_by_default_cursor_agent_starts_in_the_workspace` |
| `false` on Cursor, and the temp folder is in a git repo, or a folder above it holds `AGENTS.md`, `CLAUDE.md`, `CLAUDE.local.md`, `.cursorrules`, `.cursor/rules`, or a `skills` folder in `.cursor`, `.claude`, `.codex`, `.grok` or `.agents`, as when `TMPDIR` is in a repo | Cursor is unavailable for the attempt, and the message names what it found, so the next harness in the node's list runs. The skills folders in your home folder don't count, because they load wherever Cursor starts. | `workflow/harnesses/cursor.py:454` | `test_a_temp_folder_under_what_cursor_loads_makes_it_unavailable_without_project_instructions`, `test_your_home_folders_skills_above_the_temp_folder_do_not_count` |
| `false` where a harness still loads a file | `wf validate` warns once for each harness, where the setting is. On Codex, your global `~/.codex/AGENTS.md` and the skills in your home folder stay. On Cursor, your account's User Rules and the skills in your home folder stay, and so do the repo's hooks in `.cursor/hooks.json` and `.claude/settings.json`. | `workflow/validate.py:635`, `workflow/harnesses/__init__.py:51` | `test_project_instructions_off_warns_once_per_harness_that_keeps_a_file` |
| `false`, and a template picks the harness | `wf validate` can't tell which harness runs, so it warns about each harness that keeps a file. | `workflow/validate.py:641` | `test_project_instructions_off_warns_about_every_harness_a_template_could_pick` |

On Cursor, `false` has a cost: Cursor's semantic code search and lint diagnostics don't cover the workspace.
Claude Code loads no skill from your home folder.
Codex and Cursor load those skills as before.
Each harness row was checked live, with Claude Code 2.1.301 (the one the Agent SDK bundles), Codex 0.163.0 and cursor-agent 2026.10.18-ea085cb.
In each check, a repo's `AGENTS.md` or `CLAUDE.md`, its rules and its skills held code words.
A node with the default told them, and a node with `false` told none, and still did its task.
These checks are tests marked `-m harness`: `test_a_real_node_without_project_instructions_does_not_get_the_repos_claude_md`, `test_a_real_codex_node_without_project_instructions_gets_no_repo_agents_md_or_skill` and `test_a_real_cursor_node_without_project_instructions_gets_no_repo_rules_and_still_edits`.

##### Tools and a shell

A node with `tools` may not have a shell that reaches the Keychain, unless it sets `tools_with_shell: true`.
The tests named below are in `tests/test_tools.py`, unless another file is named.

| Condition | Effect | Source line | Conformance assertion |
|---|---|---|---|
| A node with `tools` can run on Claude Code or Cursor at `edit` or `full`, or on Codex at `full`, and doesn't set `tools_with_shell: true` | `wf validate` errors, naming each such harness and the access level, so the run doesn't start. | `workflow/validate.py:631`, `workflow/harnesses/__init__.py:49` | `test_a_node_with_tools_and_a_shell_that_reaches_the_keychain_needs_the_opt_in`, `test_a_harness_list_names_each_harness_whose_shell_reaches_the_keychain` |
| A template picks such a harness at run time, which `wf validate` can't see, and the node doesn't set `tools_with_shell: true` | The run refuses that harness, and the node fails without a session. | `workflow/nodes.py:426` | `tests/test_tool_sessions.py::test_a_run_refuses_a_harness_whose_shell_reaches_the_keychain_without_the_opt_in` |
| The node sets `tools_with_shell: true` | `wf validate` warns, naming the node and its tools: its agent could read every stored sign-in. The run attaches the tools. | `workflow/validate.py:628` | `test_a_node_that_opts_in_gets_a_warning_naming_it_and_its_tools`, `tests/test_tool_sessions.py::test_a_run_lets_a_node_that_opted_in_have_tools_and_a_shell` |
| A node with `tools` is read-only on any harness, or on Codex at `edit` | It needs no opt-in: its commands can't reach the Keychain. | `workflow/harnesses/__init__.py:49` | `test_a_node_with_tools_and_a_shell_that_reaches_the_keychain_needs_the_opt_in` |
| A node has no tools, `tools: []` included | `wf validate` says nothing about its shell. | `workflow/validate.py:624` | `test_tools_with_shell_takes_a_boolean_and_matters_only_with_tools` |
| `tools_with_shell` isn't `true` or `false`, or is in `defaults` | `wf validate` errors: the field takes only a boolean, and only on the node, so that each such node says so itself. | `workflow/spec.py:193` | `test_tools_with_shell_takes_a_boolean_and_matters_only_with_tools`, `test_tools_with_shell_goes_on_the_node_not_in_defaults` |

#### Command nodes

| Field | Default | Meaning |
|---|---|---|
| `command` | required | A template, run with `bash -c` in the workspace. |
| `env` | none | Extra environment variables. Values are templates. |
| `secret_env` | none | Names in `env` whose values are secret. The run's trace never shows them, nor the values they come from. See [The trace](#the-trace). |
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
  code 2). `wf retry <run-id>` asks the questions later.
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
    cursor: {model: gpt-5.6-sol-high}   # Cursor requires a model; the id carries its effort
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
A Cursor model id carries its own effort, as in `gpt-5.6-sol-max-fast`, `gemini-4-argon-high` or `claude-fable-5-1-thinking-max`.
So `effort` doesn't apply to Cursor, and `wf validate` warns when it's set.
The id must be one that `cursor-agent --list-models` lists.
Any other makes Cursor unavailable for that attempt, so the next harness in the node's list runs.

### limits

```yaml
limits:
  cost_usd: 60         # stop starting new attempts once the run has spent this much
  tokens: 20000000     # the same, counted in tokens
  duration: 4h         # the same, for wall-clock time
```

When a limit is reached, running attempts finish, nothing new starts, and the run pauses
with status `limit`. It isn't over: `wf retry <run-id> --limit cost_usd=100` continues it
with a bigger budget (`--limit NAME=none` removes a limit). The new limit replaces the file's
for the rest of the run, so a later `wf retry` keeps it. The run carries on where it stopped: a
retry the limit held back runs, and a sub-workflow the limit stopped continues its own steps
rather than starting over. `wf retry` without a big enough limit leaves the run as it is
and says which limit is still reached. `wf run <file> --limit ...` starts a run with a
different limit.

- **Tokens** are input plus output tokens, as each harness reports them (cached input
  included).
  Claude Code and Codex report them.
  Cursor doesn't.
- **Cost** only counts harnesses that report it in dollars.
  Claude Code does.
  Codex and Cursor don't.
- A harness that doesn't report tokens or cost adds nothing to them, and `wf validate` warns when a limit or a template counts them.
  `duration` caps every harness.

## Templates and expressions

Text is evaluated with sandboxed Jinja, in two ways:

- **Templates** use `{{ }}` and `{% %}`. Fields: `instruction`, `human`, `command`, `env`,
  `with`, `base`, `branch`, `harness`, `model`, `effort`, and end `value`s.
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
  fails, but finished items are kept, so `wf retry` reruns only the failed ones. With
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
- A harness can ask for a short wait before the retry, after an error that a retry can clear.
  Cursor does this for a `RetriableError`: the retry on Cursor starts 30 seconds later.
  There's no wait before the next harness, or when no attempt follows.
  A limit reached before or during the wait ends it, and the retry doesn't start.
- An agent attempt that waits for an [agent slot](#agent-slots) hasn't started yet: the
  wait isn't a failure and uses no retry.
- When a node, or an item with `on_item_error: fail`, runs out of attempts, the run stops:
  running attempts finish, nothing new starts, and the run's status becomes `failed`.
  `wf retry` continues from there.

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
    point again.
    Cursor can't fork, so it loads the previous session itself (`session/load`).
  - The rendered instruction is sent as the next message. `session.continued` is true while
    it renders, so the template can send just the new feedback instead of the whole task.
    The runner adds that a new `submit_result` call is needed.
  - The pass starts fresh instead, and an event says why, when: the harness or model differs
    from the previous pass, the session can't be found, it's a Cursor retry after a failed
    continued attempt, or the two passes before it both continued.

### Agent slots

`max_parallel` caps the items of one node in one run.
Several runs can still open more agent sessions at once than the machine, or an account's
quota, holds.
`max_agents` caps the agent sessions that are live at once across those runs:

```yaml
# ~/.wf/config.yaml (WF_HOME moves ~/.wf)
max_agents: 4
```

The tests named below are in `tests/test_capacity.py`, unless another file is named.

| Condition | Effect | Source line | Conformance assertion |
|---|---|---|---|
| Runs of the user that use the same `~/.wf` | They share one set of slots, in `~/.wf/agent-slots/`. Runs with another `WF_HOME` have slots of their own. | `workflow/capacity.py:54` | `test_two_runs_in_separate_processes_share_the_cap`, `test_each_wf_home_has_its_own_slots`, `test_a_slot_file_names_its_holder` |
| Neither `WF_MAX_AGENTS` nor `max_agents` is set | There is no cap. No attempt waits, and no slot file is made. | `workflow/nodes.py:561` | `test_without_a_cap_nothing_waits_and_no_slot_is_made` |
| `WF_MAX_AGENTS` is set | It sets the same cap, and wins over `max_agents` in the file, which is still checked. | `workflow/capacity.py:64`, `workflow/capacity.py:66` | `test_the_cap_comes_from_wf_max_agents_or_the_config_in_wf_home`, `test_a_bad_cap_stops_wf_run_before_the_run_exists` |
| The cap isn't a whole number of 1 or more, including `max_agents` with no value | `wf run` and `wf retry` say why and exit 1 before anything runs. | `workflow/capacity.py:78`, `workflow/runner.py:73`, `workflow/runner.py:150` | `test_a_bad_cap_stops_wf_run_before_the_run_exists`, `test_a_bad_cap_stops_wf_retry` |
| `config.yaml` names a setting wf doesn't know, or doesn't hold settings by name | The same. So a misspelt `max_agents` can't quietly mean no cap. | `workflow/settings.py:50` | `test_a_wrong_file_fails_every_read` in `tests/test_settings.py`, `test_a_bad_cap_stops_wf_run_before_the_run_exists`, `test_a_bad_cap_stops_wf_retry` |
| An agent attempt starts, and a slot is free | It takes the slot before its session opens, and gives it back when the attempt ends. | `workflow/nodes.py:566`, `workflow/nodes.py:584` | `test_two_runs_in_separate_processes_share_the_cap`, `test_a_slot_file_names_its_holder` |
| An attempt holds a slot | The slot is the file `~/.wf/agent-slots/<n>`, which the attempt's process locks with `flock`. The file names its holder: pid, run, node and attempt. | `workflow/capacity.py:96`, `workflow/capacity.py:98`, `workflow/capacity.py:107` | `test_a_slot_has_one_holder_at_a_time`, `test_a_slot_file_names_its_holder` |
| A retry, or a fallback to the next harness | It takes a slot again. | `workflow/nodes.py:288` | `test_a_retry_and_a_fallback_take_a_slot_again` |
| A command node, a human node, a workflow node itself, or a dry run | It takes no slot. The agent nodes inside a workflow node do. | `workflow/nodes.py:262`, `workflow/nodes.py:561` | `test_command_and_human_nodes_take_no_slot`, `test_a_dry_run_takes_no_slot`, `test_an_agent_in_a_sub_workflow_waits_for_a_slot` |
| Several attempts wait for one slot | Each tries again every `POLL_SECONDS` (0.2 seconds), and the first try after the slot comes free gets it. So they get it one at a time, in an order that is not defined. | `workflow/capacity.py:46`, `workflow/capacity.py:117`, `workflow/capacity.py:120` | `test_a_wait_tries_again_every_0_2_seconds`, `test_attempts_that_wait_for_one_slot_take_it_one_at_a_time` |
| Every slot is taken | The attempt waits. The wait isn't a failure and uses no retry. The `attempt_started` event and the node's `timeout` come once the attempt has its slot. | `workflow/nodes.py:574`, `workflow/nodes.py:290`, `workflow/nodes.py:292` | `test_a_waiting_attempt_keeps_its_retries_and_its_timeout_starts_with_its_slot`, `test_a_retry_and_a_fallback_take_a_slot_again` |
| An attempt waits | `events.jsonl` gets `slot_waiting`, then `slot_taken` with `waited_s`. Until the attempt has its slot, `agents_waiting` in `status.json` lists it with its node, attempt number and start time, and `wf status` names it. | `workflow/nodes.py:570`, `workflow/nodes.py:580`, `workflow/context.py:174`, `workflow/cli.py:412` | `test_a_waiting_attempt_keeps_its_retries_and_its_timeout_starts_with_its_slot`, `test_a_killed_process_frees_its_slot` |
| A run limit is reached during the wait (the `duration` limit counts the wait) | The wait ends, the attempt doesn't start, and the run pauses at the limit. A slot that comes free at that moment goes back unused. | `workflow/capacity.py:117`, `workflow/nodes.py:578` | `test_a_limit_reached_while_waiting_ends_the_wait_and_the_attempt_never_starts`, `test_a_slot_that_comes_free_as_a_limit_is_reached_goes_back_unused`, `test_a_retry_that_a_limit_stops_while_it_waits_starts_once_after_wf_retry` |
| A process that holds a slot dies | The operating system releases the lock, and an attempt that waits can take the slot. | `workflow/capacity.py:98` | `test_a_killed_process_frees_its_slot` |
| A process dies while its attempt waits | `wf status` says `stopped`, without `agents_waiting`. | `workflow/operate.py:58` | `test_a_killed_waiter_is_stopped_and_no_longer_waiting` |
| Processes run with different caps | A cap of N uses slots 0 to N-1, so the total stays within the biggest cap. | `workflow/capacity.py:95` | `test_a_smaller_cap_uses_only_the_first_slots` |
| A `wf run` or `wf retry` process starts | It reads the cap once, and keeps it until it ends. | `workflow/runner.py:73`, `workflow/runner.py:150` | `test_wf_run_and_wf_retry_read_the_cap_once_when_they_start` |

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

`submit_result` is a Python function in the runner, and each harness reaches it its own way:
- **Claude Code**: an SDK MCP server inside the runner process, like
  `claude_code_chat/native_tools.py`.
- **Codex**: a dynamic tool, declared when the thread starts. The app server sends each call
  to the runner as an `item/tool/call` request on the same connection.
- **Cursor**: an MCP server the runner serves over HTTP on 127.0.0.1, given to Cursor when the session starts.
  Each session gets its own URL, with an unguessable token in it.
  A request from a web page (an Origin header that isn't 127.0.0.1 or localhost) gets no answer.

Each call arrives tied to its session: a process, a thread id or a session's URL.
So a result can only reach the attempt it belongs to.

A node's `tools` reach the agent the same way.
wf connects to each vendor's MCP server itself, and offers the agent the operations the node may use as tools of its own.
Claude Code gets an SDK MCP server per tool, and Codex a namespace of dynamic tools per tool.
Cursor gets an MCP server over HTTP per tool.
Each call comes back to the runner, which passes it on with the stored sign-in, so the secret never reaches the agent.

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
- `branch` names a node's branch instead, as a template: `branch: "{{ start.branch }}"`
  lets whoever starts the run choose the branch that carries the result. The name is
  rendered on the node's first pass; later passes and `wf retry` keep that branch. A
  `for_each` item, also one in a sub-workflow that a `for_each` node runs, renders its
  name again on every pass. Worktrees go by item index, so the item fails if its index
  names another branch than before.
- The name can't be empty, `wf`, or under `wf/`, in any case. A branch that already
  exists is refused, unless this run made it for the same node. So is a name that git
  can't have beside an existing branch: `fix/a` beside `fix/a/b`, or `FIX/a` beside
  `fix/a` where git ignores case (`core.ignorecase`, as on macOS and Windows).
  `branches.json` in the run folder records the branches a run made.
- `wf discard --branches` deletes a named branch while a worktree of the run has it
  checked out, or while the branch is on the commit that the run left it on. After that,
  somebody may have made it again or worked on it, so it stays, and discard says so. It
  also says when git refuses to delete a branch, for example one that is checked out. A
  dry run that succeeds deletes its named branches.
- A node keeps the same worktree across passes, so a loop keeps working on one branch.
  Each item gets its own.
- Worktrees live outside the repo, under `~/.wf/worktrees/`, so agents searching the repo
  don't find copies of it.
- After an `edit` or `full` node finishes in a worktree the runner created, the runner
  commits anything left uncommitted (`wf: <node> (<run-id>)`). It never commits in your own
  checkout. The runner tells `edit` agents not to commit, because some harness sandboxes
  keep `.git` read-only.
- The commit skips git hooks, and it is signed when your git config signs commits.
  "Commit signing" below has the rules.
- When a run succeeds, its worktrees are removed and its branches are kept. A failed run
  keeps its worktrees for `wf retry`.
- `wf validate` warns when two `edit` nodes could run at the same time in the same workspace.

### Commit signing

The runner signs its commits as your git config says, with nobody at a terminal.
So use a signing key that needs no passphrase, PIN or touch.
For example, set a key file's absolute path as the repo's `user.signingkey`.
The tests named below are in `tests/test_workspaces.py`.

| Condition | Effect | Source line | Conformance assertion |
|---|---|---|---|
| The runner commits in a worktree it made | The commit skips every git hook, so its message is only `wf: <node> (<run-id>)`, with no trailers. | `workflow/workspaces.py:188` | `test_the_runners_commit_message_has_no_trailer_even_when_a_hook_would_add_one` |
| Your git config signs commits (`commit.gpgsign`) | The runner's commit is signed too, with your signing key (`user.signingkey`). | `workflow/workspaces.py:189` | `test_the_runner_signs_its_commits_when_your_git_config_does` |
| A run may commit: an `edit` or `full` agent works in a worktree the runner made, directly, through a node's workspace or in a sub-workflow | `wf run` and `wf retry` sign a test commit before any agent works. The test commit goes to a temporary object folder, so the repository keeps nothing. | `workflow/runner.py:94`, `workflow/runner.py:194`, `workflow/runner.py:293`, `workflow/workspaces.py:257` | `test_checking_the_key_leaves_nothing_in_the_repository`, `test_an_agent_that_edits_another_nodes_worktree_needs_a_key_that_signs`, `test_an_edit_agent_in_a_sub_workflow_needs_a_key_that_signs_when_it_works_in_a_worktree`, `test_a_run_from_an_unborn_branch_with_a_base_of_its_own_checks_the_key`, `test_a_retried_run_checks_the_key_again_before_any_agent_works`, `test_a_retried_run_checks_the_key_for_an_edit_agent_in_a_sub_workflow`, `test_a_retried_run_without_its_run_json_checks_the_key_where_it_ran` |
| No agent of the run commits: each `edit` agent works in your checkout, and the others only read | There is no test commit, so a key that can't sign doesn't stop the run. | `workflow/runner.py:284` | `test_a_run_the_runner_makes_no_commits_in_doesnt_check_the_key` |
| The test commit fails, or isn't signed within 30 seconds (`SIGNING_TIMEOUT`) | The command exits 1 before any agent works. The error says the key needs a touch, a PIN or a passphrase, or doesn't work. It names the fix: a key file (ssh) or a key id (OpenPGP) that needs none, as the repository's `user.signingkey`. | `workflow/workspaces.py:225`, `workflow/workspaces.py:60` | `test_a_run_whose_key_cant_sign_fails_at_its_start_and_says_how_to_fix_it`, `test_a_key_waiting_for_a_touch_fails_the_run_at_its_start_within_the_timeout`, `test_the_fix_for_an_openpgp_key_names_a_key_id` |
| The repository signs with ssh, and `user.signingkey` is a relative path | The command exits 1 before any agent works, and asks for the key's absolute path. The runner commits in worktrees outside your checkout, where the relative path finds no key. | `workflow/workspaces.py:211` | `test_a_relative_signing_key_fails_the_run_at_its_start` |
| The signing program asks for a passphrase or a PIN at a terminal | It finds none, because git runs in a session of its own, so signing fails at once. | `workflow/workspaces.py:423` | `test_the_signing_program_has_no_terminal_to_ask_on` |
| `wf retry` of a run that has ended | There is no test commit. It says how the run ended. | `workflow/runner.py:194` | `test_retrying_a_run_that_succeeded_says_so_without_checking_the_key` |
| The background process of a detached run fails the test | `status.json` says `failed`, with the error, so `wf wait` reports the cause and not a crash. | `workflow/runner.py:197` | `test_a_detached_run_whose_background_check_fails_records_why` |
| The runner's commit fails | The error says that signing failed only after a timeout, or when a signed test commit fails and an unsigned one works. Otherwise it is git's own error, as `git commit failed: ...`. | `workflow/workspaces.py:197`, `workflow/workspaces.py:263` | `test_a_commit_that_cant_be_signed_says_so`, `test_a_commit_that_fails_for_another_reason_doesnt_blame_signing`, `test_a_commit_whose_signing_test_cant_write_either_doesnt_blame_signing` |
| An agent succeeds, and then the runner's commit fails | The node fails with the attempt that ran, so `wf retry` starts at the next attempt number. | `workflow/nodes.py:524` | `test_a_failed_commit_keeps_the_attempt_number_so_wf_retry_starts_at_the_next_one` |
| wf stops git: after `SIGNING_TIMEOUT`, for a cancelled job, on Ctrl-C, SIGHUP or SIGTERM | Its process group, with the signing program, gets SIGTERM, so git removes its temporary files. Whatever still runs 5 seconds later (`GRACE`) gets SIGKILL. | `workflow/workspaces.py:457`, `workflow/workspaces.py:462` | `test_a_signing_timeout_lets_git_remove_its_temporary_file`, `test_a_signing_program_that_ignores_sigterm_is_killed` |
| A job is cancelled, for example by a node's `timeout` or Ctrl-C during a run | Its git command stops, and none of its later git commands starts. Its error says it was stopped, not that signing failed. | `workflow/nodes.py:249`, `workflow/workspaces.py:418` | `test_cancelling_a_job_stops_its_git_command_and_the_signing_program`, `test_a_job_cancelled_while_it_waits_for_git_starts_none`, `test_a_stopped_commit_isnt_called_a_signing_failure` |
| Ctrl-C while git runs in wf's main thread, as in the test before a run | git stops, and wf stops as for any Ctrl-C. | `workflow/workspaces.py:449` | `test_ctrl_c_stops_git_and_the_signing_program` |
| wf gets SIGHUP (its terminal closed) or SIGTERM | It stops every git command, also one that is just starting, and starts no new one. Then it exits by that signal, as before. SIGKILL can't be handled, so it still leaves git running. | `workflow/cli.py:241`, `workflow/workspaces.py:374`, `workflow/workspaces.py:383` | `test_wf_stops_git_and_the_signing_program_when_its_terminal_closes_or_it_is_killed`, `test_wf_stops_the_runners_commit_and_starts_no_more_git_when_it_is_killed`, `test_a_signal_while_git_starts_still_stops_that_git` |
| SIGHUP is ignored when wf starts, as under `nohup` | It stays ignored. | `workflow/workspaces.py:364` | `test_a_hangup_that_wf_ignores_stays_ignored` |

## Run folder

```
.wf/runs/<run-id>/              <run-id> is like 20261018-1432-implement-k3f9
  workflow/                     copies of the workflow files as they were run
  input.json
  branches.json                 the branches `branch` named, and where the run left them
  output.json                   the end output, once the run succeeds
  status.json                   running | waiting | succeeded | failed | limit
  events.jsonl                  one line per event: node started, finished, route taken, agent session started, ...
  trace.jsonl                   one record per line of what the run read, decided and did (see The trace)
  blobs/                        values too long for a line of the trace, by their sha256
  checkpoints.sqlite            LangGraph checkpoints, used by wf retry
  nodes/<node>/<pass>/          one folder per pass ...
    [<index>/]                  ... and per item
      instruction.md            the rendered instruction (or command, or message)
      output.json
      attempt-<n>.log           harness transcript or command output
      attempt-<n>.stdout        a command's stdout, whole
      attempt-<n>.stderr        a command's stderr, whole
```

- `.wf/` is added to the repo's `.git/info/exclude`, so it never shows up in `git status`.
- While a run is `running`, `agents_waiting` in `status.json` lists the attempts waiting
  for an [agent slot](#agent-slots).
- A runs folder keeps run folders outside the repo instead: `--runs-dir <dir>`, or the
  `WF_RUNS_DIR` environment variable (the option wins). Run folders then go to
  `<dir>/<repo key>/<run-id>/`. The repo key is the repo folder's name and a hash of its
  path, like `wf-3f9a1c2b4d5e`, so the runs of two repos never mix. The repo's working
  tree and `.git/info/exclude` then stay as they were. `workspace: new` still adds its
  branches, and git's records of its worktrees in `.git/worktrees/`; the worktree folders
  are under `~/.wf/worktrees/` (see [Workspaces](#workspaces)). A runs folder inside the
  repo goes into `.git/info/exclude`, as `.wf/` does. Later commands for the run need the
  same setting, in the same folder. A blank value counts as no setting. For a run in
  `.wf/runs/` while `WF_RUNS_DIR` is set, the commands that wf prints start with
  `WF_RUNS_DIR=`, which turns the setting off for that command.
- `wf retry` uses the copies in `workflow/`, so editing the YAML doesn't change a run
  that's already in progress.

### The trace

`trace.jsonl` records each step of a run, so you can debug the run or review it later from the trace alone.
`events.jsonl` stays as it is, for progress lines and `wf report`.
Each line is one JSON object.
wf only adds lines, and never changes one.
A sub-workflow writes to the trace of its top-level run.

| `kind` | Written when | Holds |
|---|---|---|
| `run` | A `wf run` or `wf retry` process starts to drive the run. | `started` or `retried`, the workflow and the input. |
| `pass` | A node starts a pass. | The node and pass, and for a `for_each` node the number of items. |
| `job` | A pass of a node, or an item, starts in this process. | The item, and the number of its first attempt. |
| `render` | wf renders a template or evaluates an expression. | The field, the template, each variable path that it read with its value, and the result or the error. |
| `routes` | A node chooses its route. | Each route in order: its `when`, the values that it read, its result, and if its `max` was used up. Then the route that the node took. |
| `attempt` | An attempt starts, before wf opens its session. | The harness, model, effort, access, `web`, workspace and tools, and how the session is to start: `fresh`, `fork` or `load`, with the session that it continues. |
| `turn` | wf sends the instruction or a reminder. | The text that wf sent. |
| `message` | The agent writes text. | The text. |
| `tool_call` | The agent calls a tool: when the harness says that the call starts, and again when it gives more of the arguments. | The tool and all of its arguments. The last record of an id holds all that the harness gave. A call that a timeout stops has one too. |
| `tool_result` | The tool answers. | All of the answer, and if it is an error. |
| `submit_result` | The agent calls `submit_result`. | The arguments, and the problems that wf found, or that wf accepted the result. |
| `turn_end` | The turn of the agent ends. | How the turn ended, and what it used. |
| `attempt_end` | An attempt ends. | All that `attempt` holds, how the session started and its id, the start and end, how the attempt ended and its usage. For a command, the command, its `env` values, its exit code, and the files with its stdout and stderr. |
| `job_end` | A job ends. | Its output or its error, or that it waits for a person. |
| `run_stopped` | The process stops to drive the run. | The status of the run, and its output when it succeeded. |

These fields render: `instruction`, `human`, `command`, `env.<name>`, `base`, `branch`, `harness`, `with.<name>`, `for_each`, `routes[<i>].when` and `end.output.<name>`.
`model` and `effort` render too, with the harness and the key that sets them.

Each record has an `id`, and the `parent` id that contains it:

```
run                          the run
review#2                     pass 2 of review: its pass, job, render, routes and job_end records
build#1[3]                   item 3 of pass 1 of build
review#2.a1                  attempt 1 of that pass
review#2.a1.t2               turn 2 of that attempt: the first reminder
review#2.a1.t2.c4            tool call 4 of that turn (s1 is a submit_result call, m1 a message)
end                          the end outputs
build#1[3].a1/implement#2    pass 2 of implement, in the sub-workflow that attempt 1 of build[3] runs
```

A record about a node also has `node`, `pass`, `index` for an item, `attempt` and `turn`.
Inside a sub-workflow it also has `where`, such as `build[3]/`.

A render lists each variable path that the template read while it ran, such as `plan.tasks[0].title`, with the value that the path had.
A branch of the template that doesn't run reads nothing.
When a template reads a field inside a list or a map, the render lists the field, and not all of the list or map.

A value of more than 1,000 characters goes to `blobs/<sha256>.txt`, or to `.json` when it isn't text.
wf writes each such file once, however often the value occurs.
The record holds `{"$blob": "blobs/<sha256>.txt", "size": ..., "start": ...}` in place of the value.

Secrets don't go into the trace:

- `secret_env` on a command node names the `env` values that are secret.
  The trace shows `[secret]` in place of such a value.
  It does the same for the values that the secret comes from: start inputs, fields of node outputs, `for_each` items, and `with` values.
  The output of a sub-workflow that feeds a secret of its parent is hidden inside the sub-workflow too.
  An `item` is secret only in the node whose `for_each` gives it.
- From the time that the run has a secret value, the trace removes its text from each later record, wherever it occurs.
  It does so for short text too.
  A secret that only an agent gives is hidden from its `submit_result` call, but not from the tool calls before that call.
- A `tool_result` of a wf tool holds only the size of the answer, as the transcript of the attempt does.
  The token of a wf tool never goes into the trace.

When wf can't write the trace, it says so once, and the run continues.

`wf trace <run-id>` reads the trace:

| Command | Shows |
|---|---|
| `wf trace <run-id>` | All the records, as an outline: passes, renders, routes, attempts, turns, tool calls and outputs. |
| `--node <node> --pass <n>` | Only that node and pass. A node is its id, which matches it in each item and sub-workflow, or its path, such as `build[1]/implement`. |
| `--field <field>` | Each render of the field, with what it read and gave. `routes` shows each route decision. `env` shows each `env.<name>`. |
| `--var <path>` | The value of a variable path in each render that read it, pass after pass, also when the render read all of the path or a field inside it. |
| `--json` | The same, as JSON, with each value from `blobs/` read back in full. |

The tests named below are in `tests/test_trace.py`, unless another file is named.

| Condition | Effect | Source line | Conformance assertion |
|---|---|---|---|
| A run starts, renders, routes, makes attempts, and ends | The trace has a record of each kind, and each `parent` is the id of a record that contains it. | `workflow/trace.py:356` | `test_a_runs_trace_holds_each_kind_of_record_and_its_ids_nest` |
| A sub-workflow runs | Its records go into the trace of the top-level run, under the id of the attempt that runs it. | `workflow/context.py:92` | `test_a_sub_workflows_records_nest_under_the_attempt_that_runs_it` |
| A template or expression runs | The render lists each variable path that ran, with its value. A branch that doesn't run reads nothing. A list or map that the template also used itself, such as with `\| length`, is listed with its fields. | `workflow/templates.py:111`, `workflow/templates.py:301` | `test_a_render_reads_each_path_it_used_with_its_value` |
| A value has more than 1,000 characters, or looks like a reference to a blob | It goes to `blobs/` once, and each record refers to it by its sha256. | `workflow/trace.py:154` | `test_a_large_value_is_stored_once_and_referenced`, `test_wf_trace_reads_only_blobs_of_the_run` |
| A command node names an `env` value in `secret_env` | The trace shows `[secret]` for it, for the input that it comes from, and for its text in each later record, also after `wf retry`. | `workflow/trace.py:372`, `workflow/runner.py:372` | `test_a_secret_never_enters_the_trace` |
| The secret value comes from `item` | The trace hides the item in that node only. It also hides the element of the `for_each` list, or all that the `for_each` reads when it isn't a plain path. An index such as `[-1]` counts as any element. | `workflow/trace.py:292` | `test_a_secret_item_is_hidden_whatever_its_for_each_and_only_in_its_node` |
| The secret value comes from the output of a workflow node, or from its `history` | The sub-workflow hides the end output that gives it, and what that end output reads. All of a node's `history`, or all of `history`, hides each end output. | `workflow/trace.py:264`, `workflow/graph.py:122` | `test_a_secret_that_a_sub_workflow_returns_is_hidden_inside_it_too` |
| A secret value is short | Its text is removed all the same. | `workflow/trace.py:118` | `test_a_short_secret_is_removed_wherever_it_shows` |
| A harness reports a `submit_result` call before `submit_result` runs | The `tool_call` hides the secret fields of the result, as the `submit_result` record does. | `workflow/trace.py:535` | `test_a_secret_in_an_agents_result_is_hidden_from_its_harness_record_of_the_call` |
| `secret_env` names a variable that `env` doesn't set | `wf validate` errors. | `workflow/validate.py:352` | `tests/test_validate.py::test_secret_env_names_only_what_env_sets` |
| An agent uses a wf tool | The `tool_call` holds the arguments. The `tool_result` holds only the size of the answer, on each harness. | `workflow/harnesses/claude_code.py:235`, `workflow/harnesses/codex.py:313`, `workflow/harnesses/cursor.py:394` | `tests/test_tool_sessions.py::test_claude_code_reports_text_and_tool_calls_for_the_trace_but_not_what_a_wf_tool_returned`, `tests/test_tool_sessions.py::test_codex_reports_text_and_tool_calls_for_the_trace_but_not_what_a_wf_tool_returned`, `tests/test_tool_sessions.py::test_cursor_reports_text_and_tool_calls_for_the_trace_but_not_what_a_wf_tool_returned` |
| A timeout stops a tool call on Codex or Cursor | The trace has its `tool_call`, written when the call started. | `workflow/harnesses/codex.py:246`, `workflow/harnesses/cursor.py:383`, `workflow/harnesses/cursor.py:419` | `tests/test_tool_sessions.py::test_codex_reports_a_tool_call_that_a_timeout_stops_for_the_trace`, `tests/test_tool_sessions.py::test_cursor_reports_a_tool_call_that_a_timeout_stops_for_the_trace` |
| The harness gives more of a call's arguments after the call started | The `tool_call` record repeats with the same id and all the arguments. | `workflow/harnesses/cursor.py:384`, `workflow/trace.py:535` | `tests/test_tool_sessions.py::test_cursor_reports_a_tool_call_again_when_its_input_comes_late` |
| An agent attempt fails before its session opens | Its `attempt` record has the harness, model, effort, access and workspace all the same. | `workflow/nodes.py:417` | `test_an_attempt_that_fails_before_its_session_has_its_settings` |
| A wf tool gets its token | The token goes into no file in the run folder or in `~/.wf`, the trace included. | `workflow/tools/relay.py:199` | `tests/test_tool_sessions.py::test_no_secret_reaches_an_agent_or_the_run_folder` |
| A value holds text that UTF-8 can't hold, such as a lone surrogate | The trace writes it escaped, and the attempt goes on. `wf trace` prints it escaped too. | `workflow/trace.py:127`, `workflow/trace_view.py:158` | `test_writing_the_trace_survives_text_that_utf8_cannot_hold`, `test_wf_trace_prints_text_that_utf8_cannot_hold_escaped` |
| A command's log or stream file can't be opened | The command doesn't start. | `workflow/nodes.py:677` | `test_a_command_whose_stream_file_cant_be_opened_never_starts` |
| `wf retry` stops without driving the run: it still waits for a person, or the run has ended | It writes no `run` record. Each `run` record has a `run_stopped` record. | `workflow/runner.py:242` | `test_each_run_record_has_a_run_stopped_record_whatever_wf_retry_does` |
| `wf trace <run-id> --var <path>` | It shows the value of the path in each render that read it, pass after pass. | `workflow/trace_view.py:119` | `test_wf_trace_var_shows_a_value_across_the_passes_of_a_loop` |
| `wf trace <run-id> --field routes` | It shows each route with its `when`, the values that it read, its result and its `max`, and the route that the node took. | `workflow/trace_view.py:259` | `test_wf_trace_says_why_a_route_was_taken` |
| `wf trace --json` | Each value from `blobs/` comes back in full. | `workflow/trace_view.py:62` | `test_wf_trace_reads_blobs_back_in_json_and_shows_a_missing_one_as_its_reference` |
| A reference names a file outside `blobs/`, or a blob that is gone | `wf trace` shows the reference, and reads no other file. | `workflow/trace_view.py:47`, `workflow/trace_view.py:188` | `test_wf_trace_reads_only_blobs_of_the_run`, `test_wf_trace_reads_blobs_back_in_json_and_shows_a_missing_one_as_its_reference` |

## Watching runs

A person or a supervisor can see a run while it works.
The commands below only read the run folder, so they never change the run.
The tests named in the table are in `tests/test_progress.py`, `tests/test_observe.py` and `tests/test_ui.py`.

### Progress

`wf status <run-id>` adds a line that says how far the run is:

```
20261019-1432-implement-k3f9 [implement]: running
  step 2 of 4 (implement), review → implement 1 of at most 3, about 31%
```

`wf status <run-id> --json` gives the same under `progress`, with the state of each node.
A node is `waiting`, `running`, `paused` (the run waits for a person, a limit or `wf retry`), `done`, `failed` or `skipped`.

The percentage is an estimate.
wf can't know before the end which routes a run takes, or how many times a loop goes back.
It counts like this:

- A walk from `start` finds the routes that go back.
  Each of them closes a loop.
- Without the loops, a node's step is the number of nodes on the longest path from `start` to it.
  The longest path from `start` to `end` gives the number of steps.
- The run's open nodes are the nodes that run, wait to start, failed, or finished and wait for their route.
  The rest of the way is the longest path from an open node to `end`, with what is left of that node.
  The open node with the most left decides how far the run is.
  Of open nodes with as much left, the one with the least done decides.
  So a slow parallel step holds the estimate back.
- A step that a route left out weighs nothing, also on a path that leads back into the branch that the route chose.
  So it counts no more, in the steps or in the percentage.
- A loop counts once for each time that the run went back along it.
  A lap is the longest path from the step that the loop goes back to, to the step that goes back.
- Each step weighs 1, and each loop counts as if the run goes back up to its `max`.
  So the percentage is low rather than high.
  In a run without parallel steps, it doesn't go down.
- When the runs folder holds earlier runs of the same workflow that succeeded, wf uses the newest 10 of them.
  A dry run uses only dry runs, and a real run only real runs.
  Each step then weighs its mean time per pass in them.
  A step that none of them ran weighs the mean of the other steps.
  Each loop counts as many times as they went back on average.
  `wf status` then also gives the time left, and `left_s` in the JSON.
- A step in progress counts in part.
  For a `for_each` step, that part is the share of its items that finished.
  For a workflow node, it is the share of its sub-workflow that is done, with the items of its steps.
  With earlier runs, another step counts its time so far against its mean time, or against the mean of the other steps.
  While the run doesn't move, that time stops at the run's last event.

### Agent sessions and events

Each agent session adds `session_started` to `events.jsonl` when it opens, and `session_ended` when it closes.
`session_started` names the node, pass, item, attempt, harness, model and effort, the process that runs the session, and its transcript in the run folder.

A session runs until its `session_ended`, or until its attempt, pass or item ends.
The end of an attempt, pass or item of a workflow node also ends each session inside it.
A sub-workflow that its attempt stops, for example at the node's `timeout`, can't pass up its own `session_ended`.

`wf sessions [run-id]` lists the agent sessions that run now, in one run or in each run of the repo.
For each session it gives the run, node, pass, item, harness and model.
It also gives when the session started, its last tool call, and how long it has been quiet.
`--json` gives the same for a supervisor, with the paths of the transcript and the instruction.

`wf watch <run-id>` prints the run's last 20 events as `wf run` prints them, each with its time, and then each new event as it comes.
`--from-start` prints all the events first.
Each time the run needs attention, as `wf wait` would report it, `wf watch` prints the run's state and goes on.
So it follows the run through `wf answer` and `wf retry`.
It stops when the run succeeds or is discarded, or at Ctrl-C.
Keep it open in a terminal pane to follow a run.

### The web page

`wf ui` starts a web server on this machine and prints the page's address, `http://127.0.0.1:8765/#token=<key>`, unless the port is in use.
`--port` sets another port from 0 to 65535, and `--open` opens the page in the browser.
Each server makes a new random key.
The page sends the key with each request for data, and the server answers no request without it.
So another program or user on the machine can't read the transcripts through the server.
The page lists the runs of the repo, newest first.
With `--all-repos` it lists the runs of each repo in the runs folder.
For the run that you choose, the page shows these:

- the workflow graph, with each node in the color of its state, a word and a sign for the state, and each loop's count
- the progress estimate
- the numbers of each node as `wf report` counts them: passes, attempts, failed attempts, items, tokens, cost and time
- the agent sessions that run now
- the events, as `wf watch` prints them
- the transcript, instruction and output of each attempt

The page gets new data on its own: the run every 2 seconds, its events every second, and the list of runs every 5 seconds.
For each request, the server reads again only the attempt folders of the nodes that run, or that new events name.
So a refresh costs no more for a run with many passes that ended.
It is three plain files in `workflow/ui/`, with no build step and nothing from another server.

| Condition | Effect | Source line | Conformance assertion |
|---|---|---|---|
| A run of a workflow with a loop, and no earlier runs that succeeded | Each step weighs 1, and the loop counts up to its `max`. Event by event, the percentage goes up and never down. That holds also between a node's end and its route. | `workflow/progress.py:233`, `workflow/progress.py:98` | `test_without_earlier_runs_each_step_weighs_one_and_each_loop_counts_to_its_max`, `test_the_percentage_never_goes_down_event_by_event_even_before_a_route_is_chosen` |
| A loop's way back has no `max` | The smallest `max` of another route in the loop bounds it. Only the way back counts its laps. | `workflow/shape.py:111`, `workflow/progress.py:282` | `test_a_loop_whose_way_back_has_no_max_is_bounded_by_the_route_that_has_one_and_counted_by_its_own` |
| A heavier path leads into a loop | A lap counts only the loop's own path. | `workflow/shape.py:88` | `test_a_lap_is_the_loop_s_own_path_not_a_heavier_one_into_it` |
| A needs edge closes a loop | The lap counts when the step that the edge leaves finishes, after the step that the edge goes to has run. When `start` also leads into the step that the edge leaves, its first end is not a lap. | `workflow/progress.py:102`, `workflow/progress.py:284` | `test_a_loop_that_a_needs_edge_closes_counts_its_lap_when_it_goes_back`, `test_a_needs_edge_loop_counts_no_lap_before_its_first_step_ran` |
| The runs folder holds earlier runs of the workflow that succeeded | Steps weigh their mean time and loops their mean count, from `wf report`. `left_s` gives the time left. A dry run counts only for a dry run. The numbers belong to the version of the workflow that asks for them. | `workflow/progress.py:160`, `workflow/progress.py:176` | `test_with_earlier_runs_steps_weigh_their_time_and_loops_their_mean_count`, `test_wf_status_says_how_far_a_run_is_and_learns_from_earlier_real_runs` |
| A step that no earlier run ran | It weighs the mean of the other steps, and its time so far counts against that mean. | `workflow/progress.py:231`, `workflow/progress.py:265` | `test_a_step_that_no_earlier_run_ran_weighs_the_mean_of_the_others` |
| The run has gone past a loop | The loop counts no more. | `workflow/progress.py:288` | `test_a_loop_the_run_has_left_counts_no_more` |
| A route leaves out a branch | The nodes on it are `skipped`. The steps, the percentage and the time left leave them out. | `workflow/progress.py:271`, `workflow/progress.py:316` | `test_a_branch_a_route_left_out_is_skipped_and_counts_no_more` |
| A branch that a route left out leads back into the chosen branch | Its steps weigh nothing, in the time and in the step count. | `workflow/progress.py:233`, `workflow/progress.py:234` | `test_a_branch_left_out_that_leads_back_into_the_chosen_one_counts_no_more` |
| Parallel steps are in progress | The step with the most left decides the estimate. Of steps with as much left, the one with the least done decides, whatever the order of the steps. | `workflow/progress.py:271` | `test_a_slow_parallel_step_holds_the_estimate_back`, `test_open_steps_with_as_much_left_are_told_apart_by_the_least_done_not_by_chance` |
| A run waits for a person, is at a limit, or stopped | Its steps in progress are `paused`, and the time of each step stops at the run's last event. | `workflow/progress.py:309`, `workflow/progress.py:223` | `test_a_step_in_progress_of_a_run_that_does_not_move_is_paused`, `test_a_run_that_does_not_move_stops_its_clock_at_its_last_event` |
| A `for_each` step, or a sub-workflow, is in progress | Its items that finished, or its sub-workflow's own estimate, count for it in part, before its time does. | `workflow/progress.py:260`, `workflow/progress.py:246` | `test_the_items_that_finished_beat_the_time_a_step_took`, `test_a_sub_workflow_in_progress_counts_its_own_steps_and_items` |
| A run's files go while `wf status` reads them | It says where the run stands, with no estimate. | `workflow/progress.py:212`, `workflow/report.py:260` | `test_wf_status_still_says_where_a_run_stands_when_its_files_change_meanwhile` |
| An agent session opens, and closes | `events.jsonl` gets `session_started`, and then `session_ended`. | `workflow/nodes.py:479`, `workflow/nodes.py:516` | `test_each_session_starts_and_ends_with_an_event`, `test_a_session_inside_a_sub_workflow_names_its_transcript_from_the_run_folder` |
| A session has no `session_ended`, its attempt goes on, and its process drives the run | `wf sessions` lists it. | `workflow/observe.py:70` | `test_wf_sessions_lists_the_agent_sessions_that_run_now` |
| The attempt, pass or item of a session ends, or that of the workflow node around it | The session is over, also with no `session_ended`. | `workflow/observe.py:65`, `workflow/observe.py:94` | `test_a_session_ends_with_its_attempt_its_item_or_the_workflow_node_around_it`, `test_a_session_inside_a_workflow_node_that_timed_out_is_not_listed` |
| A process that no longer drives the run left a session open | `wf sessions` doesn't list it. | `workflow/observe.py:70` | `test_a_session_left_open_by_a_process_that_no_longer_drives_the_run_is_not_listed` |
| `wf watch` follows a run | It prints each new event, and the run's state each time that the run needs attention. It stops once the run succeeds or is discarded. | `workflow/observe.py:178`, `workflow/observe.py:174`, `workflow/observe.py:168` | `test_wf_watch_prints_the_steps_and_tool_calls_as_they_come_until_the_run_ends`, `test_wf_watch_goes_on_through_a_question_until_the_run_succeeds`, `test_wf_watch_stops_when_the_run_is_discarded`, `test_wf_watch_on_a_run_that_ended_prints_the_last_events_and_how_it_ended` |
| A request to `wf ui` for data has no key, or another key | The server refuses it with 401. | `workflow/ui.py:315` | `test_the_server_gives_data_only_to_the_page_that_has_its_key` |
| A request to `wf ui` names another host in its `Host` header | The server refuses it with 403. So a web page can't read it through a DNS name that points to `127.0.0.1`. | `workflow/ui.py:307` | `test_the_server_refuses_other_hosts_unknown_runs_and_files_outside_the_passes` |
| The page asks for a file of a run | Only `instruction.md`, `output.json`, `session.json` and `attempt-<n>.log` under `nodes/` come back, and no link out of `nodes/` is followed. | `workflow/ui.py:265` | `test_the_page_shows_files_of_the_passes_and_nothing_else` |
| The page shows what a run wrote | It puts it in the page as text, never as HTML, and the server lets the page run only its own script. | `workflow/ui.py:340` | `test_the_server_serves_the_page_and_its_json_on_this_machine_only` |
| `--port` isn't a number from 0 to 65535 | `wf ui` says so and exits with code 1, before it starts a server. | `workflow/cli.py:251` | `test_a_port_that_is_not_a_port_is_refused_with_a_message` |
| The port is in use | `wf ui` uses a free port, and says which. | `workflow/ui.py:357` | `test_a_port_in_use_gives_way_to_a_free_one` |
| The page asks for a run again | The server reads again only the attempt folders of the nodes that run, or that new events name, from the newest pass that it read before. A node writes its files before the event that ends it. | `workflow/ui.py:179`, `workflow/ui.py:185`, `workflow/ui.py:210` | `test_a_refresh_reads_only_the_folders_of_nodes_that_run_or_that_new_events_name`, `test_a_transcript_shows_once_the_running_session_writes_it` |

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
12. `branch` is only on nodes with `workspace: new`. On a `for_each` node it uses `item` or
    `index`. A `for_each` workflow node gives `item` or `index`, through `with`, to each
    `branch` in its sub-workflow. `branch` isn't empty, isn't `wf` or under `wf/` in any
    case, and doesn't read the node's own workspace.
    No two `branch` fields, in a file and the sub-workflows it runs, can name the same
    branch, or a branch and one under it (`fix/a` and `fix/a/b`). Names compare with case
    ignored, on every machine: git ignores case on macOS and Windows, so a file that
    validates on Linux must also run there. The spaces around a name don't count. The
    check reads each `{{ }}` or `{% %}` as text without a `/`, and a sub-workflow's
    `{{ start.<input> }}` as the value that `with` gives. So `fix/{{ start.task }}` and
    `review/{{ start.task }}` are apart, and so are `fm/{{ start.task }}/author` and
    `fm/{{ start.task }}/review`. The run refuses a clash that the check can't see.
13. Each `tools` entry names a known tool, and no tool is listed twice, in a node and in `defaults`.
    `write` is `true` or `false`.
14. A node with `tools` can't run where its agent's commands reach the Keychain, unless it sets `tools_with_shell: true`.
15. Each name in a node's `secret_env` is set in its `env`.
    Each name in `defaults.secret_env` is set in the `env` of a command node.

Warnings:
- A template reads a node that might not have run at that point, outside an `{% if %}`
  that checks it.
- A `command` uses node outputs without `env` or `| quote`, or uses a `start` input that a
  parent workflow fills from node output, or a `workspace.<node>.branch` that a `branch`
  names from node output.
- An agent with `access: full` reads output from a node with `web: true`. Web pages can
  contain instructions aimed at agents, and this passes them to one with no restrictions.
- Two `edit` nodes could run at the same time in the same workspace.
- A `for_each` reads a list whose schema has no `maxItems`.
- The workflow uses `run.cost_usd` or `limits.cost_usd` with a harness that doesn't report cost, or `run.tokens` or `limits.tokens` with one that doesn't report tokens.
- A node that can run on `cursor` sets `effort`.
- A node uses a tool that isn't set up on this machine (`wf tools <name> setup`).
- A node has `write: true` for a tool that is set up for reads only (`wf tools <name> setup --write`).
- A node sets `tools_with_shell: true`: its agent could read every stored sign-in.
- A node with `project_instructions: false` can run on a harness that still loads an instruction file.
  Codex loads your global `~/.codex/AGENTS.md` and the skills in your home folder.
  Cursor loads your account's User Rules, the skills in your home folder, and the repo's hooks.
- An agent with `access: full` reads output from an agent with `tools`.
  Messages, notes and mail that other people write can carry instructions aimed at agents.

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
    cursor.py       cursor-agent acp, the Agent Client Protocol over stdio
    mcp_http.py     submit_result as an MCP server over HTTP, for Cursor
  settings.py       ~/.wf/config.yaml: wf's own settings, and the names it knows
  capacity.py       the agent slots: one cap on live agent sessions across all runs
  tools/            wf tools: the five vendor servers, sign-in and the Keychain, the relay, doctor
  nodes.py          runs one attempt of each node kind, including the reminder loop
  scheduler.py      the rules in "When a node runs": pending, settled, routes, end
  graph.py          LangGraph wiring
  runs.py           run folder, events, finding a run by id
  trace.py          the run's trace: trace.jsonl, blobs/, and keeping secrets out
  trace_view.py     wf trace
  shape.py          the graph without its loops: steps and loops, for wf status and wf ui
  progress.py       how far a run is, as an estimate, for wf status
  observe.py        wf sessions and wf watch: the sessions that run now, and the event stream
  ui.py, ui/        wf ui: the server and the page
  cli.py            init, validate, graph, run, retry, tools
```

The `wf` command is a `[project.scripts]` entry in `pyproject.toml`:
`wf = "workflow.cli:main"`.

Dependencies to add to `pyproject.toml`: `jinja2`, `pyyaml`, `jsonschema` and
`langgraph-checkpoint-sqlite`.
(`pyyaml` and `jsonschema` are already installed through LangChain, but the package should list what it imports.)
Codex and Cursor need no Python package: the runner talks to the `codex` and `cursor-agent` CLIs.

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
A `failed` `TurnEnd` can also set `retry_after`, the seconds the runner waits before the retry on the same harness.

| | Claude Code | Codex | Cursor |
|---|---|---|---|
| Runs as | `claude-agent-sdk`: one process per session | one `codex app-server --disable hooks --disable apps` per run, JSON-RPC over stdio, one thread per session | `cursor-agent --model <model> acp`, JSON-RPC over stdio: one process per session, because the agent keeps one model per process, each with a settings folder of its own, outside the repo |
| `submit_result` | SDK MCP server | dynamic tool (`experimentalApi`) | MCP server over HTTP, a URL per session |
| `read-only` | Read, Grep, Glob, and Bash limited to read-only git commands | sandbox `read-only` | ask mode and a `Write(**)` deny rule; reads and the runner's `git` tool allowed, commands refused |
| `edit` | Read, Edit, Write, Bash with `permission_mode="dontAsk"` | sandbox `workspace-write` | agent mode; writes inside the workspace and commands allowed, writes to temp folders and `~/.cursor` denied |
| `full` | `permission_mode="bypassPermissions"` | sandbox `danger-full-access` | everything allowed |
| `web: false` | no WebSearch or WebFetch | `web_search: "disabled"` (the default is `cached`) | web search and fetch requests refused |
| Repo instructions | `setting_sources=["project"]`, `claude_code` system prompt | `AGENTS.md` and the repo's skills | the repo's rules, `AGENTS.md` and skills, but not its `.cursor/cli.json` |
| `project_instructions: false` | `setting_sources=[]` | `project_doc_max_bytes: 0`, and each repo skill off | cursor-agent starts in an empty folder, and `session/new` gives the workspace |
| Usage limit | a `RateLimitEvent` that isn't `allowed` | `codexErrorInfo: "usageLimitExceeded"` with `willRetry: false` | the turn's last message: a sign-in, plan or payment notice, or a `[resource_exhausted]` error that isn't a `RetriableError`. A `RetriableError` fails the attempt, and the retry waits 30 seconds |
| Usage | tokens and dollars | tokens | none |
| `session: continue` | fork with `fork_session` | fork with `thread/fork` | `session/load` (no fork) |
| wf tools | an SDK MCP server per tool | a namespace of dynamic tools per tool | an MCP server over HTTP per tool, a URL per session |

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
- Cursor asks before what it may not do by itself.
  The runner answers from the node's access level with "allow once" or "reject".
  It never answers "allow always", which Cursor would save.
- Cursor saves the model it was started with, and the parameters it last used for that model, into its settings folder.
  It also reads its permissions from there.
  So each cursor-agent process gets a new folder in `~/.wf/temp/<run-id>/cursor-agent/`.
  It's outside the repo because Cursor also writes your account's details there.
  A session that a later pass may continue is kept there until the run succeeds.
- Outside the workspace, Cursor writes to temp folders and to Markdown files in `~/.cursor` without asking.
  For `edit`, deny rules block those writes.
  For `read-only`, a deny rule blocks every write.
- Cursor's sandbox is off at every level.
  With it on, a command still asks first, and one the runner allows runs outside the sandbox.
  In a live check, such a command wrote outside the workspace and reached the network.
  So the sandbox would confine nothing the runner doesn't already decide.
- The effort in a Cursor model id applies only when the client says it takes parameterized models, which the adapter does.
  Each attempt's transcript records the model and parameters Cursor reports for the session.
- `--disable-project-configs` keeps the repo's `.cursor/cli.json` from changing Cursor's permissions.
- Cursor reports a failed request as a paragraph at the end of the turn's last message, not as an error.
  So the adapter reads that message to tell a usage limit or a login problem from other failures.
  By its name, a `RetriableError` is an error that a retry can clear, so the adapter never takes it for a usage limit.
  On 2026-10-19 a turn ended with `Error: RetriableError: [resource_exhausted] Error` while the account had about half of its monthly allowance left.
  So a `RetriableError` fails the attempt, and the node's retry starts 30 seconds later.
- What Cursor can't switch off: a web fetch that Cursor's server marks as needing no approval runs even with `web: false`.

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
- Human nodes call `interrupt()`, and `wf retry` continues with `Command(resume=answers)`.
- An `AsyncSqliteSaver` in the run folder keeps checkpoints. LangGraph also saves the
  results of tasks that finished within a step, so a run that continues later doesn't redo them.
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
- `wf retry` with an edited version of the run's workflow.
- A separate cap on agent sessions per harness, as Claude Code, Codex and Cursor have
  separate quotas.
  The [agent slots](#agent-slots) cap all harnesses together.

## Open questions

None right now. How to drive Codex and Cursor, and whether loops should continue sessions,
were answered in `docs/research/harness-adapters.md`.

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
