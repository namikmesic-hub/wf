"""A fake Claude Code for tests, in place of the Claude Agent SDK's client.

It plays scripted turns the way Claude Code 2.1 runs them with --replay-user-messages, as live
sessions showed:
- it repeats each user message, with its uuid, when it reads it.
- it reads a message that comes while a turn runs at the turn's next step, after a tool's result.
- a message still unread when the turn ends starts a turn of its own, with a result of its own.

    fake = FakeClaude(folder, *turns)
    fake.install(monkeypatch)        claude_code.options hands the client the SessionSpec itself

Each turn is a list of actions:
    {"tool": "Read"}                 a step: a tool call and its result
    {"submit": {...}}                a step that calls submit_result with these arguments
    {"pause": "p"}                   write <folder>/p.paused, and wait until <folder>/p.go is there
Every turn ends with a ResultMessage. `fake.read` lists the user messages it read, in order.
"""

import asyncio
import itertools
import time
from pathlib import Path

from claude_agent_sdk import AssistantMessage, ResultMessage, ToolResultBlock, ToolUseBlock, UserMessage

from workflow.harnesses import claude_code


class FakeClaude:
    def __init__(self, folder: Path, *turns: list[dict]):
        self.folder, self.turns = folder, list(turns)
        self.read: list[str] = []
        self.cost = 0.0  # the session's running total, as Claude Code reports it

    def install(self, monkeypatch) -> None:
        monkeypatch.setattr(claude_code, "options", lambda spec: spec)
        monkeypatch.setattr(claude_code, "ClaudeSDKClient", lambda spec: _Client(self, spec))


class _Client:
    def __init__(self, fake: FakeClaude, spec):
        self.fake, self.spec = fake, spec
        self.inbox: asyncio.Queue = asyncio.Queue()  # user messages written and not read yet
        self.out: asyncio.Queue = asyncio.Queue()
        self.ids = itertools.count(1)
        self.task: asyncio.Task | None = None

    async def connect(self) -> None:
        self.task = asyncio.create_task(self._run())

    async def query(self, prompt) -> None:
        async for message in prompt:
            self.inbox.put_nowait(message)

    async def receive_messages(self):
        while True:
            yield await self.out.get()

    async def disconnect(self) -> None:
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)

    async def _run(self) -> None:
        while True:
            self._read(await self.inbox.get())
            for action in self.fake.turns.pop(0) if self.fake.turns else []:
                if "pause" in action:
                    await self._pause(action["pause"])
                    continue
                name, arguments = ("mcp__wf__submit_result", action["submit"]) if "submit" in action else (action["tool"], {})
                call = f"call-{next(self.ids)}"
                self.out.put_nowait(AssistantMessage(content=[ToolUseBlock(id=call, name=name, input=arguments)], model="fake"))
                text, is_error = self.spec.tool.submit(arguments) if "submit" in action else ("ok", False)
                self.out.put_nowait(UserMessage(content=[ToolResultBlock(tool_use_id=call, content=text, is_error=is_error)]))
                while not self.inbox.empty():  # what came during the step, read before the next one
                    self._read(self.inbox.get_nowait())
            self.fake.cost += 0.01
            self.out.put_nowait(ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
                                              num_turns=1, session_id="fake-session", total_cost_usd=self.fake.cost,
                                              usage={"input_tokens": 100, "output_tokens": 10}))

    def _read(self, message: dict) -> None:
        text = message["message"]["content"]
        self.fake.read.append(text)
        self.out.put_nowait(UserMessage(content=text, uuid=message["uuid"]))

    async def _pause(self, name: str) -> None:
        (self.fake.folder / f"{name}.paused").touch()
        deadline = time.monotonic() + 30
        while not (self.fake.folder / f"{name}.go").exists() and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
