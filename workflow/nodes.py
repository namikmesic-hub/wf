"""Nodes: run one job and report how it went.

graph.py's `execute` node calls run_job() once per Send. A job is one pass of
a node, or one item of a for_each node. Every kind goes through the same steps:

1. Find the workspace: the folder the job runs in.
2. Render the node's templates with the variables the job carries.
3. Make attempts, each within the node's timeout: 1 + retry of them.
4. Leave a record in the pass folder: instruction.md (what was asked),
   attempt-<n>.log (what happened) and output.json (the result).

run_job() never raises. A failure comes back as a Finished record with
ok=False, so one failing job doesn't cancel the others running in the same
step. The exception is interrupt() in human nodes, which LangGraph handles
itself: it saves the run and stops the graph. When the run continues (right
away at a terminal, or later with `wf resume`), LangGraph runs this job again
from the top, and this time interrupt() returns the person's answers.

Agent nodes (_agent) go through the attempt loop the spec describes, written
once for every harness: open a session, send the instruction, then up to two
reminders, and stop as soon as submit_result has a result. A harness that
reports itself unavailable (usage limit, login, can't start) sends the loop
straight to the next harness in the node's list; other failures count
against `retry`. What an attempt spends is counted even when it fails; for a
turn cut off by the timeout, that's what the harness reported before it.
With `session: continue`, a pass forks the session the node's last pass ended
in (Claude Code's fork_session, Codex's thread/fork), so that session is never
changed and a retry forks from the same point again. Cursor can't fork: it
loads the session itself, and refuses to load it again after a continued
attempt failed, so the retry starts fresh. `session.continued` is
true while its instruction renders, and NEW_RESULT asks for a fresh
submit_result call. It starts fresh instead, with an event saying why, when
the harness or model differs from the last pass, the fork fails, or the two
passes before it both continued.

With a cap on live agent sessions (capacity.py), each agent attempt first takes
one of the agent slots, and holds it until the attempt ends. An
attempt that waits for one isn't failing: the wait uses no retry, the node's
timeout starts once the attempt has its slot, and the wait shows in
events.jsonl (slot_waiting, slot_taken) and in status.json (agents_waiting).
A limit reached while it waits ends the wait, and the attempt doesn't start.

A node with `tools` doesn't run on a harness whose commands could reach the
Keychain at its access level (harnesses.KEYCHAIN_SHELL) unless it sets
`tools_with_shell: true`: that harness counts as unavailable, and the next one
in the node's list takes over. `wf validate` says so before the run.

An agent node's wf tools (`tools`) are connected for each attempt and handed
to the session in SessionSpec.tools (tools/relay.py). A tool that isn't set up
fails the job at once, since a retry would find it the same; a server that
didn't answer fails just the attempt. So does a tool that stopped working
during the session (its sign-in refused, its connection lost), even when the
agent then submitted a result: it worked without the tool. Dry runs connect
none: scripted results don't call tools.

Workspaces: `new` is a git worktree (workspaces.py). For an `edit` or `full`
agent in a worktree the runner made, a retry starts from the commit the job
started from, and when the agent succeeds the runner commits what it left
(`edit` agents are told not to commit themselves). A node's `branch` names
its worktree's branch on its first pass; the run folder's branches.json
records it, so later passes and `wf resume` keep using it, and no other node
or run can take it over. An item's `branch` renders on every pass, and must
name the branch its index already has.

Workflow nodes (_workflow) run their sub-workflow with the same graph, as a
separate LangGraph thread in the run's checkpoints: one thread per attempt.
So a sub-workflow that fails is just this job failing (retry, on_item_error
and `wf resume` all work as for any node), and a retry starts a fresh thread.
The sub-workflow runs in a fresh contextvars.Context, so LangGraph doesn't
take it for part of the parent's graph. When it pauses for a person, this job
relays the questions with its own interrupt(); when the run continues, the
answers go back down with Command(resume=...). Its progress events are passed
up with the path in front: build[2]/implement.
"""

import asyncio
import codecs
import contextvars
import json
import os
import signal
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager, nullcontext
from dataclasses import replace
from pathlib import Path
from typing import Any

from langgraph.errors import GraphBubbleUp
from langgraph.types import interrupt

from . import capacity, dryrun, output_tool, schemas, templates, workspaces
from .context import RunContext
from .runs import Run, now, write_json
from .harnesses import SessionSpec, Unavailable, Usage, reaches_keychain
from .spec import seconds
from .state import Finished, Job

TAIL = 20_000  # characters of stdout and stderr kept in a command's output


