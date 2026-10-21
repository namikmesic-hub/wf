"""The Claude Code adapter: settings per access level, messages from wf steer against a fake
Claude Code (fake_claude.py), and (opt-in) one real run.

The real run costs a little quota. Run it with:  uv run pytest -m harness
"""

import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from claude_agent_sdk import AssistantMessage, ResultMessage, ToolResultBlock, ToolUseBlock, UserMessage
from fake_claude import FakeClaude
from steering import STEER, at_pause, left_in_mailbox, steer_events, steer_lines
from workflow import cli, runner, schemas, steer
from workflow.harnesses.base import SessionSpec, Unavailable, Usage
from workflow.harnesses.claude_code import GIT, REPLAY, TOOL, ClaudeCode, ClaudeSession, options
from workflow.output_tool import FINISH, ResultTool, REMINDER


def spec(tmp_path, access="read-only", web=False, **extra) -> SessionSpec:
    tool = ResultTool(schemas.object_schema({"summary": {"type": "string"}}))
    return SessionSpec(cwd=tmp_path, access=access, web=web, model="claude-opus-5-5", effort="high",
                       tool=tool, log=tmp_path / "attempt-1.log", emit=lambda e: None, **extra)


def test_read_only_reads_and_searches_and_gets_git_without_a_shell(tmp_path, monkeypatch):
    monkeypatch.delenv("WF_CLAUDE_AUTH", raising=False)
    o = options(spec(tmp_path))
    assert o.tools == ["Read", "Grep", "Glob"]  # no Bash at all
    assert o.allowed_tools == ["Read", "Grep", "Glob", GIT, TOOL]
    assert o.permission_mode == "dontAsk"  # anything not allowed is refused, never asked about


def test_edit_adds_editing_and_any_shell_command(tmp_path):
    o = options(spec(tmp_path, access="edit"))
    assert o.tools == ["Read", "Grep", "Glob", "Edit", "Write", "Bash"]
    assert o.allowed_tools == ["Read", "Grep", "Glob", "Edit", "Write", "Bash", TOOL]
    assert o.permission_mode == "dontAsk"


def test_full_has_no_restrictions_except_web(tmp_path):
    o = options(spec(tmp_path, access="full"))
    assert o.tools is None  # Claude Code's whole default tool set
    assert o.permission_mode == "bypassPermissions"
    assert o.disallowed_tools == ["WebSearch", "WebFetch"]
    assert options(spec(tmp_path, access="full", web=True)).disallowed_tools == []


def test_web_adds_search_and_fetch(tmp_path):
    o = options(spec(tmp_path, web=True))
    assert o.tools[-2:] == ["WebSearch", "WebFetch"]
    assert o.allowed_tools[-3:] == ["WebSearch", "WebFetch", TOOL]


def test_every_session_sets_everything_explicitly(tmp_path, monkeypatch):
    monkeypatch.delenv("WF_CLAUDE_AUTH", raising=False)
    o = options(spec(tmp_path, max_turns=30, budget_usd=2.5))
    assert (o.cwd, o.model, o.effort, o.max_turns, o.max_budget_usd) == (tmp_path, "claude-opus-5-5", "high", 30, 2.5)
    assert o.setting_sources == ["project"]  # the repo's CLAUDE.md, not your user settings
    assert o.system_prompt == {"type": "preset", "preset": "claude_code"}
    assert list(o.mcp_servers) == ["wf"]
    assert o.env == {"ANTHROPIC_API_KEY": "", "ANTHROPIC_AUTH_TOKEN": ""}  # use the subscription login
    assert o.extra_args == {REPLAY: None}  # Claude Code says which messages it read: wf steer needs that


def test_without_project_instructions_no_setting_source_loads(tmp_path):
    o = options(spec(tmp_path, project_instructions=False))
    assert o.setting_sources == []  # no CLAUDE.md, AGENTS.md, .claude/rules, skills or settings from the repo
    assert o.system_prompt == {"type": "preset", "preset": "claude_code"}  # Claude Code's own prompt stays


def test_api_key_auth_can_be_chosen(tmp_path, monkeypatch, wf_home):
    monkeypatch.setenv("WF_CLAUDE_AUTH", "api-key")
    assert options(spec(tmp_path)).env == {}
    monkeypatch.delenv("WF_CLAUDE_AUTH")
    (wf_home / "config.toml").write_text('[claude]\nauth = "api-key"\n')
    assert options(spec(tmp_path)).env == {}
    monkeypatch.setenv("WF_CLAUDE_AUTH", "subscription")  # the environment wins
    assert options(spec(tmp_path)).env == {"ANTHROPIC_API_KEY": "", "ANTHROPIC_AUTH_TOKEN": ""}


