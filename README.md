<img src="assets/icon.svg" width="64" height="64" alt="">

# wf

Run YAML workflows of coding-agent steps.

A workflow is a graph of steps.
Its steps are tasks for coding agents (Claude Code, Codex or Cursor), shell commands, questions for a person, and other workflows.
Every step declares what it outputs, and routes decide what runs next.
So loops like "implement, test, review, and go round until clean" are a few lines of YAML.
wf runs the graph on LangGraph:
- parallel steps work in their own git worktrees;
- a failed attempt is retried, or falls back to another agent;
- limits cap a run's cost, tokens and time;
- any run can pause for a person and be answered later, from a terminal or by
  an autonomous operator.

One cap on live agent sessions holds across all your runs that share a `~/.wf`.
Agents can also read Slack, Granola, Linear, Notion and Gmail through tools that wf brings itself.

## Install

Needs Python 3.13, [uv](https://docs.astral.sh/uv/), and Claude Code, Codex or Cursor set up.
Cursor runs through Cursor's SDK, so it needs Node.js 22.18 or later and a Cursor API key (`wf runtimes cursor signin`).
Its `edit` steps run through `cursor-agent`, logged in.
Until a key is set, all its steps run through `cursor-agent`, and each run warns once.

```sh
uv tool install --editable .
```

## Quick start

```sh
wf examples                              # copy two complete workflows into ./wf-examples
wf validate wf-examples/implement.yaml   # check the wiring before anything runs
wf graph wf-examples/implement.yaml      # see it as a Mermaid diagram
wf run wf-examples/implement.yaml task="Add a --version flag"
wf report --workflow implement           # what the runs did, per step
wf trace <run-id> --var review.blocking_issues   # what one run's templates read, pass by pass
```

`wf steer <run-id> <node> <message>` tells an agent something while it works.
Claude Code and Codex agents read it at their next step, and so does Cursor when its SDK takes the message.
When an agent can't take it during a turn, wf keeps it for the next turn.
wf drops it when the agent submits its result first, which is common.

`wf run <file> --dry-run <script.yaml>` walks a workflow with scripted agent
results, for free. `wf --help` lists every command.

`wf retry <run-id>` continues a failed or paused run, or a run that a limit stopped,
from its last checkpoint. Steps and items that finished don't run again.
When a model fails again and again, `--model` gives that step, or one item of it, another model for the rest of the run:

```sh
wf retry <run-id> --harness 'review[2]=claude-code' --model 'review[2]=claude-opus-5-5'
```

`wf continue <run-id> "<request>"` gives a run that succeeded a follow-up request.
The run goes on from the node that its workflow's `follow_up` names, on the same branches and agent sessions.
Without `follow_up`, the first agent node with `edit` or `full` access takes the request.
Then wf adds the request to the instruction of that node.
Its routes count from zero again, and `--limit` gives it a budget of its own.

Steps that read Slack, Granola, Linear, Notion or Gmail list them in `tools`.
Sign in to each once per machine:

```sh
wf tools list                            # each tool, and whether it's set up
wf tools slack setup                     # sign in; the secret goes to the macOS Keychain
wf tools doctor                          # check that every tool you set up works
```

## Watch a run

`wf status` says how far a run is: its step on the way from start to end, the times each loop went back, and a percentage.
The percentage is an estimate.
Until earlier finished runs show how long each step takes, wf counts each loop as if it goes back up to its `max`.
Then it gives the time left too.

```sh
wf status <run-id>     # step 3 of 4 (tests), review → implement 1 of at most 3, about 38%
wf sessions            # each agent session that runs now: node, harness, model, last tool call, quiet time
wf watch <run-id>      # the run's steps and tool calls as they come, until it succeeds: keep it open in a pane
wf ui                  # a web page on 127.0.0.1 with all of it, at the address that it prints
```

The `wf status` line above is for a run of `implement.yaml` with no earlier finished runs, at its tests for the second time.

`wf ui` is a small web page that wf serves on your machine only.
It lists the runs of the repo.
For each run it draws the workflow graph, with the state of each step.
It also shows the progress, the numbers of `wf report` for each step with its state, the live events, and each session's transcript and instruction.
A step that runs shows what it has done so far: its attempts, its time, and its tokens and cost.
It gets new data on its own: the run every 2 seconds, its events every second, and the list of runs every 5 seconds.
It needs no build step and no other service.
The address that `wf ui` prints holds a key, so other programs and users on the machine can't read the runs through it.

![wf ui: a run of implement.yaml at its tests for the second time](assets/wf-ui.png)

## Configuration

wf's own settings are in `~/.wf/config.toml`, and `WF_HOME` moves `~/.wf`.
wf only reads the file, and each key in it is optional:

```toml
runs_dir = "~/wf-runs"        # keep run folders outside the repos, a folder per repo
max_agents = 4                # at most 4 live agent sessions, across all runs

[claude]
auth = "subscription"         # or "api-key", to use ANTHROPIC_API_KEY

[cursor]
runtime = "sdk"               # or "acp", to run each Cursor node through cursor-agent
api_key_file = "~/.config/cursor-key"   # the Cursor API key, in place of the macOS Keychain

[ui]
port = 8765                   # where wf ui listens, on 127.0.0.1

[tools]
signin_port = 47152           # where wf tools <name> setup waits for the browser

[git]
signing_timeout = 30          # the seconds that signing a commit of the runner may take

[paths]                       # for a machine with more than one install: else wf finds each on PATH
codex = "/opt/homebrew/bin/codex"
cursor_agent = "~/.local/bin/cursor-agent"
node = "/opt/homebrew/opt/node@22/bin/node"

[metrics]                     # see Metrics below
enabled = true
url = "https://wf:password@clickhouse.example.com:8443/wf"
```

A key takes its value from its command-line flag, else from its environment variable, else from the file, else from wf's default.
The flags are `--runs-dir`, and the `--port` of `wf ui` and of `wf tools <name> setup`.
The environment variables are `WF_RUNS_DIR`, `WF_MAX_AGENTS`, `WF_CLAUDE_AUTH` and `WF_CURSOR_RUNTIME`.
A variable with an empty value counts as no variable.
Only an empty `WF_RUNS_DIR` turns the runs folder off, also when the file names one.
`wf config show` prints the value of each key and where it comes from.

wf doesn't start when the file has an unknown key or a value of the wrong kind.
Its message names the key, the file and what the key allows.
The file must be yours, and no other user may change it, because it names programs that wf runs.
Group write is fine when no other user is in the group of the file, as with a group for each user.
wf doesn't read `~/.wf/config.yaml` any more.
While that file is there, wf stops, and names the key in `config.toml` for each of its settings.

## Metrics

wf runs no infrastructure.
You run ClickHouse yourself, on this machine or on another one, and `[metrics]` in `config.toml` names it.
`url` is one connection string for the HTTP interface of ClickHouse: `https://user:password@host:port/database`, or `http://` for a ClickHouse on this machine.
Percent-encode the user and the password, for example `%40` for `@` and `%2F` for `/`.
With `enabled = true`, the string must have each of its parts, and wf names each part that it lacks and doesn't start.
With `enabled = false`, or without `[metrics]`, wf exports nothing.
wf doesn't write the tables of runs yet.
Until the export of runs makes them, each panel of the example dashboard below shows an error for a missing table.

The password is the one secret of `config.toml`.
wf never shows it: `wf config show` and each message of wf show `***` in its place.
wf doesn't show a string that doesn't parse, or that lacks the user or the password, because it can't find the password there.
When the file sets `url` under `[metrics]`, it must be yours alone, because the password goes there.
Then wf doesn't start until you run `chmod 600 ~/.wf/config.toml`.
Mode 0600 keeps out other users, but not the programs that you run.
So an agent can read the password too, at each access level and on each harness.
Only a read-only session on Cursor's SDK stays in the workspace and the files and folders that its instruction names.

`docs/metrics/compose.yaml` is an example that runs ClickHouse in Docker on this machine, with a profile for little memory.
You start it and you stop it, and wf never runs Docker:

```sh
docker compose -f docs/metrics/compose.yaml up -d     # ClickHouse on 127.0.0.1:8123
docker compose -f docs/metrics/compose.yaml down      # stop it (down -v also deletes its data)
```

Its compose project is `wf-clickhouse`.
A `COMPOSE_PROJECT_NAME` in your shell replaces that name, so unset it, or give both commands `-p wf-clickhouse`.
An older wf started a stack with Grafana and a Collector as the compose project `wf-metrics`.
`docker compose -p wf-metrics down` stops that stack, and `down -v` also deletes its data.

The example sets the user `wf`, the password `wf` and the database `wf`, so these are the lines of `config.toml` for it:

```toml
[metrics]
enabled = true
url = "http://wf:wf@127.0.0.1:8123/wf"
```

ClickHouse then listens on 127.0.0.1 only.
Each user and program on this machine that knows the password can read and change the data.
For private data, change the password in both files.
The HTTP port runs a statement only for a request to 127.0.0.1 or localhost with the header `X-ClickHouse-User`, so a web page can't send one.

Beside the example are two files for a stack that you run.
`docs/metrics/grafana/wf-runs.json` is a dashboard of wf runs for Grafana, which reads the database `wf` through a ClickHouse datasource with the uid `clickhouse`.
`docs/metrics/collector/config.yaml` configures an OpenTelemetry Collector that takes OTLP from Claude Code and Codex, and writes it to the database `otel`.
It needs the contrib distribution of the Collector, which has the ClickHouse exporter, as the image `otel/opentelemetry-collector-contrib:0.162.0`.
The Collector reads the address, the user and the password of ClickHouse from its environment.

## Docs

- [docs/workflow-spec.md](docs/workflow-spec.md): the file format, and how runs behave.
- [docs/tools.md](docs/tools.md): the tools agents can use, how each signs in, and which of their operations count as reads.
- [skills/wf-workflows](skills/wf-workflows/SKILL.md): an agent skill for building,
  changing and improving workflows.
- [integrations/firstmate](integrations/firstmate/README.md): let
  [firstmate](https://github.com/kunchenguid/firstmate)'s first mate run and
  supervise wf runs.
- [docs/research/harness-adapters.md](docs/research/harness-adapters.md): how each
  coding agent is driven.
- The code is meant to be read: `workflow/__init__.py` gives the order.

## Status

Claude Code, Codex and Cursor are supported.
Tools attach to sessions on all three.

## Development

```sh
uv run pytest                  # fake agents, no cost
uv run pytest -m harness       # real agents: uses your Claude Code, Codex and Cursor quota
```

No test starts a container.
One test reads `docs/metrics/compose.yaml` with `docker compose config`, and pytest skips it on a machine without Docker with Compose.

The tests of Cursor's runtime run the real Cursor host on a fake Cursor SDK, so they need Node.js 22.18 or later.
The host has its own TypeScript checks: `npm ci && npm run check && npm test` in `workflow/harnesses/cursor_host`.

The tests need git 2.34 or later: they hide your own git config with `GIT_CONFIG_GLOBAL` (2.32), and sign with ssh keys (2.34).