class _Failed(Exception):
    """The job failed. `attempts` is the number of the last attempt made."""

    def __init__(self, message: str, attempts: int, stopped: str | None = None):
        super().__init__(message)
        self.attempts, self.stopped = attempts, stopped


async def run_job(job: Job, ctx: RunContext, emit: Callable[[dict], None]) -> Finished:
    node_id, index = job["node"], job["index"]
    node = ctx.workflow.nodes[node_id]
    workspace, usage = None, Usage()  # usage: what this job's attempts spent, failed ones included
    extra: dict = {}  # more for the Finished record (an agent's session)

    def finished(ok: bool, output: Any = None, error: str | None = None, attempts: int = job["attempt"], **more) -> Finished:
        return Finished(node=node_id, pass_no=job["pass_no"], index=index, ok=ok, output=output, error=error,
                        attempts=attempts, workspace=workspace,
                        usage={"cost_usd": usage.cost_usd, "tokens": usage.tokens}, **more)

    try:
        folder = ctx.run.pass_dir(node_id, job["pass_no"], index)  # inside: a full disk fails the job, not the run
        variables = {**job["vars"], **({"item": job["item"], "index": index} if index is not None else {})}
        workspace = await _workspace(job, ctx, variables)
        variables["workspace"] = {**job["vars"]["workspace"], node_id: workspace}
        if node.kind == "agent":
            output, attempts = await _agent(job, ctx, folder, variables, workspace, emit, usage, extra)
        elif node.kind == "command":
            output, attempts = await _command(job, ctx, folder, variables, workspace, emit)
        elif node.kind == "human":
            output, attempts = _human(job, ctx, folder, variables), job["attempt"]
        else:
            output, attempts = await _workflow(job, ctx, folder, variables, workspace, emit, usage)
    except GraphBubbleUp:
        raise  # interrupt(): LangGraph pauses the run here
    except _Failed as error:
        return finished(False, error=str(error), attempts=error.attempts, **({"stopped": error.stopped} if error.stopped else {}))
    except templates.TemplateError as error:
        return finished(False, error=f"template: {error}")
    except workspaces.WorkspaceError as error:
        return finished(False, error=f"workspace: {error}")
    except Exception as error:  # a bug or an environment problem: report it, don't crash the run
        return finished(False, error=f"{type(error).__name__}: {error}")
    try:
        (folder / "output.json").write_text(json.dumps(output, indent=2, default=str) + "\n", encoding="utf-8")
    except OSError as error:  # the job ran; only keeping a record of it failed
        return finished(False, error=f"can't write output.json: {error}", attempts=attempts)
    return finished(True, output=output, attempts=attempts, **extra)


# ---------- workspaces ----------


async def _workspace(job: Job, ctx: RunContext, variables: dict) -> dict:
    """{path, branch, base} of the folder this job runs in."""
    node_id = job["node"]
    kind = ctx.workflow.setting(node_id, "workspace")
    if kind == "repo":
        return dict(ctx.home)
    if kind == "temp":
        folder = workspaces.temp_root(ctx.run.id).joinpath(*ctx.prefix, node_id, str(job["pass_no"]))
        folder = folder / str(job["index"]) if job["index"] is not None else folder
        folder.mkdir(parents=True, exist_ok=True)
        return {"path": str(folder), "branch": None, "base": None}
    if kind == "new":
        base = ctx.workflow.setting(node_id, "base")
        base = templates.render(base, variables).strip() if base else ctx.home["base"]
        if not base:
            raise _Failed("`workspace: new` needs a git repo with at least one commit", job["attempt"])
        parts = [*ctx.prefix, node_id] + ([str(job["index"])] if job["index"] is not None else [])  # e.g. build/2/implement
        template = ctx.workflow.setting(node_id, "branch")
        if template is None:  # "" is a name too, and an empty one is refused
            return await _git(ctx, workspaces.worktree, ctx.home["path"], ctx.run.id, parts, base)
        # A node keeps the name its first pass gave. An item renders it again on every pass:
        # another item may be at its index now (node ids start with a letter, indexes are digits)
        named = ctx.root.run.branches().get("/".join(parts))
        if named is None or any(part.isdigit() for part in parts):
            named = templates.render(template, variables).strip()
        return await _git(ctx, _named_worktree, ctx.root.run, ctx.home["path"], parts, base, named)
    shared = job["vars"]["workspace"].get(kind)
    if shared is None:
        raise _Failed(f"`{kind}` hasn't run yet, so it has no workspace to share", job["attempt"])
    return shared


