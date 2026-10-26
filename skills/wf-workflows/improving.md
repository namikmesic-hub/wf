# Improving a workflow from evidence

## Reading a report

`wf report --workflow <name> --real --json` sums every real run of the
workflow in this folder (`wf report <run-id> ...` for chosen runs; `--version V`
for runs of one version of the files). Items of a for_each node and nodes
inside sub-workflows are counted together, e.g. `build[]/implement`.

- `runs[]`: each run's status, duration, tokens, cost, whether it was a dry run,
  and `version`, a hash of the workflow files it ran. Judge cost and reliability
  on real runs of one version: dry runs' failures and spending are scripted.
- `nodes.<name>`: `runs` reached, `passes`, `attempts` and `failed_attempts`,
  `failures` (the commonest errors), `unavailable` (by harness), `items` and
  `failed_items`, `tokens`, `cost_usd`, `duration_s`, `sessions`.
- `routes[]`: every route in file order with `taken` and `max_reached` (runs in
  which it used up its `max`). A route with `taken: 0` across many runs is a
  branch that never happens.
- `notes[]`: the facts most worth acting on. Each one needs a cause.

Then read the evidence behind each note, in the run folder
`.wf/runs/<run-id>/` (or `<runs-dir>/<repo key>/<run-id>/` with a runs folder, `wf config show` names it):

- `nodes/<node>/<pass>/[<item>/]instruction.md`: exactly what the step was told.
- `attempt-<n>.log`: an agent's transcript (JSON lines: `sent`, `text`, `tool`,
  `tool_result`, `result`) or a command's output.
- `output.json` per pass, and `events.jsonl` for the order of everything.
- `trace.jsonl`: each step of the run and why it happened, which `wf trace` reads (next section).

## Reading the trace

`wf report` says what happened across runs.
`wf trace <run-id>` says why it happened in one run, from its `trace.jsonl`.
Add `--json` to get the answer as data, with long values read back in full.

- `wf trace <run-id> --var <path>` shows the value of a variable in each render that read it, pass after pass.
  Use it to see if feedback reached the next pass: `--var review.blocking_issues` must show the issues in the `instruction` of `implement` on pass 2.
- `wf trace <run-id> --field instruction --node <node>` shows the template once, then for each pass the values that it read and the text that it gave.
  `command`, `env`, `with`, `harness`, `model`, `effort`, `for_each` and `end` work the same way.
- `wf trace <run-id> --node <node> --field routes` shows why the node went where it went.
  You see each route with its `when`, the values that it read, its result, and if its `max` was used up.
- `wf trace <run-id> --node <node> --pass <n>` shows one pass in full.
  You see each attempt with its harness, model, effort, access and session, each turn and reminder, and each tool call with its arguments and answer.
  You also see each `submit_result` call with the problems that wf found in it.

A template error shows in its render, with the values that the template read before it failed.

## Symptoms, causes, changes

**A loop reached its max.** The exit condition didn't hold in time.
- The author never saw the feedback: check its `instruction.md` on pass 2+ for
  the reviewer's issues and test output; add them, plus `history.<node>` notes.
  `wf trace <run-id> --var review.blocking_issues` shows which passes read them.
- The reviewer moves the goalposts: ask it to check that earlier issues were
  fixed and to list only blocking problems (`history.review | tojson`).
- Tests fail for reasons the author can't fix (environment, flaky tests): fix
  the environment or make that check a separate route.
- Raise `max` only after the loop converges on most runs.

**Retries with "never made a valid submit_result call".** The agent finished
without submitting, or kept failing the contract.
- Look for `submit_result` errors: `wf trace <run-id> --node <node>` lists each call with the problems wf found.
  A field it keeps getting wrong needs a clearer `description`, an `enum`, or a simpler type.
- A contract with many fields or deep nesting: split the node, or flatten.
- The task is too big for one turn budget: split it, or raise `max_turns`.

**The same failure on every attempt of a command.** Deterministic: retries
can't help. Fix the command or its environment; give the node `retry: 0`.

**A harness is often unavailable.** Usage limits. Give the node a fallback list
(`harness: [claude-code, codex]` with `model`/`effort` maps), or spread heavy
nodes across harnesses.

**One node spends most of the cost or tokens.** Lower its `effort` or model, cut
what it's asked to read (send the plan and file list, not the whole context),
move deterministic parts into command nodes, or cap it with `budget_usd` /
`max_turns`. Set `limits.tokens` on any workflow with loops or fan-out.

**Many failed items.** Look for a shared cause in `errors.<node>` and the item
logs: an ambiguous per-item instruction, items that conflict (shared files,
lower `max_parallel`), or items too big (ask the planner for smaller ones).
`on_item_error: continue` keeps a run useful while you fix it.

**A person answers the same way every time.** Replace the human node with a
route condition, or move it behind a route so it's asked only when it matters.

**Runs fail early at the same node.** Read its failure in `status.json` and the
attempt log; a template error (reading a field of a node that didn't run) means
a missing guard: validate's "might not have run" warnings point at them.
`wf trace <run-id> --node <node>` shows the render that failed, with the values it read.

**A route is never taken.** Its condition can't become true, or the case never
comes up: dry-run it to see which; delete it if the case is truly gone.
`wf trace <run-id> --field routes` shows the values each `when` read on every pass.

## The loop

Change one thing per cause, validate, dry-run the paths you touched, then let
the next real runs come in and compare `wf report` before and after. Keep the
dry-run scripts next to the workflow: they are its tests.
