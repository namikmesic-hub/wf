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

Needs Python 3.13, [uv](https://docs.astral.sh/uv/), and Claude Code, Codex or Cursor (`cursor-agent`) logged in.

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

`wf steer <run-id> <node> <message>` tells an agent something while it works, as you would in its own terminal.

`wf run <file> --dry-run <script.yaml>` walks a workflow with scripted agent
results, for free. `wf --help` lists every command.

`wf retry <run-id>` continues a failed or paused run, or a run that a limit stopped,
from its last checkpoint. Steps and items that finished don't run again.
When a model fails again and again, `--model` gives that step, or one item of it, another model for the rest of the run:

```sh
wf retry <run-id> --harness 'review[2]=claude-code' --model 'review[2]=claude-opus-5-5'
```

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
It also shows the progress, the numbers of `wf report`, the live events, and each session's transcript and instruction.
It gets new data on its own: the run every 2 seconds, its events every second, and the list of runs every 5 seconds.
It needs no build step and no other service.
The address that `wf ui` prints holds a key, so other programs and users on the machine can't read the runs through it.

![wf ui: a run of implement.yaml at its tests for the second time](assets/wf-ui.png)

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

The tests need git 2.34 or later: they hide your own git config with `GIT_CONFIG_GLOBAL` (2.32), and sign with ssh keys (2.34).