def _named_worktree(run: Run, repo: str, parts: list[str], base: str, branch: str) -> dict:
    """The worktree on the branch a node's `branch` named: one this run made for it, or a new one.
    A branch that exists and that this run didn't make is never taken over, and neither is one
    that git can't have beside a branch that exists (fix/a beside fix/a/b, or FIX/a where git
    ignores case)."""
    path = "/".join(parts)
    if problem := workspaces.branch_problem(repo, branch):
        raise workspaces.WorkspaceError(f"can't name the branch `{branch}`: {problem}")
    made = run.branches()
    if path in made:  # a later pass, or the run resumed
        if made[path] != branch:
            raise workspaces.WorkspaceError(f"`{path}` worked on branch `{made[path]}` in an earlier pass, and now names "
                                            f"`{branch}`: worktrees go by item index, so keep each item at its index")
        return workspaces.worktree(repo, run.id, parts, base, branch)
    if workspaces.run_worktrees(repo, run.id).get(path, (None,))[0] == branch:  # made, but not recorded: the run stopped
        run.add_branch(path, branch)
        return workspaces.worktree(repo, run.id, parts, base, branch)
    fold = workspaces.ignores_case(repo)
    if owner := next((p for p, b in made.items() if workspaces.clash(b, branch, fold)), None):
        taken = made[owner]
        raise workspaces.WorkspaceError(f"`{owner}` already works on branch `{taken}` in this run" if taken == branch else
                                        f"`{owner}` works on branch `{taken}` in this run, and git can't have both "
                                        f"`{taken}` and `{branch}`")
    if existing := workspaces.clashing_branch(repo, branch):
        raise workspaces.WorkspaceError(
            f"branch `{branch}` already exists and this run didn't make it; delete it or name another" if existing == branch
            else f"branch `{existing}` exists, and git can't have both `{existing}` and `{branch}`; delete it or name another")
    workspace = workspaces.worktree(repo, run.id, parts, base, branch)
    run.add_branch(path, branch)  # once it exists: the record lists only branches this run made
    return workspace


_GIT = threading.Lock()  # held by the thread itself: see _git


async def _git(ctx: RunContext, operation, *args):
    """Run a blocking workspaces.* git operation in a thread, one at a time.

    A thread can't be cancelled. If this job is (a timeout, Ctrl-C), the await ends at once,
    and `stop` tells the thread to stop its git command and start no more. The thread holds
    a lock of its own until then, so the next git operation waits for it."""
    stop = threading.Event()

    def locked():
        with _GIT, workspaces.stopped_by(stop):
            return operation(*args)

    async with ctx.git_lock():
        try:
            return await asyncio.to_thread(locked)
        except asyncio.CancelledError:
            stop.set()
            raise


def _runner_commits(ctx: RunContext, node_id: str, workspace: dict) -> bool:
    """Does the runner commit (and reset) for this agent? Only edit/full agents in its own worktrees."""
    return ctx.workflow.setting(node_id, "access") in ("edit", "full") and workspaces.made_by_runner(workspace["path"])



# ---------- attempts ----------


def _no_slot(number: int, harness: str | None) -> AbstractAsyncContextManager[None]:
    return nullcontext()


async def _attempts(job: Job, ctx: RunContext, emit, attempt: Callable[[int, str | None], Awaitable[Any]],
                    harnesses: list[str | None] = [None],
                    slot: Callable[[int, str | None], AbstractAsyncContextManager[None]] = _no_slot) -> tuple[Any, int]:
    """Up to 1 + retry attempts on each harness in turn, each within the node's timeout.
    attempt(number, harness) makes one, inside slot(number, harness): for an agent, its
    agent slot (_agent_slot). Returns (output, attempt number)."""
    node_id = job["node"]
    retry = ctx.workflow.setting(node_id, "retry")
    timeout = ctx.workflow.setting(node_id, "timeout")
    label = _label(job)
    first, number, error = job["attempt"], job["attempt"], ""
    handed_over = False  # did the last harness give up because it was unavailable (already said)?
    for position, harness in enumerate(harnesses):
        for tries in range(retry + 1):
            if number > first and (reason := ctx.limit_reached()):
                raise _limit_stop(error, number, reason)
            on = {"harness": harness} if harness else {}
            fallback = {"fallback": True} if position and not tries and not handed_over else {}
            handed_over = False
            deadline = None
            try:
                async with slot(number, harness):  # an attempt starts once it has its slot
                    if number > 1:
                        emit({"event": "attempt_started", **label, **on, **fallback, "attempt": number})
                    deadline = asyncio.timeout(seconds(timeout) if timeout else None)
                    async with deadline:
                        result = await attempt(number, harness)
                return result, number
            except _LimitWhileWaiting as reached:
                raise _limit_stop(error, number, str(reached)) from None
            except TimeoutError:  # the node's timeout, or a timeout inside the harness
                ours = deadline is not None and deadline.expired()
                error = f"timed out after {timeout}" if ours else "a call inside the harness timed out"
            except _Unavailable as failure:  # don't retry this harness: go to the next one
                error = f"{harness} is unavailable: {failure}"
                following = harnesses[position + 1] if position + 1 < len(harnesses) else None
                emit({"event": "harness_unavailable", **label, **on, "attempt": number, "error": error, "next": following})
                number += 1
                handed_over = True
                break
            except _AttemptFailed as failure:
                error = str(failure)
            except dryrun.ScriptExhausted as failure:  # another attempt would find no entry either
                emit({"event": "attempt_failed", **label, **on, "attempt": number, "error": str(failure)})
                raise _Failed(str(failure), number) from None
            emit({"event": "attempt_failed", **label, **on, "attempt": number, "error": error})
            number += 1
    raise _Failed(error, number - 1)