def test_opening_finds_the_claude_code_program_as_the_sdk_does(tmp_path, monkeypatch):
    from claude_agent_sdk import CLINotFoundError
    from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport

    assert callable(SubprocessCLITransport._find_cli)  # the SDK still has the lookup that open() uses
    asyncio.run(ClaudeCode().open(tmp_path))  # this SDK brings its own copy of the program

    def not_found(self):
        raise CLINotFoundError("Claude Code not found. Install with:\n  npm install -g @anthropic-ai/claude-code")

    monkeypatch.setattr(SubprocessCLITransport, "_find_cli", not_found)
    with pytest.raises(Unavailable, match="Claude Code isn't installed: Claude Code not found. Install with:$"):
        asyncio.run(ClaudeCode().open(tmp_path))

    program = tmp_path / "claude"  # a file in a folder where the SDK looks, but not a program you may run
    program.write_text("#!/bin/sh\n")
    monkeypatch.setattr(SubprocessCLITransport, "_find_cli", lambda self: str(program))
    with pytest.raises(Unavailable, match="Claude Code can't run: .*claude isn't a program that you may run"):
        asyncio.run(ClaudeCode().open(tmp_path))
    program.chmod(0o755)
    asyncio.run(ClaudeCode().open(tmp_path))


# ---------- wf steer ----------


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / "repo").mkdir()
    monkeypatch.chdir(tmp_path / "repo")
    monkeypatch.setattr(runner, "interactive", lambda: False)
    subprocess.run(["git", "init", "-q"], check=True)
    return tmp_path


def run_flow() -> int:
    Path("flow.yaml").write_text("""
version: 1
name: cc
defaults: {harness: claude-code, harnesses: {claude-code: {model: opus}}}
start: {input: {}}
nodes:
  ask: {needs: [start], instruction: Say hi}
end:
  output:
    out: "{{ ask.summary }}"
    tokens: {type: integer, value: "{{ run.tokens }}"}
    cost: {type: number, value: "{{ run.cost_usd }}"}
""")
    return cli.main(["run", "flow.yaml"])


GOOD = {"submit": {"summary": "hi"}}


def test_claude_code_reads_a_message_from_wf_steer_at_its_next_step(repo, monkeypatch):
    claude = FakeClaude(repo, [{"tool": "Read"}, {"pause": "p"}, {"tool": "Bash"}, GOOD])
    claude.install(monkeypatch)
    steering = at_pause(repo, "p")
    assert run_flow() == 0
    done = steering.result()
    assert done.returncode == 0, done.stderr
    assert done.stdout.startswith("Queued for ask (attempt 1 on claude-code)")
    assert claude.read == [f"Say hi\n\n{FINISH}", STEER]  # in the same turn: no turn of its own
    queued, delivered = steer_events()
    assert (queued["status"], queued["text"], queued["harness"]) == ("queued", STEER, "claude-code")
    assert (delivered["status"], delivered["id"]) == ("delivered", queued["id"])
    assert [line["status"] for line in steer_lines()] == ["queued", "delivered"]
    assert left_in_mailbox() == []


def test_a_message_claude_code_hasnt_read_when_the_agent_submits_is_dropped(repo, monkeypatch):
    claude = FakeClaude(repo, [GOOD, {"pause": "p"}])  # no step after the message comes
    claude.install(monkeypatch)
    steering = at_pause(repo, "p")
    assert run_flow() == 0
    assert steering.result().returncode == 0
    queued, dropped = steer_events()
    assert (queued["status"], dropped["status"], dropped["id"]) == ("queued", "dropped", queued["id"])
    assert dropped["reason"] == "the agent submitted its result before it read the message"


