"""Scripted: the harness behind dry runs. It plays a script instead of calling an agent.

Every agent attempt in a dry run gets one of these sessions, whatever harness
the node names, so fallbacks between harnesses still happen: the session
takes the node's next entry from the script (dryrun.py) and, on its first turn,
does what the entry says:
- a result: write its `_files` into the workspace, then call submit_result with
  the rest, exactly as an agent would (a result that doesn't fit fails the turn,
  with the problems);
- `_fail`: the turn fails, so the node's retry applies;
- `_unavailable`: the harness is out, so the next harness in the node's list runs.
Later turns (the reminders) do nothing. The attempt's log records the harness,
model and effort the session was given. `_tokens` and `_cost_usd` are reported
as the turn's usage, so limits can be tested. A node with no entry left raises
dryrun.ScriptExhausted, which fails the node at once: a retry would find none either.
"""

import itertools

from .. import dryrun
from .base import SessionSpec, TurnEnd, Usage, transcript

_ids = itertools.count(1)


class Scripted:
    def __init__(self, name: str, script: dryrun.Script):
        self.name, self.script = name, script

    async def open(self, run_dir) -> None:
        pass

    async def check_model(self, model: str | None, access: str = "read-only") -> None:
        pass

    async def session(self, spec: SessionSpec) -> "ScriptedSession":
        return ScriptedSession(self, spec)

    async def close(self) -> None:
        pass


class ScriptedSession:
    def __init__(self, harness: Scripted, spec: SessionSpec):
        self.harness, self.spec = harness, spec
        self.id = f"dry-run-{next(_ids)}"
        self.turns = 0

    def pending_usage(self) -> Usage:
        return Usage()

    async def steer(self, text: str, ident: str) -> None:
        return None  # a message from `wf steer` goes as the next turn, which does nothing

    async def send(self, text: str) -> TurnEnd:
        spec = self.spec
        transcript(spec.log, "sent", text=text)
        self.turns += 1
        if self.turns > 1:
            return TurnEnd("done")
        entry = self.harness.script.take("agents", spec.where, spec.node)  # raises ScriptExhausted when used up
        transcript(spec.log, "dry_run", harness=self.harness.name, model=spec.model, effort=spec.effort, entry=entry)
        if entry is None:
            raise dryrun.ScriptExhausted(f"dry run: the script has no entries for {spec.where or spec.node}")
        usage = Usage(int(entry.get("_tokens", 0)), float(entry.get("_cost_usd", 0.0)))  # failed attempts spend too
        if "_unavailable" in entry:
            return TurnEnd("unavailable", f"dry run: {entry['_unavailable']}", usage)
        if "_fail" in entry:
            return TurnEnd("failed", f"dry run: {entry['_fail']}", usage)
        dryrun.write_files(spec.cwd, entry.get("_files", {}))
        reply, is_error = spec.tool.submit({k: v for k, v in entry.items() if not k.startswith("_")})
        if is_error:
            return TurnEnd("failed", f"dry run: the scripted result for {spec.where or spec.node} doesn't fit: {reply}", usage)
        spec.emit({"event": "agent_tool", "tool": "submit_result", "detail": "(scripted)"})
        return TurnEnd("done", usage=usage)

    async def close(self) -> None:
        pass