def _limit_stop(error: str, number: int, reason: str) -> _Failed:
    """Attempt `number` doesn't start because a limit was reached. `error`: the last attempt's failure, if any."""
    stop = f"no new attempt because a limit was reached ({reason})"
    return _Failed(f"{error}; {stop}" if error else stop, number - 1, stopped="limit")


class _AttemptFailed(Exception):
    """One attempt failed; another may follow."""


class _LimitWhileWaiting(Exception):
    """A limit was reached while the attempt waited for its agent slot, so it doesn't start."""


class _Unavailable(Exception):
    """The harness can't run this attempt (usage limit, login, can't start): try the next harness."""


def _path(ctx: RunContext, job: Job) -> str:
    """The node's full path, as dry-run scripts name it: build[1]/implement, or each[2]."""
    return ctx.where + job["node"] + (f"[{job['index']}]" if job["index"] is not None else "")


def _label(job: Job) -> dict:
    return {"node": job["node"], "pass": job["pass_no"], **({"index": job["index"]} if job["index"] is not None else {})}


# ---------- agent nodes ----------


async def _agent(job: Job, ctx: RunContext, folder: Path, variables: dict, workspace: dict, emit, usage: Usage,
                 extra: dict) -> tuple[dict, int]:
    node_id, wf = job["node"], ctx.workflow
    commits = _runner_commits(ctx, node_id, workspace)
    note = [DONT_COMMIT] if commits and wf.setting(node_id, "access") == "edit" else []
    continuing = wf.setting(node_id, "session") == "continue" and job["index"] is None  # items always start fresh
    previous = job.get("session")  # the last pass's session, for session: continue

    def render(continued: bool) -> str:
        text = templates.render(wf.nodes[node_id].instruction, {**variables, "session": {"continued": continued}})
        return "\n\n".join([text.rstrip(), *note, *([output_tool.NEW_RESULT] if continued else []), output_tool.FINISH])

    fresh = render(False)
    (folder / "instruction.md").write_text(fresh, encoding="utf-8")
    start, entered_before = await _start_commit(ctx, folder, workspace) if commits else (None, False)
    harness = wf.setting(node_id, "harness")
    names = list(harness) if isinstance(harness, list) else [templates.render(harness, variables).strip()]
    chosen = {name: tuple(_rendered_setting(job, ctx, name, key, variables) for key in ("model", "effort")) for name in names}
    schema = schemas.node_output_schema(wf, node_id)
    listed = None if ctx.root.script else wf.setting(node_id, "tools")  # None: the node doesn't list tools
    uses = listed or ()

    async def attempt(number: int, name: str) -> dict:
        # Every attempt but the job's very first starts from the commit the job started from:
        # retries, and attempts after a crash or `wf resume` (which may find half-done edits)
        if start and (number > job["attempt"] or entered_before):
            await _git(ctx, workspaces.reset, workspace["path"], start)
        access = wf.setting(node_id, "access")
        if uses and reaches_keychain(name, access) and not wf.nodes[node_id].tools_with_shell:
            raise _Unavailable(f"at `{access}` access its commands could read every stored sign-in, "
                               "and the node doesn't set `tools_with_shell: true`")
        try:
            agent = await ctx.harness(name)
        except Unavailable as error:
            raise _Unavailable(str(error)) from error
        async with _attached(uses, number) as tools:
            return await converse(number, name, agent, tools)

    async def converse(number: int, name: str, agent, tools: list) -> dict:
        """One session: the instruction, then reminders, until submit_result has a result."""
        model, effort = chosen[name]
        source, why_not = _continue_from(previous, name, model) if continuing else (None, None)
        if why_not:
            emit({"event": "session_fresh", **_label(job), "attempt": number, "reason": why_not})
        spec = SessionSpec(
            cwd=Path(workspace["path"]),
            access=wf.setting(node_id, "access"),
            web=wf.setting(node_id, "web"),
            model=model,
            effort=effort,
            max_turns=wf.harness_setting(node_id, name, "max_turns"),
            budget_usd=wf.harness_setting(node_id, name, "budget_usd"),
            tool=output_tool.ResultTool(schema),
            log=folder / f"attempt-{number}.log",
            emit=lambda event: emit({**event, **_label(job), "attempt": number, "harness": name}),
            node=node_id,
            where=_path(ctx, job),
            continuable=continuing,
            fork_from=source["id"] if source else None,
            prior_cost_usd=source["cost_usd"] if source else 0.0,
            tools=tools,
            only_wf_tools=listed is not None,
        )
        try:
            session = await agent.session(spec)
        except Unavailable as error:
            if source is None:
                raise _Unavailable(str(error)) from error
            emit({"event": "session_fresh", **_label(job), "attempt": number, "reason": str(error)})
            source, spec = None, replace(spec, fork_from=None, prior_cost_usd=0.0)  # the fork failed: start fresh
            try:
                session = await agent.session(spec)
            except Unavailable as error:
                raise _Unavailable(str(error)) from error
        instruction = render(True) if source else fresh
        if source:
            emit({"event": "session_continued", **_label(job), "attempt": number})
            (folder / "instruction.md").write_text(instruction, encoding="utf-8")
        spent_before = usage.cost_usd
        try:
            for text in [instruction] + [output_tool.REMINDER] * output_tool.REMINDERS:
                try:
                    end = await session.send(text)
                except Unavailable as error:
                    raise _Unavailable(str(error)) from error
                except dryrun.ScriptExhausted:
                    raise
                except Exception as error:  # the SDK or the agent process broke
                    raise _AttemptFailed(f"{name}: {type(error).__name__}: {error}") from error
                usage.add(end.usage)
                ctx.add_usage(end.usage)  # live, so limits apply before the next attempt
                if spec.tool.result is not None:
                    extra["session"] = {"harness": name, "model": model, "id": session.id,
                                        "cost_usd": (source["cost_usd"] if source else 0.0) + usage.cost_usd - spent_before,
                                        "streak": source["streak"] + 1 if source else 0}
                    (folder / "session.json").write_text(json.dumps(extra["session"], indent=2) + "\n", encoding="utf-8")
                    return schemas.absent_as_none(spec.tool.result, schema)
                if end.status == "unavailable":
                    raise _Unavailable(end.message)
                if end.status == "failed":
                    raise _AttemptFailed(end.message)
            raise _AttemptFailed("the agent never made a valid submit_result call")
        finally:
            cut_off = session.pending_usage()  # zero unless a timeout stopped a turn midway
            usage.add(cut_off)
            ctx.add_usage(cut_off)
            await session.close()

    result, number = await _attempts(job, ctx, emit, attempt, names,
                                     slot=lambda number, name: _agent_slot(job, ctx, emit, number, name))
    if commits:
        try:
            await _git(ctx, workspaces.commit_all, workspace["path"], f"wf: {node_id} ({ctx.run.id})")
        except workspaces.WorkspaceError as error:  # attempt `number` ran: a resumed job starts at the next one
            raise _Failed(f"workspace: {error}", number) from error
    return result, number


