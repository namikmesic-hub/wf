---
name: wf
description: >-
  Agent-only procedure for running wf workflows (YAML graphs of agent, command,
  human and sub-workflow steps) and supervising them as process-event sources.
  Load before starting a wf run, on any `procevent wf <source-id> <sequence>`
  check wake, and before answering, retrying or reporting a wf run.
user-invocable: false
---

# wf

wf runs a workflow file: a graph of steps.
In it, agents (Claude Code, Codex, Cursor) do the work in their own git worktrees, commands test it, and people answer questions.
It is deterministic about order, retries, loops and limits.
You supervise it, and you don't drive each step.

A wf run is a background process you watch as a process-event source, through
the `wf` extension adapter (bound with `bin/fm-extension.sh bind`). Load
`process-event-sources` for the general source rules; this skill adds what is
specific to wf. Never run `wf wait` yourself in a conversational turn: the
source does the waiting. Writing, changing or improving a workflow file is the
`wf-workflows` skill's job: load it before touching one.

## Starting a run

Work in the project folder the workflow belongs to: wf keeps runs in that
folder's `.wf/runs/`, and every later `wf` command for the run must run there.
With `WF_RUNS_DIR` set (or `--runs-dir <dir>` on every command), the run folders
go outside the project instead, to `<dir>/<repo key>/`. Then the project's
working tree and `.git/info/exclude` stay as they were: only `workspace: new`
adds its branches and worktree records under `.git`. Use that for a clone you
must not write into. The commands wf prints for the run include `--runs-dir`.

1. `wf validate <workflow.yaml>`. Fix errors before going on; read warnings.
2. `wf run <workflow.yaml> name=value ... --detach --json`. It prints
   `{"run": "<run-id>", "dir": "<run folder>", "pid": ...}` and returns at once.
   Missing inputs are an error, never a question: pass them all.
3. Watch it:

   ```sh
   bin/fm-procevent.sh register-extension wf wf-<run-id> --config-ref "wf-run:<run folder>"
   bin/fm-procevent.sh reconcile
   ```

   Keep the owner token `register-extension` prints.

`wf status <run-id> --json` and `wf list --json` show where runs stand at any time.
A `running` run whose `agents_waiting` is not empty waits for a free agent slot.
The cap on agent sessions across all runs that use the same `~/.wf` comes from
`max_agents` in `~/.wf/config.yaml` or from `WF_MAX_AGENTS`.
That is not a stall.
The attempt starts, with its timeout and retries intact, once a slot comes free
and it is the first to take it.
The order among waiting attempts is not defined.
A run limit still ends the wait.

## On a `procevent wf <source-id> <sequence>` wake

The captured result is the run's state as JSON. Act on its `status`, then
acknowledge it with `bin/fm-procevent.sh handled <source-id> <sequence>`.

`waiting`
: `questions` lists what the run waits on: `{key, node, message, schema}`.
  Load `ask-user-authority` and decide each one. Answer yourself only what is
  within your authority, and say why; take the rest to the captain.
  Write the answers as one JSON object, `{"<key>": {<field>: <value>, ...}}`, one
  entry per question, each matching its `schema`, then
  `wf answer <run-id> <answers.json> --detach`. wf checks the answers first and
  changes nothing if any is missing or wrong; read its error and fix the file.

`failed`
: `node` and `error` say what failed. The evidence is in the run folder:
  `nodes/<node>/<pass>/[<item>/]attempt-<n>.log` (agent transcript or command
  output), `instruction.md`, and `events.jsonl`.
  `wf trace <run-id> --node <node>` shows what each template read, and each attempt, tool call and `submit_result` call.
  Load `diagnostic-reasoning`.
  An environment problem (missing tool, dependency, credentials) is yours to
  fix, then `wf retry <run-id> --detach`; finished steps and items are not
  redone. A workflow-file bug, or a task that keeps failing, goes to the captain.

`stopped`
: No wf process is driving the run any more (a crash, a reboot). Run
  `wf retry <run-id> --detach`; it carries on from the last checkpoint.

`limit`
: The run hit one of its limits (`reason` says which) and is paused. Spending
  more is the captain's call: tell them what it spent and how far it got
  (`wf report <run-id>`), and ask whether to continue and with what budget. If
  they say yes, run `wf retry <run-id> --limit cost_usd=<their number> --detach`
  (or `tokens=`, `duration=`, as the reason says); the source keeps watching. If
  they say no, leave the run paused and retire the source yourself.

`succeeded`
: `output` is the workflow's end output. Code changes are on branches named
  `wf/<run-id>/...`, or on the branch a node's `branch` names (a workflow can
  take it as an input, such as `branch=fm/<task>`). Their worktrees are already
  removed. wf never merges or pushes unless the workflow does. Hand any branch
  to the project's normal ship path. The source retires itself.

To abandon a run, retire its source with the printed owner token
(`bin/fm-procevent.sh retire wf-<run-id> --if-owner <token>`) and tell the
captain; the run folder stays for inspection.

## Talking to the captain about it

Report what the run did and what it needs: the question in plain words, the
failure and your diagnosis, or the result and its branches. Never the event
line, the result path, or the JSON.
