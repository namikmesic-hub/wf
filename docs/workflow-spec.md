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
| `wf retry <run-id> [--limit NAME=VALUE] [--model NODE=MODEL]` | Continues a failed or paused run from its last checkpoint, or one a limit stopped (with a bigger limit). Finished nodes and items aren't rerun. `--model`, with `--harness` and `--effort`, gives a node or one item another model for the rest of the run (see [Another model on retry](#another-model-on-retry)). |
| `wf continue <run-id> <request> [--limit NAME=VALUE]` | Gives a run that succeeded a follow-up request. The run goes on from the nodes that its workflow's `follow_up` names, or else from its first agent node that can change files, on the same branches (see [Follow-ups](#follow-ups)). |
| `wf trace <run-id> [--node N] [--pass P] [--field F] [--var PATH] [--json]` | Shows what one run read, decided and did, from its trace: how each template rendered, why each route was taken, each attempt, turn and tool call. See [The trace](#the-trace). |
| `wf status <run-id>` | Says where a run stands, and how far it is on its way from `start` to `end`, as an estimate (see [Watching runs](#watching-runs)). `--json` gives the same as JSON. |
| `wf sessions [run-id]` | Lists the agent sessions that run now, in one run or in each run of the repo, with the last tool call of each. |
| `wf watch <run-id>` | Prints the run's steps and tool calls as they come, until the run needs attention. |
| `wf ui` | Serves a web page on `127.0.0.1` that shows the runs: the graph, the progress, the metrics of `wf report`, the events and the transcripts. |
| `wf config show [--runs-dir <dir>]` | Prints the value of each key of `~/.wf/config.toml`, and its source: a flag, the environment, the file or wf's default (see [Configuration](#configuration)). |
| `wf tools list` | Lists the tools agent nodes can use (see [docs/tools.md](tools.md)), how each signs in, and whether it's set up. |
| `wf tools <name> setup [--write]` | Signs in to a tool once, in the browser or with a pasted token, and keeps the secret in the macOS Keychain. It asks Slack and Linear for read scopes only, unless `--write` is given. |
| `wf tools <name> doctor`, `wf tools doctor` | Checks that a tool's sign-in is there and still good, and makes one harmless read call. Prints pass or fail with the next step. |
| `wf steer <run-id> <node> [--item N] <message>` | Sends a message into the live agent session of a node, while the run works (see [Steering a live session](#steering-a-live-session)). `--file <file>` reads a long message from a file, and `--file -` from stdin. |

`wf run` exit codes: 0 succeeded, 1 failed or stopped by a limit, 2 paused waiting for a person
(only with `--no-input` or when there's no terminal).

Every command that makes or finds runs (`run`, `retry`, `continue`, `answer`, `wait`, `status`, `list`,
`report`, `discard`, `sessions`, `watch`, `ui`, `steer`) takes `--runs-dir <dir>`, to keep run folders outside the repo (see
[Run folder](#run-folder)). The commands that wf prints for a run in a runs folder, like
`wf retry <run-id>`, include the option.
So they find the run whatever `WF_RUNS_DIR` or `runs_dir` in `config.toml` says where you type them.

### Setup

Each harness needs its own login, and only the harnesses a workflow uses need to be set up.

- **Claude Code**: sign in once with `claude`, then `/login`.
  Runs use your subscription.
  With `auth = "api-key"` under `[claude]` in `~/.wf/config.toml`, or `WF_CLAUDE_AUTH=api-key`, they use `ANTHROPIC_API_KEY` instead.
- **Codex**: the `codex` CLI, signed in with `codex login`. `wf` is tested against one Codex
  version and warns when the installed one differs, because the parts of Codex it uses are
  marked experimental. `codex` under `[paths]` names the program for a machine with more than one.
- **Cursor**: Node.js 22.18 or later, and a Cursor API key.
  wf runs Cursor through Cursor's SDK (`@cursor/sdk`), in the Cursor host, a program in TypeScript that ships with wf.
  Create the key in the Cursor dashboard, then run `wf runtimes cursor signin`, which keeps it in the macOS Keychain.
  Or name a file that holds the key with `api_key_file` under `[cursor]` in `~/.wf/config.toml`.
  Until a key is set, read-only and full nodes run through `cursor-agent`, and the run warns once and says how to add a key.
  wf never reads the key from the environment, and removes `CURSOR_API_KEY` from the environment that Cursor and its agents see.
  An agent that runs commands at `edit` or `full` access could still read the key from the Keychain.
  It could read every other sign-in there too ([docs/tools.md](tools.md)).
  The first Cursor session installs the SDK beside the host, from the host's lockfile, with `npm ci`.
  `wf runtimes cursor doctor` checks Node, the SDK, the key, and what your model ids map to.
  `edit` nodes still run through `cursor-agent`, signed in with `cursor-agent login` (see [Harness adapters](#harness-adapters)).
  For one release, `runtime = "acp"` under `[cursor]` in `~/.wf/config.toml` (or `WF_CURSOR_RUNTIME=acp`) runs every Cursor node through `cursor-agent`, as before.
  `node` and `cursor_agent` under `[paths]` name the programs for a machine with more than one install.
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

### Configuration

wf's own settings for the user are in `~/.wf/config.toml`, and `WF_HOME` moves `~/.wf`.
wf reads the file with `tomllib` when a command starts, and never writes it.
The package `workflow/config/` reads it, and each other module gets its values from that package.
Each key is optional:

| Key | Its value | Default | Variable | Flag |
|---|---|---|---|---|
| `runs_dir` | The folder for run folders, with a folder for each repo (see [Run folder](#run-folder)) | none: `.wf/runs` in each repo | `WF_RUNS_DIR` | `--runs-dir` |
| `max_agents` | The cap on live agent sessions across runs (see [Agent slots](#agent-slots)) | none: no cap | `WF_MAX_AGENTS` | |
| `claude.auth` | `subscription`, or `api-key` to use `ANTHROPIC_API_KEY` | `subscription` | `WF_CLAUDE_AUTH` | |
| `cursor.runtime` | `sdk`, or `acp` to run each Cursor node through `cursor-agent` | `sdk` | `WF_CURSOR_RUNTIME` | |
| `cursor.api_key_file` | A file that holds the Cursor API key | none: the macOS Keychain | | |
| `ui.port` | The port of `wf ui` on 127.0.0.1, or 0 for any free port | 8765 | | `--port` of `wf ui` |
| `tools.signin_port` | Where `wf tools <name> setup` waits for the redirect of the browser | 47152 | | `--port` of `wf tools <name> setup` |
| `git.signing_timeout` | The seconds that signing one commit may take | 30 | | |
| `paths.codex`, `paths.cursor_agent`, `paths.node` | The program to run, for a machine with more than one install | none: the program on `PATH` | | |
| `metrics.enabled` | `true` to export the records of runs to ClickHouse | `false` | | |
| `metrics.url` | The connection string of your ClickHouse (see [Metrics](#metrics)) | none | | |

A key in a table has its table in its name: `cursor.runtime` is `runtime` under `[cursor]`.
In the file, a path starts with `/` or `~`, because wf reads the file in any folder.
A path from a variable or a flag can be relative to the folder that wf runs in.
`wf config show` prints the path of the file, then a line for each key: its name, its value and where the value comes from.

```
$ WF_CURSOR_RUNTIME=sdk wf config show
/Users/you/.wf/config.toml
runs_dir             none (.wf/runs in each repo)                   default
max_agents           4                                              file
cursor.runtime       sdk                                            environment (WF_CURSOR_RUNTIME)
...
metrics.url          https://wf:***@clickhouse.example.com:8443/wf  file
```

The tests below are in `tests/test_config.py`, unless a row names another file.

| Condition | Effect | Source line | Conformance assertion |
|---|---|---|---|
| A key has a flag, a variable, a value in the file and a default | The flag wins, then the variable, then the file, then the default. `wf config show` names where each value comes from. | `workflow/config/load.py:52`, `workflow/config/keys.py:255` | `test_each_key_takes_its_flag_then_its_variable_then_the_file_then_wf_s_default`, `test_every_key_has_a_sample_and_the_sources_of_today_stay` |
| A variable has a blank value | It counts as no variable, so the file or wf's default decides. A blank `WF_RUNS_DIR` is the one that gives wf's default, also over the file. So `WF_RUNS_DIR= wf retry <run-id>` finds a run in `.wf/runs`. | `workflow/config/load.py:62`, `workflow/config/keys.py:234` | `test_a_blank_variable_counts_as_not_set_but_a_blank_wf_runs_dir_turns_the_runs_folder_off`, `test_a_blank_wf_runs_dir_turns_off_the_runs_folder_of_config_toml` in `tests/test_runs_dir.py`, `test_the_cap_comes_from_wf_max_agents_or_the_config_in_wf_home` in `tests/test_capacity.py` |
| A flag is blank | It counts as no flag. | `workflow/config/__init__.py:76` | `test_a_blank_flag_is_no_flag` |
| The file has a key or a table that wf doesn't know, or a value in place of a table | Every command exits 1 before it starts. The message names the key, the file and the keys of its table. It says where a key goes that is in another table. A key in quotes at the top, as `"ui.port"`, is one that wf doesn't know. | `workflow/config/load.py:122`, `workflow/cli.py:297` | `test_an_unknown_key_is_refused_with_the_keys_that_its_place_takes` |
| A value in the file, or in a variable, isn't of the kind of its key | The same. The message names the key or the variable, the file and what the key allows. wf checks the file also when a variable wins over it. A path with the `~` of a user that doesn't exist is of the wrong kind. So is a number of seconds too big for Python. | `workflow/config/load.py:60`, `workflow/config/load.py:67`, `workflow/config/keys.py:116` | `test_a_value_of_the_wrong_kind_is_refused_with_the_key_the_file_and_what_is_allowed`, `test_a_variable_with_a_wrong_value_is_refused`, `test_a_bad_cap_stops_wf_run_before_the_run_exists` in `tests/test_capacity.py` |
| The configuration has more than one problem | The message names each of them, one on each line. | `workflow/config/load.py:71` | `test_every_problem_is_named_at_once_and_stops_every_command` |
| The file isn't valid TOML, or isn't UTF-8 | The same, with the line and the column of the problem, and without the text of the file. | `workflow/config/load.py:91` | `test_a_file_that_isn_t_toml_is_refused_without_its_text` |
| `config.toml` belongs to another user than you and root, or its mode lets other users change it | Every command exits 1 before it starts, because the file names programs that wf runs. The message names the fix: `chmod go-w`, or a file of your own. Group write is fine while no other user has the group of the file, as primary group or as member. | `workflow/config/load.py:172`, `workflow/config/load.py:186`, `workflow/config/load.py:192` | `test_a_file_that_other_users_can_change_or_that_another_user_owns_is_refused`, `test_group_write_is_fine_while_the_group_of_the_file_is_yours_alone` |
| `~/.wf/config.yaml` is there | Every command exits 1 before it starts. The message names each setting of `config.yaml` and its key in `config.toml`, and never a value. | `workflow/config/legacy.py:18` | `test_while_config_yaml_is_there_wf_stops_and_names_where_each_of_its_settings_goes`, `test_a_config_yaml_without_settings_still_stops_wf` |
| A command runs | It keeps the configuration that it read when it started, also in its threads. A change of the file counts from the next command. | `workflow/cli.py:303`, `workflow/config/__init__.py:62` | `test_a_command_keeps_the_configuration_that_it_started_with`, `test_commands_that_end_in_another_order_than_they_started_leave_nothing_pinned` |
| A command starts a background driver for a run | The driver gets the location of the run: `--runs-dir` for a run in a runs folder, else a blank `WF_RUNS_DIR`. So it finds the run, also after a change of `config.toml`. | `workflow/runs.py:112`, `workflow/operate.py:181` | `test_a_detached_run_finds_its_folder_after_runs_dir_changes_in_config_toml` in `tests/test_runs_dir.py` |
| firstmate polls a run while the configuration has a problem | The poll answers the error `unavailable`, which firstmate can try again, before it waits for the run. So the poll still reports the next state of the run once. | `workflow/firstmate.py:151` | `test_a_configuration_with_problems_refuses_the_poll_and_keeps_the_state_for_the_next_one` in `tests/test_firstmate.py` |
| A module outside `workflow/config/` needs a value | It gets it from the package. It doesn't read `config.toml`, `WF_HOME` or the variable of a key. | `workflow/config/__init__.py:54` | `test_only_the_config_package_reads_config_toml_or_the_variables_of_its_keys` |
| There's no `config.toml` | Each key has wf's default, and `wf config show` says that the file isn't there. wf makes no file. | `workflow/config/load.py:83`, `workflow/config/show.py:13` | `test_without_a_file_each_key_has_wf_s_default_and_wf_writes_no_file`, `test_wf_config_show_says_when_there_is_no_file` |
| `wf config show` | It prints each key with its value and its source: `flag`, `environment`, `file` or `default`, with the name of the flag or the variable. It shows `metrics.url` with `***` for the password. | `workflow/config/show.py:10` | `test_wf_config_show_gives_each_key_its_value_and_where_it_comes_from` |

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
| `follow_up` | no | The nodes where a follow-up request goes in. Without it, the first agent node with `edit` or `full` access takes the request ([Follow-ups](#follow-ups)). |

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
`run`, `history`, `workspace`, `errors`, `results`, `item`, `index`, `session`, `follow_up`.

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
Through `cursor-agent`, Cursor loads your Cursor account's User Rules and the skills in your home folder, such as `~/.claude/skills` and `~/.agents/skills`.
Through Cursor's SDK, a session loads only the repo's layer of Cursor's settings, and with `false` no layer at all.

A repo's instruction files can give an agent a role that isn't the workflow's.
For example, firstmate's `AGENTS.md` tells each agent that loads it that it is the first mate.
`project_instructions: false` keeps the repo's files out of a node's sessions, and the node's instruction still reaches the agent.
The default, `true`, loads them as before.
A sub-workflow uses its own `defaults`, so a workflow that sets `false` in its `defaults` sets it in each sub-workflow too.
The tests named below are in `tests/test_claude_code.py`, `tests/test_codex.py`, `tests/test_cursor.py` (Cursor's SDK) and `tests/test_cursor_acp.py` (`cursor-agent`) for the rows of those harnesses.
The tests of the other rows are in `tests/test_validate.py`, unless another file is named.

| Condition | Effect | Source line | Conformance assertion |
|---|---|---|---|
| A node sets `project_instructions`, or `defaults` does | The node's own value wins, then `defaults`, then `true`. Only `true` or `false` is accepted. | `workflow/spec.py:195`, `workflow/spec.py:299`, `workflow/nodes.py:499` | `tests/test_agents.py::test_project_instructions_reach_the_session_from_the_node_or_defaults`, `test_project_instructions_takes_a_boolean_on_a_node_or_in_defaults` |
| `false` on Claude Code | No setting source loads: no `CLAUDE.md` or `AGENTS.md` at any level, no `.claude/rules`, no `.claude/settings.json`, and no skills in `.claude/skills`. Claude Code's own system prompt stays. | `workflow/harnesses/claude_code.py:183` | `test_without_project_instructions_no_setting_source_loads`, `test_a_real_node_without_project_instructions_does_not_get_the_repos_claude_md` (`-m harness`) |
| `false` on Codex | The thread gets `project_doc_max_bytes: 0`, so no `AGENTS.md` and no fallback file such as `CLAUDE.md`. Each skill that `skills/list` finds in the repo is switched off in the thread's config. | `workflow/harnesses/codex.py:189`, `workflow/harnesses/codex.py:224` | `test_without_project_instructions_a_thread_gets_no_repo_agents_md_and_no_repo_skill` |
| `false` on Codex, and `skills/list` doesn't answer | Codex is unavailable for the attempt, so the next harness in the node's list runs. | `workflow/harnesses/codex.py:230` | `test_a_codex_that_does_not_list_its_skills_is_unavailable_to_a_node_without_project_instructions` |
| `false` on Cursor's SDK | The session loads no layer of Cursor's settings: the host leaves out `settingSources`. With `true`, it loads the `project` layer. | `workflow/harnesses/cursor_host/src/cursor.ts:165` | `test_without_project_instructions_no_cursor_settings_layer_loads`, `test_a_real_cursor_node_without_project_instructions_gets_no_repo_rules` (`-m harness`, with a Cursor API key) |
| `false` on Cursor through `cursor-agent` | cursor-agent starts in a new empty folder in the system's temp folder, because it reads rules, `AGENTS.md`, `CLAUDE.md` and skills from the folder it starts in, the folders above it, and the repo it is in. `session/new` still gives the workspace, where the agent reads, edits and runs commands. The folder goes with the session. | `workflow/harnesses/cursor_acp.py:306`, `workflow/harnesses/cursor_acp.py:458` | `test_without_project_instructions_cursor_agent_starts_in_an_empty_folder_and_works_in_the_workspace`, `test_by_default_cursor_agent_starts_in_the_workspace` |
| `false` on Cursor through `cursor-agent`, and the temp folder is in a git repo, or a folder above it holds `AGENTS.md`, `CLAUDE.md`, `CLAUDE.local.md`, `.cursorrules`, `.cursor/rules`, or a `skills` folder in `.cursor`, `.claude`, `.codex`, `.grok` or `.agents`, as when `TMPDIR` is in a repo | Cursor is unavailable for the attempt, and the message names what it found, so the next harness in the node's list runs. The skills folders in your home folder don't count, because they load wherever Cursor starts. | `workflow/harnesses/cursor_acp.py:471` | `test_a_temp_folder_under_what_cursor_loads_makes_it_unavailable_without_project_instructions`, `test_your_home_folders_skills_above_the_temp_folder_do_not_count` |
| `false` where a harness still loads a file | `wf validate` warns once for each harness, where the setting is. On Codex, your global `~/.codex/AGENTS.md` and the skills in your home folder stay. On Cursor through `cursor-agent`, your account's User Rules, the skills in your home folder and the repo's hooks in `.cursor/hooks.json` and `.claude/settings.json` stay. That is for `edit` nodes, and for each node when `cursor.runtime` is `acp` or wf has no Cursor API key. Cursor's SDK keeps none of them, so it gets no warning. | `workflow/validate.py:698`, `workflow/harnesses/__init__.py:62`, `workflow/harnesses/__init__.py:73` | `test_project_instructions_off_warns_once_per_harness_that_keeps_a_file`, `test_project_instructions_off_on_cursors_sdk_keeps_nothing_out_and_gets_no_warning` |
| `false`, and a template picks the harness | `wf validate` can't tell which harness runs, so it warns about each harness that keeps a file. | `workflow/validate.py:704` | `test_project_instructions_off_warns_about_every_harness_a_template_could_pick` |

Through `cursor-agent`, `false` has a cost: Cursor's semantic code search and lint diagnostics don't cover the workspace.
Claude Code and Cursor's SDK load no skill from your home folder.
Codex and `cursor-agent` load those skills as before.
Each harness row was checked live, with Claude Code 2.1.301 (the one the Agent SDK bundles), Codex 0.163.0 and cursor-agent 2026.10.18-ea085cb.
In each check, a repo's `AGENTS.md` or `CLAUDE.md`, its rules and its skills held code words.
A node with the default told them, and a node with `false` told none, and still did its task.
These checks are tests marked `-m harness`: `test_a_real_node_without_project_instructions_does_not_get_the_repos_claude_md`, `test_a_real_codex_node_without_project_instructions_gets_no_repo_agents_md_or_skill` and `test_a_real_cursor_node_without_project_instructions_gets_no_repo_rules_and_still_edits`.
The same check for Cursor's SDK is `test_a_real_cursor_node_without_project_instructions_gets_no_repo_rules`, which needs a Cursor API key.

##### Tools and a shell

A node with `tools` may not have a shell that reaches the Keychain, unless it sets `tools_with_shell: true`.
The tests named below are in `tests/test_tools.py`, unless another file is named.

| Condition | Effect | Source line | Conformance assertion |
|---|---|---|---|
| A node with `tools` can run on Claude Code or Cursor at `edit` or `full`, or on Codex at `full`, and doesn't set `tools_with_shell: true` | `wf validate` errors, naming each such harness and the access level, so the run doesn't start. | `workflow/validate.py:694`, `workflow/harnesses/__init__.py:60` | `test_a_node_with_tools_and_a_shell_that_reaches_the_keychain_needs_the_opt_in`, `test_a_harness_list_names_each_harness_whose_shell_reaches_the_keychain` |
| A template picks such a harness at run time, which `wf validate` can't see, and the node doesn't set `tools_with_shell: true` | The run refuses that harness, and the node fails without a session. | `workflow/nodes.py:464` | `tests/test_tool_sessions.py::test_a_run_refuses_a_harness_whose_shell_reaches_the_keychain_without_the_opt_in` |
| The node sets `tools_with_shell: true` | `wf validate` warns, naming the node and its tools: its agent could read every stored sign-in. The run attaches the tools. | `workflow/validate.py:691` | `test_a_node_that_opts_in_gets_a_warning_naming_it_and_its_tools`, `tests/test_tool_sessions.py::test_a_run_lets_a_node_that_opted_in_have_tools_and_a_shell` |
| A node with `tools` is read-only on any harness, or on Codex at `edit` | It needs no opt-in: its commands can't reach the Keychain. | `workflow/harnesses/__init__.py:60` | `test_a_node_with_tools_and_a_shell_that_reaches_the_keychain_needs_the_opt_in` |
| A node has no tools, `tools: []` included | `wf validate` says nothing about its shell. | `workflow/validate.py:687` | `test_tools_with_shell_takes_a_boolean_and_matters_only_with_tools` |
| `tools_with_shell` isn't `true` or `false`, or is in `defaults` | `wf validate` errors: the field takes only a boolean, and only on the node, so that each such node says so itself. | `workflow/spec.py:194` | `test_tools_with_shell_takes_a_boolean_and_matters_only_with_tools`, `test_tools_with_shell_goes_on_the_node_not_in_defaults` |

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
- The node's next pass gives its sub-workflow the sessions that the last pass ended with.
  So a node of the sub-workflow with `session: continue` continues its session, also in a follow-up.
  A `for_each` item starts its sub-workflow without sessions, as an agent node's item does (see [Sessions](#sessions)).

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
Workflows name the ids that `cursor-agent --list-models` lists.
Cursor's SDK names a model and its parameters apart, such as `gpt-5.6-sol` with `reasoning=max` and `fast=true`.
So the Cursor host maps an id to the catalog model that the id starts with.
The words after that name one variant of the model, with its parameters.
`gpt-5.6-sol[reasoning=max,fast=true]` gives the parameters as they are, as `cursor-agent` also takes them.
An id that names no model, or no single variant, makes Cursor unavailable for that attempt, so the next harness in the node's list runs.
`wf runtimes cursor doctor --model <id>` shows what an id maps to, and each attempt's transcript records the model and parameters of its session.

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
A follow-up has a budget of its own (see [Follow-ups](#follow-ups)).

- **Tokens** are input plus output tokens, as each harness reports them (cached input
  included).
  Claude Code and Codex report them.
  Cursor reports them through its SDK, but not for `edit` nodes, which run through `cursor-agent`.
  With no Cursor API key set, every Cursor node runs through `cursor-agent`, so none of them reports tokens.
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
| `results.<node>` | For `for_each` nodes only: one `{index, item, output, harness, model}` for each item of the latest pass, in list order, so an item keeps its position. The `output` of an item that failed is `none`, with `on_item_error: continue`. The `harness` and `model` are those of the item's last agent attempt. Both are `none` when no agent attempt ran the item, as for each item of a command or workflow node. The `model` is `none` when the attempt used the default model of its harness. It is an empty list when the node hasn't run. |
| `run` | `{id, dir, cost_usd, tokens, elapsed_s, base}`. `base` is the commit the repo was at when the run started. |
| `session` | `{continued}`: true while rendering a pass that continues an earlier session (see [Sessions](#sessions)). Not in `model` and `effort`, which are chosen before the session starts. |
| `follow_up` | `{request, number}` of the latest follow-up request, or `none` before the first one (see [Follow-ups](#follow-ups)). |

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
- With `continue`, the positions in the node's output shift after an item that failed.
  `results.<node>` keeps one entry for each item at its position, with `output: none` for an item that failed.
  So a later node can name each item by its position in the `for_each` list.
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
  Cursor does this for a `RetriableError`, and for a lost connection that its SDK's own retries didn't carry over.
  Then the retry on Cursor starts 30 seconds later.
  There's no wait before the next harness, or when no attempt follows.
  A limit reached before or during the wait ends it, and the retry doesn't start.
- An agent attempt that waits for an [agent slot](#agent-slots) hasn't started yet: the
  wait isn't a failure and uses no retry.
- When a node, or an item with `on_item_error: fail`, runs out of attempts, the run stops:
  running attempts finish, nothing new starts, and the run's status becomes `failed`.
  `wf retry` continues from there.
  With `--model`, it can also give the step that failed another model: see [Another model on retry](#another-model-on-retry).

### Another model on retry

A run keeps the copies of the workflow files that it started with.
So when a model fails again and again, for example at a usage limit or in an outage, each retry fails in the same way.
`wf retry` can give a node, or one item of a `for_each` node, another model for the rest of the run:

```sh
wf retry <run-id> --model plan=claude-sonnet-5-5
wf retry <run-id> --harness 'review[2]=claude-code' --model 'review[2]=claude-opus-5-5'
wf retry <run-id> --model 'review[2]=gemini-4-argon-high'
```

- A target is an agent node of the run's own workflow, for all of its items, or one item, `node[index]`.
  The override of an item wins over the override of its node.
- An override runs its target on one harness only, with no fallback to another harness.
  That harness is the one that `--harness` names, or else the node's own harness.
  The node's own harness is the one harness that it names.
  A job is an item, or a node without `for_each`.
  For one job that is still to run in the pass in progress, the harness can also come from the node's template.
  So a node that names a list of harnesses, or picks its harness with a template, needs `--harness`.
  One job that is still to run in the pass in progress doesn't need it.
- `--model` and `--effort` replace the model and the effort of the target.
  A setting that the override leaves out comes from the workflow file, for the harness of the override.
- A model that the workflow gives for the node's own harness is wrong for another harness.
  So when `--harness` changes the harness, the override needs `--model`, unless the workflow gives a model for that harness.
  A Cursor session gets no effort, because a Cursor model id carries its own.
- Finished nodes and items don't run again.
  The override applies to the attempts that are still to come: the retry of what failed, and the later passes.
  So wf checks the override only for the jobs that are still to run.
- wf checks each override before it changes anything, as `wf answer` does with answers.
  When one is wrong, wf tells why, exits with code 1, and leaves the run as it was.
  While wf keeps an override and its event, Ctrl-C, SIGTERM and a lost terminal wait, so wf never keeps only one of them.
- `run.json` keeps the overrides, so a later `wf retry` uses them too.
  A later override for the same target replaces the earlier one.
- `events.jsonl` gets an `override` event for each override, with the last error of its target.
  Each agent attempt gets an `agent_model` event with its harness, model and effort, and with its override, if it has one.
  `wf report` leaves `override` events out, because they record what an operator gave, not the work of a node.

The tests named below are in `tests/test_overrides.py`, unless another file is named.

| Condition | Effect | Source line | Conformance assertion |
|---|---|---|---|
| An option isn't `NODE=VALUE` or `NODE[INDEX]=VALUE`, or names a target twice | wf refuses it before it reads the run. | `workflow/overrides.py:81`, `workflow/overrides.py:90` | `test_a_wrong_override_changes_nothing` |
| The target isn't an agent node of the run's own workflow: no such node, a command, human or workflow node, or a step in a sub-workflow | wf refuses the override, and the run stays as it was. | `workflow/overrides.py:83`, `workflow/overrides.py:215`, `workflow/overrides.py:218`, `workflow/overrides.py:220` | `test_a_wrong_override_changes_nothing` |
| The target is an item, and the node isn't `for_each`, hasn't started, or doesn't have that item in its pass in progress or its last pass | The same. | `workflow/overrides.py:224`, `workflow/overrides.py:233`, `workflow/overrides.py:229` | `test_a_wrong_override_changes_nothing`, `test_an_item_that_the_node_went_on_without_can_get_another_model` |
| There is no `--harness`, and the node names a list of harnesses, or picks its harness with a template and the target isn't one job of the pass in progress | The same. The message asks for `--harness`. | `workflow/overrides.py:262`, `workflow/overrides.py:281` | `test_a_wrong_override_changes_nothing`, `test_an_item_keeps_the_harness_that_its_template_gives_it` |
| One job that is still to run in the pass in progress has no `--harness`, and its node picks its harness with a template | The override keeps the harness that the template gives the job. wf renders it with the job's own variables, its workspace included, as the attempt does. A job has no workspace yet when wf couldn't make it, or when the run folder is from before this rule. A template that reads that workspace can't render, so there wf asks for `--harness`. | `workflow/overrides.py:162`, `workflow/overrides.py:170`, `workflow/state.py:162` | `test_an_item_keeps_the_harness_that_its_template_gives_it`, `test_a_harness_template_that_reads_the_jobs_own_workspace_gives_its_harness` |
| `--harness` changes the harness, there is no `--model`, and the workflow's model for the node isn't one for that harness | wf refuses the override. The message asks for `--model`. | `workflow/overrides.py:267`, `workflow/overrides.py:299` | `test_a_wrong_override_changes_nothing` |
| The harness is unknown, or is `cursor` with an `--effort`, or with no model from the override or the workflow | wf refuses the override. | `workflow/overrides.py:264`, `workflow/overrides.py:271`, `workflow/overrides.py:273` | `test_a_wrong_override_changes_nothing` |
| The node has `tools`, the harness could reach the Keychain at the node's access level, and the node doesn't set `tools_with_shell: true` | The same. | `workflow/overrides.py:275` | `test_a_wrong_override_changes_nothing` |
| The run has ended | The same: no node is left to run. | `workflow/runner.py:275` | `test_an_override_for_a_run_that_has_ended_changes_nothing` |
| `--answers` is given too, and the answers or the overrides are wrong | wf tells every problem of both, and the run stays as it was. | `workflow/runner.py:200` | `test_an_override_and_a_wrong_answer_are_both_reported_and_change_nothing` |
| The models of the target | For each job of the target that is still to run in the pass in progress, the model is the one that its next attempt gets, rendered as the attempt renders it, without spaces around it. Both use the same code. wf doesn't check a finished job, because it doesn't run again. A job with no workspace yet renders its settings without one. A model or an effort that can't render, or that renders to no text, makes wf refuse the override. When the job has no workspace yet, the message asks for `--model` or `--effort`. For a target with no such job, wf takes the override's model, or the workflow's model when it isn't a template. | `workflow/overrides.py:135`, `workflow/overrides.py:252`, `workflow/overrides.py:304`, `workflow/overrides.py:321`, `workflow/nodes.py:440` | `test_a_model_that_a_template_gives_the_failed_item_on_the_new_harness_is_checked`, `test_a_workflow_model_with_spaces_around_it_is_checked_as_the_run_uses_it`, `test_a_node_override_checks_only_the_items_that_run_again`, `test_an_item_that_failed_before_it_had_a_workspace_has_its_model_checked`, `test_a_setting_that_reads_a_workspace_the_item_doesnt_have_yet_must_be_given` |
| Every other check passed | wf opens each harness as a run does, in a folder outside the run that it then removes, and asks it about the models of the target. It refuses a harness that doesn't start, and a model that Cursor can't run. A dry run skips this check, because its script plays every harness. | `workflow/runner.py:208`, `workflow/overrides.py:339`, `workflow/harnesses/remote.py:319` | `test_the_harness_check_comes_last_and_opens_the_harness_outside_the_run`, `test_a_harness_that_cant_start_here_changes_nothing`, `test_a_model_the_harness_doesnt_have_changes_nothing`, `test_a_model_that_the_workflow_gives_for_the_new_harness_is_checked_too` |
| wf can't make the folder to open the harnesses in | wf refuses the override, and tells why. | `workflow/overrides.py:341` | `test_no_folder_to_open_the_harnesses_in_changes_nothing` |
| wf can't remove that folder, or the folder that Cursor makes by its name | wf refuses the override, and names the folder that stays. | `workflow/overrides.py:347`, `workflow/overrides.py:357` | `test_a_check_folder_that_stays_changes_nothing` |
| The harness is Claude Code | Opening it looks for the Claude Code program as the Claude Agent SDK does. So a machine without it, or with a file there that you may not run, is refused. A program that breaks when it runs, or a missing sign-in, shows when the attempt runs. | `workflow/harnesses/claude_code.py:137`, `workflow/harnesses/claude_code.py:150` | `test_opening_finds_the_claude_code_program_as_the_sdk_does` in `tests/test_claude_code.py` |
| Every check passes | `events.jsonl` gets an `override` event for each override, in one write. Then `run.json` keeps the overrides and the new limits from `--limit`, in one write too, and the run continues. Ctrl-C, SIGTERM and a lost terminal wait until both writes end, and then stop wf as before. | `workflow/runner.py:210`, `workflow/runner.py:285`, `workflow/runner.py:298`, `workflow/runner.py:309`, `workflow/runs.py:138`, `workflow/runs.py:266` | `test_a_failed_item_runs_again_with_the_model_it_is_given_and_the_others_stay_done`, `test_a_new_limit_is_kept_only_when_the_overrides_are_right`, `test_ctrl_c_waits_until_an_override_and_its_event_are_both_kept` |
| A write of `events.jsonl` or `run.json` fails | wf cuts `events.jsonl` back to its size before, so the events go again, and `run.json` stays as it was. Either all the new settings and their events are kept, or none. wf tells why and exits with code 1. | `workflow/runs.py:276`, `workflow/runner.py:304`, `workflow/runner.py:213` | `test_an_override_never_applies_without_its_event`, `test_a_limit_and_an_override_are_kept_together_or_not_at_all`, `test_events_that_cant_all_be_written_are_cut_off` |
| The `override` event of a node | Its `last_error` is the error of the node's failed job in the pass in progress, also when a limit stopped the run. Else it is the run's failure at the node, or the first item that the node went on without. | `workflow/overrides.py:386` | `test_a_node_that_a_limit_stopped_gives_its_override_its_last_error`, `test_a_later_override_replaces_the_earlier_one` |
| An attempt of the target starts | It runs on the harness of the override, with its model and effort, or with the workflow's for that harness. The `agent_model` event of the attempt names them and the override. Finished items don't run again. | `workflow/nodes.py:436`, `workflow/nodes.py:477` | `test_a_failed_item_runs_again_with_the_model_it_is_given_and_the_others_stay_done`, `test_a_harness_that_can_run_here_takes_the_failed_node`, `test_a_new_harness_takes_the_workflows_model_for_it_and_cursor_gets_no_effort` |
| A node and one of its items both have an override | The item's override applies to that item. | `workflow/overrides.py:106` | `test_an_item_override_wins_over_its_node` |
| A later `wf retry` has no override options, or the node starts another pass | The overrides in `run.json` still apply. | `workflow/runner.py:221` | `test_an_override_holds_for_later_retries_and_later_passes` |
| A later override names the same target | It replaces the earlier one, settings it leaves out included. | `workflow/runs.py:144` | `test_a_later_override_replaces_the_earlier_one` |
| `wf retry --detach` | wf checks and keeps the overrides before the run goes to the background, and the background process reads them from `run.json`. | `workflow/runner.py:221` | `test_a_detached_retry_hands_the_override_to_its_background_process` |
| `wf report` | It leaves `override` events out, so a node that never ran gets no row, and the time of a pass doesn't count from an override. | `workflow/report.py:86` | `test_a_report_leaves_overrides_out` |

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
    Cursor can't fork, so it loads the previous session itself: its SDK resumes the agent
    (`Agent.resume`), and `cursor-agent` loads the session (`session/load`).
    Cursor keeps such a session in `~/.wf/sessions/<run-id>-<hash>/`, which only you can open.
    On the SDK, that is the agent's store: its agents, runs, run events and checkpoints, with no API key and no account details.
    Through `cursor-agent`, it is the session's folder from `acp-sessions/`: the config folder, with your account's details, still goes.
    When the run succeeds, only the sessions that a follow-up can continue stay, and `wf discard` removes them.
  - The rendered instruction is sent as the next message. `session.continued` is true while
    it renders, so the template can send just the new feedback instead of the whole task.
    The runner adds that a new `submit_result` call is needed.
  - The pass starts fresh instead, and an event says why, when: the harness or model differs
    from the previous pass, the session can't be found, it's a Cursor retry after a failed
    continued attempt, or the two passes before it both continued.
  - The items of a `for_each` node always start fresh, also in a workflow node.
    Each pass renders the list again, so the item at an index can be a different task.

### Agent slots

`max_parallel` caps the items of one node in one run.
Several runs can still open more agent sessions at once than the machine, or an account's
quota, holds.
`max_agents` caps the agent sessions that are live at once across those runs:

```toml
# ~/.wf/config.toml (WF_HOME moves ~/.wf)
max_agents = 4
```

The tests named below are in `tests/test_capacity.py`, unless another file is named.

| Condition | Effect | Source line | Conformance assertion |
|---|---|---|---|
| Runs of the user that use the same `~/.wf` | They share one set of slots, in `~/.wf/agent-slots/`. Runs with another `WF_HOME` have slots of their own. | `workflow/capacity.py:47` | `test_two_runs_in_separate_processes_share_the_cap`, `test_each_wf_home_has_its_own_slots`, `test_a_slot_file_names_its_holder` |
| Neither `WF_MAX_AGENTS` nor `max_agents` is set | There is no cap. No attempt waits, and no slot file is made. | `workflow/nodes.py:638` | `test_without_a_cap_nothing_waits_and_no_slot_is_made` |
| `WF_MAX_AGENTS` is set | It sets the same cap, and wins over `max_agents` in the file, which is still checked. An empty `WF_MAX_AGENTS` counts as no variable. | `workflow/capacity.py:54`, `workflow/config/load.py:65` | `test_the_cap_comes_from_wf_max_agents_or_the_config_in_wf_home`, `test_a_bad_cap_stops_wf_run_before_the_run_exists` |
| The cap isn't a whole number of 1 or more | Every command says why and exits 1 before anything runs. | `workflow/config/keys.py:235`, `workflow/cli.py:297` | `test_a_bad_cap_stops_wf_run_before_the_run_exists`, `test_a_bad_cap_stops_wf_retry` |
| `config.toml` has a key that wf doesn't know | The same. So a misspelt `max_agents` can't quietly mean no cap. | `workflow/config/load.py:122` | `test_an_unknown_key_is_refused_with_the_keys_that_its_place_takes` in `tests/test_config.py`, `test_a_bad_cap_stops_wf_run_before_the_run_exists`, `test_a_bad_cap_stops_wf_retry` |
| An agent attempt starts, and a slot is free | It takes the slot before its session opens, and gives it back when the attempt ends. | `workflow/nodes.py:643`, `workflow/nodes.py:661` | `test_two_runs_in_separate_processes_share_the_cap`, `test_a_slot_file_names_its_holder` |
| An attempt holds a slot | The slot is the file `~/.wf/agent-slots/<n>`, which the attempt's process locks with `flock`. The file names its holder: pid, run, node and attempt. | `workflow/capacity.py:72`, `workflow/capacity.py:74`, `workflow/capacity.py:83` | `test_a_slot_has_one_holder_at_a_time`, `test_a_slot_file_names_its_holder` |
| A retry, or a fallback to the next harness | It takes a slot again. | `workflow/nodes.py:318` | `test_a_retry_and_a_fallback_take_a_slot_again` |
| A command node, a human node, a workflow node itself, or a dry run | It takes no slot. The agent nodes inside a workflow node do. | `workflow/nodes.py:290`, `workflow/nodes.py:638` | `test_command_and_human_nodes_take_no_slot`, `test_a_dry_run_takes_no_slot`, `test_an_agent_in_a_sub_workflow_waits_for_a_slot` |
| Several attempts wait for one slot | Each tries again every `POLL_SECONDS` (0.2 seconds), and the first try after the slot comes free gets it. So they get it one at a time, in an order that is not defined. | `workflow/capacity.py:44`, `workflow/capacity.py:93`, `workflow/capacity.py:96` | `test_a_wait_tries_again_every_0_2_seconds`, `test_attempts_that_wait_for_one_slot_take_it_one_at_a_time` |
| Every slot is taken | The attempt waits. The wait isn't a failure and uses no retry. The `attempt_started` event and the node's `timeout` come once the attempt has its slot. | `workflow/nodes.py:651`, `workflow/nodes.py:320`, `workflow/nodes.py:322` | `test_a_waiting_attempt_keeps_its_retries_and_its_timeout_starts_with_its_slot`, `test_a_retry_and_a_fallback_take_a_slot_again` |
| An attempt waits | `events.jsonl` gets `slot_waiting`, then `slot_taken` with `waited_s`. Until the attempt has its slot, `agents_waiting` in `status.json` lists it with its node, attempt number and start time, and `wf status` names it. | `workflow/nodes.py:647`, `workflow/nodes.py:657`, `workflow/context.py:184`, `workflow/cli.py:532` | `test_a_waiting_attempt_keeps_its_retries_and_its_timeout_starts_with_its_slot`, `test_a_killed_process_frees_its_slot` |
| A run limit is reached during the wait (the `duration` limit counts the wait) | The wait ends, the attempt doesn't start, and the run pauses at the limit. A slot that comes free at that moment goes back unused. | `workflow/capacity.py:93`, `workflow/nodes.py:655` | `test_a_limit_reached_while_waiting_ends_the_wait_and_the_attempt_never_starts`, `test_a_slot_that_comes_free_as_a_limit_is_reached_goes_back_unused`, `test_a_retry_that_a_limit_stops_while_it_waits_starts_once_after_wf_retry` |
| A process that holds a slot dies | The operating system releases the lock, and an attempt that waits can take the slot. | `workflow/capacity.py:74` | `test_a_killed_process_frees_its_slot` |
| A process dies while its attempt waits | `wf status` says `stopped`, without `agents_waiting`. | `workflow/operate.py:58` | `test_a_killed_waiter_is_stopped_and_no_longer_waiting` |
| Processes run with different caps | A cap of N uses slots 0 to N-1, so the total stays within the biggest cap. | `workflow/capacity.py:71` | `test_a_smaller_cap_uses_only_the_first_slots` |
| A `wf run` or `wf retry` process starts | It reads the cap once, and keeps it until it ends. | `workflow/runner.py:82`, `workflow/runner.py:162` | `test_wf_run_and_wf_retry_read_the_cap_once_when_they_start` |

### Steering a live session

`wf steer` sends a message into the live agent session of a node.
With it, a person who supervises a run can redirect an agent while it works.
A session is live from when its harness opens it until its attempt ends.
`--item <index>` names the item of a `for_each` node.
A node in a sub-workflow has the path that events give it, such as `build/implement` or `build[1]/implement`.
A message has at most 100,000 characters.

The message goes to the agent as its next user message.
How soon depends on the harness:
- **Codex** adds it to the running turn with `turn/steer`, and the agent reads it at its next step.
  `wf steer` says `delivered`.
- **Claude Code** keeps it, and the agent reads it at its next step, beside that step's tool results.
  `wf steer` says `queued`, and the run records `delivered` when Claude Code reads it.
  Claude Code tells the agent that the user sent the message while it worked.
  All the same, in one of two live checks with Haiku 5.5, the agent took the message for tool output and ignored it.
- **Cursor**, through its SDK, offers the message to the running turn (`run.steer`).
  When Cursor takes it, `wf steer` says `delivered`.
  When Cursor sends it back, `wf steer` says `queued`, and the runner keeps the message for the next turn.
  Through `cursor-agent`, for `edit` nodes, Cursor can't add a message to a running turn, so the message is always `queued`.

The messages that the runner keeps go together as the next turn, when the agent's turn ends without a result, before any reminder.
They count as `delivered` when that turn ends.
A turn of the instruction often runs until the agent submits its result.
So a message that waits for the end of the turn usually comes too late, and is dropped.

The messages pass through files in `~/.wf/steer/<run-id>-<hash>/`, outside the run folder and outside every repo.
Only you can open that folder.
The hash is of the run folder's path, so runs with one id in two repos keep apart.
`wf steer` trusts your user account, as every wf command does: any process that runs as you can steer a run.
That includes an agent that can run commands as you: on Claude Code and Cursor with `edit` or `full` access, and on Codex with `full` access.
`wf discard` removes the folder.
A run takes at most 100 messages at once: those that wait for the process that drives the run, and those that it hasn't answered yet.

`unconfirmed` means that the harness may have the message, but wf can't tell whether the agent read it.

The tests named below are in `tests/test_steer.py`, unless another file is named.

| Condition | Effect | Source line | Conformance assertion |
|---|---|---|---|
| The node has a live session | `wf steer` hands the message to the wf process that drives the run, and waits for the answer. It prints `delivered`, `queued` or `unconfirmed`, with the attempt and the harness, and exits 0. `--json` prints the answer as JSON. | `workflow/steer.py:228`, `workflow/steer.py:336` | `test_a_message_from_wf_steer_joins_the_running_turn` in `tests/test_codex.py`, `test_a_message_reaches_a_node_in_a_sub_workflow_from_stdin` |
| Codex runs a turn | The message goes into that turn with `turn/steer`, which names the turn, so it can't go into a later one: `delivered`. | `workflow/harnesses/codex.py:242` | `test_a_message_from_wf_steer_joins_the_running_turn` in `tests/test_codex.py` |
| Cursor's SDK runs a turn | The message goes to the run's `steer`. Cursor's `complete_delivered` is `delivered`. When Cursor sends the message back, or the run has no `steer`, it is `queued` for the next turn. When Cursor doesn't answer within 8 seconds, the message is `queued` too, and a `steer.status` event brings Cursor's answer later. A late `delivered` is recorded as `delivered`, and a message that Cursor sends back then goes as the next turn. The message is `unconfirmed` when the Cursor host doesn't answer within 10 seconds, or when the session closes before Cursor answers. | `workflow/harnesses/cursor_host/src/cursor.ts:374`, `workflow/harnesses/cursor_host/src/cursor.ts:380`, `workflow/harnesses/remote.py:590`, `workflow/harnesses/remote.py:498`, `workflow/harnesses/remote.py:630` | `test_a_message_from_wf_steer_joins_the_running_turn`, `test_a_message_from_wf_steer_goes_to_cursor_as_the_next_turn`, `test_a_message_that_cursor_declines_late_goes_back_to_the_runner_for_its_next_turn`, `test_a_message_that_cursor_never_answers_for_is_unconfirmed_when_the_session_closes` in `tests/test_cursor.py` |
| Codex doesn't answer `turn/steer` within 10 seconds | `unconfirmed`, with the reason, and the run records that. The message isn't sent again, because Codex may have taken it. | `workflow/harnesses/codex.py:247`, `workflow/steer.py:401` | `test_a_message_codex_never_answers_for_is_unconfirmed_and_never_sent_again` in `tests/test_codex.py` |
| Claude Code runs a turn, and has repeated a message in the session | The message goes to Claude Code with an id: `queued`. With `--replay-user-messages`, Claude Code repeats the id when it reads the message, and the run records `delivered`. When it reads the message before the write returns, `wf steer` says `delivered`, and the run records that once. | `workflow/harnesses/claude_code.py:261`, `workflow/harnesses/claude_code.py:319` | `test_claude_code_reads_a_message_from_wf_steer_at_its_next_step`, `test_claude_code_takes_a_message_only_mid_turn_once_it_repeated_one`, `test_a_message_claude_code_reads_during_its_write_is_recorded_once_as_delivered` in `tests/test_claude_code.py` |
| Claude Code reads the message only after the turn's last step | Claude Code gives the message a turn of its own. The runner reads that turn to its result, counts the tokens and cost of both results, and sends no reminder before it. When a timeout cuts off the message's turn, the first result still counts. A result that comes during a write waits for the write: a write that fails gets no turn. A result that ends the attempt, because the agent submitted or the turn failed, ends the turn at once. | `workflow/harnesses/claude_code.py:361`, `workflow/harnesses/claude_code.py:363`, `workflow/harnesses/claude_code.py:365`, `workflow/harnesses/claude_code.py:257` | `test_a_message_after_the_last_step_gets_a_turn_of_its_own_and_no_reminder_goes`, `test_a_turn_cut_off_after_a_message_from_wf_steer_still_counts_the_result_before_it`, `test_a_write_that_fails_after_the_turns_result_doesnt_leave_the_turn_waiting`, `test_a_result_that_ends_the_attempt_doesnt_wait_for_a_write_that_stalls` in `tests/test_claude_code.py` |
| The harness can't take the message now: Cursor through `cursor-agent`, Cursor's SDK when it sends the message back, a session between turns, a turn that ended as `turn/steer` came, Claude Code before it repeated a message, or a session that breaks as its harness takes the message | `queued`. When the turn ends without a result, the kept messages go together as the next turn, in the order they came, before any reminder. Messages that the harness is still taking when the turn ends go too. The run records `delivered` when that turn ends, or `unconfirmed` when it fails. | `workflow/nodes.py:574`, `workflow/steer.py:397`, `workflow/steer.py:405`, `workflow/steer.py:419` | `test_a_message_from_wf_steer_goes_to_cursor_as_the_next_turn` in `tests/test_cursor.py` and `tests/test_cursor_acp.py`, `test_a_message_that_cursor_declines_late_goes_before_a_later_one_in_the_next_turn`, `test_a_message_whose_hand_over_breaks_is_kept_for_the_next_turn`, `test_a_message_that_misses_the_turn_goes_as_the_next_turn_before_a_reminder` in `tests/test_codex.py`, `test_a_message_reaches_an_item_from_a_file_and_waits_for_the_turn_to_end`, `test_a_message_the_harness_still_takes_as_the_turn_ends_goes_before_a_reminder`, `test_the_next_turn_waits_for_a_message_that_comes_as_the_last_one_is_handed_over`, `test_messages_kept_together_go_as_one_turn_and_no_more_wait_than_the_limit`, `test_a_turn_that_fails_leaves_the_message_it_carried_unconfirmed` |
| The attempt ends before the agent reads the message: the agent submitted its result, or the attempt failed or timed out | A kept message is dropped, and the run records why. A message in a turn that didn't end is `unconfirmed`. | `workflow/steer.py:439`, `workflow/harnesses/base.py:214`, `workflow/harnesses/claude_code.py:379` | `test_a_message_for_cursor_is_dropped_when_the_agent_submits_first` in `tests/test_cursor.py` and `tests/test_cursor_acp.py`, `test_a_message_codex_never_took_is_dropped_when_the_agent_submits_first` in `tests/test_codex.py`, `test_a_message_claude_code_hasnt_read_when_the_agent_submits_is_dropped` in `tests/test_claude_code.py`, `test_a_message_kept_for_the_next_turn_is_dropped_when_the_attempt_fails`, `test_a_timeout_leaves_the_carried_message_unconfirmed_and_drops_the_kept_one` |
| The attempt ends while the harness takes a message | The hand-over finishes first, for up to 12 seconds, so `wf steer` gets a true answer. A hand-over that takes longer is cut: `wf steer` says `unconfirmed`, with the reason, and the run records that. A message that Claude Code repeated before the cut stays `delivered`, whether the turn's result came before or after the end of the write. When the agent submitted its result, a node timeout that comes during the hand-over cuts the hand-over, and the result stays. Any other cancellation, such as Ctrl-C, cancels the attempt as before, also when it comes with the node timeout. | `workflow/steer.py:429`, `workflow/steer.py:389`, `workflow/steer.py:328`, `workflow/cli.py:477`, `workflow/harnesses/claude_code.py:273` | `test_a_message_being_handed_over_as_the_attempt_ends_gets_a_true_answer`, `test_a_hand_over_cut_when_the_attempt_ends_is_unconfirmed_not_queued_or_none`, `test_a_result_stays_when_the_nodes_timeout_comes_while_a_hand_over_finishes`, `test_a_cancellation_from_outside_during_a_hand_over_cancels_an_attempt_that_has_its_result`, `test_wf_steer_says_when_wf_cant_tell_whether_the_agent_read_the_message`, `test_a_message_claude_code_read_stays_delivered_when_the_result_comes_during_its_write` in `tests/test_claude_code.py` |
| A message reaches a live session | `events.jsonl` gets a `steer` event, and the attempt's log a `steer` line, each with the message's id: `delivered`, `queued` or `unconfirmed` with the text, then `delivered`, `dropped` or `unconfirmed`, with the reason. The terminal shows each one. A log line that can't be written is lost alone: the event still goes, and neither the message nor the attempt fails. | `workflow/harnesses/base.py:205`, `workflow/harnesses/base.py:209`, `workflow/runner.py:609` | `test_a_message_from_wf_steer_goes_to_cursor_as_the_next_turn` in `tests/test_cursor.py`, `test_a_log_line_that_cant_be_written_fails_nothing_and_the_event_still_goes` |
| The node isn't an agent node of the run, a `for_each` node has no `--item`, a node without `for_each` has one, or the run's copy of its workflow is damaged | `wf steer` says what is wrong, and exits 1. | `workflow/steer.py:180` | `test_a_message_for_what_isnt_an_agent_node_is_refused`, `test_wf_steer_says_what_is_wrong_with_the_node_or_the_message`, `test_a_run_whose_copy_of_its_workflow_is_damaged_says_so` |
| The run isn't `running`, or no wf process drives it | `wf steer` says that nothing was sent, and why, and exits 1. It writes nothing. | `workflow/steer.py:235` | `test_a_run_that_isnt_running_has_no_live_session`, `test_a_running_run_that_no_process_drives_has_no_live_session` |
| The run is running, but the node has no live session, or its attempt is still opening one | The same, with the reason. Nothing in the run changes. | `workflow/steer.py:344`, `workflow/steer.py:342` | `test_a_message_for_a_node_without_a_live_session_fails_and_changes_nothing`, `test_a_message_while_the_session_opens_is_refused_with_why` |
| A request is written into the run folder or into the repo | No session gets it: the driver reads only `~/.wf/steer/<run-id>-<hash>/`, which is outside every repo, and which only you can open. | `workflow/workspaces.py:299`, `workflow/steer.py:243` | `test_a_request_written_into_the_repo_reaches_no_session`, `test_the_mailbox_is_in_wfs_own_folder_and_only_you_can_open_it`, `test_runs_with_one_id_in_two_repos_have_two_mailboxes`, `test_wf_discard_removes_the_runs_mailbox` |
| A message is longer than 100,000 characters, isn't UTF-8 text, or 10 messages already wait for the session (the one being handed over included) | `wf steer` says so, and nothing is sent. `wf steer` reads at most one character more than the limit. | `workflow/cli.py:507`, `workflow/cli.py:502`, `workflow/steer.py:579`, `workflow/steer.py:367`, `workflow/cli.py:471` | `test_wf_steer_says_what_is_wrong_with_the_node_or_the_message`, `test_wf_steer_reads_no_more_of_a_file_than_it_needs`, `test_a_message_that_isnt_utf_8_is_refused`, `test_wf_steer_says_nothing_was_sent_when_too_many_messages_wait_for_the_session`, `test_a_request_too_long_or_not_from_wf_steer_is_answered_and_never_delivered`, `test_messages_kept_together_go_as_one_turn_and_no_more_wait_than_the_limit`, `test_the_message_being_handed_over_counts_toward_the_limit` |
| The run has 100 messages from `wf steer`: waiting for the process that drives the run, or taken and not answered yet | The run is full: `wf steer` says so, sends nothing, and exits 1. It counts the messages and adds its own while it holds the mailbox's lock, so senders at the same time can't pass the limit together. A taken message counts until its answer is written. The driver answers at most 100 messages at once, also when requests come from elsewhere. | `workflow/steer.py:121`, `workflow/steer.py:248`, `workflow/steer.py:477`, `workflow/steer.py:495` | `test_wf_steer_adds_no_request_to_a_full_run_and_says_so`, `test_senders_at_the_same_time_cant_pass_the_runs_limit_together`, `test_a_message_the_driver_took_counts_toward_the_runs_limit_until_its_answer`, `test_the_driver_answers_no_more_messages_at_once_than_the_runs_limit` |
| The driver looks at the mailbox | It reads at most 1,000 names of requests and takes at most 10 requests in one look. Other files in the mailbox don't count toward these limits, so they can't hide a request. It removes each of its answers 30 seconds after it writes it, and reads no answer again. When the graph stops, it answers the requests in one more look. | `workflow/steer.py:556`, `workflow/steer.py:534`, `workflow/steer.py:549`, `workflow/steer.py:519` | `test_one_look_at_the_mailbox_reads_at_most_ten_requests`, `test_one_look_at_the_mailbox_reads_a_limited_number_of_names`, `test_a_request_is_seen_however_many_other_files_are_listed_before_it`, `test_an_answer_goes_once_its_sender_has_gone_whatever_the_wall_clock_says`, `test_a_request_that_comes_as_the_graph_stops_is_answered` |
| The process that drives the run doesn't take the message within 15 seconds, or `wf steer` is stopped (Ctrl-C) or can't use the mailbox first, also just after it wrote the request | `wf steer` deletes the request, so it can't arrive later. After 15 seconds it says that nothing was sent. When it can't use the mailbox, it says so. | `workflow/steer.py:259`, `workflow/steer.py:253`, `workflow/cli.py:466` | `test_a_message_nobody_takes_is_withdrawn_and_can_never_arrive`, `test_a_stopped_wf_steer_takes_its_message_back`, `test_a_wf_steer_stopped_just_after_it_writes_its_request_takes_it_back`, `test_a_mailbox_that_wf_steer_cant_read_says_so_and_takes_the_message_back` |
| The process finds several requests, or one sent more than 15 seconds ago, in the future, or in an earlier boot | It takes them oldest first, by the time that `wf steer` puts in the name of each request. It answers an old one that there is no live session, and never delivers it. Both processes tell time by `CLOCK_MONOTONIC` and the boot it counts from, so a change of the wall clock changes nothing. | `workflow/steer.py:250`, `workflow/steer.py:534`, `workflow/steer.py:581`, `workflow/steer.py:170` | `test_requests_go_oldest_first_and_an_old_one_is_answered_but_never_delivered`, `test_the_oldest_request_goes_first_even_when_its_file_was_written_last` |
| The system doesn't say which boot it is in | `wf steer` says so, and exits 1. The driver reads no request, and a time from an unknown boot counts as old. wf reads the boot from `/proc` on Linux, and from `sysctlbyname()` on macOS, so it runs no program from `PATH`. | `workflow/steer.py:241`, `workflow/steer.py:454`, `workflow/steer.py:143`, `workflow/steer.py:152` | `test_with_no_boot_id_wf_steer_is_off_and_no_request_counts_as_new`, `test_the_boot_id_comes_from_the_system_and_no_program_on_path_runs` |

### Follow-ups

A run ends when it gets to `end`.
`wf continue` gives a run that succeeded a follow-up request.
Then the run goes on as a new pass of the same run, in the same run folder:

```sh
wf continue <run-id> "Also add a --quiet flag"
wf continue <run-id> --file request.md --limit cost_usd=20 --detach --json
```

The top-level key `follow_up` says where a request goes in.
`to` is a node id, or a list of node ids, as in a route.
These nodes become pending when the follow-up starts, and each one starts once its `needs` are settled.
The request gets to the templates in the variable `follow_up`, which is `none` until the first follow-up:

```yaml
follow_up:
  to: implement

nodes:
  implement:
    needs: [plan]
    workspace: new
    access: edit
    session: continue
    instruction: |
      {% if follow_up and not review %}
      A follow-up request for the change on this branch: {{ follow_up.request }}
      {% elif not review %}
      {{ start.task }}
      {% else %}
      Fix these problems: {{ review.blocking_issues | tojson }}
      {% endif %}
```

`follow_up.request` is the text of the latest request, and `follow_up.number` counts the follow-ups from 1.
The run keeps only the latest request.
The trace keeps each request.

A workflow without `follow_up` takes follow-ups too, when it has an agent node with `access: edit` or `access: full`.
The access that `defaults` gives counts.
Command, human and workflow nodes don't take a request, and a node in a sub-workflow doesn't either.
The request goes to the first of these agent nodes in the order that the graph runs:

- wf steps the rules of [When a node runs](#when-a-node-runs) through a run in which each node finishes in the step that starts it.
  In that run, each route leads to all of its targets, and each node starts once.
- The agent node that starts first takes the request.
  Of two such nodes that start in the same step, the one that comes first in the file takes it.
- The node is the same for each run of the workflow.
  So when a route chooses between agents that change files, a run can send its follow-up to an agent that it didn't run.
  Name the node in `follow_up` when this isn't what you want.

The follow-up clears the outputs of the nodes that it can lead to, as with `follow_up`, with one difference.
A node that a run can reach before the node that takes the request keeps its output.
These are the nodes that a path from `start` leads to without passing through that node.
So the node that takes the request finds what it found on its first pass of a run.
That is the output of each node in its `needs` that ran before it, and of each node that a route before it chose.
A loop that leads back to these nodes doesn't change this.

wf adds the request to the instruction of that node, after the text that the template gives, in a section with the heading `## Follow-up request`.
The section says if an earlier pass of the node did the task, or if the node didn't run before.
In each follow-up, it comes on the first pass of the node that has a job, and in each attempt of that pass.
A pass of `for_each` with no items has no job.
Then the next pass of the node gets the section, such as one that a loop starts.
A pass after the pass with the section doesn't get it.
An instruction that reads `follow_up` gets no section, so the template says where the request goes.
In a workflow without `follow_up`, a template can read `follow_up` when a node takes follow-ups in this way.

`wf validate` says which node takes a follow-up request, as a note and not as a warning.
To send the request to another node, or to more than one, name them in `follow_up`.
With `follow_up`, wf adds no section to an instruction, and the templates bring the request in.

What a follow-up keeps, and what starts again:

- **Outputs.** The follow-up runs again the part of the graph that the nodes that take the request can lead to.
  With `follow_up`, each node in that part reads `none` until it runs again, and its `errors` are empty.
  Without `follow_up`, a node in that part that a run can reach before the node that takes the request keeps its output, as above.
  So the template that a node uses on its first pass also works on its first pass in a follow-up.
  Each other node, and `start`, keeps its output.
  `history.<node>` keeps each output of each pass.
- **Passes.** The pass numbers of each node go on.
  So the folders, the events and the trace of the earlier passes stay as they are.
- **Routes.** Each route counts from zero again.
  So a loop with `max: 3` can go back 3 times in each follow-up.
- **Limits.** A follow-up has a budget of its own.
  Its limits are those of the workflow file, with the `--limit` values of `wf continue` on top.
  They count only what the follow-up spends, and the time that it runs.
  The `--limit` values that earlier passes got don't apply.
  `wf retry --limit` changes the limits for the rest of the follow-up.
  `run.cost_usd`, `run.tokens` and `run.elapsed_s` still count all of the run.
- **Worktrees and branches.** A run that succeeded removed its worktrees, and kept their branches.
  `wf continue` brings back each worktree that the run had, on its branch, with the worktrees of its sub-workflows.
  A temp folder that a node shares comes back empty.
  So the follow-up works on the commits that each branch has now.
  When a branch is gone, or another worktree has it, `wf continue` says so and changes nothing.
- **Bases.** A worktree keeps the base that its node gives.
  By default, that is the commit that the run started at, so `git diff {{ workspace.implement.base }}...HEAD` shows all the work of the run.
  A `base` that names a branch gives the commit that the branch has at each pass.
- **Your checkout.** When a node that the follow-up can reach works in `repo`, the checkout must be on the branch that the run worked on.
  Else `wf continue` says so and changes nothing.
- **Sessions.** On its first pass in a follow-up, a `session: continue` node continues its last session.
  It does so also when the two passes before it both continued, and the count of continued passes in a row starts again there.
  When the run succeeded, Cursor kept the session of each node that a follow-up can reach, so a follow-up continues it there too.
  When the harness can't find the session, the pass starts fresh, and a `session_fresh` event says why.
  A node in a sub-workflow continues its session too, because a workflow node gives its next pass the sessions of its sub-workflow.
  The items of a `for_each` node start fresh, as in each pass.
- **Models.** The overrides that `wf retry --model` gave still apply.
- **Status and checkpoints.** `wf continue` first sets the status to `running`, with the number of the follow-up.
  Then one write of the checkpoints holds the request and its limits, as an input of the graph.
  The next step of the graph takes them in.
  When wf stops before that write, `wf retry <run-id>` finds that the run ended, and the run succeeded again without the follow-up.
  When wf stops after it, `wf retry <run-id>` starts the follow-up.

`wf continue` refuses a run that runs, waits for a person, failed, stopped or is paused at a limit.
`wf retry` and `wf answer` continue those runs.
It also refuses a run of a workflow that has no `follow_up` and no agent node with `edit` or `full` access.
Then it says to add `follow_up`.
A run keeps the workflow files that it started with.
So a `follow_up` that a file gets later counts only for the runs that start after that change.

`--detach`, `--json` and `--no-input` work as for `wf retry`.
With `--detach --json`, wf prints the run id, the run folder, the process id of the background process and the number of the follow-up.
The request is UTF-8 text of at most 100,000 characters.
`--file <file>` reads it from a file, and `--file -` from stdin.

A follow-up shows as a new pass of the run:

- `wf status` and `wf list` say `(follow-up 2)`, and their JSON has `"follow_up": 2`.
  The Firstmate adapter gives the same JSON.
- `events.jsonl` gets a `follow_up` event with the number and the nodes, but not the request.
- The trace gets a `run` record `continued`, with the number and the request.
  A request that feeds a `secret_env` value shows as `[secret]`.
- `wf status` counts a follow-up as a new lap from the nodes that take the request.
  A node that the follow-up leaves out is `skipped`.
- `wf report` counts the follow-ups of each run.
  The time that a run waited for a follow-up isn't part of its duration.
- `wf graph` shows a `follow-up` box, with a dotted arrow to each node that takes the request.

A dry run's follow-up takes the next entries of its script.
A dry run deletes its branches when it succeeds, so their worktrees start again where the run left them.

The name `follow_up` is reserved, so a node named `follow_up` must get another name.

The tests named below are in `tests/test_continue.py`.

| Condition | Effect | Source line | Conformance assertion |
|---|---|---|---|
| A run succeeded, and a node of its workflow takes a follow-up | `wf continue` brings back the worktrees, sets the status, and writes the request into the checkpoints. Then it drives the run from the nodes that take the request. | `workflow/runner.py:677`, `workflow/runner.py:732`, `workflow/runner.py:734`, `workflow/graph.py:157`, `workflow/runner.py:252` | `test_a_follow_up_runs_again_from_its_node_on_the_same_branch_with_the_request`, `test_each_follow_up_has_its_number_and_wf_list_says_it` |
| The workflow has `follow_up` | The nodes that `to` names take the request. wf adds no section to an instruction, also when no template reads `follow_up`. | `workflow/scheduler.py:162`, `workflow/state.py:336` | `test_without_follow_up_the_first_agent_that_can_change_files_takes_the_request`, `test_a_workflows_own_follow_up_wins_and_its_nodes_get_no_section` |
| The workflow has no `follow_up` | wf steps the rules of the scheduler through a run in which each node finishes in the step that starts it. In that run, each route leads to all of its targets. The agent node with `edit` or `full` access that starts first takes the request. Of those that start in the same step, the first in the file takes it. The node is the same for each run. A file with a node named `start` or `end` has no such node. | `workflow/scheduler.py:157`, `workflow/scheduler.py:164`, `workflow/scheduler.py:169`, `workflow/scheduler.py:172`, `workflow/scheduler.py:185`, `workflow/state.py:220` | `test_without_follow_up_the_first_agent_that_can_change_files_takes_the_request`, `test_a_follow_up_goes_to_the_first_agent_in_the_order_of_the_graph_and_says_when_it_never_ran`, `test_validate_reports_a_node_named_start_with_an_agent_that_changes_files` |
| A follow-up of a workflow without `follow_up` starts | The nodes that the node that takes the request can lead to read `none`, except those that a path from `start` leads to without passing through it. So a node in its `needs` that ran before it keeps its output. A node that a route before it chose keeps its output too, also when a loop leads back to it. | `workflow/state.py:226`, `workflow/state.py:227`, `workflow/state.py:228`, `workflow/scheduler.py:190`, `workflow/scheduler.py:193` | `test_a_follow_up_keeps_the_outputs_before_its_node_also_of_a_node_that_a_loop_leads_back_to`, `test_a_follow_up_keeps_the_output_of_the_route_that_the_run_took_before_its_node`, `test_a_follow_up_keeps_the_output_of_a_planner_that_a_route_chose_before_its_node` |
| That node starts its first pass with a job in a follow-up, and its instruction doesn't read `follow_up` | wf adds the request after the instruction, in a section `## Follow-up request`, in each job and attempt of that pass. The section says if an earlier pass of the node did the task. A pass of `for_each` with no items has no job, so the next pass of the node gets the section. A pass after the pass with the section doesn't get it. An instruction that reads `follow_up` gets no section. | `workflow/state.py:325`, `workflow/state.py:327`, `workflow/state.py:337`, `workflow/state.py:340`, `workflow/nodes.py:615`, `workflow/nodes.py:620`, `workflow/nodes.py:429` | `test_a_follow_up_goes_to_the_first_agent_that_can_change_files_with_the_request_in_its_instruction`, `test_a_follow_up_goes_to_the_first_agent_in_the_order_of_the_graph_and_says_when_it_never_ran`, `test_the_request_goes_to_the_first_pass_with_a_job_when_a_for_each_pass_has_no_items`, `test_an_instruction_that_reads_follow_up_itself_is_left_as_it_is` |
| The follow-up starts | With `follow_up`, the nodes that `to` can lead to read `none`. The others keep their outputs. The routes count from zero, and a `follow_up` event goes to `events.jsonl` with the nodes that take the request. | `workflow/graph.py:77`, `workflow/state.py:212`, `workflow/state.py:225`, `workflow/state.py:226`, `workflow/state.py:230`, `workflow/state.py:234` | `test_taking_in_a_follow_up_clears_what_its_nodes_did_and_keeps_the_rest`, `test_routes_count_from_zero_again_in_a_follow_up` |
| `to` names several nodes | Each of them starts, and a node that needs them all runs after them. | `workflow/state.py:235`, `workflow/validate.py:1177` | `test_a_follow_up_can_start_at_several_nodes` |
| A template reads `follow_up` | It gets `{request, number}` of the latest request, or `none` before the first follow-up. The state keeps no list of earlier requests. | `workflow/state.py:86`, `workflow/state.py:135`, `workflow/state.py:426` | `test_the_trace_shows_each_follow_up_as_a_new_pass_of_the_run`, `test_taking_in_a_follow_up_clears_what_its_nodes_did_and_keeps_the_rest` |
| A `session: continue` node runs its first pass in a follow-up | It continues its last session, also after two continued passes in a row. | `workflow/state.py:233`, `workflow/state.py:431`, `workflow/nodes.py:752` | `test_a_session_continue_node_continues_its_last_session_in_a_follow_up` |
| The run succeeds, and a `session: continue` node on Cursor ran in it | Cursor's saved session of each node that a follow-up can reach stays in `~/.wf/sessions/<run-id>-<hash>/`, in folders that only you can open. Those of the other nodes go, as does the rest of the run's temp folder. A follow-up continues the session that stays. | `workflow/runner.py:554`, `workflow/runner.py:797`, `workflow/workspaces.py:321`, `workflow/workspaces.py:332`, `workflow/harnesses/cursor.py:360`, `workflow/harnesses/cursor_acp.py:462` | `test_a_follow_up_continues_the_agent_that_the_run_ended_with` and `test_a_follow_up_without_follow_up_continues_the_agent_that_takes_the_request` in `tests/test_cursor.py`, `test_a_follow_up_loads_the_session_that_the_run_ended_with` in `tests/test_cursor_acp.py` |
| `wf discard` deletes a run | Its saved sessions go too. | `workflow/runs.py:440` | `test_wf_discard_removes_the_saved_sessions_of_a_run` in `tests/test_cursor.py` |
| A workflow node runs a later pass, in a follow-up or in a loop | Its new sub-workflow thread starts with the sessions that the last one ended with, so a `session: continue` node in it continues its session. A `for_each` item's thread starts without sessions. | `workflow/state.py:322`, `workflow/nodes.py:893`, `workflow/nodes.py:902` | `test_a_session_continue_node_in_a_sub_workflow_continues_its_session_in_a_follow_up`, `test_a_workflow_node_passes_its_sub_workflows_sessions_to_its_next_pass` in `tests/test_sessions.py` |
| The follow-up spends tokens or dollars, or runs for some time | Its limits are those of the file, with its own `--limit` values on top. They count only what it spends after it starts. | `workflow/state.py:223`, `workflow/graph.py:81`, `workflow/limits.py:69`, `workflow/context.py:142`, `workflow/runner.py:745` | `test_a_follow_up_has_a_budget_of_its_own_from_the_command_line`, `test_a_follow_up_counts_cost_and_time_from_its_start` |
| `wf retry` continues a follow-up, also with `--limit` | Its limits are the follow-up's, with the ones that `wf retry` kept in this follow-up on top. The `--limit` values of earlier passes don't apply. | `workflow/runner.py:189`, `workflow/runner.py:191`, `workflow/runner.py:301`, `workflow/runner.py:846`, `workflow/runs.py:130`, `workflow/runs.py:138` | `test_a_follow_up_has_a_budget_of_its_own_from_the_command_line`, `test_limits_that_earlier_passes_got_dont_apply_to_a_follow_up` |
| A job runs again before the first step of a stream | It sees the clock, the usage and the start of the follow-up as the saved state has them. | `workflow/runner.py:449`, `workflow/context.py:220` | `test_a_job_that_runs_again_before_the_first_step_counts_only_the_follow_ups_time` |
| The run succeeds | wf records the branch and the commit of each worktree in `worktrees.json`, before the worktrees go. | `workflow/runner.py:555`, `workflow/runs.py:295` | `test_a_sub_workflows_worktree_comes_back_on_its_branch_and_a_gone_one_is_refused` |
| A follow-up starts | wf brings back each worktree in `worktrees.json` on its branch, and each temp folder that a node shares, empty. | `workflow/runner.py:813`, `workflow/runner.py:822`, `workflow/runner.py:828`, `workflow/workspaces.py:543` | `test_a_follow_up_finds_the_workspaces_of_nodes_it_doesnt_run_again`, `test_a_sub_workflows_worktree_comes_back_on_its_branch_and_a_gone_one_is_refused` |
| A branch is gone, another worktree has it checked out, or a folder can't be made | `wf continue` says why, removes the worktrees that it brought back, exits 1 and changes nothing else. | `workflow/workspaces.py:559`, `workflow/runner.py:830`, `workflow/runner.py:832` | `test_a_branch_that_is_gone_or_checked_out_elsewhere_changes_nothing`, `test_a_workspace_that_cant_come_back_changes_nothing` |
| A node that the follow-up can reach works in `repo`, and the checkout is on another branch than the run's | `wf continue` names both branches, exits 1 and changes nothing. | `workflow/runner.py:780`, `workflow/runner.py:791` | `test_a_follow_up_that_works_in_your_checkout_needs_the_branch_the_run_worked_on` |
| The checkpoints can't be written | The status goes back to what it was, and the worktrees go again. wf says why and exits 1. Ctrl-C, SIGTERM and a lost terminal wait until the writes end. | `workflow/runner.py:736`, `workflow/runner.py:739` | `test_a_follow_up_that_cant_be_written_into_the_checkpoints_changes_nothing` |
| wf stopped after it set the status, and before it wrote the request | `wf retry` finds that the run ended, so the run succeeded again, without the follow-up number. | `workflow/runner.py:552`, `workflow/runs.py:539` | `test_a_follow_up_that_stopped_before_its_request_was_written_leaves_the_run_as_it_was` |
| wf stopped after it wrote the request | The state says `running` with the number, never `succeeded` with the old output. `wf continue` refuses another request, and `wf retry` starts the follow-up. | `workflow/runs.py:198`, `workflow/runner.py:766`, `workflow/runner.py:265` | `test_a_follow_up_that_never_started_is_taken_in_by_wf_retry` |
| The run waits for a person, failed, is paused at a limit, or stopped | `wf continue` names the command that continues it, exits 1 and changes nothing. | `workflow/runner.py:759`, `workflow/runner.py:761`, `workflow/runner.py:763`, `workflow/runner.py:766` | `test_a_run_that_waits_or_failed_is_refused_and_stays_as_it_was`, `test_a_run_at_a_limit_or_one_that_stopped_is_refused` |
| Another process drives the run | `wf continue` says so, and names `wf wait`. | `workflow/runner.py:699` | `test_a_run_that_still_runs_is_refused` |
| The workflow of the run has no `follow_up` and no agent node with `edit` or `full` access, or the checkpoints don't hold the run's end | `wf continue` says so, and exits 1. For the workflow, it says to add `follow_up`. | `workflow/runner.py:770`, `workflow/runner.py:776` | `test_a_workflow_without_follow_up_or_an_agent_that_changes_files_is_refused`, `test_a_run_whose_end_isnt_in_its_checkpoints_is_refused` |
| `wf retry` of a run that succeeded, whose workflow takes follow-ups | It says how to give the run a follow-up. | `workflow/runner.py:263`, `workflow/runner.py:871` | `test_a_branch_that_is_gone_or_checked_out_elsewhere_changes_nothing`, `test_wf_retry_of_a_run_that_succeeded_says_how_to_give_it_a_follow_up` |
| The request is missing, given twice, empty or too long | `wf continue` says so before it reads the run. | `workflow/cli.py:775`, `workflow/cli.py:506`, `workflow/cli.py:508` | `test_the_request_is_checked_before_the_run_is_read`, `test_the_request_can_come_from_stdin` |
| `--detach --json` | wf prints the run, its folder, the process id and the number of the follow-up, and a background `wf retry` drives the follow-up. | `workflow/runner.py:749`, `workflow/runner.py:750` | `test_a_detached_follow_up_prints_json_and_wait_reports_it` |
| `wf status`, `wf list`, `wf wait` and the Firstmate adapter read a run that took a follow-up | The state has `follow_up` with its number, from `status.json`, and the text says `(follow-up N)`. | `workflow/runs.py:198`, `workflow/cli.py:536`, `workflow/firstmate.py:175` | `test_each_follow_up_has_its_number_and_wf_list_says_it`, `test_a_detached_follow_up_prints_json_and_wait_reports_it`, `test_firstmate_sees_the_follow_up_as_a_new_pass_of_the_same_run` |
| The trace of a run with a follow-up | It has a `run` record `continued` with the number and the request, and `wf trace` shows them. A request that feeds a secret shows as `[secret]`, and no event holds it. | `workflow/runner.py:866`, `workflow/state.py:234`, `workflow/trace_view.py:204` | `test_the_trace_shows_each_follow_up_as_a_new_pass_of_the_run`, `test_a_secret_request_stays_out_of_the_trace_and_the_events` |
| `wf status` estimates a follow-up | It starts a new lap from the nodes that the `follow_up` event names: the nodes that take the request. Each loop counts from zero, and a node that the follow-up leaves out is `skipped`. | `workflow/progress.py:184`, `workflow/progress.py:189` | `test_wf_status_counts_a_follow_up_as_a_new_lap_from_its_node`, `test_wf_status_says_a_node_that_a_follow_up_skips_is_skipped` |
| `wf report` reads a run with a follow-up, or `wf status` learns from it | It counts the follow-up, and leaves the time that the run waited for it out of the duration. What wf learnt from a run is learnt again when its events grow. | `workflow/report.py:80`, `workflow/progress.py:136`, `workflow/progress.py:517` | `test_wf_report_counts_follow_ups_and_not_the_time_the_run_waited_for_them`, `test_what_earlier_runs_teach_is_learnt_again_once_a_follow_up_adds_to_them` |
| `wf ui` shows a run that took a follow-up | The page reads only the events of the follow-up. Its states, numbers and progress are those that `wf status` and `wf report` give, and the time that the run waited doesn't count. | `workflow/progress.py:184`, `workflow/report.py:80`, `workflow/report.py:187` | `test_wf_ui_reads_only_the_follow_up_into_the_view_of_a_run_it_has_shown` |
| `wf graph` draws a workflow that takes follow-ups | A `follow-up` box has a dotted arrow to each node that takes the request. It does so also for a file that validate refuses, as a route to a node that doesn't exist leads nowhere there. | `workflow/diagram.py:34`, `workflow/scheduler.py:185` | `test_wf_graph_shows_where_a_follow_up_comes_in`, `test_without_follow_up_the_first_agent_that_can_change_files_takes_the_request`, `test_graph_prints_mermaid` in `tests/test_cli.py` |
| A workflow has more nodes in a row than the limit of Python on recursion | wf finds the node that takes a request, because the steps of the scheduler keep no stack. | `workflow/scheduler.py:172` | `test_the_node_that_takes_a_request_is_found_in_a_workflow_longer_than_the_limit_on_recursion` |
| A dry run takes a follow-up | It takes the next entries of its script, and its branches start again where the run left them. | `workflow/runner.py:710`, `workflow/workspaces.py:557` | `test_a_dry_run_takes_the_next_entries_of_its_script_and_makes_its_branches_again` |
| `follow_up.to` names `start`, `end` or a node that doesn't exist, or `follow_up` has another key, or a node is named `follow_up` | `wf validate` errors. | `workflow/validate.py:1135`, `workflow/spec.py:307`, `workflow/spec.py:47` | `test_follow_up_names_nodes_of_the_workflow` |
| A template reads `follow_up` | `wf validate` errors in a workflow where no node takes a follow-up. It warns when a field is read outside `{% if follow_up %}`, and when a command puts it into the shell unquoted. | `workflow/validate.py:294`, `workflow/validate.py:446`, `workflow/validate.py:1116`, `workflow/validate.py:543` | `test_a_template_reads_follow_up_only_in_a_workflow_that_has_it_and_checks_it_first`, `test_an_instruction_that_reads_follow_up_itself_is_left_as_it_is` |
| The workflow has `follow_up`, and no node that a follow-up runs again reads `follow_up` | `wf validate` warns, also when a node before the entry reads it. | `workflow/validate.py:1143` | `test_a_template_reads_follow_up_only_in_a_workflow_that_has_it_and_checks_it_first` |
| `wf validate` checks a file without errors and without `follow_up` | It prints a note, which isn't a warning. The note names the node that takes a follow-up request, or it says that `wf continue` refuses the runs of the workflow. The checks of a follow-up that warn are only for a workflow with `follow_up`. | `workflow/validate.py:76`, `workflow/validate.py:83`, `workflow/validate.py:86`, `workflow/cli.py:351` | `test_validate_says_where_a_follow_up_goes_and_doesnt_warn`, `test_a_follow_up_goes_to_the_first_agent_in_the_order_of_the_graph_and_says_when_it_never_ran`, `test_validate_reports_warnings_and_passes` in `tests/test_cli.py` |
| The workflow has `follow_up`, and a template reads a node whose output a follow-up clears, and that node might not run again before it | `wf validate` warns, and says that this is because of a follow-up. It steps the rules of the scheduler through the follow-up in each way it can go. Any job can fail and run again later, any route can be taken while its `max` allows, and a `for_each` list can be empty. A node counts as run before another, or before the end, only when it finished first in each of those ways. It steps each state once, so it ends for each workflow, also for a list that leads back to its node many times. With more than about ten nodes that run at once, only the nodes that each way to a node passes through count. | `workflow/validate.py:460`, `workflow/validate.py:1147`, `workflow/validate.py:1219`, `workflow/validate.py:1265`, `workflow/validate.py:1293` | `test_a_template_that_reads_a_node_a_follow_up_clears_gets_a_warning`, `test_a_workflow_with_follow_up_validates`, `test_a_follow_up_can_start_at_several_nodes`, `test_an_entry_that_only_a_route_leads_from_isnt_certain_before_another_entry`, `test_an_entry_that_the_scheduler_starts_first_to_break_a_wait_reads_no_later_entry`, `test_a_node_after_an_entry_counts_as_run_before_a_join_that_waits_for_it`, `test_an_entry_waits_for_a_later_entry_it_needs_when_no_wait_can_lock`, `test_a_node_that_an_entry_leads_to_isnt_run_before_a_join_that_the_scheduler_can_start_first`, `test_a_node_that_a_join_waits_for_runs_first_when_both_become_pending_together`, `test_each_read_that_validate_doesnt_warn_about_finds_an_output_however_a_follow_up_runs`, `test_validate_ends_for_an_empty_list_that_leads_back_to_itself_many_times` |

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
- **Cursor**, through its SDK: a custom tool of the SDK, which the Cursor host gives the session.
  The host runs one session, and passes each call to the runner as a `call` frame over its stdin and stdout.
- **Cursor**, through `cursor-agent`: an MCP server the runner serves over HTTP on 127.0.0.1, given to Cursor when the session starts.
  Each session gets its own URL, with an unguessable token in it.
  A request from a web page (an Origin header that isn't 127.0.0.1 or localhost) gets no answer.

Each call arrives tied to its session: a process, a thread id or a session's URL.
So a result can only reach the attempt it belongs to.

A node's `tools` reach the agent the same way.
wf connects to each vendor's MCP server itself, and offers the agent the operations the node may use as tools of its own.
Claude Code gets an SDK MCP server per tool, and Codex a namespace of dynamic tools per tool.
Cursor gets a custom tool per operation through its SDK, and an MCP server over HTTP per tool through `cursor-agent`.
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
  keeps its worktrees for `wf retry`. `wf continue` brings them back from their branches
  (see [Follow-ups](#follow-ups)).
- `wf validate` warns when two `edit` nodes could run at the same time in the same workspace.

### Commit signing

The runner signs its commits as your git config says, with nobody at a terminal.
So use a signing key that needs no passphrase, PIN or touch.
For example, set a key file's absolute path as the repo's `user.signingkey`.
The tests named below are in `tests/test_workspaces.py`.

| Condition | Effect | Source line | Conformance assertion |
|---|---|---|---|
| The runner commits in a worktree it made | The commit skips every git hook, so its message is only `wf: <node> (<run-id>)`, with no trailers. | `workflow/workspaces.py:186` | `test_the_runners_commit_message_has_no_trailer_even_when_a_hook_would_add_one` |
| Your git config signs commits (`commit.gpgsign`) | The runner's commit is signed too, with your signing key (`user.signingkey`). | `workflow/workspaces.py:187` | `test_the_runner_signs_its_commits_when_your_git_config_does` |
| A run may commit: an `edit` or `full` agent works in a worktree the runner made, directly, through a node's workspace or in a sub-workflow | `wf run` and `wf retry` sign a test commit before any agent works. The test commit goes to a temporary object folder, so the repository keeps nothing. | `workflow/runner.py:103`, `workflow/runner.py:203`, `workflow/runner.py:371`, `workflow/workspaces.py:255` | `test_checking_the_key_leaves_nothing_in_the_repository`, `test_an_agent_that_edits_another_nodes_worktree_needs_a_key_that_signs`, `test_an_edit_agent_in_a_sub_workflow_needs_a_key_that_signs_when_it_works_in_a_worktree`, `test_a_run_from_an_unborn_branch_with_a_base_of_its_own_checks_the_key`, `test_a_retried_run_checks_the_key_again_before_any_agent_works`, `test_a_retried_run_checks_the_key_for_an_edit_agent_in_a_sub_workflow`, `test_a_retried_run_without_its_run_json_checks_the_key_where_it_ran` |
| No agent of the run commits: each `edit` agent works in your checkout, and the others only read | There is no test commit, so a key that can't sign doesn't stop the run. | `workflow/runner.py:362` | `test_a_run_the_runner_makes_no_commits_in_doesnt_check_the_key` |
| The test commit fails, or its signing takes longer than `git.signing_timeout` in `config.toml` (30 seconds) | The command exits 1 before any agent works. The error says the key needs a touch, a PIN or a passphrase, or doesn't work. It names the fix: a key file (ssh) or a key id (OpenPGP) that needs none, as the repository's `user.signingkey`. | `workflow/workspaces.py:223`, `workflow/config/keys.py:194` | `test_a_run_whose_key_cant_sign_fails_at_its_start_and_says_how_to_fix_it`, `test_a_key_waiting_for_a_touch_fails_the_run_at_its_start_within_the_timeout`, `test_the_fix_for_an_openpgp_key_names_a_key_id` |
| The repository signs with ssh, and `user.signingkey` is a relative path | The command exits 1 before any agent works, and asks for the key's absolute path. The runner commits in worktrees outside your checkout, where the relative path finds no key. | `workflow/workspaces.py:209` | `test_a_relative_signing_key_fails_the_run_at_its_start` |
| The signing program asks for a passphrase or a PIN at a terminal | It finds none, because git runs in a session of its own, so signing fails at once. | `workflow/workspaces.py:477` | `test_the_signing_program_has_no_terminal_to_ask_on` |
| `wf retry` of a run that has ended | There is no test commit. It says how the run ended. | `workflow/runner.py:203` | `test_retrying_a_run_that_succeeded_says_so_without_checking_the_key` |
| The background process of a detached run fails the test | `status.json` says `failed`, with the error, so `wf wait` reports the cause and not a crash. | `workflow/runner.py:206` | `test_a_detached_run_whose_background_check_fails_records_why` |
| The runner's commit fails | The error says that signing failed only after a timeout, or when a signed test commit fails and an unsigned one works. Otherwise it is git's own error, as `git commit failed: ...`. | `workflow/workspaces.py:195`, `workflow/workspaces.py:261` | `test_a_commit_that_cant_be_signed_says_so`, `test_a_commit_that_fails_for_another_reason_doesnt_blame_signing`, `test_a_commit_whose_signing_test_cant_write_either_doesnt_blame_signing` |
| An agent succeeds, and then the runner's commit fails | The node fails with the attempt that ran, so `wf retry` starts at the next attempt number. | `workflow/nodes.py:605` | `test_a_failed_commit_keeps_the_attempt_number_so_wf_retry_starts_at_the_next_one` |
| wf stops git: after `git.signing_timeout`, for a cancelled job, on Ctrl-C, SIGHUP or SIGTERM | Its process group, with the signing program, gets SIGTERM, so git removes its temporary files. Whatever still runs 5 seconds later (`GRACE`) gets SIGKILL. | `workflow/workspaces.py:511`, `workflow/workspaces.py:516` | `test_a_signing_timeout_lets_git_remove_its_temporary_file`, `test_a_signing_program_that_ignores_sigterm_is_killed` |
| A job is cancelled, for example by a node's `timeout` or Ctrl-C during a run | Its git command stops, and none of its later git commands starts. Its error says it was stopped, not that signing failed. | `workflow/nodes.py:277`, `workflow/workspaces.py:472` | `test_cancelling_a_job_stops_its_git_command_and_the_signing_program`, `test_a_job_cancelled_while_it_waits_for_git_starts_none`, `test_a_stopped_commit_isnt_called_a_signing_failure` |
| Ctrl-C while git runs in wf's main thread, as in the test before a run | git stops, and wf stops as for any Ctrl-C. | `workflow/workspaces.py:503` | `test_ctrl_c_stops_git_and_the_signing_program` |
| wf gets SIGHUP (its terminal closed) or SIGTERM | It stops every git command, also one that is just starting, and starts no new one. Then it exits by that signal, as before. SIGKILL can't be handled, so it still leaves git running. | `workflow/cli.py:303`, `workflow/workspaces.py:428`, `workflow/workspaces.py:437` | `test_wf_stops_git_and_the_signing_program_when_its_terminal_closes_or_it_is_killed`, `test_wf_stops_the_runners_commit_and_starts_no_more_git_when_it_is_killed`, `test_a_signal_while_git_starts_still_stops_that_git` |
| wf stops git, and every process of its group has exited, but nothing has collected them yet | macOS refuses to signal such a group (EPERM). wf takes the group as gone, as for a group that doesn't exist, so it sends no more signals and goes on. | `workflow/workspaces.py:525`, `workflow/workspaces.py:531` | `test_a_signal_while_git_starts_still_stops_that_git` |
| SIGHUP is ignored when wf starts, as under `nohup` | It stays ignored. | `workflow/workspaces.py:418` | `test_a_hangup_that_wf_ignores_stays_ignored` |

## Run folder

```
.wf/runs/<run-id>/              <run-id> is like 20261018-1432-implement-k3f9
  workflow/                     copies of the workflow files as they were run
  input.json
  run.json                      the workflow's name, and the limits and models that `wf retry` keeps
  branches.json                 the branches `branch` named, and where the run left them
  worktrees.json                the branch and commit of each worktree when the run last succeeded, for wf continue
  output.json                   the end output, once the run succeeds
  status.json                   running | waiting | succeeded | failed | limit, and the latest follow-up's number
  events.jsonl                  one line per event: node started, finished, route taken, agent session started, override, ...
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
- A runs folder keeps run folders outside the repo instead.
  It is `--runs-dir <dir>`, else the `WF_RUNS_DIR` environment variable, else `runs_dir` in `~/.wf/config.toml` (see [Configuration](#configuration)).
  Run folders then go to `<dir>/<repo key>/<run-id>/`.
  The repo key is the repo folder's name and a hash of its path, like `wf-3f9a1c2b4d5e`, so the runs of two repos never mix.
  The repo's working tree and `.git/info/exclude` then stay as they were.
  `workspace: new` still adds its branches, and git's records of its worktrees in `.git/worktrees/`.
  The worktree folders are under `~/.wf/worktrees/` (see [Workspaces](#workspaces)).
  A runs folder inside the repo goes into `.git/info/exclude`, as `.wf/` does.
  Later commands for the run need the same setting, in the same folder.
  A blank `--runs-dir` counts as no option, and a blank `WF_RUNS_DIR` as no runs folder, also when `config.toml` names one.
  For a run in `.wf/runs/` while `WF_RUNS_DIR` or `config.toml` names a runs folder, the commands that wf prints start with `WF_RUNS_DIR=`.
  That turns the setting off for that command.
  They keep it under a blank `WF_RUNS_DIR` too, while `config.toml` names a runs folder, because a new shell has no such variable.
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
| `run` | A `wf run`, `wf retry` or `wf continue` process starts to drive the run. | `started`, `retried` or `continued`, the workflow and the input. For `continued`, the number of the follow-up and its request. |
| `pass` | A node starts a pass. | The node and pass, and for a `for_each` node the number of items. |
| `job` | A pass of a node, or an item, starts in this process. | The item, and the number of its first attempt. |
| `render` | wf renders a template or evaluates an expression. | The field, the template, each variable path that it read with its value, and the result or the error. |
| `routes` | A node chooses its route. | Each route in order: its `when`, the values that it read, its result, and if its `max` was used up. Then the route that the node took. |
| `attempt` | An attempt starts, before wf opens its session. | The harness, model, effort, access, `web`, workspace and tools, and how the session is to start: `fresh`, `fork` or `load`, with the session that it continues. |
| `turn` | wf sends the instruction, a reminder, or a `wf steer` message that waited for the turn to end. | The text that wf sent, and in `sends` which of the three it is. |
| `message` | The agent writes text. | The text. |
| `thinking` | A block of the model's thinking ends, or the turn ends first. Only Cursor's SDK reports it. | `duration_ms`, the milliseconds that the block took, or `none` when the turn ended first. Its text, as `message` holds the agent's text. |
| `request` | The runtime gives the id of a request of the turn. Only Cursor's SDK reports it. | `request_id`, which names the request in the records of the runtime. |
| `tool_call` | The agent calls a tool: when the harness says that the call starts, and again when it gives more of the arguments. | The tool and all of its arguments. The last record of an id holds all that the harness gave. A call that a timeout stops has one too. |
| `tool_result` | The tool answers. | All of the answer, and if it is an error. |
| `submit_result` | The agent calls `submit_result`. | The arguments, and the problems that wf found, or that wf accepted the result. |
| `turn_end` | The turn of the agent ends. | How the turn ended, and what it used, in `usage`. |
| `attempt_end` | An attempt ends. | All that `attempt` holds, how the session started and its id, the start and end, how the attempt ended and its `usage`. For a command, the command, its `env` values, its exit code, and the files with its stdout and stderr. |
| `job_end` | A job ends. | Its output or its error, or that it waits for a person. |
| `run_stopped` | The process stops to drive the run. | The status of the run, and its output when it succeeded. |

The `usage` of `turn_end` and `attempt_end` holds `tokens` and `cost_usd`, as the limits count them.
It also holds the parts of `tokens`: `input_tokens`, `cache_read_tokens`, `cache_write_tokens` and `output_tokens`.
`input_tokens` counts only the input that the prompt cache didn't read or write.
`reasoning_tokens` is the part of `output_tokens` that the model used to reason.
A count that the harness doesn't report is 0.
Claude Code reports reasoning tokens only when its result has `output_tokens_details.thinking_tokens`.
`events.jsonl` and `wf report` keep only `tokens` and `cost_usd`.

These fields render: `instruction`, `human`, `command`, `env.<name>`, `base`, `branch`, `harness`, `with.<name>`, `for_each`, `routes[<i>].when` and `end.output.<name>`.
`model` and `effort` render too, with the harness and the key that sets them.

Each record has an `id`, and the `parent` id that contains it:

```
run                          the run
review#2                     pass 2 of review: its pass, job, render, routes and job_end records
build#1[3]                   item 3 of pass 1 of build
review#2.a1                  attempt 1 of that pass
review#2.a1.t2               turn 2 of that attempt: the first reminder
review#2.a1.t2.c4            tool call 4 of that turn (s1 is a submit_result call, m1 a message, k1 a block of thinking, r1 a request)
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
| A run starts, renders, routes, makes attempts, and ends | The trace has a record of each kind, and each `parent` is the id of a record that contains it. | `workflow/trace.py:404` | `test_a_runs_trace_holds_each_kind_of_record_and_its_ids_nest` |
| A sub-workflow runs | Its records go into the trace of the top-level run, under the id of the attempt that runs it. | `workflow/context.py:97` | `test_a_sub_workflows_records_nest_under_the_attempt_that_runs_it` |
| A template or expression runs | The render lists each variable path that ran, with its value. A branch that doesn't run reads nothing. A list or map that the template also used itself, such as with `\| length`, is listed with its fields. | `workflow/templates.py:111`, `workflow/templates.py:301` | `test_a_render_reads_each_path_it_used_with_its_value` |
| A value has more than 1,000 characters, or looks like a reference to a blob | It goes to `blobs/` once, and each record refers to it by its sha256. | `workflow/trace.py:161` | `test_a_large_value_is_stored_once_and_referenced`, `test_wf_trace_reads_only_blobs_of_the_run` |
| A command node names an `env` value in `secret_env` | The trace shows `[secret]` for it, for the input that it comes from, and for its text in each later record, also after `wf retry`. | `workflow/trace.py:420`, `workflow/runner.py:450` | `test_a_secret_never_enters_the_trace` |
| The secret value comes from `item` | The trace hides the item in that node only. It also hides the element of the `for_each` list, or all that the `for_each` reads when it isn't a plain path. An index such as `[-1]` or `[i]` counts as any element. The item of each entry in `results` and `errors` of that node is secret too. So a later process hides an item of a literal list, after `wf retry` or `wf answer`. | `workflow/trace.py:314`, `workflow/trace.py:350`, `workflow/trace.py:263` | `test_a_secret_item_is_hidden_whatever_its_for_each_and_only_in_its_node`, `test_a_secret_item_from_a_literal_list_stays_hidden_in_results_after_a_new_process_continues_the_run`, `test_a_secret_item_that_failed_stays_hidden_in_errors_after_a_new_process_continues_the_run` |
| The secret value comes from the output of a workflow node, or from its `history` | The sub-workflow hides the end output that gives it, and what that end output reads. All of a node's `history`, or all of `history`, hides each end output. | `workflow/trace.py:276`, `workflow/graph.py:131` | `test_a_secret_that_a_sub_workflow_returns_is_hidden_inside_it_too` |
| The secret value comes from the output of a `for_each` workflow node through `results` | The sub-workflow hides the end output that the item's `output` gives. All of an item's entry, all of a node's `results`, or all of `results`, hides each end output. | `workflow/trace.py:289` | `test_a_secret_that_a_sub_workflow_item_returns_through_results_is_hidden_inside_it_too` |
| The secret value comes through `results.<node>`, from the output or the item of an entry | The trace hides it also where the node gives it: in the `job_end` and `submit_result` records of its items. An item's value is hidden in the item's `job` record, and in what the `for_each` list reads. When that list is `results` of another node, that node hides the value too. | `workflow/trace.py:332`, `workflow/trace.py:344`, `workflow/trace.py:346`, `workflow/trace.py:328` | `test_a_secret_read_through_results_is_hidden_where_its_node_and_item_give_it`, `test_a_secret_that_reaches_results_through_a_for_each_list_is_hidden_where_the_first_node_gives_it`, `test_the_secret_paths_of_lists_that_read_each_others_results_come_to_an_end` |
| The secret value comes from a field of the output of a `for_each` node | The same field of each item's output is secret in `history` and in `results` too. | `workflow/trace.py:260`, `workflow/trace.py:261` | `test_a_secret_in_the_output_of_a_for_each_node_is_secret_where_history_and_results_hold_it` |
| A secret value is short | Its text is removed all the same. | `workflow/trace.py:125` | `test_a_short_secret_is_removed_wherever_it_shows` |
| A harness reports a `submit_result` call before `submit_result` runs | The `tool_call` hides the secret fields of the result, as the `submit_result` record does. | `workflow/trace.py:584` | `test_a_secret_in_an_agents_result_is_hidden_from_its_harness_record_of_the_call` |
| `secret_env` names a variable that `env` doesn't set | `wf validate` errors. | `workflow/validate.py:386` | `tests/test_validate.py::test_secret_env_names_only_what_env_sets` |
| An agent uses a wf tool | The `tool_call` holds the arguments. The `tool_result` holds only the size of the answer, on each harness. | `workflow/harnesses/claude_code.py:343`, `workflow/harnesses/codex.py:342`, `workflow/harnesses/remote.py:527`, `workflow/harnesses/cursor_acp.py:412` | `tests/test_tool_sessions.py::test_claude_code_reports_text_and_tool_calls_for_the_trace_but_not_what_a_wf_tool_returned`, `tests/test_tool_sessions.py::test_codex_reports_text_and_tool_calls_for_the_trace_but_not_what_a_wf_tool_returned`, `tests/test_tool_sessions.py::test_cursors_sdk_reports_text_and_tool_calls_for_the_trace_but_not_what_a_wf_tool_returned`, `tests/test_tool_sessions.py::test_cursor_reports_text_and_tool_calls_for_the_trace_but_not_what_a_wf_tool_returned` |
| A timeout stops a tool call on Codex or Cursor | The trace has its `tool_call`, written when the call started. | `workflow/harnesses/codex.py:275`, `workflow/harnesses/remote.py:555`, `workflow/harnesses/cursor_acp.py:401`, `workflow/harnesses/cursor_acp.py:437` | `tests/test_tool_sessions.py::test_codex_reports_a_tool_call_that_a_timeout_stops_for_the_trace`, `tests/test_tool_sessions.py::test_cursors_sdk_reports_a_tool_call_that_a_timeout_stops_for_the_trace`, `tests/test_tool_sessions.py::test_cursor_reports_a_tool_call_that_a_timeout_stops_for_the_trace` |
| The harness gives more of a call's arguments after the call started | The `tool_call` record repeats with the same id and all the arguments. | `workflow/harnesses/remote.py:555`, `workflow/harnesses/cursor_acp.py:402`, `workflow/trace.py:584` | `tests/test_cursor.py::test_progress_the_transcript_and_the_trace_show_text_and_tool_calls`, `tests/test_tool_sessions.py::test_cursor_reports_a_tool_call_again_when_its_input_comes_late` |
| Cursor's SDK sends the pieces of a block of thinking, then the time that the block took | The Cursor host sends one `turn.thinking` event, and the trace has one `thinking` record on the turn. A block that the end of the turn cuts off has `duration_ms: none`. `wf trace` shows each one. | `workflow/harnesses/cursor_host/src/cursor.ts:306`, `workflow/harnesses/cursor_host/src/cursor.ts:276`, `workflow/harnesses/remote.py:546`, `workflow/trace.py:600`, `workflow/trace_view.py:243` | `tests/test_cursor.py::test_the_trace_has_cursors_thinking_with_its_time_and_the_id_of_each_request`, `tests/test_cursor.py::test_a_real_cursor_turn_gives_the_trace_its_request_id_and_the_time_of_its_thinking` (`-m harness`, with a Cursor API key), and the host's own tests |
| A turn on Cursor's SDK starts | The trace has one `request` record on the turn for each request id that the run, its stream or its result gives. A local run of the SDK writes its `request` message only to the store of the agent, so the id of the run gives it. | `workflow/harnesses/cursor_host/src/cursor.ts:256`, `workflow/harnesses/cursor_host/src/cursor.ts:314`, `workflow/harnesses/remote.py:546`, `workflow/trace.py:603` | `tests/test_cursor.py::test_the_trace_has_cursors_thinking_with_its_time_and_the_id_of_each_request`, `tests/test_cursor.py::test_a_real_cursor_turn_gives_the_trace_its_request_id_and_the_time_of_its_thinking` (`-m harness`, with a Cursor API key), and the host's own tests |
| An agent attempt fails before its session opens | Its `attempt` record has the harness, model, effort, access and workspace all the same. | `workflow/nodes.py:455` | `test_an_attempt_that_fails_before_its_session_has_its_settings` |
| A turn or an attempt of an agent ends | Its `usage` has the parts of the tokens that the harness reported. On Claude Code, `reasoning_tokens` is the `output_tokens_details.thinking_tokens` of each result. On Codex, `input_tokens` is `inputTokens` less the cache reads and writes, which Codex counts in it. | `workflow/trace.py:575`, `workflow/nodes.py:596`, `workflow/harnesses/claude_code.py:389`, `workflow/harnesses/codex.py:536`, `workflow/harnesses/remote.py:402` | `tests/test_claude_code.py::test_the_trace_keeps_the_parts_of_the_tokens_and_the_thinking_that_each_result_reports`, `tests/test_codex.py::test_the_trace_keeps_the_parts_of_each_turns_tokens`, `tests/test_codex.py::test_input_that_the_cache_read_and_wrote_all_of_leaves_no_input_tokens`, `tests/test_cursor.py::test_the_trace_keeps_the_parts_of_the_tokens_that_the_sdk_reports` |
| A Codex thread is a fork, or a timeout cuts off a turn | The attempt counts the parts of the tokens that it used. A fork leaves out those of its source thread. A turn that a timeout cuts off keeps the parts reported so far, and a cancelled Cursor turn adds what Cursor reports as it ends. | `workflow/harnesses/codex.py:306`, `workflow/harnesses/codex.py:236`, `workflow/harnesses/remote.py:655` | `tests/test_codex.py::test_a_forked_thread_counts_only_its_own_parts_of_the_tokens`, `tests/test_codex.py::test_a_turn_that_a_timeout_cuts_off_keeps_the_parts_that_codex_reported`, `tests/test_cursor.py::test_a_turn_that_a_timeout_cancels_still_counts_what_cursor_reports_as_it_ends` |
| A Codex turn overflows the model's context window | Codex sets its total to the window's size, with each part 0. The turn counts the tokens, and each part as 0, not less. | `workflow/harnesses/base.py:81` | `tests/test_codex.py::test_a_turn_that_overflows_the_context_window_counts_its_tokens_and_no_parts` |
| A wf tool gets its token | The token goes into no file in the run folder or in `~/.wf`, the trace included. | `workflow/tools/relay.py:199` | `tests/test_tool_sessions.py::test_no_secret_reaches_an_agent_or_the_run_folder` |
| A value holds text that UTF-8 can't hold, such as a lone surrogate | The trace writes it escaped, and the attempt goes on. `wf trace` prints it escaped too. | `workflow/trace.py:134`, `workflow/trace_view.py:158` | `test_writing_the_trace_survives_text_that_utf8_cannot_hold`, `test_wf_trace_prints_text_that_utf8_cannot_hold_escaped` |
| A command's log or stream file can't be opened | The command doesn't start. | `workflow/nodes.py:803` | `test_a_command_whose_stream_file_cant_be_opened_never_starts` |
| `wf retry` stops without driving the run: it still waits for a person, or the run has ended | It writes no `run` record. Each `run` record has a `run_stopped` record. | `workflow/runner.py:265` | `test_each_run_record_has_a_run_stopped_record_whatever_wf_retry_does` |
| `wf trace <run-id> --var <path>` | It shows the value of the path in each render that read it, pass after pass. | `workflow/trace_view.py:119` | `test_wf_trace_var_shows_a_value_across_the_passes_of_a_loop` |
| `wf trace <run-id> --field routes` | It shows each route with its `when`, the values that it read, its result and its `max`, and the route that the node took. | `workflow/trace_view.py:266` | `test_wf_trace_says_why_a_route_was_taken` |
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
Between them, the session adds `agent_usage` each time that the tokens and cost of its attempt so far change.
It looks after each turn, every 30 seconds while a turn runs, and when the session closes.
While a turn runs, the numbers are what the harness has reported of that turn so far.
A harness that reports nothing before a turn ends, such as Cursor, changes them only after each turn.

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
`--port`, or `port` under `[ui]` in `config.toml`, sets another port from 0 to 65535, and `--open` opens the page in the browser.
Each server makes a new random key.
The page sends the key with each request for data, and the server answers no request without it.
So another program or user on the machine can't read the transcripts through the server.
The page lists the runs of the repo, newest first.
With `--all-repos` it lists the runs of each repo in the runs folder.
For the run that you choose, the page shows these:

- the workflow graph, with each node in the color of its state, a word and a sign for the state, and each loop's count
- the progress estimate
- the steps, as `wf report` counts them, also the steps in sub-workflows, each with its state: passes, attempts, failed attempts, items, tokens, cost and time
- the agent sessions that run now
- the events, as `wf watch` prints them
- the transcript, instruction and output of each attempt

The state of a step comes from the run's events, also for a step in a sub-workflow.
For a step in a sub-workflow, the events of the current attempt of the workflow node around it count.
A step that ran in an attempt that ended before the step did is `failed`.
When a workflow node is done, each step that its sub-workflow didn't reach is `skipped`.
When the run goes back to a workflow node, each step in its sub-workflow is `waiting` again.
An attempt with a new number starts the sub-workflow over.
After a limit, a question or a crash, the same attempt goes on, and its steps keep their states and numbers.
A step in the items of a `for_each` workflow node is `running` while an item runs it.
Else it is `failed` if it failed in an item, and `waiting` while an item has it still to do.
The graph shows the same state as the steps.

A step that runs adds what its current pass has done so far to its numbers.
That is the attempt that runs, and the time since the pass started.
An agent attempt that waits for its agent slot doesn't run yet.
It is also the tokens and cost in the last `agent_usage` of each of its sessions.
While the run doesn't move, that time stops at the run's last event.

The page gets new data on its own: the run every 2 seconds, its events every second, and the list of runs every 5 seconds.
For each request, the server reads only the events that came since it last read the run.
It looks only for the files of the attempts that can still change.
A node writes its files before the event that ends its attempt.
So the server looks for them once more after that event, and then stops.
If a look fails for another reason than a missing file, the server looks again at the next request.
The same is true for a workflow file that the server can't read.
The server keeps what it read of each run that a page asked for in the last minute.
The list of runs asks for each run that moves, so the server keeps all of them.
It keeps at least 64 runs: those that pages asked for last.
So a refresh costs no more for a long run, or for each tab that shows the run.
A run that the server can't read is in the list without its progress, and its own page says what is wrong.
It is three plain files in `workflow/ui/`, with no build step and nothing from another server.

| Condition | Effect | Source line | Conformance assertion |
|---|---|---|---|
| A run of a workflow with a loop, and no earlier runs that succeeded | Each step weighs 1, and the loop counts up to its `max`. Event by event, the percentage goes up and never down. That holds also between a node's end and its route. | `workflow/progress.py:341`, `workflow/progress.py:224` | `test_without_earlier_runs_each_step_weighs_one_and_each_loop_counts_to_its_max`, `test_the_percentage_never_goes_down_event_by_event_even_before_a_route_is_chosen` |
| A loop's way back has no `max` | The smallest `max` of another route in the loop bounds it. Only the way back counts its laps. | `workflow/shape.py:111`, `workflow/progress.py:387` | `test_a_loop_whose_way_back_has_no_max_is_bounded_by_the_route_that_has_one_and_counted_by_its_own` |
| A heavier path leads into a loop | A lap counts only the loop's own path. | `workflow/shape.py:88` | `test_a_lap_is_the_loop_s_own_path_not_a_heavier_one_into_it` |
| A needs edge closes a loop | The lap counts when the step that the edge leaves finishes, after the step that the edge goes to has run. When `start` also leads into the step that the edge leaves, its first end is not a lap. When a route made the step that the edge goes to pending already, that end is not a lap. The edge doesn't send the run back. | `workflow/progress.py:229`, `workflow/progress.py:389` | `test_a_loop_that_a_needs_edge_closes_counts_its_lap_when_it_goes_back`, `test_a_needs_edge_loop_counts_no_lap_before_its_first_step_ran`, `test_a_needs_edge_loop_counts_no_lap_when_a_route_made_its_first_step_pending_already` |
| The runs folder holds earlier runs of the workflow that succeeded | Steps weigh their mean time and loops their mean count, from `wf report`. `left_s` gives the time left. A dry run counts only for a dry run. The numbers belong to the version of the workflow that asks for them. | `workflow/progress.py:115`, `workflow/progress.py:131` | `test_with_earlier_runs_steps_weigh_their_time_and_loops_their_mean_count`, `test_wf_status_says_how_far_a_run_is_and_learns_from_earlier_real_runs` |
| A step that no earlier run ran | It weighs the mean of the other steps, and its time so far counts against that mean. | `workflow/progress.py:339`, `workflow/progress.py:370` | `test_a_step_that_no_earlier_run_ran_weighs_the_mean_of_the_others` |
| The run has gone past a loop | The loop counts no more. | `workflow/progress.py:393` | `test_a_loop_the_run_has_left_counts_no_more` |
| A route leaves out a branch | The nodes on it are `skipped`. The steps, the percentage and the time left leave them out. | `workflow/progress.py:376`, `workflow/progress.py:448` | `test_a_branch_a_route_left_out_is_skipped_and_counts_no_more` |
| A branch that a route left out leads back into the chosen branch | Its steps weigh nothing, in the time and in the step count. | `workflow/progress.py:341`, `workflow/progress.py:342` | `test_a_branch_left_out_that_leads_back_into_the_chosen_one_counts_no_more` |
| Parallel steps are in progress | The step with the most left decides the estimate. Of steps with as much left, the one with the least done decides, whatever the order of the steps. | `workflow/progress.py:376` | `test_a_slow_parallel_step_holds_the_estimate_back`, `test_open_steps_with_as_much_left_are_told_apart_by_the_least_done_not_by_chance` |
| A run waits for a person, is at a limit, or stopped | Its steps in progress are `paused`, and the time of each step stops at the run's last event. | `workflow/progress.py:441`, `workflow/progress.py:332` | `test_a_step_in_progress_of_a_run_that_does_not_move_is_paused`, `test_a_run_that_does_not_move_stops_its_clock_at_its_last_event` |
| A `for_each` step, or a sub-workflow, is in progress | Its items that finished, or its sub-workflow's own estimate, count for it in part, before its time does. | `workflow/progress.py:365`, `workflow/progress.py:354` | `test_the_items_that_finished_beat_the_time_a_step_took`, `test_a_sub_workflow_in_progress_counts_its_own_steps_and_items` |
| A run's files go while `wf status` reads them | It says where the run stands, with no estimate. | `workflow/progress.py:420`, `workflow/report.py:363` | `test_wf_status_still_says_where_a_run_stands_when_its_files_change_meanwhile` |
| An agent session opens, and closes | `events.jsonl` gets `session_started`, and then `session_ended`. | `workflow/nodes.py:527`, `workflow/nodes.py:597` | `test_each_session_starts_and_ends_with_an_event`, `test_a_session_inside_a_sub_workflow_names_its_transcript_from_the_run_folder` |
| What the attempt of an agent session has used changes | `events.jsonl` gets `agent_usage` with the tokens and cost so far: after each turn, every 30 seconds while a turn runs, and when the session closes. A change in the parts of the tokens alone sends no event. | `workflow/nodes.py:532`, `workflow/nodes.py:542`, `workflow/nodes.py:562`, `workflow/nodes.py:586` | `test_a_session_tells_what_its_attempt_has_used_so_far_while_and_after_each_turn_runs`, `test_a_session_tells_its_usage_again_only_when_the_tokens_or_the_cost_change`, `test_a_timed_out_turn_still_counts_the_tokens_it_used` |
| A session has no `session_ended`, its attempt goes on, and its process drives the run | `wf sessions` lists it. | `workflow/observe.py:88` | `test_wf_sessions_lists_the_agent_sessions_that_run_now` |
| The attempt, pass or item of a session ends, or that of the workflow node around it | The session is over, also with no `session_ended`. | `workflow/observe.py:76`, `workflow/observe.py:112` | `test_a_session_ends_with_its_attempt_its_item_or_the_workflow_node_around_it`, `test_a_session_inside_a_workflow_node_that_timed_out_is_not_listed` |
| A process that no longer drives the run left a session open | `wf sessions` doesn't list it. | `workflow/observe.py:88` | `test_a_session_left_open_by_a_process_that_no_longer_drives_the_run_is_not_listed` |
| `wf watch` follows a run | It prints each new event, and the run's state each time that the run needs attention. It stops once the run succeeds or is discarded. | `workflow/observe.py:196`, `workflow/observe.py:192`, `workflow/observe.py:186` | `test_wf_watch_prints_the_steps_and_tool_calls_as_they_come_until_the_run_ends`, `test_wf_watch_goes_on_through_a_question_until_the_run_succeeds`, `test_wf_watch_stops_when_the_run_is_discarded`, `test_wf_watch_on_a_run_that_ended_prints_the_last_events_and_how_it_ended` |
| A request to `wf ui` for data has no key, or another key | The server refuses it with 401. | `workflow/ui.py:440` | `test_the_server_gives_data_only_to_the_page_that_has_its_key` |
| A request to `wf ui` names another host in its `Host` header | The server refuses it with 403. So a web page can't read it through a DNS name that points to `127.0.0.1`. | `workflow/ui.py:432` | `test_the_server_refuses_other_hosts_unknown_runs_and_files_outside_the_passes` |
| The page asks for a file of a run | Only `instruction.md`, `output.json`, `session.json` and `attempt-<n>.log` under `nodes/` come back, and no link out of `nodes/` is followed. | `workflow/ui.py:390` | `test_the_page_shows_files_of_the_passes_and_nothing_else` |
| The page shows what a run wrote | It puts it in the page as text, never as HTML, and the server lets the page run only its own script. | `workflow/ui.py:465` | `test_the_server_serves_the_page_and_its_json_on_this_machine_only` |
| `--port` isn't a number from 0 to 65535 | `wf ui` says so and exits with code 1, before it starts a server. | `workflow/config/__init__.py:81` | `test_a_port_that_is_not_a_port_is_refused_with_a_message` |
| `wf ui` has no `--port` | It listens on `port` under `[ui]` in `config.toml`, else on 8765. With `--all-repos`, `runs_dir` in the file is a runs folder too. | `workflow/cli.py:432` | `test_wf_ui_takes_its_port_and_its_runs_folder_from_config_toml_unless_a_flag_names_them` |
| The port is in use | `wf ui` uses a free port, and says which. | `workflow/ui.py:482` | `test_a_port_in_use_gives_way_to_a_free_one` |
| The page asks for a run again | The server reads only the events after those that it read before. It looks only for the files of attempts that run, and once more after the event that ends an attempt. What it adds up is what `wf report` and `wf status` count. | `workflow/ui.py:216`, `workflow/ui.py:354`, `workflow/ui.py:363` | `test_a_refresh_reads_only_the_new_events_and_the_files_of_attempts_that_can_change`, `test_a_transcript_shows_once_the_running_session_writes_it` |
| `events.jsonl` was cut back and grew again since the page last asked | The server reads the run again from its start. | `workflow/ui.py:213` | `test_a_refresh_starts_over_when_events_jsonl_is_cut_back_and_grows_again` |
| A look for a file of an attempt fails for another reason than a missing file | The server looks again at the next request, also for a node that finished. | `workflow/ui.py:381`, `workflow/ui.py:362` | `test_an_error_while_looking_for_a_finished_node_s_files_hides_its_attempt_only_until_the_next_refresh` |
| A sub-workflow attempt starts over | The page lists the attempts of each of its steps in both sub-workflow attempts. | `workflow/ui.py:321` | `test_a_sub_workflow_attempt_that_starts_over_lists_the_attempts_of_both` |
| The page shows the steps | Each step has its state, also a step in a sub-workflow, from the run's events. The graph shows the same state. A step whose attempt around it ended is `failed`, and a step of a workflow node that the run goes back to is `waiting`. When the workflow node is done, a step that its sub-workflow didn't reach is `skipped`. | `workflow/progress.py:307`, `workflow/progress.py:318`, `workflow/progress.py:324`, `workflow/progress.py:321` | `test_each_step_has_a_state_and_a_step_that_runs_shows_its_numbers_so_far`, `test_each_step_of_a_sub_workflow_has_a_state_from_its_own_events` |
| A step runs | Its numbers on the page add the attempt that runs, and the time since its pass started. They also add the last `agent_usage` of each of its sessions, in its sub-workflow too. A step inside an attempt that ended adds nothing more. | `workflow/report.py:151`, `workflow/report.py:165`, `workflow/report.py:208` | `test_each_step_has_a_state_and_a_step_that_runs_shows_its_numbers_so_far`, `test_a_pass_that_runs_adds_what_it_has_done_so_far_also_when_its_sub_workflow_starts_over`, `test_the_spending_so_far_of_items_and_of_agents_inside_workflow_items_adds_up` |
| An agent attempt waits for its agent slot | The page doesn't count it as an attempt until it has the slot. | `workflow/report.py:103` | `test_an_agent_attempt_that_waits_for_its_slot_has_not_started`, `test_a_waiting_attempt_keeps_its_retries_and_its_timeout_starts_with_its_slot` |
| A node fails before it starts, and `wf retry` starts it | Only its pass counts in its time, in `wf report` and on the page. | `workflow/report.py:90`, `workflow/report.py:136` | `test_a_node_that_failed_before_it_started_adds_no_time_once_its_pass_ran` |
| The same attempt of a workflow node goes on after a limit, a question or a crash | Its steps keep their states, times and spending. An attempt with a new number starts them over. | `workflow/progress.py:256`, `workflow/report.py:96` | `test_a_sub_workflow_attempt_that_goes_on_keeps_what_its_steps_did`, `test_a_sub_workflow_attempt_that_goes_on_keeps_the_time_and_spending_of_its_steps` |
| A workflow file can't be read, for another reason than a missing or damaged file | wf ui reads it again at the next request, and then plays the events that came before. A missing or damaged file isn't read again. | `workflow/progress.py:291`, `workflow/progress.py:296`, `workflow/ui.py:182` | `test_a_sub_workflow_that_can_t_be_read_for_now_gets_its_events_once_it_can`, `test_a_workflow_file_that_can_t_be_read_for_now_is_read_at_the_next_refresh` |
| More than 64 runs move | The server keeps what it read of each run that a page asked for in the last minute. So each refresh of the list reads only the new events of each run. | `workflow/ui.py:254` | `test_the_views_of_runs_that_a_page_asks_for_stay_when_more_than_64_move` |
| The server can't read a run that moves | The list shows that run without its progress, and the other runs with theirs. | `workflow/ui.py:129` | `test_a_damaged_run_leaves_out_only_its_own_progress_from_the_run_list` |
| An item of a `for_each` workflow node ends, or its attempt does | The server stops looking for the files of the attempts inside it, and goes on for the other items. | `workflow/ui.py:349` | `test_attempts_inside_workflow_items_end_with_their_item_or_their_attempt_and_not_a_sibling_s` |

### Metrics

wf runs no infrastructure: no command of wf starts a container or runs Docker.
You run ClickHouse yourself, on this machine or on another one, and `[metrics]` in `~/.wf/config.toml` names it:

```toml
[metrics]
enabled = true
url = "https://user:password@host:port/database"
```

`url` is one connection string for the HTTP interface of ClickHouse, because wf speaks that interface with Python's standard library.
So its scheme is `https`, or `http` for a ClickHouse on this machine.
A `clickhouse://` string of ClickHouse names its native TCP port, and wf refuses it.
The user, the password and the database use percent-encoding, as `%40` for `@`, `%3A` for `:`, `%2F` for `/` and `%23` for `#`.
With `enabled = false`, or without `[metrics]`, wf exports nothing.
wf doesn't write the tables of runs yet.
Until the export of runs makes them, each panel of the example dashboard shows an error for a missing table.

The password is the one secret of `config.toml`.
It never goes into a trace, a run folder, a log or a message: each place that shows the URL shows `***` for the password.
wf shows the string only when it parses and has a user and a password, because only then can wf find the password.
The commands and the agents of a run don't get the password in their environment.
Mode 0600 keeps out other users, but not your own programs.
So an agent can read the password in the file too, at each access level and on each harness.
Only a read-only session on Cursor's SDK stays in the workspace and the files and folders that its instruction names (see [Harness adapters](#harness-adapters)).

`docs/metrics/compose.yaml` is an example of ClickHouse in Docker on this machine, which you start and stop yourself:

```sh
docker compose -f docs/metrics/compose.yaml up -d
docker compose -f docs/metrics/compose.yaml down        # down -v also deletes the data
```

It runs ClickHouse alone, on `127.0.0.1:8123`, with the user `wf`, the password `wf` and the database `wf`.
So its URL is `http://wf:wf@127.0.0.1:8123/wf`.
Its compose project is `wf-clickhouse`, and its data is in the named volume `clickhouse` of that project.
A `COMPOSE_PROJECT_NAME` in the shell replaces that name, so unset it, or give each command `-p wf-clickhouse`.
An older wf started a stack with Grafana and a Collector as the compose project `wf-metrics`.
`docker compose -p wf-metrics down` stops that stack, and `down -v` also deletes its data.
ClickHouse runs with small caches, a memory limit of 1 GiB, one thread for each query, and no system log tables.
Over HTTP, it runs a statement only for a request to 127.0.0.1 or localhost that has the header `X-ClickHouse-User`.
A page of another site can't send that header without the consent of the port, which ClickHouse doesn't give.
A page on a DNS name that points to 127.0.0.1 can send it, but then its request names that DNS name in `Host`.
The example makes no tables: the export of runs makes its tables in the database of the URL.

Beside it are two example files for a stack that you run:

- `docs/metrics/grafana/wf-runs.json` is the dashboard "wf runs" for Grafana, with eight panels.
  They show runs by state, runs over time, errors and retries per model, and tokens and cost per model.
  The other four are tool calls per model, node duration, review problems per model, and problem states per pass.
  Each panel reads the tables of the database `wf` with `FINAL`, through a ClickHouse datasource with the uid `clickhouse`.
- `docs/metrics/collector/config.yaml` configures an OpenTelemetry Collector that takes OTLP from Claude Code and Codex.
  It needs the contrib distribution of the Collector, which has the ClickHouse exporter, as the image `otel/opentelemetry-collector-contrib:0.162.0`.
  It writes the data into the ClickHouse database `otel`, and reads the address, the user and the password from its environment.
  Of the spans of Codex, it keeps only `codex.sampling`, `session_task.turn` and `run_turn`.
  It removes the email and the ids of the account from each record, and cuts long tool text to its first 512 bytes.

The tests below are in `tests/test_config.py`, unless a row names another file.

| Condition | Effect | Source line | Conformance assertion |
|---|---|---|---|
| `metrics.enabled` is `true`, and `metrics.url` lacks the scheme, the user, the password, the host, the port or the database | Every command exits 1 before it starts. The message names each part that the string lacks. It shows the string, with `***` for the password, only when the string has a user and a password. | `workflow/config/load.py:157`, `workflow/config/connection.py:91` | `test_with_metrics_enabled_wf_names_each_part_that_the_url_lacks_and_never_its_password` |
| `metrics.enabled` is `true`, and `metrics.url` is missing or blank | The same. The message asks for the connection string of your ClickHouse. | `workflow/config/load.py:163` | `test_with_metrics_enabled_the_url_must_be_set` |
| The scheme of `metrics.url` isn't `http` or `https`, as in a `clickhouse://` string | The same. The message says that wf speaks the HTTP interface of ClickHouse. | `workflow/config/connection.py:94` | `test_with_metrics_enabled_wf_names_each_part_that_the_url_lacks_and_never_its_password` |
| The user or the password has a `/`, `?`, `#` or space without percent-encoding. Or text that isn't a scheme comes before `://`, or the port isn't a number from 1 to 65535 | The string doesn't parse. A `/` in the password puts an `@` after the host, which tells wf so. No message and no line of `wf config show` shows the string, because the password could be in any part of it. The message also names each part that the string lacks. wf reads the parts in the order of `https://user:password@host:port/database`, and the last `@` ends the user and the password. Text before `://` that isn't a scheme can hold the login, before its last `@`. With an `@` after the `://`, the login can start on either side of it, so wf names no missing login. | `workflow/config/connection.py:60`, `workflow/config/connection.py:61`, `workflow/config/connection.py:64`, `workflow/config/connection.py:85` | `test_with_metrics_enabled_wf_names_each_part_that_the_url_lacks_and_never_its_password`, `test_wf_config_show_hides_a_url_that_doesn_t_parse` |
| A character that isn't ASCII stands in place of a `:`, `/`, `?`, `#` or `@` before the database, such as a full-width colon | The string doesn't parse, because Python's `urlsplit` refuses it, or because the character is in text before `://` that isn't a scheme. The message says that the string has such a character. wf reads the parts with the ASCII character in its place, and names each part that the string still lacks. The database starts at the first `/`, `?` or `#` after the last `@`, and `urlsplit` checks only the text before it. So wf reads a full-width `@` in the database as text. | `workflow/config/connection.py:57`, `workflow/config/connection.py:70`, `workflow/config/connection.py:142` | `test_with_metrics_enabled_wf_names_each_part_that_the_url_lacks_and_never_its_password` |
| `metrics.url` parses, but lacks the user or the password | No message and no line of `wf config show` shows it, because a password could be where wf reads the host, the port or the database. | `workflow/config/connection.py:113` | `test_wf_config_show_hides_a_url_that_doesn_t_parse` |
| The user, the password and the database use percent-encoding | wf decodes them. A raw `@` in the password also works, because the last `@` ends the user and the password. | `workflow/config/connection.py:89` | `test_the_user_and_the_password_of_the_url_are_percent_encoded` |
| `metrics.enabled` is `false`, or there's no `[metrics]` | wf exports nothing, and takes a URL that lacks parts. | `workflow/config/load.py:159` | `test_with_metrics_off_or_absent_wf_takes_any_url_and_exports_nothing` |
| `config.toml` sets `metrics.url`, and its mode gives other users access (wider than 0600) | Every command exits 1 before it starts. The message names the mode and the fix: `chmod 600`. The rule holds for each URL, because wf can't tell a URL without a password from one with a password where wf reads another part. Other users can read a file without `metrics.url`. | `workflow/config/load.py:183` | `test_a_file_that_sets_metrics_url_must_be_yours_alone`, `test_a_file_without_metrics_url_can_be_read_by_others` |
| A run, its trace, the environment of its commands, and the output of `wf run`, `wf status`, `wf trace`, `wf report` and `wf config show` | None of them holds the password. The repr of the configuration shows `***` for it. | `workflow/config/connection.py:110` | `test_the_password_is_in_no_trace_record_run_folder_or_output`, `test_the_password_of_a_config_is_never_in_its_repr` |
| `docs/metrics/compose.yaml` | `docker compose config` reads it: ClickHouse alone, on `127.0.0.1:8123`, in the project `wf-clickhouse`. It has its user, password and database, a named volume, the files for little memory and for the headers, and no SQL. Pytest skips the test without Docker with Compose. | `docs/metrics/compose.yaml:26`, `docs/metrics/compose.yaml:30` | `test_the_example_compose_file_parses_and_runs_clickhouse_alone_on_127_0_0_1` in `tests/test_metrics.py` |
| The README | It gives the commands that start and stop the example, and the command that stops the stack of an older wf. It also gives the URL of the example, which loads with `metrics.enabled`. It says that a `COMPOSE_PROJECT_NAME` in the shell replaces the project name. | `README.md:188` | `test_the_readme_gives_the_commands_and_the_url_of_the_example` in `tests/test_metrics.py` |
| The dashboard and the Collector | The dashboard reads the database of the example through one datasource. The Collector takes the login of ClickHouse from its environment, and its filter knows the `service.name` of the Codex app server. The Collector's file names the contrib image that validates it. | `docs/metrics/grafana/wf-runs.json:23`, `docs/metrics/collector/config.yaml:51` | `test_the_dashboard_reads_the_database_of_the_example_through_one_datasource`, `test_the_collector_takes_the_login_of_clickhouse_from_its_environment`, `test_the_codex_that_wf_starts_names_its_service_as_the_collector_s_filter_expects` in `tests/test_metrics.py` |
| wf | It has no `wf metrics` command, and no module of `workflow/` runs `docker`. | `workflow/cli.py:98` | `test_wf_runs_no_docker_and_has_no_metrics_command` in `tests/test_metrics.py` |

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
   It can for a plain path, also through filters that keep its items, such as `| list` or `| selectattr('ready')`.
8. `item` and `index` appear only in `for_each` nodes. `for_each` reads a list.
   `results.<node>` names a `for_each` node, and its fields are checked against the node's output and item schemas.
   The items of a `for_each` list from `results.<node>` are checked against the schema of its entries.
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
16. `follow_up.to` names nodes of the workflow, not `start` or `end`.
    A template reads `follow_up` only in a workflow that takes follow-ups.
    That is a workflow with the `follow_up` key, or with an agent node that has `edit` or `full` access.

Warnings:
- A template reads a node that might not have run at that point, outside an `{% if %}`
  that checks it.
  A read by position in `results.<node>`, such as `results.build[0]`, `results.build[-1]` or `results.build[i]`, counts as a read of that node.
  `results.<node>` is never `none`, so `{% if results.build is not none %}` doesn't check the node.
- A template reads a field of the output at a position of `results.<node>`, as in `results.build[0].output.done`, and the node has `on_item_error: continue`.
  The read is outside an `{% if %}` or an expression that checks that output first, as `{% if results.build[0].output %}` or `... if results.build[0].output else none`.
  A read through a name isn't checked, such as a loop variable, `item`, or a name that `set` or `with` gives.
  Neither is a read through a filter, such as `selectattr`, `rejectattr` or `map`.
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
  Cursor, through `cursor-agent`, loads your account's User Rules, the skills in your home folder, and the repo's hooks.
  That is for its `edit` nodes, and for every node when `cursor.runtime` is `acp` or wf has no Cursor API key.
- An agent with `access: full` reads output from an agent with `tools`.
  Messages, notes and mail that other people write can carry instructions aimed at agents.
- A template reads a field of `follow_up` outside an `{% if follow_up %}` that checks it.
- The workflow has `follow_up`, and no template reads `follow_up`.
- The workflow has `follow_up`, and a template reads a node whose output a follow-up clears.
  And that node might not run again before the template in a follow-up.

Notes are information, and not problems.
`wf validate` prints them for a file without errors, and `wf run` doesn't:
- For a workflow without `follow_up`, the node that takes a follow-up request.
  Or, when it has none, that `wf continue` refuses the runs of the workflow.

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
    remote.py       a harness whose sessions run in another program: the wf-session protocol
    cursor.py       Cursor's SDK, in the Cursor host (cursor_host/, TypeScript), through remote.py
    files_tool.py   read and search files, only where the step may, for read-only agents on Cursor's SDK
    cursor_acp.py   cursor-agent acp, the Agent Client Protocol over stdio: `edit`, cursor.runtime acp, and no API key
    mcp_http.py     submit_result as an MCP server over HTTP, for cursor-agent
  config/           ~/.wf/config.toml: the keys, their one precedence, the checks, and wf config show
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
  steer.py          wf steer: a message into the live agent session of a running node
  cli.py            init, validate, graph, run, retry, tools
```

The `wf` command is a `[project.scripts]` entry in `pyproject.toml`:
`wf = "workflow.cli:main"`.

Dependencies to add to `pyproject.toml`: `jinja2`, `pyyaml`, `jsonschema` and
`langgraph-checkpoint-sqlite`.
(`pyyaml` and `jsonschema` are already installed through LangChain, but the package should list what it imports.)
Codex needs no Python package: the runner talks to the `codex` CLI.
Cursor needs Node.js: the runner starts the Cursor host, which installs `@cursor/sdk` beside it from its lockfile, and `edit` nodes run `cursor-agent`.

### Harness adapters

Details and sources are in `docs/research/harness-adapters.md`.

Each harness implements two small interfaces:

```python
class Harness(Protocol):
    name: str
    async def open(self, run_dir: Path) -> None            # once per run
    async def check_model(self, model: str | None, access: str) -> None  # after open: Unavailable if it can't run
                                                           # the model at that access level (no model: at all)
    async def session(self, spec: SessionSpec) -> Session  # new, or forked from spec.fork_from: raises Unavailable, or
                                                           # StartFailed when a retry may work
    async def close(self) -> None

class Session(Protocol):
    id: str
    async def send(self, text: str) -> TurnEnd   # one turn: done, unavailable or failed
    def pending_usage(self) -> Usage             # what a turn that hasn't returned has used so far
    async def steer(self, text: str, ident: str) # a wf steer message: "delivered", "queued", "unconfirmed", or None for the next turn
    async def close(self) -> None                # also interrupts a running turn
    # optional: late_usage() -> Usage, what a turn that close() ended reported as it ended
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
| Runs as | `claude-agent-sdk`: one process per session | one `codex app-server --disable hooks --disable apps` per run, JSON-RPC over stdio, one thread per session | the Cursor host, `node wf-cursor-host.js`, on `@cursor/sdk`: one process per session, which speaks the wf-session protocol over stdio, with the SDK's own reconnects on (`enableAgentRetries`) |
| `submit_result` | SDK MCP server | dynamic tool (`experimentalApi`) | a custom tool of the SDK: each call is a `call` frame to the runner |
| `read-only` | Read, Grep, Glob, and Bash limited to read-only git commands | sandbox `read-only` | none of Cursor's own tools: wf's file tools and `git` tool read only the workspace and the files and folders the instruction names. Cursor's sandbox is on, and no other MCP server is offered |
| `edit` | Read, Edit, Write, Bash with `permission_mode="dontAsk"` | sandbox `workspace-write` | through `cursor-agent`'s ACP: agent mode, with writes inside the workspace and commands allowed, and writes to temp folders and `~/.cursor` denied |
| `full` | `permission_mode="bypassPermissions"` | sandbox `danger-full-access` | all of Cursor's tools but `askQuestion`, with the sandbox off |
| `web: false` | no WebSearch or WebFetch | `web_search: "disabled"` (the default is `cached`) | no `webSearch` or `webFetch` tool |
| Repo instructions | `setting_sources=["project"]`, `claude_code` system prompt | `AGENTS.md` and the repo's skills | Cursor's `project` settings layer |
| `project_instructions: false` | `setting_sources=[]` | `project_doc_max_bytes: 0`, and each repo skill off | no settings layer (`settingSources` left out) |
| Usage limit | a `RateLimitEvent` that isn't `allowed` | `codexErrorInfo: "usageLimitExceeded"` with `willRetry: false` | a `RateLimitError` that a retry can't clear, a plan or payment notice, or a `[resource_exhausted]` error that isn't a `RetriableError`. A `RetriableError` or a lost connection fails the attempt, and the retry waits 30 seconds |
| Usage | tokens and dollars | tokens | tokens, but none for `edit` (ACP) |
| `session: continue` | fork with `fork_session` | fork with `thread/fork` | the SDK resumes the last pass's agent (`Agent.resume`), with no fork |
| wf tools | an SDK MCP server per tool | a namespace of dynamic tools per tool | a custom tool of the SDK per operation |
| `wf steer` | a user message, read at the agent's next step; `--replay-user-messages` says when | `turn/steer` into the running turn | the run's `steer`, into the running turn. When Cursor sends it back: the next turn |

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
- Cursor's SDK can't refuse one call as it comes, as ACP lets the runner do.
  So the host sets up front which tools a session gets, and cancels the turn when Cursor still calls another one.
  For example, a turn of a read-only session fails when it runs a shell, a search of Cursor's own, or another MCP server's tool.
- A read-only session on the SDK reads and searches files only through wf's tools (`read_file`, `search_files`, `find_files`, `list_dir`, `workflow/harnesses/files_tool.py`).
  They read the workspace, and the run folder, the run's repo or another node's workspace when the instruction names it.
  They also read each other file or folder that the instruction names by an absolute path or a `~/` path, such as a report.
  A path in quotes may hold spaces, such as `"/Applications/Visual Studio Code.app"`.
  The home folder and the folders above it stay out, also when the instruction names them.
  So a search can't walk the data of every app.
  They read regular files only.
  A search runs in a process of its own, which stops after 30 seconds or when its call is cancelled.
  So an agent's slow pattern can't hold the run.
  A glob is matched without regular expressions, so no pattern can hold a call either.
  Cursor's own read and search tools take any path.
  A search from the home folder walks other apps' private data, and macOS then asks you about each search.
- The SDK has no answer per call to keep writes in the workspace, and no test has shown yet that Cursor's sandbox keeps them there.
  So `edit` nodes run through `cursor-agent`'s ACP.
  Two checks (`-m harness`) come before they move.
  `test_cursors_sandbox_keeps_edit_writes_in_the_workspace_and_lets_commands_reach_the_network` must pass, so writes stay in the workspace and commands still work as on ACP.
  `test_cursors_sandbox_keeps_edit_commands_from_the_keychain` says whether Cursor at `edit` still counts as reaching the Keychain.
- The Cursor host sets the SDK's `enableAgentRetries`, so the SDK carries a turn over a lost connection by itself.
  ACP switches that loop off, so on `cursor-agent` one lost stream ended the turn.
- The host maps the SDK's errors to classes.
  A refused API key, a usage limit, or a model Cursor can't run makes Cursor unavailable.
- With no API key set, read-only and full nodes run through `cursor-agent`'s ACP, as they did before the SDK.
  The run's first such session warns once, and the warning says how to add a key.
  A lost connection that the SDK couldn't carry over fails the attempt, and the retry waits 30 seconds, also as a session opens.
- A message from `wf steer` that Cursor doesn't answer within 8 seconds is `queued`.
  When Cursor sends it back later, the runner sends it as the next turn.
  That is what the runner does with any message that a harness can't take.
  A message that Cursor never answers for is `unconfirmed` when the session closes.
- A frame is at most 1 MiB.
  A tool's answer that is bigger reaches the agent as an error that says so, and a bigger turn fails.
- Each turn's tokens come from the SDK's usage report.
  Cursor bills later, so a session's cost in dollars isn't counted, and the transcript keeps what Cursor reported as billed when the session closed.
- The host ships as its TypeScript source, which Node 22.18 or later runs as it is.
  The SDK can't be bundled into one file, so the first Cursor session installs it beside the host with `npm ci --omit=dev --ignore-scripts`, from the host's `package-lock.json`.
  The protocol's frames are in `workflow/harnesses/cursor_host/protocol/`, as JSON Schema and an example of each.

Through `cursor-agent`'s ACP, for `edit` nodes and for every node when `cursor.runtime` is `acp` or wf has no Cursor API key:
- Cursor asks before what it may not do by itself.
  The runner answers from the node's access level with "allow once" or "reject".
  It never answers "allow always", which Cursor would save.
- Cursor saves the model it was started with, and the parameters it last used for that model, into its settings folder.
  It also reads its permissions from there.
  So each cursor-agent process gets a new folder in `~/.wf/temp/<run-id>/cursor-agent/`.
  It's outside the repo because Cursor also writes your account's details there.
  A session that a later pass may continue moves to `~/.wf/sessions/<run-id>-<hash>/cursor-agent/`, without the config folder.
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
  `wf retry --model` changes only the harness, model and effort of a node or an item.
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
