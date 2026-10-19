"""Runner: start a run or continue one, and drive the graph until it stops.

    start(file, pairs, input_file, no_input) -> exit code     (wf run)
    resume(run_id, no_input) -> exit code                     (wf resume)

Driving a run alternates between two things:
1. Streaming the graph (_stream, inside asyncio.run): graph.astream() with
   stream_mode="custom". Each chunk is a progress event a node sent with
   runtime.stream_writer: print it, add it to events.jsonl. When the stream
   ends, graph.aget_state() says why: state.interrupts lists what the graph
   is waiting for, and no interrupts means it reached END.
2. Deciding what's next, outside asyncio:
   - Human nodes: at a terminal, ask now and stream again with
     Command(resume={interrupt id: answers}). That's how LangGraph continues
     after interrupt(): the node runs again, and interrupt() returns the value
     sent for its id. Without a terminal (or with --no-input), the run is left
     `waiting` and `wf resume` asks later. Exit code 2.
   - A failure (graph.wait_for_resume): the run is `failed`. `wf resume`
     continues it with Command(resume=...), and the failed jobs run again.
   - A limit (graph.wait_for_resume too): the run is `limit`, paused. `wf resume
     --limit NAME=VALUE` keeps the bigger limit in run.json and continues it the
     same way (limits.py); without one big enough, it says so and stays put.
   - END: write output.json, or record why the run failed or hit a limit.

Questions are asked between streams, not inside the event loop, so Ctrl-C
and Ctrl-D at a prompt behave as they do in any terminal program: the run is
left `waiting`, and `wf resume` asks again.

Every stream opens the same SQLite checkpointer and uses the run id as
LangGraph's thread_id, so a later `wf resume` process finds the run exactly
where this one left it.
"""

import asyncio
import json
import os
import shutil
import sys
from pathlib import Path

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command, StateSnapshot

from . import ask, capacity, dryrun, graph, limits, operate, runs, schemas, settings, spec, validate, workspaces
from .context import RunContext

RECURSION_LIMIT = 10_000  # LangGraph's cap on steps; each pass of a node takes two
STOPPED = 130  # exit code after Ctrl-C, as shells report it


def interactive() -> bool:
    """Is there a person at a terminal to answer questions?"""
    return sys.stdin.isatty() and sys.stdout.isatty()


class InputError(Exception):
    pass


def start(path: Path, pairs: list[str], input_file: Path | None, no_input: bool,
          detach: bool = False, as_json: bool = False, dry_run: Path | None = None,
          limit_pairs: list[str] | None = None) -> int:
    try:
        overrides = limits.parse(limit_pairs or [])
    except limits.LimitError as error:
        print(f"wf run: --limit {error}", file=sys.stderr)
        return 1
    try:
        max_agents = capacity.configured()  # a bad cap stops a run before it exists, a detached one too
    except (capacity.CapError, settings.SettingsError) as error:
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
    asking = interactive() and not no_input and not detach
    try:
        inputs = collect_inputs(workflow, pairs, input_file, asking, base=script.inputs if script else None)
    except InputError as error:
        print(f"wf run: {error}", file=sys.stderr)
        return 1
    repo = Path.cwd()
    home = _home(repo)  # once: run.json and the run itself use the same branch and commit
    run = runs.create(repo, workflow.name, report.files, path, inputs, home=home, dry_run=script is not None,
                      limits=overrides)
    if script is not None:
        script.keep(run.dir / "dry-run")
    if detach:  # a background `wf resume` starts it, with the lock this process takes now
        with run.driving() as lock:
            pid = operate.detach(run, ["resume", run.id, "--no-input"], lock)
        _say({"run": run.id, "dir": str(run.dir), "pid": pid}, f"Run {run.id} started in the background. "
             f"Follow it with: wf wait {run.id}", as_json)
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
            return _drive(ctx, {"input": inputs}, asking)
    except runs.RunBusy as error:
        print(f"wf run: {error}", file=sys.stderr)
        return 1