DONT_COMMIT = "Don't commit: the runner commits your changes when you're done."


def _rendered_setting(job: Job, ctx: RunContext, harness: str, name: str, variables: dict) -> str | None:
    """`model` or `effort` for one harness, rendered: a template like "{{ item.model }}" gives each item its own."""
    source = ctx.workflow.harness_setting(job["node"], harness, name)
    if source is None:
        return None
    try:
        value = templates.render_value(source, variables)
    except templates.TemplateError as error:
        raise templates.TemplateError(f"{name}: {error}") from error
    if not isinstance(value, str) or not value.strip():
        shown = "none" if value is None else json.dumps(value, ensure_ascii=False, default=str)
        raise _Failed(f"`{name}` of node `{job['node']}` renders to {shown} for {harness}, "
                      "but it must be a non-empty string", job["attempt"])
    return value.strip()


@asynccontextmanager
async def _agent_slot(job: Job, ctx: RunContext, emit, number: int, harness: str) -> AsyncIterator[None]:
    """Hold one of the agent slots (capacity.py) for attempt `number`, waiting for one if
    they're all taken, until a limit is reached (_LimitWhileWaiting). No cap, or a dry run:
    there's nothing to hold."""
    root = ctx.root
    if root.max_agents is None or root.script is not None:
        yield
        return
    where = _path(ctx, job)
    holder = {"pid": os.getpid(), "run": root.run.id, "node": where, "attempt": number}
    slot = capacity.take(root.max_agents, holder)
    try:
        if slot is None:
            on = {**_label(job), "attempt": number, "harness": harness}
            emit({"event": "slot_waiting", **on, "max_agents": root.max_agents})
            began, key = time.monotonic(), f"{where}#{number}"
            ctx.note_slot_wait(key, {"node": where, "attempt": number, "since": now()})
            try:
                slot = await capacity.wait(root.max_agents, holder, stop=lambda: ctx.limit_reached() is not None)
            finally:
                ctx.note_slot_wait(key, None)
            reason = ctx.limit_reached()  # asked again: the slot may have come free just as a limit was reached
            if slot is None or reason:
                raise _LimitWhileWaiting(reason)
            emit({"event": "slot_taken", **on, "waited_s": round(time.monotonic() - began, 1)})
        yield
    finally:
        if slot is not None:
            slot.release()