def test_a_message_after_the_last_step_gets_a_turn_of_its_own_and_no_reminder_goes(repo, monkeypatch):
    claude = FakeClaude(repo, [{"pause": "p"}], [GOOD])
    claude.install(monkeypatch)
    steering = at_pause(repo, "p")
    assert run_flow() == 0
    assert steering.result().returncode == 0
    assert claude.read == [f"Say hi\n\n{FINISH}", STEER]
    assert REMINDER not in claude.read
    assert [e["status"] for e in steer_events()] == ["queued", "delivered"]
    (folder,) = (Path(".wf") / "runs").iterdir()
    results = [json.loads(line) for line in (folder / "nodes/ask/1/attempt-1.log").read_text().splitlines()
               if json.loads(line)["kind"] == "result"]
    assert len(results) == 2  # one send() read both turns' results
    out = json.loads((folder / "output.json").read_text())
    assert (out["tokens"], round(out["cost"], 6)) == (220, 0.02)  # and counted both


def trace(kind: str) -> list[dict]:
    (folder,) = (Path(".wf") / "runs").iterdir()
    return [r for r in map(json.loads, (folder / "trace.jsonl").read_text().splitlines()) if r["kind"] == kind]


def test_the_trace_keeps_the_parts_of_the_tokens_and_the_thinking_that_each_result_reports(repo, monkeypatch):
    counts = {"input_tokens": 2, "cache_creation_input_tokens": 33476, "cache_read_input_tokens": 1200, "output_tokens": 271,
              "output_tokens_details": {"thinking_tokens": 186}}
    FakeClaude(repo, [{"tool": "Read"}], [GOOD], usage=counts).install(monkeypatch)  # a reminder: two turns
    assert run_flow() == 0
    each = {"tokens": 34949, "cost_usd": 0.01, "input_tokens": 2, "output_tokens": 271, "cache_read_tokens": 1200,
            "cache_write_tokens": 33476, "reasoning_tokens": 186}
    assert [r["usage"] for r in trace("turn_end")] == [pytest.approx(each), pytest.approx(each)]
    (attempt,) = trace("attempt_end")
    assert attempt["usage"] == pytest.approx({name: 2 * value for name, value in each.items()})


class _Writer:
    """A client that keeps what is written to Claude Code, and checks that the message is held first."""

    def __init__(self, session):
        self.session, self.written = session, []

    async def query(self, prompt):
        async for message in prompt:
            assert message["uuid"] in self.session._held  # Claude Code can say it read it before the write returns
            self.written.append(message)


class _Broken:
    async def query(self, prompt):
        raise OSError("Claude Code is gone")


def test_claude_code_takes_a_message_only_mid_turn_once_it_repeated_one(tmp_path):
    session = ClaudeSession(spec(tmp_path))
    session._client = _Writer(session)
    steer = lambda ident: asyncio.run(session.steer("Stop", ident))
    assert steer("id-1") is None  # between turns: the runner sends it as the next turn
    session._working = True
    assert steer("id-2") is None  # Claude Code hasn't shown yet that it repeats what it reads
    session._repeats = True
    assert steer("id-3") == "queued"
    assert [m["uuid"] for m in session._client.written] == ["id-3"] and session._held == ["id-3"]
    session._client = _Broken()
    with pytest.raises(OSError):
        steer("id-4")
    assert session._held == ["id-3"]  # a message that wasn't written isn't waited for


@pytest.mark.harness
def test_a_real_read_only_agent_node_on_claude_code(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    git = lambda *a: subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *a], check=True, capture_output=True)
    git("init", "-q")
    Path("notes.txt").write_text("The secret word is: marmalade\n")
    git("add", "notes.txt")
    git("commit", "-qm", "first")
    Path("notes.txt").write_text("The secret word is: quince\n")  # only `git diff` shows the old one
    Path("flow.yaml").write_text("""
version: 1
name: real
defaults:
  harness: claude-code
  harnesses:
    claude-code: {model: claude-haiku-5-5, effort: low}
start: {input: {}}
nodes:
  read:
    needs: [start]
    instruction: Use git to see what changed in notes.txt since the last commit. Report the old and the new secret word.
    output:
      old: {type: string, pattern: '^[a-z]+(?!\\n)$', description: The old secret word in lowercase and nothing else}
      new: {type: string, pattern: '^[a-z]+(?!\\n)$', description: The new secret word in lowercase and nothing else}
end:
  output:
    old: "{{ read.old }}"
    new: "{{ read.new }}"
    cost: {type: number, value: "{{ run.cost_usd }}"}
""")
    assert cli.main(["run", "flow.yaml"]) == 0, capsys.readouterr()
    (folder,) = Path(".wf/runs").iterdir()
    out = json.loads((folder / "output.json").read_text())
    assert (out["old"], out["new"]) == ("marmalade", "quince")  # (?!\n) because Python's $ also matches before a final newline
    assert out["cost"] > 0
    log = [json.loads(line) for line in (folder / "nodes" / "read" / "1" / "attempt-1.log").read_text().splitlines()]
    assert any(e["kind"] == "tool" and e["name"] == GIT for e in log)  # it used the runner's git tool
    assert Path("notes.txt").read_text() == "The secret word is: quince\n"  # and changed nothing