def resume(run_id: str, no_input: bool, answers_file: Path | None = None, detach: bool = False,
           as_json: bool = False, limit_pairs: list[str] | None = None) -> int:
    """Continue a run: retry what failed, answer what's waiting (from `answers_file`, or by
    asking at a terminal), or carry on after a crash. With `detach`, check everything here,
    then let a background process do it. `limit_pairs` (--limit) set new limits for the
    rest of the run, e.g. to continue one a limit stopped.

    The run's lock is taken before anything is checked, so two operators answering
    at once can't both answer the same questions: the second gets RunBusy."""
    name = "wf answer" if answers_file else "wf resume"
    try:
        run = runs.find(Path.cwd(), run_id)
        workflow = spec.load(run.workflow_file)  # the copy, as it was when the run started
        answers = json.loads(Path(answers_file).read_text(encoding="utf-8")) if answers_file else None
        overrides = limits.parse(limit_pairs or [])
        max_agents = capacity.configured()
    except (OSError, spec.SpecError, ValueError, limits.LimitError, capacity.CapError,
            settings.SettingsError) as error:  # ValueError: not JSON, or not UTF-8
        print(f"{name}: {'--limit ' if isinstance(error, limits.LimitError) else ''}{error}", file=sys.stderr)
        return 1
    try:
        with run.driving() as lock:
            return _checked_resume(run, workflow, name, answers, no_input, detach, as_json, lock, overrides, max_agents)
    except runs.RunBusy as error:
        print(f"{name}: {error}", file=sys.stderr)
        return 1


def _checked_resume(run: runs.Run, workflow: spec.Workflow, name: str, answers: dict | None, no_input: bool,
                    detach: bool, as_json: bool, lock: runs.Lock, overrides: dict, max_agents: int | None) -> int:
    ctx = RunContext(workflow, run, home={}, max_agents=max_agents)
    asking = interactive() and not no_input and not detach
    if run.info().get("dry_run"):  # a dry run continues with the rest of its script
        try:
            ctx.script = dryrun.Script.resume(run.dir / "dry-run")
        except dryrun.ScriptError as error:
            print(f"{name}: {error}", file=sys.stderr)
            return 1
    state = asyncio.run(_snapshot(ctx))
    ctx.limits = limits.effective(workflow.limits, {**run.limits(), **overrides})
    if any(i.value.get("kind") == "limit" for i in state.interrupts):  # paused at a limit: is there room now?
        still = limits.check(ctx.limits, state.values.get("usage") or {}, state.values.get("elapsed_s", 0.0))
        if still:
            print(f"{name}: run {run.id} is still over a limit ({still[1]}). "
                  f"Continue it with a bigger one: {limits.hint(run.id, still[0])}", file=sys.stderr)
            return 1
    if overrides:
        run.set_limits(overrides)
        print(f"Limits for the rest of this run: {limits.describe(ctx.limits)}", file=sys.stderr if as_json else sys.stdout)
    if answers is not None:
        asked = operate.questions(state.interrupts) if state.values else []
        problems = operate.check_answers(asked, answers) if asked else [f"run {run.id} isn't waiting for answers"]
        if problems:
            for problem in problems:
                print(f"{name}: {problem}", file=sys.stderr)
            return 1
    if detach:
        args = ["resume", run.id, "--no-input"]
        if answers is not None:  # keep the answers with the run, for the background process
            kept = run.dir / "answers" / f"{run.status().get('seq', 0)}.json"  # one per state: we hold the lock
            kept.parent.mkdir(exist_ok=True)
            kept.write_text(json.dumps(answers, indent=2), encoding="utf-8")
            args += ["--answers", str(kept)]
        pid = operate.detach(run, args, lock)
        _say({"run": run.id, "pid": pid}, f"Run {run.id} continues in the background. Follow it with: wf wait {run.id}", as_json)
        return 0
    return _resume(ctx, state, asking, answers)