@asynccontextmanager
async def _attached(uses, number: int):
    """The node's wf tools, connected for one attempt."""
    if not uses:
        yield []
        return
    from .tools import keychain, relay, signin  # relay loads the MCP client, which runs without tools don't need

    try:
        async with relay.attach(uses) as tools:
            yield tools
            failure = next((tool.failure for tool in tools if tool.failure), None)
            if failure:
                raise failure
    except (signin.NotSetUp, keychain.Unavailable) as error:  # a retry would find it the same
        raise _Failed(str(error), number) from error
    except signin.Unreachable as error:
        raise _AttemptFailed(str(error)) from error


async def _start_commit(ctx: RunContext, folder: Path, workspace: dict) -> tuple[str, bool]:
    """The commit this job started from, kept in its pass folder so a resumed or replayed job
    goes back to the same one. Returns (commit, whether the job was entered before)."""
    record = folder / "start-commit"
    if record.exists():
        return record.read_text(encoding="utf-8").strip(), True
    commit = await _git(ctx, workspaces.head, workspace["path"])
    record.write_text(commit + "\n", encoding="utf-8")
    return commit, False


def _continue_from(previous: dict | None, harness: str, model: str | None) -> tuple[dict | None, str | None]:
    """(the session to fork, None) or (None, why this pass starts fresh). A first pass has no reason."""
    if previous is None:
        return None, None
    if previous["harness"] != harness:
        return None, f"the last pass ran on {previous['harness']}"
    if previous["model"] != model:
        return None, f"the last pass used model {previous['model']}"
    if previous["streak"] >= 2:
        return None, "the two passes before it both continued"
    return previous, None


# ---------- command nodes ----------


async def _command(job, ctx: RunContext, folder: Path, variables: dict, workspace: dict, emit) -> tuple[dict, int]:
    node_id = job["node"]
    node = ctx.workflow.nodes[node_id]
    command = templates.render(node.command, variables)
    env = {**os.environ, **{k: templates.render(v, variables) for k, v in ctx.workflow.setting(node_id, "env").items()}}
    check = ctx.workflow.setting(node_id, "check")
    (folder / "instruction.md").write_text(command, encoding="utf-8")

    async def attempt(number: int, harness: None) -> dict:
        scripted = ctx.root.script.take("commands", _path(ctx, job), node_id) if ctx.root.script else None
        if scripted is not None:  # a dry run's scripted result: the command doesn't run
            log = folder / f"attempt-{number}.log"
            log.write_text(f"dry run: not run; scripted result {json.dumps(scripted)}\n", encoding="utf-8")
            result = {"exit_code": scripted["exit_code"], "stdout": scripted.get("stdout", ""),
                      "stderr": scripted.get("stderr", ""), "log": str(log)}
        else:
            result = await run_shell(command, Path(workspace["path"]), env, folder / f"attempt-{number}.log")
        if check and result["exit_code"] != 0:
            raise _AttemptFailed(f"exited with code {result['exit_code']}")
        return result

    return await _attempts(job, ctx, emit, attempt)


