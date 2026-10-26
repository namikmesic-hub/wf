"""Runner: start a run or continue one, and drive the graph until it stops.

    start(file, pairs, input_file, no_input) -> exit code     (wf run)
    retry(run_id, no_input) -> exit code                      (wf retry, wf answer)

Driving a run alternates between two things:
1. Streaming the graph (_stream, inside asyncio.run): graph.astream() with
   stream_mode="custom". Each chunk is a progress event a node sent with
   runtime.stream_writer: print it, add it to events.jsonl. When the stream
   ends, graph.aget_state() says why: state.interrupts lists what the graph
   is waiting for, and no interrupts means it reached END. While it streams,
   `wf steer` messages reach the agent sessions that are open (steer.py).
2. Deciding what's next, outside asyncio:
   - Human nodes: at a terminal, ask now and stream again with
     Command(resume={interrupt id: answers}). That's how LangGraph continues
     after interrupt(): the node runs again, and interrupt() returns the value
     sent for its id. Without a terminal (or with --no-input), the run is left
     `waiting` and `wf retry` asks later. Exit code 2.
   - A failure (graph.wait_for_resume): the run is `failed`. `wf retry`
     continues it with Command(resume=...), and the failed jobs run again.
   - A limit (graph.wait_for_resume too): the run is `limit`, paused. `wf retry
     --limit NAME=VALUE` keeps the bigger limit in run.json and continues it the
     same way (limits.py); without one big enough, it says so and stays put.
   - `wf retry --model NODE=MODEL` (with --harness and --effort) gives a node
     or one item another model for the rest of the run (overrides.py). It keeps
     the override in run.json only when all of the checks pass.
   - END: write output.json, or record why the run failed or hit a limit.

Questions are asked between streams, not inside the event loop, so Ctrl-C
and Ctrl-D at a prompt behave as they do in any terminal program: the run is
left `waiting`, and `wf retry` asks again.

Every stream opens the same SQLite checkpointer and uses the run id as
LangGraph's thread_id, so a later `wf retry` process finds the run exactly
where this one left it.

The run's trace (trace.py) gets a `run` record just before a process drives the
run. It gets a `run_stopped` record with the run's status when the process
stops, so each `run` record has its `run_stopped` record.
"""

import asyncio
import contextlib
import json
import os
import shutil
import signal
import sys
import threading
from collections.abc import Iterator
from pathlib import Path

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command, StateSnapshot

from . import ask, capacity, config, dryrun, graph, limits, operate, overrides, runs, scheduler, schemas, spec, steer, validate, workspaces
from .context import RunContext
from .harnesses.base import short

RECURSION_LIMIT = 10_000  # LangGraph's cap on steps; each pass of a node takes two
STOPPED = 130  # exit code after Ctrl-C, as shells report it


def interactive() -> bool:
    """Is there a person at a terminal to answer questions?"""
    return sys.stdin.isatty() and sys.stdout.isatty()


class InputError(Exception):
    pass


def start(path: Path, pairs: list[str], input_file: Path | None, no_input: bool,
          detach: bool = False, as_json: bool = False, dry_run: Path | None = None,
          limit_pairs: list[str] | None = None, runs_dir: Path | None = None) -> int:
    try:
        overrides = limits.parse(limit_pairs or [])
    except limits.LimitError as error:
        print(f"wf run: --limit {error}", file=sys.stderr)
        return 1
    try:
        max_agents = capacity.configured()  # a bad cap stops a run before it exists, a detached one too
    except config.ConfigError as error:
        print(f"wf run: {error}", file=sys.stderr)
        return 1
    report = validate.check(path)
    for problem in report.problems:
        print(problem, file=sys.stderr if as_json else sys.stdout)
    if report.errors:
        return 1
    workflow = report.workflow
    workflows = [spec.load(f) for f in report.files]
    script = None
    if dry_run is not None:  # check the whole script against every workflow file before anything runs
        try:
            script = dryrun.Script.load(dry_run, workflows)
        except dryrun.ScriptError as error:
            print(f"wf run: the dry-run script has problems:\n{error}", file=sys.stderr)
            return 1
    _note_uncommitted(workflows, [*report.files, *([dry_run] if dry_run else [])])
    repo = Path.cwd()
    home = _home(repo)  # once: run.json and the run itself use the same branch and commit
    if problem := _signing_problem(workflows, home):
        print(f"wf run: {problem}", file=sys.stderr)
        return 1
    asking = interactive() and not no_input and not detach
    try:
        inputs = collect_inputs(workflow, pairs, input_file, asking, base=script.inputs if script else None)
    except InputError as error:
        print(f"wf run: {error}", file=sys.stderr)
        return 1
    try:
        run = runs.create(repo, workflow.name, report.files, path, inputs, home=home, dry_run=script is not None,
                          limits=overrides, runs_dir=runs_dir)
    except OSError as error:  # e.g. --runs-dir names a file
        print(f"wf run: can't make the run folder in {runs.runs_folder(repo, runs_dir)}: {error.strerror or error}",
              file=sys.stderr)
        return 1
    if script is not None:
        script.keep(run.dir / "dry-run")
    if detach:  # a background `wf retry` starts it, with the lock this process takes now
        with run.driving() as lock:
            pid = operate.detach(run, ["retry", run.id, "--no-input"], lock)
        _say({"run": run.id, "dir": str(run.dir), "pid": pid}, f"Run {run.id} started in the background. "
             f"Follow it with: {run.command('wait')}", as_json)
        return 0
    print(f"Run {run.id}" + (f": {workflow.description}" if workflow.description else ""))
    if script is not None:
        print(f"Dry run: agents, answers and the commands it lists come from {dry_run}")
        if real := script.unscripted_commands(workflows):
            print(f"Dry run: these commands aren't in the script, so they really run if they're reached: {', '.join(real)}")
    try:
        with run.driving():
            ctx = RunContext(workflow, run, home, script=script, limits=limits.effective(workflow.limits, overrides),
                             max_agents=max_agents)
            ctx.trace.run("started", workflow=workflow.name, inputs=inputs, dry_run=script is not None)
            return _drive(ctx, {"input": inputs}, asking)
    except runs.RunBusy as error:
        print(f"wf run: {error}", file=sys.stderr)
        return 1


