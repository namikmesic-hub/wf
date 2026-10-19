"""wf: run YAML workflows of coding-agent steps on LangGraph.

A workflow file (docs/workflow-spec.md; examples in examples/) describes a graph of steps: tasks for
Claude Code, Codex or Cursor, shell commands, questions for a person, and other
workflows. `wf validate` checks a file, `wf graph` draws it, `wf run` runs it.

Read the modules in this order. The first group knows nothing about LangGraph:
    spec.py        the file format, as Pydantic models
    schemas.py     field maps -> JSON Schema, and checking values against them
    templates.py   sandboxed Jinja, plus reading templates without running them
    scheduler.py   the graph, and the rules for which node runs next
    validate.py    every check in the spec's Validation section
    diagram.py     `wf graph`: the workflow as a Mermaid flowchart

Then the runner, built on LangGraph:
    state.py       what the graph remembers (RunState, the inbox reducer) and
                   the bookkeeping the schedule node does with it
    context.py     what nodes get that isn't saved (LangGraph's runtime context)
    limits.py      the run's limits: reaching one pauses it, `wf resume --limit` continues it
    settings.py    wf's own settings for the user, in ~/.wf/config.yaml
    capacity.py    the cap on live agent sessions across all runs that share a ~/.wf (WF_MAX_AGENTS)
    runs.py        the run folder on disk
    workspaces.py  git worktrees for `workspace: new`, and the runner's commits
    output_tool.py submit_result: how an agent hands back its typed result
    tools/         wf tools (Slack, Linear, ...): sign-in, the Keychain, and the
                   relay that attaches them to agent sessions
    harnesses/     the coding-agent adapters: base.py, then claude_code.py,
                   codex.py, cursor.py (with mcp_http.py, which serves it
                   submit_result), and git_tool.py (read-only git without a shell)
    nodes.py       running one job: agents, commands, human questions via interrupt()
    graph.py       the LangGraph wiring: schedule -> Send -> execute -> schedule
    ask.py         questions at the terminal
    runner.py      wf run / wf resume: streaming, interrupts, Command(resume=...)
    cli.py         the `wf` command

Then the tools around runs, none of which LangGraph knows about:
    operate.py     running wf without a terminal: detach, wait, answer
    firstmate.py   the firstmate extension: wf runs as firstmate event sources
    dryrun.py      dry runs: scripts of agent results, answers and command results,
                   played by harnesses/scripted.py
    report.py      `wf report`: what runs did, per node and route
    examples/      two complete workflows (`wf examples` copies them out)
"""