async def run_shell(command: str, cwd: Path, env: dict, log: Path) -> dict:
    """Run `bash -c command`. Returns the command node output: exit code, the last
    TAIL characters of stdout and stderr, and the log file holding everything."""
    process = await asyncio.create_subprocess_exec(
        "bash", "-c", command, cwd=cwd, env=env,
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        start_new_session=True,  # its own process group, so a timeout can stop everything it started
    )
    tails = {"stdout": "", "stderr": ""}
    with log.open("w", encoding="utf-8") as file:

        async def pump(stream: asyncio.StreamReader, name: str) -> None:
            decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
            while chunk := await stream.read(65536):
                text = decoder.decode(chunk)
                tails[name] = (tails[name] + text)[-TAIL:]
                file.write(text)
                file.flush()

        try:
            # A TaskGroup, unlike gather(), cancels the other reader if one fails
            try:
                async with asyncio.TaskGroup() as readers:
                    readers.create_task(pump(process.stdout, "stdout"))
                    readers.create_task(pump(process.stderr, "stderr"))
            except ExceptionGroup as group:  # e.g. the disk filled up: an attempt failure, with its real cause
                raise _AttemptFailed(f"reading the command's output: {group.exceptions[0]}") from group
            code = await process.wait()
        except BaseException:  # timeout or Ctrl-C: stop the whole process group
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.wait()
            raise
    return {"exit_code": code, "stdout": tails["stdout"], "stderr": tails["stderr"], "log": str(log)}


# ---------- human nodes ----------


def _human(job: Job, ctx: RunContext, folder: Path, variables: dict) -> dict:
    """Ask a person. interrupt() pauses the run; when it continues, it returns the answers."""
    node_id = job["node"]
    message = templates.render(ctx.workflow.nodes[node_id].human, variables)
    (folder / "instruction.md").write_text(message, encoding="utf-8")
    schema = schemas.node_output_schema(ctx.workflow, node_id)
    answers = interrupt({"kind": "human", "node": node_id, "pass": job["pass_no"], "index": job["index"],
                         "message": message, "schema": schema})
    problems = schemas.check(answers, schema)
    if problems:
        raise _Failed("the answers don't match the node's output: " + "; ".join(problems), job["attempt"])
    return schemas.absent_as_none(answers, schema)


# ---------- workflow nodes ----------


async def _workflow(job: Job, ctx: RunContext, folder: Path, variables: dict, workspace: dict, emit, usage: Usage) -> tuple[dict, int]:
    from .graph import build  # graph.py imports this module

    node_id, wf = job["node"], ctx.workflow
    node = wf.nodes[node_id]
    sub = ctx.load((Path(wf.path).parent / node.workflow).resolve())
    given = {k: templates.render_value(v, variables) if isinstance(v, str) else v for k, v in node.with_.items()}
    inputs = _sub_inputs(sub, given, job["attempt"])
    (folder / "instruction.md").write_text(json.dumps({"workflow": node.workflow, "with": inputs}, indent=2) + "\n",
                                           encoding="utf-8")
    try:  # the sub-workflow's `repo` is this node's workspace; its base is that folder's commit now
        base = await _git(ctx, workspaces.head, workspace["path"])
    except workspaces.WorkspaceError:
        base = None
    home = {**workspace, "base": base}
    label = node_id if job["index"] is None else f"{node_id}[{job['index']}]"
    path = [node_id] + ([str(job["index"])] if job["index"] is not None else [])

    def relay(event: dict) -> None:  # the sub-workflow's progress, with the path in front
        if event.get("event") in ("limit", "run_finished"):
            return  # the run's own, not the sub-workflow's: the top-level graph reports those
        emit({**event, "node": f"{label}/{event['node']}"} if "node" in event else event)

    async def attempt(number: int, _harness) -> dict:
        child = ctx.child(sub, folder / f"attempt-{number}", home, path, label)
        # The folder nests a level per pass and attempt at every depth, so it names the thread uniquely
        thread = f"{ctx.run.id}/{child.run.dir.relative_to(ctx.root.run.dir)}"
        # What the parent has already been told this thread spent: only a limit pause tells it
        # early (a question or a crash reruns the job before it reports anything)
        credited_file = child.run.dir / "credited.json"
        credited = json.loads(credited_file.read_text(encoding="utf-8")) if credited_file.exists() else {}
        state = await _run_sub(build(child.saver), child, thread, {"input": inputs}, relay, label)
        values = state.values
        spent = values.get("usage") or {}
        usage.add(Usage(spent.get("tokens", 0) - credited.get("tokens", 0),
                        spent.get("cost_usd", 0.0) - credited.get("cost_usd", 0.0)))
        if values.get("status") == "succeeded":
            return values["output"]
        if values.get("status") == "limit":
            child.run.dir.mkdir(parents=True, exist_ok=True)
            write_json(credited_file, spent)  # this job's Finished reports it now
            # The attempt isn't over: it paused. Reported as the attempt before it, so retry()
            # reruns it under the same number, which finds the same thread and continues it
            raise _Failed(values.get("limit_reason") or "a limit was reached", number - 1, stopped="limit")
        failure = values.get("failure") or {}
        raise _AttemptFailed(f"sub-workflow {sub.name} failed: {failure.get('node')}: {failure.get('error')}")

    return await _attempts(job, ctx, emit, attempt)


