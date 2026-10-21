# wf for firstmate

Lets a [firstmate](https://github.com/kunchenguid/firstmate) first mate start wf
runs, get woken when one needs attention, answer its questions, retry it after
a failure, give it a follow-up request after it succeeded, and report the result.

It has two parts:

- **An extension package** (`process-event-adapter/1`), written by
  `wf integrate firstmate <dir>`. firstmate polls it for each watched run and
  wakes the first mate on every new state: waiting, failed, stopped, limit,
  succeeded. The logic is in `workflow/firstmate.py`; the package is a small
  shim that runs it with this wf's Python.
- **A skill**, `wf/SKILL.md`, that tells the first mate how to start, watch,
  answer, retry and report wf runs.

## Setup

1. Put `wf` on the first mate's PATH, for example
   `uv tool install --editable /path/to/wf`.
2. Write the package to a folder outside any git repository, then bind it in
   your firstmate checkout (firstmate's consent step):

   ```sh
   wf integrate firstmate ~/.local/share/firstmate-packages/wf
   bin/fm-extension.sh bind ~/.local/share/firstmate-packages/wf \
     --adapter wf --trust-same-user-code --consent artifact-references
   bin/fm-extension.sh verify io.github.namikmesic.wf
   ```

3. Copy `wf/SKILL.md` into your firstmate checkout as
   `.agents/skills/wf/SKILL.md`, and add a trigger line to
   `.agents/skills/agent-skill-trigger-index/SKILL.md`:

   ```
   - `wf` - load before starting a wf run, on any `procevent wf <source-id> <sequence>` check wake, and before answering, retrying, continuing or reporting a wf run.
   ```

4. So the first mate can also write and improve workflows, install
   `skills/wf-workflows/` the same way (see [skills/README.md](../../skills/README.md)).

The binding points at a copy firstmate keeps in its home. After changing
`workflow/firstmate.py`, nothing needs rebinding; after `wf integrate` writes a
new package version, bind it again.

The skill is a copy too, so copy `wf/SKILL.md` into your firstmate checkout
again after each wf update. An old copy can tell the first mate to use a
command that the new wf doesn't have.

## What the first mate gets

| Command | Use |
|---|---|
| `wf run <file> ... --detach --json` | start a run in the background |
| `wf answer <run-id> <answers.json> [--detach]` | answer the questions a run waits on |
| `wf retry <run-id> --detach` | continue a failed or stopped run |
| `wf continue <run-id> <request> --detach --json` | give a run that succeeded a follow-up request, on the same branches |
| `wf status <run-id> --json`, `wf list --json` | where runs stand, and in `progress` of status, how far, as an estimate |
| `wf sessions [run-id] --json` | the agent sessions that run now, with each one's last tool call and quiet time |
| `wf watch <run-id>` | the run's steps and tool calls as they come, until it succeeds, for a terminal pane |
| `wf ui [--all-repos]` | a web page on this machine with the graph, progress, metrics, events and transcripts |
| `wf steer <run-id> <node> <message>` | tell an agent something while it works |
| `wf wait <run-id> [--consumer NAME]` | block until a run needs attention (what the adapter polls) |

Only one process drives a run at a time (a lock in the run folder), so the
first mate can't retry a run that is still going.

To keep all runs within what the machine and the accounts hold, set
`max_agents = <n>` in `~/.wf/config.toml` (or `WF_MAX_AGENTS`).
Agent attempts in every run that uses that `~/.wf` then share that many sessions.
An attempt waits for a free one, and `wf status` lists the waits (see "Agent
slots" in the spec).

firstmate's own AGENTS.md tells each agent that loads it that it is the first mate.
So a workflow that works on the firstmate repository sets `project_instructions: false` in its `defaults`.
A sub-workflow uses its own `defaults`, so each sub-workflow that the run uses sets it too.
Then its agents don't get that file, and only the workflow's instructions tell them what they are.
See "Instruction files" in the spec for what each harness still loads.

firstmate gives its built-in answers channel only to its own adapters, so the
first mate answers wf questions itself with `wf answer`, under its usual
ask-user rules.
