"""Output tool: `submit_result`, the one tool the runner gives every agent attempt.

Its input schema is the node's `output` schema. The agent finishes by calling
it, and the runner either records the arguments as the attempt's result or
sends back what's wrong, so the agent can fix the call while it still has the
context. That's why the runner uses this instead of each harness's own
structured-output feature: it works the same on every harness, and mistakes
get fixed in the same session.

The rules, from "The output tool" in the spec:
- The instruction ends with FINISH, telling the agent to call submit_result.
- Arguments are checked against the schema (JSON Schema 2020-12). Problems go
  back as a tool error, spelled out with field paths in the text, because
  Codex shows the model only the reply's text, not the error flag.
- The first valid call is the result. Later calls get an error.
- If a turn ends without a valid call, the runner sends REMINDER, up to
  REMINDERS times, in the same session (see nodes.py).

ResultTool.submit() is plain Python. Each harness adapter wraps it in its own
tool type (an SDK MCP tool for Claude Code, a dynamic tool for Codex, a tool on
an MCP server over HTTP for Cursor), so this logic exists once. So is the
record of each call that the run's trace keeps (`record`, from trace.Attempt).
"""

from collections.abc import Callable
from typing import Any

from . import schemas

NAME = "submit_result"
DESCRIPTION = (
    "Submit your result for this task. The task isn't finished until a call succeeds. "
    "If the call returns errors, fix them and call it again."
)
FINISH = (
    "When you're done, call the `submit_result` tool with your result. The task isn't complete "
    "until that call succeeds. If it returns errors, fix them and call it again."
)
REMINDER = "You haven't called submit_result yet. Call it now with your result."
# A continued session already holds "Result recorded" from an earlier pass
NEW_RESULT = "This is a new pass of the task: when you're done, call submit_result again. Earlier results don't count for it."
REMINDERS = 2


class ResultTool:
    """submit_result for one attempt. `record(arguments, problems, reply)` hears of every call."""

    def __init__(self, schema: dict, record: Callable[[Any, list[str], str], None] | None = None):
        self.schema = schema
        self.result: dict | None = None
        self.record = record or (lambda arguments, problems, reply: None)

    def submit(self, args: Any) -> tuple[str, bool]:
        """Returns (text for the agent, is_error)."""
        if self.result is not None:
            reply = "A result was already recorded, so this call was ignored. You're done."
            self.record(args, ["a result was already recorded"], reply)
            return reply, True
        problems = schemas.check(args, self.schema)
        if problems:
            listed = "\n".join(f"- {p}" for p in problems)
            reply = ("Error: the result doesn't match the schema, so it wasn't recorded. "
                     f"Fix these and call submit_result again:\n{listed}")
            self.record(args, problems, reply)
            return reply, True
        self.result = args
        reply = "Result recorded. You're done."
        self.record(args, [], reply)
        return reply, False
