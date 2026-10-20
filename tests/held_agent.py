"""A `wf` process whose claude-code agent is a fake that holds its session until told to let go.

    python held_agent.py <marks folder> <release file> <wf arguments>...

When its session is live, the agent reads notes.txt (an agent_tool event), writes
<marks>/<pid>.started, waits until the release file exists, writes
<marks>/<pid>.ended, then submits a summary.
Each mark holds time.time(). test_capacity.py starts several of these to see
which sessions were live together.
"""

import asyncio
import os
import sys
import time
from pathlib import Path

sys.path.insert(1, str(Path(__file__).resolve().parent.parent))  # wf itself; this folder is first already

from fakes import Fake
from workflow import cli, harnesses
from workflow.harnesses.base import TurnEnd, Usage


def held(marks: Path, release: Path):
    async def turn(session):
        mark = marks / str(os.getpid())
        session.spec.emit({"event": "agent_tool", "tool": "Read", "detail": "notes.txt"})
        Path(f"{mark}.started").write_text(str(time.time()))
        while not release.exists():
            await asyncio.sleep(0.05)
        Path(f"{mark}.ended").write_text(str(time.time()))
        session.spec.tool.submit({"summary": "held"})
        return TurnEnd("done", usage=Usage(10, 0.0))
    return turn


if __name__ == "__main__":
    marks, release, *args = sys.argv[1:]
    harnesses.FACTORIES["claude-code"] = Fake("claude-code", held(Path(marks), Path(release))).factory
    sys.exit(cli.main(args))