@pytest.mark.harness
def test_a_real_node_without_project_instructions_does_not_get_the_repos_claude_md(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    git = lambda *a: subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *a], check=True, capture_output=True)
    git("init", "-q")
    Path("CLAUDE.md").write_text("The code word for this project is: periwinkle\n")
    git("add", "CLAUDE.md")
    git("commit", "-qm", "first")
    ask = ("Don't read, search or open any file, and use no tool but submit_result. From what you were told alone, "
           "what is the code word for this project? Submit none if you weren't given one.")
    Path("flow.yaml").write_text(f"""
version: 1
name: real
defaults:
  harness: claude-code
  harnesses:
    claude-code: {{model: claude-haiku-5-5, effort: low}}
start: {{input: {{}}}}
nodes:
  told:
    needs: [start]
    instruction: "{ask}"
    output: {{word: {{type: string}}}}
  kept_out:
    needs: [start]
    project_instructions: false
    instruction: "{ask}"
    output: {{word: {{type: string}}}}
end:
  output:
    told: "{{{{ told.word }}}}"
    kept_out: "{{{{ kept_out.word }}}}"
""")
    assert cli.main(["run", "flow.yaml"]) == 0, capsys.readouterr()
    (folder,) = Path(".wf/runs").iterdir()
    out = json.loads((folder / "output.json").read_text())
    assert out["told"].strip(" .").lower() == "periwinkle"  # the repo's CLAUDE.md reaches a node by default
    assert "periwinkle" not in out["kept_out"].lower()  # and not one with the field off, whose instruction still did
    log = [json.loads(line) for line in (folder / "nodes" / "kept_out" / "1" / "attempt-1.log").read_text().splitlines()]
    assert not any(e["kind"] == "tool" and "CLAUDE.md" in json.dumps(e["input"]) for e in log)  # it didn't read the file either


class _Stream:
    """A client whose messages come from a queue the test fills, and whose writes can fail later."""

    def __init__(self, session, fail_after: float | None = None):
        self.session, self.fail_after, self.out = session, fail_after, asyncio.Queue()

    async def query(self, prompt):
        async for message in prompt:
            if self.fail_after is not None:
                await asyncio.sleep(self.fail_after)
                raise OSError("Claude Code is gone")

    async def receive_messages(self):
        while True:
            yield await self.out.get()


def _result():
    return ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1, session_id="s")


def test_a_message_claude_code_reads_during_its_write_is_recorded_once_as_delivered(tmp_path):
    events = []
    session = ClaudeSession(spec(tmp_path))
    session.spec.emit = events.append
    session._working = session._repeats = True
    stream = session._client = _Stream(session)

    async def read_at_once(prompt):  # Claude Code reads it, and says so, before the write returns
        async for message in prompt:
            stream.out.put_nowait(UserMessage(content="Stop", uuid=message["uuid"]))
            await asyncio.sleep(0.05)

    stream.query = read_at_once

    async def drive():
        reading = asyncio.get_running_loop().create_task(session._read("the prompt"))
        status = await session.steer("Stop", "id-1")
        stream.out.put_nowait(_result())
        await asyncio.wait_for(reading, 2)
        return status

    assert asyncio.run(drive()) == "delivered"
    assert events == [] and session._held == []  # steer.py records it once, from the answer


def test_a_write_that_fails_after_the_turns_result_doesnt_leave_the_turn_waiting(tmp_path):
    session = ClaudeSession(spec(tmp_path))
    session._working = session._repeats = True
    session._client = _Stream(session, fail_after=0.2)

    async def drive():
        writing = asyncio.get_running_loop().create_task(session.steer("Stop", "id-1"))
        await asyncio.sleep(0.05)  # the write is under way when the turn's result comes
        session._client.out.put_nowait(_result())
        status = await asyncio.wait_for(session._read("the prompt"), 2)
        with pytest.raises(OSError):
            await writing
        return status

    assert asyncio.run(drive()) == ("done", "")
    assert session._held == []


