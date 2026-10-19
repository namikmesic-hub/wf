"""Doctor: is a tool ready for agents to use?

check() asks three questions in order and stops at the first one that fails, with the next
step in words:
1. Is there a sign-in in the Keychain?
2. Is it still good? Connecting refreshes a token that has expired, and the server must take it.
3. Does a harmless read call work? Each tool's is its `probe` in catalog.py: who you are, or
   your list of labels.
"""

import asyncio
from dataclasses import dataclass, field
from typing import Literal

from ..spec import ToolUse
from . import keychain, relay
from .catalog import Tool
from .signin import NotSetUp, Unreachable


@dataclass
class Report:
    tool: Tool
    status: Literal["pass", "fail", "not set up"] = "fail"
    lines: list[tuple[str, str]] = field(default_factory=list)  # (what, finding)

    def add(self, what: str, finding: str) -> "Report":
        self.lines.append((what, finding))
        return self

    def failed(self, problem: str, next_step: str) -> "Report":
        return self.add("problem", problem).add("next step", next_step)

    def text(self) -> str:
        return "\n".join([f"{self.tool.name}: {self.status}"] + [f"  {what:<10} {finding}" for what, finding in self.lines])


async def check(tool: Tool) -> Report:
    report = Report(tool)
    try:
        stored = await asyncio.to_thread(keychain.load, tool.name)
    except keychain.Unavailable as error:
        return report.failed(str(error), f"unlock your login Keychain, then run `wf tools {tool.name} doctor` again")
    if stored is None:
        report.status = "not set up"
        return report.add("next step", f"run `wf tools {tool.name} setup`")
    how = "a pasted token" if stored.get("method") == "token" else "an OAuth sign-in"
    report.add("secret", f"{how} in the macOS Keychain, set up {stored.get('set_up', 'at an unknown date')}")
    again = f"run `wf tools {tool.name} doctor` again"
    try:
        async with relay.attach([ToolUse(name=tool.name)]) as (server,):
            report.add("sign-in", f"accepted by {tool.url}, which offers {len(server.operations)} read operations")
            name, arguments = tool.probe
            if name not in {operation.name for operation in server.operations}:
                return report.failed(f"{tool.title} no longer offers `{name}`",
                                     "update wf: its list of the operations that only read may be out of date")
            content, is_error = await server.call(name, arguments)
    except NotSetUp as error:
        return report.failed(error.problem, error.fix)
    except keychain.Unavailable as error:
        return report.failed(str(error), f"unlock your login Keychain, then {again}")
    except Unreachable as error:
        return report.failed(str(error), f"check your network connection, then {again}")
    if is_error:
        said = " ".join(" ".join(block.get("text", "") for block in content).split())
        return report.failed(f"the read call `{name}` failed: {said[:300]}",
                             f"check what {tool.title} needs in docs/tools.md, then {again}")
    report.status = "pass"
    return report.add("read call", f"`{name}` answered")