def _sub_inputs(sub, given: dict, attempt: int) -> dict:
    """`with`, plus the sub-workflow's input defaults, checked against its start.input."""
    schema = schemas.object_schema(sub.start.input, sub.types)
    values = schemas.without_none(given, schema)  # none counts as left out, so a default replaces it
    for name, field in sub.start.input.items():
        if name not in values and "default" in field:
            values[name] = field["default"]
    problems = schemas.check(values, schema)
    if problems:
        raise _Failed(f"`with` doesn't fit {sub.name}'s inputs: " + "; ".join(problems), attempt)
    return values


async def _run_sub(graph, child: RunContext, thread: str, first_input: dict, relay, label: str):
    """Run (or continue) a sub-workflow thread until it ends or a limit pauses it, relaying
    its questions upward. Returns its state."""
    from .runner import RECURSION_LIMIT
    from langgraph.types import Command

    config = {"configurable": {"thread_id": thread}, "recursion_limit": RECURSION_LIMIT}

    async def stream(command):
        # The parent's stream writer only works in the parent's context, so events
        # cross over through a queue that this job drains (below)
        events, done = asyncio.Queue(), object()

        async def produce():
            try:
                async for event in graph.astream(command, config, context=child, stream_mode="custom", durability="sync"):
                    events.put_nowait(event)
                return await graph.aget_state(config)
            finally:
                events.put_nowait(done)

        task = asyncio.get_running_loop().create_task(produce(), context=contextvars.Context())
        try:
            while (event := await events.get()) is not done:
                relay(event)
            return await task
        finally:
            if not task.done():  # this job was cancelled: stop the sub-workflow too
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    state = await _isolated(graph.aget_state(config))
    if not state.values:
        command = first_input  # a new attempt
    elif _at_limit(state):  # a limit paused it, and the run continues (with a bigger one): so does it
        command = Command(resume={i.id: "continue" for i in state.interrupts})
    elif state.interrupts:
        command = Command(resume=_ask_up(state.interrupts, label))  # it was waiting for a person
    elif state.next:
        command = None  # it stopped mid-step (a crash): carry on
    else:
        return state  # it already finished before this job was interrupted
    while True:
        state = await stream(command)
        if not state.interrupts or _at_limit(state):
            return state
        command = Command(resume=_ask_up(state.interrupts, label))


def _at_limit(state) -> bool:
    """Is this sub-workflow paused at a limit (graph.wait_for_resume), not asking a question?"""
    return bool(state.interrupts) and all(i.value.get("kind") == "limit" for i in state.interrupts)


def _ask_up(interrupts, label: str) -> dict:
    """Pass a sub-workflow's questions up as this job's own interrupt; returns {their id: answer}.

    The first time, interrupt() stops the run here. When it continues, LangGraph runs this
    job again, and interrupt() returns the answers the runner sent down. LangGraph hands
    back stored answers by position: the job's first interrupt() call gets the first answer.
    A rerun can make fewer calls than the original (an attempt whose sub-workflow already
    finished doesn't ask again), so an answer can arrive for an earlier question. Answers
    are keyed by question id, so those are recognized and skipped.
    """
    wanted = {i.id for i in interrupts}
    while True:
        answers = interrupt({"kind": "relay", "node": label,
                             "questions": [{"id": i.id, "question": i.value} for i in interrupts]})
        if isinstance(answers, dict) and set(answers) == wanted:
            return answers


async def _isolated(coroutine):
    """Run a coroutine in a task with a fresh contextvars.Context, so a graph run inside it
    starts its own thread instead of joining the graph of the node that called it."""
    task = asyncio.get_running_loop().create_task(coroutine, context=contextvars.Context())
    try:
        return await task  # if this job is cancelled, awaiting cancels the task too
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