@pytest.mark.parametrize("ending, expected", [
    ("submitted", ("done", "")),
    ("failed", ("failed", "Claude Code: it broke")),
])
def test_a_result_that_ends_the_attempt_doesnt_wait_for_a_write_that_stalls(tmp_path, ending, expected):
    session = ClaudeSession(spec(tmp_path))
    session._working = session._repeats = True
    session._client = _Stream(session, fail_after=60)  # the write never returns

    async def drive():
        writing = asyncio.get_running_loop().create_task(session.steer("Stop", "id-1"))
        await asyncio.sleep(0.05)
        out = session._client.out
        if ending == "submitted":
            out.put_nowait(AssistantMessage(content=[ToolUseBlock(id="call-1", name=TOOL, input={"summary": "hi"})],
                                            model="fake"))
            out.put_nowait(UserMessage(content=[ToolResultBlock(tool_use_id="call-1", content="ok", is_error=False)]))
            out.put_nowait(_result())
        else:
            out.put_nowait(ResultMessage(subtype="error_during_execution", duration_ms=1, duration_api_ms=1,
                                         is_error=True, num_turns=1, session_id="s", result="it broke"))
        status = await asyncio.wait_for(session._read("the prompt"), 2)
        writing.cancel()
        await asyncio.gather(writing, return_exceptions=True)
        return status

    assert asyncio.run(drive()) == expected
    assert session._held == []


@pytest.mark.parametrize("write_takes", [0.05, 60])  # ends during the teardown, or is cut off by it
def test_a_message_claude_code_read_stays_delivered_when_the_result_comes_during_its_write(tmp_path, monkeypatch,
                                                                                           write_takes):
    monkeypatch.setattr(steer, "HANDOVER_SECONDS", 0.2)
    events = []
    session = ClaudeSession(spec(tmp_path))
    session.spec.emit = events.append
    session._working = session._repeats = True
    stream = session._client = _Stream(session)

    async def read_then_submit(prompt):  # Claude Code reads it, the agent submits, and the write goes on
        async for message in prompt:
            stream.out.put_nowait(UserMessage(content="Stop", uuid=message["uuid"]))
            stream.out.put_nowait(AssistantMessage(content=[ToolUseBlock(id="call-1", name=TOOL, input={"summary": "hi"})],
                                                   model="fake"))
            stream.out.put_nowait(UserMessage(content=[ToolResultBlock(tool_use_id="call-1", content="ok", is_error=False)]))
            stream.out.put_nowait(_result())
            await asyncio.sleep(write_takes)

    stream.query = read_then_submit

    async def drive():
        live = steer.Live(session.spec, 1, "claude-code", session)
        live.start()
        reading = asyncio.get_running_loop().create_task(session._read("the prompt"))
        answer = asyncio.get_running_loop().create_task(live.take(steer.Message("id-1", "Stop")))
        assert await asyncio.wait_for(reading, 2) == ("done", "")  # the result ends the turn before the write
        await live.stop()
        live.end()
        return await answer

    answer = asyncio.run(drive())
    assert answer == {"status": "delivered", "attempt": 1, "harness": "claude-code"}
    assert [(e["id"], e["status"]) for e in events if e["event"] == "steer"] == [("id-1", "delivered")]
    assert session._held == []


def test_a_turn_cut_off_after_a_message_from_wf_steer_still_counts_the_result_before_it(tmp_path):
    session = ClaudeSession(spec(tmp_path))
    session._repeats = True
    stream = session._client = _Stream(session)

    async def drive():
        sending = asyncio.get_running_loop().create_task(session.send("Say hi"))
        await asyncio.sleep(0.05)
        assert await session.steer("Stop", "id-1") == "queued"
        first = _result()  # the first turn ends: the message gets a turn of its own
        first.usage, first.total_cost_usd = {"input_tokens": 100, "output_tokens": 10}, 0.01
        stream.out.put_nowait(first)
        stream.out.put_nowait(AssistantMessage(content=[], model="fake", usage={"input_tokens": 50}, message_id="m-2"))
        await asyncio.sleep(0.05)
        sending.cancel()  # the node's timeout cuts off the message's turn
        await asyncio.gather(sending, return_exceptions=True)
        return session.pending_usage()

    usage = asyncio.run(drive())
    assert usage == Usage(160, 0.01, input_tokens=150, output_tokens=10)  # and the parts of both
