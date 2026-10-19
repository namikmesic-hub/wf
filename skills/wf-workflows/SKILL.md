---
name: wf-workflows
description: >-
  Build, change and improve wf workflows: YAML graphs of agent, command, human
  and sub-workflow steps that `wf` runs. Use when creating a workflow or
  automating a multi-step agent process with wf, when editing or customizing a
  workflow file, and when improving a workflow from how its runs went.
---

# wf-workflows

A workflow is a graph. Each node does one step (an agent task, a shell command,
a question for a person, or another workflow) and declares its output as a
schema: that schema is the node's **contract** with every step that reads it.
Routes choose the path through the graph. Three tools keep a workflow honest,
and every path below ends on them:

- `wf validate <file>` checks wiring before anything runs: names, field paths
  through every contract, loops without `max`, risky shell quoting.
- `wf run <file> --dry-run <script.yaml>` walks one path for free. Agent
  results, answers and the commands you list come from the script; routes,
  loops, templates and end output really run. **Commands the script doesn't
  list really run**: wf names them before it starts.
- `wf report` turns runs into **evidence**: passes, retries, failures, routes
  taken, loops that ran out, where the tokens and dollars went.

Load [reference.md](reference.md) before writing or editing YAML. `wf examples`
copies two complete workflows into ./wf-examples/ to read or start from;
`wf --help` and `wf <command> --help` list the flags.

## Build a workflow

1. **Pin the job.** Name the inputs (start.input), the result (end.output), and
   for each step who should do it: a command for anything deterministic (tests,
   build, lint), an agent for judgement, a person for decisions the person
   running the workflow should own. Done when every start.input and end.output
   field has a name and type.
2. **Pick a shape** from [patterns.md](patterns.md). Done when every step is a
   node with a kind, and every loop has its exit route and `max`.
3. **Write the file.** Start from `wf init <name>` or an example. For each node
   write its contract first (`output`, every field with a `description`, since
   agents read those), then its instruction, which says what to do and which
   earlier fields to use. Set `limits` (`tokens`, and `cost_usd` for Claude
   Code) on any workflow with loops or fan-out. Done when each instruction reads
   only fields some contract declares.
4. **Validate.** `wf validate <file>` until it prints no errors. Read every
   warning: fix it, or know why it stands. Then `wf graph <file>` and check the
   shape is the one you meant. Done when no errors remain and each warning has a
   reason.
5. **Dry-run every path.** Write one script per path into `<name>.dry/` next to
   the workflow, with the inputs that path needs: the straight path; each loop
   going round once, and running out its `max`; each branch a failure takes
   (tests failing, a person saying no); each retry or fallback the workflow
   relies on. List every command that shouldn't really run. Format:
   [reference.md](reference.md#dry-runs). Clear earlier dry runs (`wf discard
   --workflow <name> --dry-runs`), run every script, then `wf report --workflow
   <name> --dry-runs --json`; a sub-workflow's routes show under its node's
   path (`build[]/review`). Done when every route shows `taken` > 0 and every
   dry run ended the way its path should.
6. **One real run** on a small input, then `wf report <run-id>`. Done when it
   succeeded, or its failure is understood and fixed.

## Change a workflow

1. Read the file, `wf graph <file>`, and `wf report --workflow <name> --real` if
   it has runs: the change should fit how it actually behaves.
2. Make the change. Changing a contract changes every template that reads it,
   so let `wf validate` list them, and update each.
3. Validate, update the scripts in `<name>.dry/` for the paths the change
   touches, and dry-run them. Done when validation shows no errors, each warning
   has a reason, and those paths end as intended.

## Improve a workflow

Load [improving.md](improving.md): it maps each symptom in a report to its
likely causes and the change that fixes each.

1. **Gather evidence.** `wf report --workflow <name> --real --json` over its
   recent runs (`--version` keeps one version of the file). For each node the
   notes name, read its attempt logs
   (`<run folder>/nodes/<node>/<pass>/attempt-<n>.log`; `wf status <run-id> --json`
   gives the run folder as `dir`). Done when every
   note in the report has a cause written next to it.
2. **Change one thing per cause**, the smallest change that removes it.
3. **Prove it.** Validate, dry-run the paths you touched, and after the next
   real runs compare their report (`--version <new>`) with the one you started
   from. Done when the note that prompted the change is gone, or you know why it
   remains.
