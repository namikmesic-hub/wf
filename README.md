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
- any run can pause for a person and be answered and resumed later, from a
  terminal or by an autonomous operator.

One cap on live agent sessions holds across all your runs that share a `~/.wf`.

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
```

`wf run <file> --dry-run <script.yaml>` walks a workflow with scripted agent
results, for free. `wf --help` lists every command.

## Docs

- [docs/workflow-spec.md](docs/workflow-spec.md): the file format, and how runs behave.
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

## Development

```sh
uv run pytest                  # fake agents, no cost
uv run pytest -m harness       # real agents: uses your Claude Code, Codex and Cursor quota
```

The tests need git 2.32 or later, which reads `GIT_CONFIG_GLOBAL`: they use it to hide your own git config.