def retry(run_id: str, no_input: bool, answers_file: Path | None = None, detach: bool = False,
          as_json: bool = False, limit_pairs: list[str] | None = None, runs_dir: Path | None = None,
          override_pairs: dict[str, list[str]] | None = None) -> int:
    """Continue a run: retry what failed, answer what's waiting (from `answers_file`, or by
    asking at a terminal), or carry on after a crash. With `detach`, check everything here,
    then let a background process do it. `limit_pairs` (--limit) set new limits for the
    rest of the run, e.g. to continue one a limit stopped. `override_pairs` (--model,
    --harness and --effort, by setting) give a node or an item another model.

    The run's lock is taken before anything is checked, so two operators answering
    at once can't both answer the same questions: the second gets RunBusy."""
    name = "wf answer" if answers_file else "wf retry"
    try:
        run = runs.find(Path.cwd(), run_id, runs_dir)
        # The copies, as they were when the run started: the workflow and its sub-workflows
        workflows = [spec.load(f) for f in validate.check(run.workflow_file).files]
        answers = json.loads(Path(answers_file).read_text(encoding="utf-8")) if answers_file else None
        new_limits = limits.parse(limit_pairs or [])
        new_models = overrides.parse(override_pairs or {})
        max_agents = capacity.configured()
    except (OSError, spec.SpecError, ValueError, limits.LimitError, overrides.OverrideError,
            config.ConfigError) as error:  # ValueError: not JSON, or not UTF-8
        print(f"{name}: {'--limit ' if isinstance(error, limits.LimitError) else ''}{error}", file=sys.stderr)
        return 1
    try:
        with run.driving() as lock:
            return _checked_retry(run, workflows, name, answers, no_input, detach, as_json, lock, new_limits,
                                  new_models, max_agents)
    except runs.RunBusy as error:
        print(f"{name}: {error}", file=sys.stderr)
        return 1


def _checked_retry(run: runs.Run, workflows: list[spec.Workflow], name: str, answers: dict | None, no_input: bool,
                   detach: bool, as_json: bool, lock: runs.Lock, new_limits: dict, new_models: dict,
                   max_agents: int | None) -> int:
    workflow = workflows[0]
    ctx = RunContext(workflow, run, home={}, max_agents=max_agents)
    asking = interactive() and not no_input and not detach
    if run.info().get("dry_run"):  # a dry run continues with the rest of its script
        try:
            ctx.script = dryrun.Script.resume(run.dir / "dry-run")
        except dryrun.ScriptError as error:
            print(f"{name}: {error}", file=sys.stderr)
            return 1
    state = asyncio.run(_snapshot(ctx))
    ctx.limits = limits.effective(workflow.limits, {**_given_limits(run, state.values or {}), **new_limits})
    if any(i.value.get("kind") == "limit" for i in state.interrupts):  # paused at a limit: is there room now?
        still = limits.check(ctx.limits, *limits.spent(state.values))
        if still:
            print(f"{name}: run {run.id} is still over a limit ({still[1]}). "
                  f"Continue it with a bigger one: {run.command('retry', limits.hint(still[0]))}", file=sys.stderr)
            return 1
    checked = _checked_overrides(run, state, workflows, new_models)
    problems = checked.problems
    if answers is not None:
        asked = operate.questions(state.interrupts) if state.values else []
        problems += operate.check_answers(asked, answers) if asked else [f"run {run.id} isn't waiting for answers"]
    if problems:
        return _refused(name, problems)
    if not _ended(run, state) and (problem := _signing_problem(workflows, _run_home(run, state))):
        print(f"{name}: {problem}", file=sys.stderr)
        if run.status().get("status") == "running":  # a background driver: tell `wf wait` why it stopped
            run.set_status("failed", error=problem)
        return 1
    if checked.found and ctx.script is None and (problems := asyncio.run(overrides.available(checked))):
        return _refused(name, problems)  # the last check, as it opens harnesses (a dry run's script plays them all)
    try:  # every check passed: only now does anything change
        _keep(run, state.values or {}, checked.found, new_limits)
    except OSError as error:
        print(f"{name}: can't keep the new settings in {run.dir}, so the run stays as it was: "
              f"{error.strerror or error}", file=sys.stderr)
        return 1
    out = sys.stderr if as_json else sys.stdout
    if new_limits:
        print(f"Limits for the rest of this run: {limits.describe(ctx.limits)}", file=out)
    for key, override in checked.found.items():
        print(f"Override for the rest of this run: {overrides.describe(key, override)}", file=out)
    ctx.overrides = run.overrides()
    if detach:
        args = ["retry", run.id, "--no-input"]
        if answers is not None:  # keep the answers with the run, for the background process
            kept = run.dir / "answers" / f"{run.status().get('seq', 0)}.json"  # one per state: we hold the lock
            kept.parent.mkdir(exist_ok=True)
            kept.write_text(json.dumps(answers, indent=2), encoding="utf-8")
            args += ["--answers", str(kept)]
        pid = operate.detach(run, args, lock)
        _say({"run": run.id, "pid": pid}, f"Run {run.id} continues in the background. Follow it with: {run.command('wait')}",
             as_json)
        return 0
    return _retry(ctx, state, asking, answers)