def _resume(ctx: RunContext, state: StateSnapshot, asking: bool, answers: dict | None) -> int:
    run = ctx.run
    if not state.values:  # created but never started (a detached start, or a crash before the first step)
        if (run.dir / "output.json").exists():
            print(f"Run {run.id} already succeeded.")
            return 0
        ctx.home = run.info().get("home") or _home(Path.cwd())
        try:
            inputs = json.loads((run.dir / "input.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:  # a run killed while it was being created
            print(f"wf resume: can't read the run's input: {error}", file=sys.stderr)
            return 1
        print(f"Starting run {run.id}")
        return _drive(ctx, {"input": inputs}, asking)
    saved = state.values.get("run")
    ctx.home = ({"path": saved["repo"], "branch": saved["branch"], "base": saved["base"]} if saved
                else run.info().get("home") or _home(Path.cwd()))  # stopped before its first step
    print(f"Resuming run {run.id}")
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
        print(f"Run {run.id} already {status}.")
        return 0 if status == "succeeded" else 1
    return _drive(ctx, command, asking)


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


def _say(data: dict, text: str, as_json: bool) -> None:
    print(json.dumps(data) if as_json else text)


def _home(repo: Path) -> dict:
    """The workflow's `repo` workspace: where wf was started, its branch and commit."""
    return {"path": str(repo.resolve()), "branch": runs.current_branch(repo), "base": runs.base_commit(repo)}


# ---------- the loop ----------


def _drive(ctx: RunContext, command, asking: bool) -> int:
    run = ctx.run
    while True:
        run.set_status("running")
        try:
            state = asyncio.run(_stream(ctx, command))
        except KeyboardInterrupt:  # asyncio.run cancels the graph's tasks first, then raises this
            run.set_status("failed", error="stopped by the user")
            print(f"\nStopped. Continue with: wf resume {run.id}", file=sys.stderr)
            return STOPPED
        except Exception as error:  # a bug, or the checkpoint store failing: never leave the run `running`
            run.set_status("failed", error=f"{type(error).__name__}: {error}")
            print(f"Run failed: {type(error).__name__}: {error}", file=sys.stderr)
            print(f"Continue with: wf resume {run.id}", file=sys.stderr)
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
        ctx.restart_clock((before.values or {}).get("elapsed_s", 0.0))
        try:
            async for event in compiled.astream(command, config, context=ctx, stream_mode="custom", durability="sync"):
                ctx.run.add_event(event)
                if (line := describe(event)) is not None:
                    print(line, flush=True)
        finally:
            await ctx.close_harnesses()
        return await compiled.aget_state(config)


def _continue(interrupts, asking: bool, run: runs.Run, answers: dict | None = None, script=None):
    """`wf resume`: retry what failed, and answer what's waiting for a person (from an
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
    """The graph paused during a run: ask now, or stop and leave it to `wf resume`."""
    limited = [i.value for i in interrupts if i.value.get("kind") == "limit"]
    if limited:
        reason, name = limited[0].get("reason"), limited[0].get("limit")
        run.set_status("limit", reason=reason, limit=name)
        print(f"Run {run.id} paused: a limit was reached ({reason}).", file=sys.stderr)
        print(f"Continue it with a bigger one: {limits.hint(run.id, name)}", file=sys.stderr)
        return 1
    failed = [i.value for i in interrupts if i.value.get("kind") == "failed"]
    if failed:
        run.set_status("failed", node=failed[0]["node"], error=failed[0]["error"])
        print(f"Run failed: {failed[0]['node']}: {failed[0]['error']}", file=sys.stderr)
        print(f"Fix the cause, then continue with: wf resume {run.id}")
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
        print(f"Waiting for a person ({', '.join(names)}). Answer with: wf resume {run.id}")
        return 2
    try:
        return {question.id: _answer_one(question.value) for question in questions}
    except (ask.NoAnswer, KeyboardInterrupt) as stop:
        print(f"\nNo answer yet. Answer later with: wf resume {run.id}")
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
        run.set_status("succeeded")
        shutil.rmtree(workspaces.temp_root(run.id), ignore_errors=True)
        workspaces.remove_run(ctx.home["path"], run.id)  # worktrees go; their branches stay
        if ctx.script is not None:  # ...except after a dry run: those branches hold scripted work
            workspaces.delete_branches(ctx.home["path"], run.id)
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
    if kind == "slot_waiting":
        return f"  {where}: attempt {event['attempt']} waits for an agent slot ({event['max_agents']} of {event['max_agents']} in use)"
    if kind == "slot_taken":
        return f"  {where}: attempt {event['attempt']} got an agent slot after {event['waited_s']:g}s"
    if kind == "warning":
        return f"⚠ {event['message']}"
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
