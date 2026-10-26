---
name: wf
description: >-
  Agent-only procedure for running wf workflows (YAML graphs of agent, command,
  human and sub-workflow steps) and supervising them as process-event sources.
  Load before starting a wf run, on any `procevent wf <source-id> <sequence>`
  check wake, and before answering, retrying, continuing or reporting a wf run.
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
With `WF_RUNS_DIR` or `runs_dir` in `~/.wf/config.toml` set (or `--runs-dir <dir>` on every command), the run folders
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
In `wf status`, `progress` says which step the run is at, how many times each loop went back, and a percentage.
The percentage is an estimate: report it as "about".
`wf sessions <run-id> --json` lists the agent sessions that run now, with each one's last tool call and how long it has been quiet.
A session that has been quiet for a long time is worth a look at its transcript before you call the run stuck.
To give the captain a live view, offer `wf watch <run-id>` for a terminal pane, or `wf ui` for a web page on their machine.
`wf watch` follows the run through questions and retries until it succeeds.
`wf ui` prints the page's address with a key in it: give the captain that whole address.
A `running` run whose `agents_waiting` is not empty waits for a free agent slot.
The cap on agent sessions across all runs that use the same `~/.wf` comes from
`max_agents` in `~/.wf/config.toml` or from `WF_MAX_AGENTS`.
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
  redone. A model that keeps failing, for example at a usage limit or in an
  outage, is yours to change for the rest of the run. Run
  `wf retry <run-id> --model <node>=<model> --detach`, with
  `--model '<node>[<item>]=<model>'` for one item. Add `--harness` when the model
  needs another harness, or when wf asks for it. wf checks it first and changes
  nothing when it is wrong. A workflow-file bug, or a task that keeps failing,
  goes to the captain.

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
  `follow_up` is in the state when the run took follow-ups: it is the number of the latest one.

To abandon a run, retire its source with the printed owner token
(`bin/fm-procevent.sh retire wf-<run-id> --if-owner <token>`) and tell the
captain; the run folder stays for inspection.

## Giving a run a follow-up

When the captain asks for more work on what a run delivered, such as a change to its pull request, give the run a follow-up.
Don't start a new run, and don't start a worker of your own.

```sh
wf continue <run-id> "<request>" --detach --json
wf continue <run-id> --file <request.md> --detach --json
```

The run goes on from the node that its workflow's `follow_up` names.
Without `follow_up`, it goes on from the first agent node that can change files, and wf adds the request to its instruction.
It works on the same branches, with the same agent sessions where the harness keeps them.
Its routes count from zero again, so its review loop gets all of its rounds again.
The follow-up has a budget of its own: the limits of the workflow file, or `--limit` values that you give.
Spending more than the file's limits is the captain's call.

The JSON gives `follow_up`, the number of the follow-up.
The source of the earlier pass retired when the run succeeded, so watch the follow-up as a new source on the same run folder:

```sh
bin/fm-procevent.sh register-extension wf wf-<run-id>-f<follow_up> --config-ref "wf-run:<run folder>"
bin/fm-procevent.sh reconcile
```

Each wake then carries `follow_up`, and you handle its states as for any run.

`wf continue` takes only a run that succeeded.
It refuses a run that runs, waits, failed, stopped or is at a limit, and names the command for it: `wf answer` or `wf retry`.
It also refuses a run whose branch is gone.
And it refuses a run whose workflow has no `follow_up` and no agent node with `edit` or `full` access.
When a node of the follow-up works in the project folder itself, the folder must be on the branch that the run worked on.
Then take the request to the captain.

## Steering an agent while it works

When an agent in a run works on the wrong thing, tell it, as you steer one of your own workers.
Keep to the task that the run was given.
A change of task is the captain's call.

```sh
wf steer <run-id> <node> "<message>"
wf steer <run-id> <node> --item <index> --file <message.md>
```

`<node>` is an agent node, as `events.jsonl` names it: `implement`, or `build[1]/implement` inside a sub-workflow.
`--item` names the item of a `for_each` node.
`--file` takes a long message from a file, and `--file -` from stdin.

`wf steer` trusts your user account, as every wf command does: any process that runs as you can steer a run.
That includes an agent that can run commands as you: on Claude Code and Cursor with `edit` or `full` access, and on Codex with `full` access.

`wf steer` says what became of the message:

`delivered`
: The agent reads it at its next step (Codex).

`queued`
: Claude Code gives it to the agent at the agent's next step.
  Cursor gets it as its next turn, when its turn ends.
  A turn often ends only when the agent submits its result, and then the message is dropped.
  A `steer` event in `events.jsonl` says when the message was delivered, or why it was dropped.
  An `unconfirmed` event means that the turn that carried the message failed, or that the attempt ended during that turn.
  The harness may have the message, but wf can't tell whether the agent read it.

`unconfirmed`
: The harness may have the message, but wf can't tell whether the agent read it.
  The attempt ended while the harness took the message, or the harness didn't answer as it took the message (Codex).
  Read the attempt's log before you send it again: the agent may have it already.

nothing was sent (`none` with `--json`)
: Exit code 1.
  The reason says why: the node has no agent session open now, the run isn't running, or 10 messages already wait for the session.
  A message has at most 100,000 characters.

the run is full
: Exit code 1, and nothing was sent.
  100 messages from `wf steer` wait for the run or are being answered.
  Send the message again when some of them have their answers.

A Claude Code agent can take a message that comes mid-turn for tool output, and ignore it.
Read the attempt's log (`attempt-<n>.log`) to see whether the agent acted on the message.

## Talking to the captain about it

Report what the run did and what it needs: the question in plain words, the
failure and your diagnosis, or the result and its branches. Never the event
line, the result path, or the JSON.