def _retry(ctx: RunContext, state: StateSnapshot, asking: bool, answers: dict | None) -> int:
    run = ctx.run
    if not state.values:  # created but never started (a detached start, or a crash before the first step)
        if (run.dir / "output.json").exists():
            print(f"Run {run.id} already succeeded.")
            return 0
        ctx.home = _run_home(run, state)
        try:
            inputs = json.loads((run.dir / "input.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:  # a run killed while it was being created
            print(f"wf retry: can't read the run's input: {error}", file=sys.stderr)
            return 1
        print(f"Starting run {run.id}")
        ctx.trace.run("started", workflow=ctx.workflow.name, inputs=inputs, dry_run=ctx.script is not None)
        return _drive(ctx, {"input": inputs}, asking)
    ctx.home = _run_home(run, state)
    print(_retrying(run, state.values, answers))
    if state.interrupts:
        command = _continue(state.interrupts, asking, run, answers, ctx.script)
        if isinstance(command, int):
            return command
    elif state.next:
        command = None  # it stopped mid-step: carry on from the last checkpoint
    else:
        status = state.values.get("status")
        if run.status().get("status") != status:  # it reached the end, but stopped before recording it
            return _finished(ctx, state.values)
        print(f"Run {run.id} already {status}.{_follow_up_hint(ctx, status)}")
        return 0 if status == "succeeded" else 1
    ctx.trace.run(**_run_record(ctx, state.values, answers))
    return _drive(ctx, command, asking)


def _checked_overrides(run: runs.Run, state: StateSnapshot, workflows: list[spec.Workflow],
                       given: dict) -> overrides.Checked:
    """The overrides given to `wf retry`, checked against the run, and what's wrong with them."""
    if not given:
        return overrides.Checked()
    if _ended(run, state):
        return overrides.Checked(problems=[f"run {run.id} has ended, so no node is left to run with another model"])
    return overrides.check(given, workflows, state.values or {})


def _refused(name: str, problems: list[str]) -> int:
    for problem in problems:
        print(f"{name}: {problem}", file=sys.stderr)
    return 1


def _keep(run: runs.Run, values: dict, found: dict, new_limits: dict) -> None:
    """Record checked overrides in events.jsonl, each with the last error of its target. Then keep them and the
    new limits in run.json, in one write. If that write fails, the events go again: events.jsonl and run.json
    always agree, and either everything changes or nothing does. Ctrl-C and the stop signals wait for both
    writes, so they can't stop wf between them. Raises OSError."""
    if not found and not new_limits:
        return
    events = []
    for key, override in found.items():
        node, index = overrides.split(key)
        error = overrides.last_error(values, node, index)
        events.append({"event": "override", "node": node, **({"index": index} if index is not None else {}),
                       **override, **({"last_error": error} if error else {})})
    with _holding_signals():
        size = run.add_events(events) if events else None
        try:
            run.keep(new_limits, found, _number(values))
        except BaseException:
            if size is not None:
                run.take_back_events(size)
            raise


@contextlib.contextmanager
def _holding_signals() -> Iterator[None]:
    """Inside this block, Ctrl-C, SIGTERM and a lost terminal (SIGHUP) wait. Each takes effect when the block
    ends, as it would have before, so the block never stops half done. Main thread only, as for every signal
    handler. A handler that Python didn't set can't be put back, so its signal doesn't wait."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    held: list[int] = []
    before = {signum: handler for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
              if (handler := signal.getsignal(signum)) is not None}
    for signum in before:
        signal.signal(signum, lambda signum, frame: held.append(signum))
    try:
        yield
    finally:
        for signum, handler in before.items():
            signal.signal(signum, handler)
        for signum in held:
            signal.raise_signal(signum)


def _note_uncommitted(workflows: list[spec.Workflow], read_by_wf: list[Path]) -> None:
    """`workspace: new` worktrees start from the last commit: say so if there are changes they won't
    have. The workflow files and the dry-run script don't count: wf reads those itself."""
    if not any(w.setting(node_id, "workspace") == "new" for w in workflows for node_id in w.nodes):
        return
    own = {Path(p).resolve() for p in read_by_wf}
    changed = [os.path.relpath(f) for f in workspaces.uncommitted(Path.cwd()) if f.resolve() not in own]
    if changed:
        shown = ", ".join(changed[:5]) + (f" and {len(changed) - 5} more" if len(changed) > 5 else "")
        print(f"Note: {len(changed)} uncommitted change(s) ({shown}) won't be in the `workspace: new` worktrees, "
              "which start from the last commit. Commit them first if its steps need them.", file=sys.stderr)


def _ended(run: runs.Run, state: StateSnapshot) -> bool:
    """Has the run ended, so `wf retry` only says how, and no agent works?"""
    if not state.values:
        return (run.dir / "output.json").exists()
    return not state.interrupts and not state.next


def _run_home(run: runs.Run, state: StateSnapshot) -> dict:
    """The run's `repo` workspace: as its first step saved it, else as run.json has it, else where it is."""
    if saved := (state.values or {}).get("run"):
        return {"path": saved["repo"], "branch": saved["branch"], "base": saved["base"]}
    return run.info().get("home") or _home(run.repo)


def _signing_problem(workflows: list[spec.Workflow], home: dict) -> str | None:
    """Why git can't sign the commits this run may make with nobody there, or None. Asked before
    any agent works, so a key that needs a person stops the run before its first agent, not at
    its first commit. `workflows`: the workflow, then its sub-workflows."""
    loaded = {Path(w.path).resolve(): w for w in workflows}
    if not _commits(workflows[0], loaded, workspaces.made_by_runner(home["path"])):
        return None
    try:
        workspaces.check_signing(home["path"])
    except workspaces.WorkspaceError as error:
        return str(error)
    return None


def _commits(workflow: spec.Workflow, loaded: dict[Path, spec.Workflow], repo_is_worktree: bool) -> bool:
    """Can the runner commit in this workflow? It commits after an `edit` or `full` agent in a
    worktree it made: `workspace: new`, a node's that is one, or `repo` when the workflow's repo
    is one (a sub-workflow's, when its parent node runs in one)."""
    def in_worktree(node_id: str, seen: tuple[str, ...] = ()) -> bool:
        kind = workflow.setting(node_id, "workspace")
        if kind in ("new", "repo", "temp"):
            return kind == "new" or (kind == "repo" and repo_is_worktree)
        return kind in workflow.nodes and kind not in seen and in_worktree(kind, (*seen, node_id))

    for node_id, node in workflow.nodes.items():
        if node.kind == "agent" and workflow.setting(node_id, "access") in ("edit", "full") and in_worktree(node_id):
            return True
        if node.kind == "workflow" and _commits(loaded[(Path(workflow.path).parent / node.workflow).resolve()],
                                                loaded, in_worktree(node_id)):
            return True
    return False


def _say(data: dict, text: str, as_json: bool) -> None:
    print(json.dumps(data) if as_json else text)


def _home(repo: Path) -> dict:
    """The workflow's `repo` workspace: where wf was started, its branch and commit."""
    return {"path": str(repo.resolve()), "branch": runs.current_branch(repo), "base": runs.base_commit(repo)}


# ---------- the loop ----------


def _drive(ctx: RunContext, command, asking: bool) -> int:
    code = _driving(ctx, command, asking)
    status = ctx.run.status()
    output = ctx.run.output() if status.get("status") == "succeeded" else None
    ctx.trace.stopped(status, output)
    return code


def _driving(ctx: RunContext, command, asking: bool) -> int:
    run = ctx.run
    while True:
        run.set_status("running")
        try:
            state = asyncio.run(_stream(ctx, command))
        except KeyboardInterrupt:  # asyncio.run cancels the graph's tasks first, then raises this
            run.set_status("failed", error="stopped by the user")
            print(f"\nStopped. Continue with: {run.command('retry')}", file=sys.stderr)
            return STOPPED
        except Exception as error:  # a bug, or the checkpoint store failing: never leave the run `running`
            run.set_status("failed", error=f"{type(error).__name__}: {error}")
            print(f"Run failed: {type(error).__name__}: {error}", file=sys.stderr)
            print(f"Continue with: {run.command('retry')}", file=sys.stderr)
            return 1
        if not state.interrupts:
            return _finished(ctx, state.values)
        command = _answer(state.interrupts, asking, run, ctx.script)
        if isinstance(command, int):
            return command


def _config(ctx: RunContext) -> dict:
    return {"configurable": {"thread_id": ctx.run.id}, "recursion_limit": RECURSION_LIMIT}


async def _snapshot(ctx: RunContext) -> StateSnapshot:
    async with AsyncSqliteSaver.from_conn_string(str(ctx.run.checkpoints)) as saver:
        return await graph.build(saver).aget_state(_config(ctx))


async def _stream(ctx: RunContext, command) -> StateSnapshot:
    """Run the graph until it ends or pauses; return where it stopped."""
    config = _config(ctx)
    ctx.new_event_loop()
    async with AsyncSqliteSaver.from_conn_string(str(ctx.run.checkpoints)) as saver:
        compiled = graph.build(saver)
        ctx.saver = saver  # sub-workflows keep their threads in the same checkpoints
        before = await compiled.aget_state(config)
        ctx.resume(before.values or {})  # the clock, the usage and a follow-up's start, for the jobs that run again
        ctx.trace.know(lambda: graph.variables(before.values, ctx))  # a job that reruns first may show them
        try:
            async with steer.answering(ctx.run, ctx.steering):
                async for event in compiled.astream(command, config, context=ctx, stream_mode="custom", durability="sync"):
                    ctx.run.add_event(event)
                    if (line := describe(event)) is not None:
                        print(line, flush=True)
        finally:
            await ctx.close_harnesses()
        return await compiled.aget_state(config)


def _continue(interrupts, asking: bool, run: runs.Run, answers: dict | None = None, script=None):
    """`wf retry`: retry what failed, and answer what's waiting for a person (from an
    answers file already checked against these questions, a dry run's script, or by asking)."""
    resume = {i.id: "retry" for i in interrupts if i.value.get("kind") == "failed"}
    resume |= {i.id: "continue" for i in interrupts if i.value.get("kind") == "limit"}  # checked: there's room now
    humans = [i for i in interrupts if i.value.get("kind") in ("human", "relay")]
    scripted = _scripted_answers(humans, script) if humans and answers is None and script else None
    if humans and answers is not None:
        resume |= operate.nest(answers)
    elif scripted is not None:
        resume |= scripted
    elif humans:
        answers = _ask_all(humans, asking, run)
        if isinstance(answers, int):
            return answers
        resume |= answers
    return Command(resume=resume)


def _answer(interrupts, asking: bool, run: runs.Run, script=None):
    """The graph paused during a run: ask now, or stop and leave it to `wf retry`."""
    limited = [i.value for i in interrupts if i.value.get("kind") == "limit"]
    if limited:
        reason, name = limited[0].get("reason"), limited[0].get("limit")
        run.set_status("limit", reason=reason, limit=name)
        print(f"Run {run.id} paused: a limit was reached ({reason}).", file=sys.stderr)
        print(f"Continue it with a bigger one: {run.command('retry', limits.hint(name))}", file=sys.stderr)
        return 1
    failed = [i.value for i in interrupts if i.value.get("kind") == "failed"]
    if failed:
        run.set_status("failed", node=failed[0]["node"], error=failed[0]["error"])
        print(f"Run failed: {failed[0]['node']}: {failed[0]['error']}", file=sys.stderr)
        print(f"Fix the cause, then continue with: {run.command('retry')}")
        return 1
    scripted = _scripted_answers(interrupts, script) if script else None
    if scripted is not None:
        return Command(resume=scripted)
    answers = _ask_all(list(interrupts), asking, run)
    return answers if isinstance(answers, int) else Command(resume=answers)


def _scripted_answers(interrupts, script) -> dict | None:
    """A dry run's answers for every waiting question, or None if the script lacks any
    (then the run waits for a person, as a real one would)."""
    asked = operate.questions(interrupts)
    places = [(q["node"], dryrun.leaf(q["node"])) for q in asked]
    if not asked or not script.covers("humans", places):  # several items' questions can share one key
        return None
    answers = {}
    for question, (path, node_id) in zip(asked, places):
        answers[question["key"]] = script.take("humans", path, node_id)
        print(f"  {question['node']}: answered from the script")
    return operate.nest(answers)


def _ask_all(questions, asking: bool, run: runs.Run) -> dict | int:
    """Answers by interrupt id, or an exit code if nobody answers now."""
    names = sorted({name for q in questions for name in _askers(q.value)})
    run.set_status("waiting", nodes=names, questions=operate.questions(questions))
    if not asking:
        print(f"Waiting for a person ({', '.join(names)}). Answer with: {run.command('retry')}")
        return 2
    try:
        return {question.id: _answer_one(question.value) for question in questions}
    except (ask.NoAnswer, KeyboardInterrupt) as stop:
        print(f"\nNo answer yet. Answer later with: {run.command('retry')}")
        return STOPPED if isinstance(stop, KeyboardInterrupt) else 2


def _answer_one(payload: dict):
    """A human node's answers, or for a sub-workflow's relayed questions, {their id: answers}."""
    if payload.get("kind") == "relay":
        return {q["id"]: _answer_one(q["question"]) for q in payload["questions"]}
    print()
    print(payload["message"].rstrip())
    return ask.ask_fields(payload["schema"])


def _askers(payload: dict, path: str = "") -> list[str]:
    """The human nodes a question comes from, e.g. check[1]/approve, or ask[2] for an item."""
    if payload.get("kind") == "relay":
        return [n for q in payload["questions"] for n in _askers(q["question"], f"{path}{payload['node']}/")]
    index = f"[{payload['index']}]" if payload.get("index") is not None else ""
    return [f"{path}{payload['node']}{index}"]


def _finished(ctx: RunContext, values: dict) -> int:
    run, status = ctx.run, values.get("status")
    if status == "succeeded":
        run.set_output(values["output"])
        run.set_status("succeeded", follow_up=_number(values) or None)
        shutil.rmtree(workspaces.temp_root(run.id), ignore_errors=True)
        workspaces.keep_sessions(run.dir, _follow_up_sessions(ctx.workflow, values))  # for wf continue
        run.leave_worktrees(workspaces.run_worktrees(ctx.home["path"], run.id))  # for wf discard and wf continue
        workspaces.remove_run(ctx.home["path"], run.id)  # worktrees go; their branches stay
        if ctx.script is not None:  # ...except after a dry run: those branches hold scripted work
            workspaces.delete_branches(ctx.home["path"], run.id, list(run.branches().values()))
        print(f"Run {run.id} succeeded. Output:")
        print(json.dumps(values["output"], indent=2, ensure_ascii=False, default=str))
        return 0
    failure = values.get("failure") or {"node": "?", "error": "unknown"}
    run.set_status("failed", **failure)
    print(f"Run failed: {failure['node']}: {failure['error']}", file=sys.stderr)
    return 1


def describe(event: dict) -> str | None:
    """One line of progress for an event, or None to stay quiet."""
    kind, node = event.get("event"), event.get("node", "")
    where = node + (f"[{event['index']}]" if event.get("index") is not None else "")
    if kind == "node_started":
        detail = [f"pass {event['pass']}"] if event.get("pass", 1) > 1 else []
        detail += [f"{event['items']} items"] if "items" in event else []
        return f"▶ {node}" + (f" ({', '.join(detail)})" if detail else "")
    if kind == "node_finished":
        return f"✓ {node}"
    if kind == "node_failed":
        return f"✗ {node}: {event['error']}"
    if kind == "item_failed":
        return f"✗ {where}: {event['error']} (continuing without it)"
    if kind == "attempt_failed":
        return f"  {where}: attempt {event['attempt']} failed: {event['error']}"
    if kind == "attempt_started":
        fallback = f", falling back to {event['harness']}" if event.get("fallback") else ""
        return f"↻ {where}: attempt {event['attempt']}{fallback}"
    if kind == "route":
        count = f" ({event['taken']}/{event['max']})" if event.get("max") else ""
        return f"  {node} → {', '.join(event['to'])}{count}"
    if kind == "agent_tool":
        return f"  {where}: {event['tool']} {event.get('detail', '')}".rstrip()
    if kind == "harness_unavailable":
        return f"  {where}: {event['error']}" + (f"; trying {event['next']}" if event.get("next") else "")
    if kind == "limit":
        return f"⚠ limit reached: {event['reason']}"
    if kind == "session_fresh":
        return f"  {where}: starting a fresh session: {event['reason']}"
    if kind == "session_continued":
        return f"  {where}: continuing the last pass's session"
    if kind == "agent_model" and event.get("override"):
        model = f" {event['model']}" if event.get("model") else ""
        return f"  {where}: attempt {event['attempt']} runs on {event['harness']}{model}, as the override for {event['override']} says"
    if kind == "slot_waiting":
        return f"  {where}: attempt {event['attempt']} waits for an agent slot ({event['max_agents']} of {event['max_agents']} in use)"
    if kind == "slot_taken":
        return f"  {where}: attempt {event['attempt']} got an agent slot after {event['waited_s']:g}s"
    if kind == "warning":
        return f"⚠ {event['message']}"
    if kind == "steer":
        detail = short(event["text"], 80) if "text" in event else event.get("reason")
        return f"  {where}: message from wf steer {event['status']}" + (f": {detail}" if detail else "")
    if kind == "follow_up":
        return f"↪ follow-up {event['number']} → {', '.join(event['to'])}"
    return None


# ---------- inputs ----------


def collect_inputs(workflow: spec.Workflow, pairs: list[str], input_file: Path | None, asking: bool,
                   base: dict | None = None) -> dict:
    """The start input, from `base` (a dry-run script's inputs), then --input, name=value pairs,
    defaults, and (at a terminal) questions; later ones win."""
    fields = workflow.start.input
    schema = schemas.object_schema(fields, workflow.types)
    values: dict = dict(base or {})
    if input_file is not None:
        try:
            given = json.loads(Path(input_file).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise InputError(f"can't read {input_file}: {error}") from error
        if not isinstance(given, dict):
            raise InputError(f"{input_file} must hold a JSON object")
        values |= given
    for pair in pairs:
        name, equals, raw = pair.partition("=")
        if not equals or not name:
            raise InputError(f"expected name=value, got `{pair}`")
        values[name] = _parse(raw, schema["properties"][name]) if name in fields else raw
    for name in values:
        if name not in fields:
            raise InputError(f"unknown input `{name}` (inputs: {', '.join(fields) or 'none'})")
    for name, field in fields.items():
        if name in values:
            continue
        if "default" in field:  # optional or not, as for a sub-workflow (nodes._sub_inputs)
            values[name] = field["default"]
        elif field.get("optional"):
            continue
        elif asking:
            try:
                values[name] = ask.ask_field(name, schema["properties"][name])
            except ask.NoAnswer:
                raise InputError(f"no answer for `{name}`") from None
        else:
            raise InputError(f"missing input `{name}`: pass {name}=..., or run in a terminal to be asked")
    problems = schemas.check(values, schema)
    if problems:
        raise InputError("bad input: " + "; ".join(problems))
    return values


def _parse(raw: str, field: dict):
    """A value that parses as JSON is used as JSON, unless only the plain text fits the field."""
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    if schemas.check(value, field) and not schemas.check(raw, field):
        return raw  # e.g. name=123 for a string field
    return value


# ---------- follow-ups ----------


def follow_up(run_id: str, request: str, no_input: bool, detach: bool = False, as_json: bool = False,
              limit_pairs: list[str] | None = None, runs_dir: Path | None = None) -> int:
    """`wf continue`: give a run that succeeded a follow-up request. Then drive it again from the nodes that take
    the request (scheduler.follow_up_targets). `limit_pairs` (--limit) give the follow-up a budget of its own.
    With `detach`, check and prepare everything here, then let a background `wf retry` drive it.

    The run's lock is taken before anything is checked, and nothing changes until every check passed. Then the
    worktrees come back, status.json says the follow-up runs, and one write of the checkpoints holds the
    request (graph.follow_up). From there, `wf retry` drives the run as it drives any run. So after a crash,
    `wf retry` takes the follow-up in, or finds the run as it was."""
    name = "wf continue"
    try:
        run = runs.find(Path.cwd(), run_id, runs_dir)
        workflows = [spec.load(f) for f in validate.check(run.workflow_file).files]
        new_limits = limits.parse(limit_pairs or [])
        max_agents = capacity.configured()
    except (OSError, spec.SpecError, ValueError, limits.LimitError, config.ConfigError) as error:
        print(f"{name}: {'--limit ' if isinstance(error, limits.LimitError) else ''}{error}", file=sys.stderr)
        return 1
    try:
        with run.driving() as lock:
            return _checked_follow_up(run, workflows, request, no_input, detach, as_json, lock, new_limits, max_agents)
    except runs.RunBusy as error:
        print(f"{name}: {error}, and only a run that succeeded takes a follow-up. Follow it with: {run.command('wait')}",
              file=sys.stderr)
        return 1


def _checked_follow_up(run: runs.Run, workflows: list[spec.Workflow], request: str, no_input: bool, detach: bool,
                       as_json: bool, lock: runs.Lock, new_limits: dict, max_agents: int | None) -> int:
    name, workflow = "wf continue", workflows[0]
    ctx = RunContext(workflow, run, home={}, max_agents=max_agents)
    dry = bool(run.info().get("dry_run"))
    if dry:  # a dry run's follow-up takes the next entries of its script
        try:
            ctx.script = dryrun.Script.resume(run.dir / "dry-run")
        except dryrun.ScriptError as error:
            print(f"{name}: {error}", file=sys.stderr)
            return 1
    state = asyncio.run(_snapshot(ctx))
    if problem := _not_continuable(run, workflow, state):
        print(f"{name}: {problem}", file=sys.stderr)
        return 1
    home = _run_home(run, state)
    if problem := _repo_problem(run, workflow, home) or _signing_problem(workflows, home):
        print(f"{name}: {problem}", file=sys.stderr)
        return 1
    try:
        _restore_worktrees(run, state.values, home, remake=dry)
    except workspaces.WorkspaceError as error:
        print(f"{name}: can't bring back the run's workspaces, so the run stays as it was: {error}", file=sys.stderr)
        return 1
    number, before = _number(state.values) + 1, run.status()
    try:
        with _holding_signals():  # status.json says the follow-up runs before the checkpoints hold it
            run.set_status("running", follow_up=number)
            try:
                asyncio.run(_give(ctx, {"request": request, "number": number, "limits": new_limits, "pending": True}))
            except BaseException:
                run.set_status(before.get("status", "unknown"), **_details(before), follow_up=before.get("follow_up"))
                raise
    except BaseException as error:  # OSError, or the checkpoint store failing: the run stays as it was
        workspaces.remove_run(home["path"], run.id)
        if not isinstance(error, Exception):
            raise
        print(f"{name}: can't give run {run.id} the follow-up, so the run stays as it was: "
              f"{getattr(error, 'strerror', None) or error}", file=sys.stderr)
        return 1
    ctx.limits = limits.effective(workflow.limits, new_limits)
    ctx.overrides = run.overrides()
    print(f"Limits for this follow-up: {limits.describe(ctx.limits)}", file=sys.stderr if as_json else sys.stdout)
    if detach:
        pid = operate.detach(run, ["retry", run.id, "--no-input"], lock)
        _say({"run": run.id, "dir": str(run.dir), "pid": pid, "follow_up": number},
             f"Run {run.id} takes follow-up {number} in the background. Follow it with: {run.command('wait')}", as_json)
        return 0
    return _retry(ctx, asyncio.run(_snapshot(ctx)), interactive() and not no_input, None)


def _not_continuable(run: runs.Run, workflow: spec.Workflow, state: StateSnapshot) -> str | None:
    """Why `wf continue` can't give this run a follow-up now, or None. The caller holds the run's lock."""
    status = run.status().get("status")
    if status == "waiting":
        return f"run {run.id} waits for a person: answer it with {run.command('answer', '<answers.json>')}"
    if status == "failed":
        return f"run {run.id} failed: continue it with {run.command('retry')}"
    if status == "limit":
        return (f"run {run.id} is paused at a limit: continue it with "
                f"{run.command('retry', limits.hint(run.status().get('limit')))}")
    if status == "running":  # and no process drives it, since this one holds its lock
        return f"run {run.id} stopped before it ended: continue it with {run.command('retry')}"
    if status != "succeeded":
        return f"run {run.id} hasn't succeeded (its status is {status}), and only a run that succeeded takes a follow-up"
    if not scheduler.follow_up_targets(workflow):
        return (f"workflow `{workflow.name}` has no `follow_up`, which names the node that takes a follow-up request. "
                "And it has no agent node with `edit` or `full` access to take it by default. Add "
                "`follow_up: {to: <node>}` to name one. A run keeps the workflow files it started with, so only the "
                "runs that start after that change take a follow-up")
    if not _ended(run, state) or (state.values or {}).get("status") != "succeeded":
        return f"run {run.id} has no checkpoint of its end to continue from: {run.command('retry')} says where it stands"
    return None


def _repo_problem(run: runs.Run, workflow: spec.Workflow, home: dict) -> str | None:
    """Why the follow-up can't work in the run's `repo` workspace, or None. It can't when a node that the
    follow-up can reach works there, and that checkout is now on another branch than the run's."""
    kinds, topo = set(), scheduler.Topology.of(workflow)
    for node in topo.reachable(scheduler.follow_up_targets(workflow, topo)):
        seen = set()
        while workflow.setting(node, "workspace") in workflow.nodes and node not in seen:
            seen.add(node)
            node = workflow.setting(node, "workspace")  # it works in that node's workspace
        kinds.add(workflow.setting(node, "workspace"))
    saved = home.get("branch")
    if "repo" not in kinds or not saved or (now := runs.current_branch(Path(home["path"]))) == saved:
        return None
    return (f"run {run.id} worked on branch `{saved}` in {home['path']}, and that checkout is on "
            f"{f'`{now}`' if now else 'a detached HEAD'} now: check out `{saved}` there first")


def _follow_up_sessions(workflow: spec.Workflow, values: dict) -> set[str]:
    """The ids of the agent sessions that a follow-up may continue: the last session of each node that it
    can reach, and those of their sub-workflows. None when no node takes a follow-up."""
    sessions, topo = values.get("sessions") or {}, scheduler.Topology.of(workflow)
    reached = topo.reachable(scheduler.follow_up_targets(workflow, topo))
    return {ident for node in reached for ident in _session_ids(sessions.get(node))}


def _session_ids(session: dict | None) -> list[str]:
    if not session:
        return []
    if "sessions" in session:  # a workflow node's: those its sub-workflow ended with
        return [ident for one in session["sessions"].values() for ident in _session_ids(one)]
    return [session["id"]] if session.get("id") else []


def _restore_worktrees(run: runs.Run, values: dict, home: dict, remake: bool) -> None:
    """Bring back each worktree that the run had when it succeeded, on its branch. Make each temp folder that a
    node shares again, empty. A dry run deleted its branches too, so with `remake` a branch that is gone
    starts again where the run left it. Raises WorkspaceError, after the worktrees go again."""
    own, temp = workspaces.root() / run.id, workspaces.temp_root(run.id).resolve()
    try:
        for path, left in run.left_worktrees().items():
            if isinstance(left, dict) and left.get("branch"):
                try:
                    workspaces.restore(home["path"], own / path, left["branch"], left.get("commit") or "", remake)
                except workspaces.WorkspaceError as error:
                    raise workspaces.WorkspaceError(f"`{path}` worked in {own / path}, and {error}") from error
        for value in (values.get("workspaces") or {}).values():
            for workspace in value if isinstance(value, list) else [value]:
                if workspace and workspace.get("path") and Path(workspace["path"]).resolve().is_relative_to(temp):
                    Path(workspace["path"]).mkdir(parents=True, exist_ok=True)
    except BaseException as error:
        workspaces.remove_run(home["path"], run.id)
        if isinstance(error, OSError):
            raise workspaces.WorkspaceError(f"can't make {error.filename}: {error.strerror or error}") from error
        raise


async def _give(ctx: RunContext, given: dict) -> None:
    async with AsyncSqliteSaver.from_conn_string(str(ctx.run.checkpoints)) as saver:
        await graph.follow_up(graph.build(saver), _config(ctx), given)


def _number(values: dict) -> int:
    """The number of the run's latest follow-up, taken in or not: 0 before `wf continue` gives one."""
    return (values.get("follow_up") or {}).get("number", 0)


def _given_limits(run: runs.Run, values: dict) -> dict:
    """The --limit values of the run's pass: those of its follow-up, with those that `wf retry` kept on top."""
    return {**(values.get("follow_up") or {}).get("limits", {}), **run.limits(_number(values))}


def _details(status: dict) -> dict:
    return {k: v for k, v in status.items() if k not in ("status", "seq", "run", "updated", "follow_up")}


def _retrying(run: runs.Run, values: dict, answers: dict | None) -> str:
    if (values.get("follow_up") or {}).get("pending"):
        return f"Continuing run {run.id} with follow-up {values['follow_up']['number']}"
    return f"{'Continuing' if answers is not None else 'Retrying'} run {run.id}"


def _run_record(ctx: RunContext, values: dict, answers: dict | None) -> dict:
    """The trace's `run` record of a `wf retry` process: retried, or continued with a pending follow-up."""
    given = values.get("follow_up") or {}
    if given.get("pending"):
        return {"action": "continued", "workflow": ctx.workflow.name, "follow_up": given["number"],
                "request": ctx.trace.at(("follow_up", "request"), given["request"])}  # a secret goes as [secret]
    return {"action": "retried", "workflow": ctx.workflow.name, **({"answers": True} if answers is not None else {})}


def _follow_up_hint(ctx: RunContext, status: str) -> str:
    if status != "succeeded" or not scheduler.follow_up_targets(ctx.workflow, ctx.topo):
        return ""
    return f" Give it a follow-up with: {ctx.run.command('continue', '<request>')}"
