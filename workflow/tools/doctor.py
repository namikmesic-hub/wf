"""Doctor: is a tool ready for agents to use?

check() asks four questions in order and stops at the first one that fails, with the next
step in words:
1. Is there a sign-in in the Keychain?
2. Is it still good? Connecting refreshes a token that has expired, and the server must take it.
3. Does a harmless read call work? Each tool's is its `probe` in catalog.py: who you are, or
   your list of labels.
4. Does wf's list of reads (catalog.py) still fit the server's own list? The report names the
   operations kept back from read-only nodes and the reads the server no longer offers. An
   operation the server marks read-only that wf keeps back is noted; one wf counts as a read
   that the server marks as changing things fails the check. This is how the lists get
   checked against each server at the first sign-in.
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
    reach = "" if keychain.can_write(stored) else " for reads only"
    report.add("secret", f"{how}{reach} in the macOS Keychain, set up {stored.get('set_up', 'at an unknown date')}")
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
    report.add("read call", f"`{name}` answered")
    return _compare(report, tool, server.offered)


def _compare(report: Report, tool: Tool, offered: list) -> Report:
    """Fit wf's list of reads against the operations the server offers, and its own hints."""
    names = {operation.name for operation in offered}
    kept = sorted(names - tool.reads)
    if kept:
        report.add("kept back", f"{', '.join(kept)}: only for nodes with `write: true`")
    if missing := sorted(tool.reads - names):
        report.add("missing", f"{', '.join(missing)}: listed as reads, but not offered")
    marked = {operation.name: operation.annotations for operation in offered if operation.annotations is not None}
    if reads := [name for name in kept if getattr(marked.get(name), "read_only_hint", None) is True]:
        report.add("note", f"{tool.title} marks {', '.join(reads)} read-only, but wf keeps them back: "
                           "if they only read, add them to workflow/tools/catalog.py")
    writes = [name for name in sorted(names & tool.reads) if getattr(marked.get(name), "read_only_hint", None) is False
              or getattr(marked.get(name), "destructive_hint", None) is True]
    if writes:
        return report.failed(f"wf counts {', '.join(writes)} as reads, but {tool.title} marks them as changing things",
                             "update wf: workflow/tools/catalog.py lists them as reads")
    report.status = "pass"
    return report
