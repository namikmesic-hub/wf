"""Graph: the LangGraph wiring that runs a workflow.

It isn't one LangGraph node per YAML node. Whether a node can start ("all of its
needs are settled") depends on what is pending anywhere in the run, and static
edges can't express that: add_edge(["a", "b"], "c") waits for both a and b, so
it would wait forever on a branch that a route skipped. Instead the graph has
four nodes, and the workflow's own graph lives in the state:

    START → schedule ── Send(job) × N ──→ execute ──→ schedule ──→ … ──→ finish → END
                └── a node failed, or a limit ──→ wait_for_resume ── wf resume ──→ schedule

- schedule records the results in the inbox, then starts every pending node
  whose needs are settled (state.Progress does the bookkeeping). It returns
  Command(goto=[Send("execute", job), ...]): a Send is "run this node with this
  input", and LangGraph runs all the Sends of a step in parallel. When nothing
  is pending or running, it goes to finish.
- execute runs one job (nodes.run_job) and appends the result to the inbox.
- wait_for_resume: a failed run, or one that reached a limit, parks here.
  interrupt() saves the run and stops the graph; `wf resume` answers it, and
  schedule reruns what failed or what the limit stopped (limits.py), unless a
  limit is still reached. (A failed sub-workflow doesn't park: it ends as
  failed, and its workflow node's job fails, so the parent can retry it or,
  with on_item_error: continue, go on without it. One that reached a limit
  does park, so that when the run continues, it continues too, from where it
  stopped: see nodes._run_sub.)
- finish computes end.output.

LangGraph runs in steps ("supersteps"): every task started in a step finishes
before the next step begins. So schedule runs once per step, after all of that
step's jobs, with all their results in the inbox. The cost: a fast branch
waits for a slow parallel one before its next node starts.

Checkpoints: after each step, LangGraph saves the state with the checkpointer
(an AsyncSqliteSaver in the run folder). It also saves the result of each task
that finished inside a step, so after a crash or a human-node pause, a resumed
run only reruns the tasks that hadn't finished.

Runtime: every node can take `runtime: Runtime[RunContext]`. runtime.context
is the RunContext (context.py); runtime.stream_writer sends progress events to
whoever streams the graph with stream_mode="custom". The CLI prints them and
adds them to events.jsonl.

The compiled graph is the same for every workflow: the workflow arrives in
the context, not in the wiring.
"""

from langgraph.graph import END as GRAPH_END
from langgraph.graph import START as GRAPH_START
from langgraph.graph import StateGraph
from langgraph.runtime import Runtime
from langgraph.types import Command, Overwrite, Send, interrupt

from . import nodes, schemas, templates
from .context import RunContext
from .state import Job, Progress, RunState


async def schedule(state: RunState, runtime: Runtime[RunContext]) -> Command:
    ctx = runtime.context
    progress = Progress(state, ctx.workflow, ctx.topo, runtime.stream_writer, ctx.root.usage if ctx.nested else None)
    if "run" not in state:
        progress.begin(state["input"], ctx.run_info)
    paused = state.get("status") == "limit"
    for finished in state.get("inbox", []):
        progress.record(finished)

    elapsed = ctx.elapsed()
    progress.s["elapsed_s"] = elapsed
    if not ctx.nested:  # execute tasks check limits against this (a sub-workflow adds to it)
        ctx.usage = dict(progress.s["usage"])
    name, reason = ctx.limit_check() or (None, None)  # e.g. ("tokens", "used 5,000 tokens, limit 1,000")
    jobs = []
    if state.get("retry"):  # `wf resume` on a failed or paused run, unless a limit is still reached
        progress.s["retry"] = False
        if reason:
            progress.stop_for_limit(reason, name)
        else:
            jobs = progress.retry()
    elif progress.s["status"] == "limit" and not paused:  # a job just stopped at a limit
        progress.stop_for_limit(reason or progress.s["limit_reason"], name)
    elif reason and not progress.stopped:
        progress.stop_for_limit(reason, name)
    jobs += progress.starts_now(elapsed)

    update = {**progress.update(), "inbox": Overwrite([])}  # Overwrite: empty the inbox, bypassing its reducer
    if jobs:  # even after a failure, jobs already counted as running get to finish
        return Command(update=update, goto=[Send("execute", job) for job in jobs])
    if progress.s["status"] == "limit" or (progress.s["failure"] is not None and not ctx.nested):
        return Command(update=update, goto="wait_for_resume")
    return Command(update=update, goto="finish")


async def execute(job: Job, runtime: Runtime[RunContext]) -> dict:
    ctx = runtime.context
    async with ctx.slots(job["node"]):
        finished = await nodes.run_job(job, ctx, runtime.stream_writer)
    return {"inbox": [finished]}


async def wait_for_resume(state: RunState) -> Command:
    # The first time through, interrupt() raises and the graph stops here. When
    # `wf resume` sends Command(resume=...), this node runs again and interrupt()
    # returns instead, so the run goes back to schedule to retry.
    if state.get("status") == "limit":
        interrupt({"kind": "limit", "reason": state.get("limit_reason"), "limit": state.get("limit_name")})
    else:
        interrupt({"kind": "failed", **state["failure"]})
    return Command(update={"retry": True}, goto="schedule")


async def finish(state: RunState, runtime: Runtime[RunContext]) -> dict:
    ctx = runtime.context
    if state.get("failure"):  # only a sub-workflow gets here failed (see schedule)
        return {"status": "failed"}
    workflow = ctx.workflow
    variables = Progress(state, workflow, ctx.topo, runtime.stream_writer, ctx.root.usage if ctx.nested else None).vars()
    output, problems = {}, []
    for name, value in workflow.end.output.items():
        try:
            if isinstance(value, str):
                output[name] = templates.render_value(value, variables)
                continue
            raw = value.get("value")
            output[name] = templates.render_value(raw, variables) if isinstance(raw, str) else raw
            field = {k: v for k, v in value.items() if k != "value"}
            schema = schemas.object_schema({name: field}, workflow.types)["properties"][name]
            problems += [f"{name}: {p}" for p in schemas.check(output[name], schema)]
        except templates.TemplateError as error:
            problems.append(f"{name}: {error}")
    if problems:
        error = "end.output: " + "; ".join(problems)
        runtime.stream_writer({"event": "run_finished", "status": "failed", "error": error})
        return {"status": "failed", "failure": {"node": "end", "error": error}}
    runtime.stream_writer({"event": "run_finished", "status": "succeeded"})
    return {"status": "succeeded", "output": output}


def build(checkpointer=None):
    """The compiled graph. `checkpointer` saves state after every step (wf run uses SQLite)."""
    builder = StateGraph(RunState, context_schema=RunContext)
    # `destinations` only tells LangGraph (and its drawings) where a Command can go
    builder.add_node("schedule", schedule, destinations=("execute", "wait_for_resume", "finish"))
    builder.add_node("execute", execute)
    builder.add_node("wait_for_resume", wait_for_resume, destinations=("schedule",))
    builder.add_node("finish", finish)
    builder.add_edge(GRAPH_START, "schedule")
    builder.add_edge("execute", "schedule")
    builder.add_edge("finish", GRAPH_END)
    return builder.compile(checkpointer=checkpointer)
