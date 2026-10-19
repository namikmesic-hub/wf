"""wf tools in agent sessions: which operations each node gets, how each harness attaches them
and relays their calls, what happens when a tool fails during a run, what transcripts keep,
and that no secret reaches an agent. test_tools.py covers setup, sign-ins and doctor.

Every test runs against FakeVendor (fake_vendor.py), with the in-memory keyring from
conftest.py, and fake Claude Code, Codex (fake_codex.py) and Cursor (fake_acp.py) agents. The
opt-in test at the bottom runs real agents: uv run pytest -m harness tests/test_tool_sessions.py
"""

import asyncio
import json
import os
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from textwrap import dedent

import pytest
import yaml
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, ToolResultBlock, ToolUseBlock, UserMessage
from mcp import Client

from fake_vendor import FakeVendor, free_port, png_base64, sign_in
from fakes import Fake, submit
from workflow import cli, harnesses, runner, schemas
from workflow.harnesses.base import SessionSpec, TurnEnd, Usage
from workflow.harnesses.claude_code import ClaudeSession, options
from workflow.harnesses.codex import Codex
from workflow.harnesses.cursor import Cursor, allows
from workflow.output_tool import ResultTool
from workflow.spec import ToolUse
from workflow.tools import relay, signin
from workflow.tools.catalog import TOOLS

FAKE_CODEX = Path(__file__).parent / "fake_codex.py"
FAKE_ACP = Path(__file__).parent / "fake_acp.py"
SECRET = "s3cret-access-token-only-wf-may-see"


@pytest.fixture
def vendor(monkeypatch):
    found = FakeVendor(["list_issues", "get_user", "save_issue", "screenshot"]).start()
    monkeypatch.setitem(TOOLS, "linear", found.tool("linear", reads=TOOLS["linear"].reads | {"screenshot"}))
    yield found
    found.stop()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / "repo").mkdir()
    monkeypatch.chdir(tmp_path / "repo")
    monkeypatch.setattr(runner, "interactive", lambda: False)
    subprocess.run(["git", "init", "-q"], check=True)
    return tmp_path


def flow(tools: str = "[linear]", harness: str = "claude-code", nodes: str = "") -> str:
    Path("flow.yaml").write_text(dedent("""
        version: 1
        name: tooled
        defaults:
          harness: {harness}
          harnesses: {{claude-code: {{model: opus}}, cursor: {{model: composer-2.5}}}}
        start: {{input: {{}}}}
        nodes:
          read:
            needs: [start]
            instruction: Read the open issues
            tools: {tools}
        end: {{output: {{}}}}
        """).format(harness=harness, tools=tools).replace("end: {output: {}}", nodes + "end: {output: {}}"))
    return "flow.yaml"


def run_dir() -> Path:
    (folder,) = (Path(".wf") / "runs").iterdir()
    return folder


def events() -> list[dict]:
    return [json.loads(line) for line in (run_dir() / "events.jsonl").read_text().splitlines()]


def attempt_logs() -> str:
    return "".join(path.read_text() for path in run_dir().rglob("attempt-*.log"))


def install(monkeypatch, fake: Fake) -> Fake:
    monkeypatch.setitem(harnesses.FACTORIES, fake.name, fake.factory)
    return fake


def calls_then_submits(name: str, arguments: dict, before=None):
    """A Fake turn that calls one of the session's tools, then submits a result."""

    async def turn(session):
        if before:
            before()
        session.fake.replies.append(await session.spec.tools[0].call(name, arguments))
        session.fake.replies.append(session.spec.tool.submit({"summary": "done"}))
        return TurnEnd("done", usage=Usage(10, 0.0))

    return turn


def attached(uses: list, act):
    """Run `act(servers)` inside relay.attach(uses)."""

    async def run():
        async with relay.attach(uses) as servers:
            return await act(servers)

    return asyncio.run(run())


# ---------- which operations a node gets ----------


def test_each_node_gets_its_own_tools_and_only_their_reads_unless_it_opts_in(repo, monkeypatch):
    vendor = FakeVendor(["list_issues", "get_user", "save_issue", "archive_everything"]).start()
    monkeypatch.setitem(TOOLS, "linear", vendor.tool("linear"))
    try:
        sign_in(vendor)
        fake = install(monkeypatch, Fake("claude-code", *[submit({"summary": "ok"})] * 3))
        flow("[linear]", nodes=("  write:\n    needs: [read]\n    instruction: File the issue\n"
                                "    tools: [{name: linear, write: true}]\n"
                                "  plain:\n    needs: [write]\n    instruction: Sum up\n"))
        assert cli.main(["run", "flow.yaml"]) == 0
    finally:
        vendor.stop()
    found = [{server.name: [op.name for op in server.operations] for server in spec.tools} for spec in fake.specs]
    assert found == [
        {"linear": ["list_issues", "get_user"]},  # a new operation counts as a write
        {"linear": ["list_issues", "get_user", "save_issue", "archive_everything"]},
        {},
    ]


def test_a_read_only_node_cannot_call_a_write(vendor):
    sign_in(vendor)
    content, is_error = attached([ToolUse(name="linear")], lambda servers: servers[0].call("save_issue", {"query": "x"}))
    assert is_error and content[0]["text"] == "Error: `save_issue` isn't one of the Linear operations this step may use."
    assert vendor.calls == []  # it never reached Linear


def test_every_page_of_a_servers_operations_is_attached(monkeypatch):
    vendor = FakeVendor(["list_issues", "get_user", "get_issue", "list_teams", "list_users"], page_size=2).start()
    monkeypatch.setitem(TOOLS, "linear", vendor.tool("linear"))
    try:
        sign_in(vendor)

        async def names(servers):
            return [op.name for op in servers[0].operations]

        assert attached([ToolUse(name="linear")], names) == ["list_issues", "get_user", "get_issue", "list_teams", "list_users"]
    finally:
        vendor.stop()


def test_an_image_the_vendor_returns_reaches_the_agent_as_an_image(vendor):
    sign_in(vendor)
    content, is_error = attached([ToolUse(name="linear")], lambda servers: servers[0].call("screenshot", {}))
    assert (content, is_error) == ([{"type": "image", "data": png_base64(), "mimeType": "image/png"}], False)


# ---------- when a tool fails ----------


def test_a_tool_that_is_not_set_up_fails_the_node_without_a_retry(repo, monkeypatch, capsys):
    fake = install(monkeypatch, Fake("claude-code"))
    assert cli.main(["run", flow("[linear]")]) == 1
    assert "read: Linear isn't set up: run `wf tools linear setup`" in capsys.readouterr().err
    assert fake.specs == []  # no session started
    assert not [e for e in events() if e["event"] == "attempt_failed"]


@pytest.mark.parametrize("settings, status", [({"mcp_status": 403}, 403), ({"reject_all": True}, 401)])
def test_a_sign_in_the_server_refuses_when_connecting_fails_the_node_without_a_retry(settings, status, repo, monkeypatch, capsys):
    vendor = FakeVendor(**settings).start()
    monkeypatch.setitem(TOOLS, "linear", vendor.tool("linear"))
    try:
        sign_in(vendor)
        install(monkeypatch, Fake("claude-code"))
        assert cli.main(["run", flow("[linear]")]) == 1
    finally:
        vendor.stop()
    assert f"read: Linear refused the stored sign-in (HTTP {status}): run `wf tools linear setup`" in capsys.readouterr().err
    assert not [e for e in events() if e["event"] == "attempt_failed"]


def test_a_server_that_does_not_answer_fails_the_attempt_and_the_retry_tries_again(vendor, repo, monkeypatch):
    sign_in(vendor)
    monkeypatch.setitem(TOOLS, "linear", vendor.tool("linear", url=f"http://127.0.0.1:{free_port()}/mcp"))
    install(monkeypatch, Fake("claude-code"))
    assert cli.main(["run", flow("[linear]")]) == 1
    failed = [e for e in events() if e["event"] == "attempt_failed"]
    assert [e["attempt"] for e in failed] == [1, 2]
    assert failed[0]["error"].startswith("can't use Linear's server at http://127.0.0.1:")


def test_the_failure_that_setup_fixes_wins_when_two_tools_fail(vendor, monkeypatch):
    sign_in(vendor)
    monkeypatch.setitem(TOOLS, "linear", vendor.tool("linear", url=f"http://127.0.0.1:{free_port()}/mcp"))
    with pytest.raises(signin.NotSetUp, match="Notion isn't set up"):  # rather than linear's Unreachable
        attached([ToolUse(name="linear"), ToolUse(name="notion")], lambda servers: asyncio.sleep(0))


def test_a_token_refused_in_the_middle_of_a_session_is_refreshed_once(vendor):
    sign_in(vendor)

    async def call(servers):
        vendor.access.clear()  # the token stops working, as an early expiry would
        return await servers[0].call("list_issues", {"query": "open"})

    assert attached([ToolUse(name="linear")], call) == ([{"type": "text", "text": "list_issues answered 'open'"}], False)


def test_requests_refused_with_the_same_token_share_one_refresh(vendor):
    sign_in(vendor)

    async def calls(servers):
        vendor.access.clear()  # both requests go out with the token the server no longer takes
        return await asyncio.gather(servers[0].call("list_issues", {"query": "a"}), servers[0].call("get_user", {"query": "b"}))

    answers = attached([ToolUse(name="linear")], calls)
    assert [is_error for _, is_error in answers] == [False, False]
    assert len(vendor.requests("/token")) == 1


def test_a_sign_in_refused_in_the_middle_of_a_session_says_what_to_run(vendor):
    sign_in(vendor)

    async def call(servers):
        vendor.access.clear()
        vendor.refresh.clear()  # revoked meanwhile
        return await servers[0].call("list_issues", {"query": "open"}), servers[0].failure

    (content, is_error), failure = attached([ToolUse(name="linear")], call)
    assert is_error and content[0]["text"] == (
        "Error: Linear didn't refresh the sign-in (invalid_grant: unknown refresh token): run `wf tools linear setup`")
    assert isinstance(failure, signin.NotSetUp)


def test_a_node_whose_tool_stopped_working_fails_even_with_a_result(vendor, repo, monkeypatch, capsys):
    sign_in(vendor)

    def revoke():
        vendor.access.clear()
        vendor.refresh.clear()

    fake = install(monkeypatch, Fake("claude-code", calls_then_submits("list_issues", {"query": "open"}, before=revoke)))
    assert cli.main(["run", flow("[linear]")]) == 1
    assert fake.replies[-1] == ("Result recorded. You're done.", False)  # the agent went on without the tool
    assert "read: Linear didn't refresh the sign-in" in capsys.readouterr().err
    assert len(fake.specs) == 1  # no retry: setup fixes it


def test_an_operation_that_answers_with_an_error_doesnt_fail_the_node(vendor, repo, monkeypatch):
    sign_in(vendor)
    vendor.errors["list_issues"] = "no such team"
    fake = install(monkeypatch, Fake("claude-code", calls_then_submits("list_issues", {"query": "open"})))
    assert cli.main(["run", flow("[linear]")]) == 0  # the agent read the error, as it would any tool's
    assert fake.replies[0][1] is True and "no such team" in fake.replies[0][0][0]["text"]


def test_a_dry_run_attaches_no_tools(repo, monkeypatch):
    monkeypatch.setitem(harnesses.FACTORIES, "claude-code", lambda: pytest.fail("a dry run called a real harness"))
    Path("script.yaml").write_text(yaml.safe_dump({"agents": {"read": [{"summary": "read them"}]}}))
    assert cli.main(["run", flow("[linear]"), "--dry-run", "script.yaml"]) == 0  # linear isn't even set up


# ---------- Claude Code ----------


def spec(cwd: Path, tools: list, access: str = "read-only") -> SessionSpec:
    return SessionSpec(cwd=cwd, access=access, web=False, model="opus", effort="high",
                       tool=ResultTool(schemas.object_schema({"summary": {"type": "string"}})),
                       log=cwd / "attempt-1.log", emit=lambda event: None, tools=tools)


@pytest.mark.parametrize("access", ["read-only", "edit", "full"])
def test_claude_code_gets_an_mcp_server_per_tool_that_relays_its_calls(vendor, tmp_path, access):
    sign_in(vendor)

    async def check(tools):
        o = options(spec(tmp_path, tools, access))
        async with Client(o.mcp_servers["linear"]["instance"]) as claude:
            listed = [t.name for t in (await claude.list_tools()).tools]
            answer = await claude.call_tool("list_issues", {"query": "mine"})
        return o, listed, answer

    o, listed, answer = attached([ToolUse(name="linear")], check)
    assert list(o.mcp_servers) == ["wf", "linear"]
    assert [name for name in o.allowed_tools if "linear" in name] == [
        "mcp__linear__list_issues", "mcp__linear__get_user", "mcp__linear__screenshot"]
    assert listed == ["list_issues", "get_user", "screenshot"]  # what Claude is told about: no writes, at any access level
    assert answer.content[0].text == "list_issues answered 'mine'"
    assert vendor.calls == [("list_issues", {"query": "mine"})]


class _Server:
    """A ToolServer with one operation whose input schema is a bare object, as some vendors write it."""

    name, description = "linear", "Linear, through wf (read-only)"
    operations = [relay.Operation("whoami", "Who you are", {"type": "object"})]

    async def call(self, name, arguments):
        return [{"type": "text", "text": f"{name} {arguments}"}], False


def test_claude_code_takes_an_object_schema_without_properties(tmp_path):
    async def check():
        o = options(spec(tmp_path, [_Server()]))
        async with Client(o.mcp_servers["linear"]["instance"]) as claude:
            (listed,) = (await claude.list_tools()).tools
            return listed.input_schema, await claude.call_tool("whoami", {})

    schema, answer = asyncio.run(check())
    assert schema.get("required", []) == [] and "type" not in schema.get("properties", {})  # not a parameter named `type`
    assert answer.content[0].text == "whoami {}"


class _Client:
    """A ClaudeSDKClient that plays messages: a call to a wf tool, and its result."""

    def __init__(self, messages):
        self.messages = messages

    async def query(self, text):
        pass

    async def receive_response(self):
        for message in self.messages:
            yield message


def test_claude_code_transcripts_leave_out_what_a_wf_tool_returned(tmp_path):
    session = ClaudeSession(spec(tmp_path, [_Server()]))
    session._client = _Client([
        AssistantMessage(content=[ToolUseBlock(id="t1", name="mcp__linear__whoami", input={"q": "me"}),
                                  ToolUseBlock(id="t2", name="Read", input={"file_path": "a.txt"})], model="opus"),
        UserMessage(content=[ToolResultBlock(tool_use_id="t1", content="private: the CEO's notes"),
                             ToolResultBlock(tool_use_id="t2", content="the file's text")]),
        AssistantMessage(content=[TextBlock(text="done")], model="opus"),
        ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1, session_id="s"),
    ])
    asyncio.run(session.send("go"))
    lines = [json.loads(line) for line in (tmp_path / "attempt-1.log").read_text().splitlines()]
    relayed, read = [line for line in lines if line["kind"] == "tool_result"]
    assert relayed == {"kind": "tool_result", "is_error": False, "kept": False, "characters": 24}
    assert read["content"] == "the file's text"
    assert "private" not in (tmp_path / "attempt-1.log").read_text()
    assert any(line == {"kind": "tool", "name": "mcp__linear__whoami", "input": {"q": "me"}} for line in lines)


def test_progress_names_a_wf_tools_operation_the_same_on_every_harness():
    from workflow.harnesses import claude_code, cursor as cursor_harness

    assert claude_code._tool_name("mcp__linear__list_issues") == "linear.list_issues"
    assert claude_code._tool_name("mcp__wf__submit_result") == "submit_result"
    assert cursor_harness._label({"kind": "other", "rawInput": {"providerIdentifier": "linear", "toolName": "list_issues"}}) == "linear.list_issues"
    assert cursor_harness._label({"kind": "other", "rawInput": {"providerIdentifier": "wf", "toolName": "git"}}) == "git"


# ---------- Codex ----------


def codex(monkeypatch, tmp_path: Path, *turns) -> Path:
    """Use the fake Codex app server; returns the log of everything wf sent it."""
    scenario, log = tmp_path / "scenario.json", tmp_path / "requests.jsonl"
    scenario.write_text(json.dumps({"turns": list(turns)}))
    monkeypatch.setitem(harnesses.FACTORIES, "codex",
                        lambda: Codex(command=[sys.executable, str(FAKE_CODEX), str(scenario), str(log)], request_timeout=10))
    return log


def codex_log(log: Path, kind: str) -> list:
    return [json.loads(line)["data"] for line in log.read_text().splitlines() if json.loads(line)["kind"] == kind]


def test_codex_gets_a_namespace_per_tool_and_its_calls_are_relayed(vendor, repo, monkeypatch):
    sign_in(vendor)
    log = codex(monkeypatch, repo, [{"tool": {"namespace": "linear", "name": "list_issues", "arguments": {"query": "open"}}},
                                    {"tool": {"namespace": "linear", "name": "save_issue", "arguments": {"query": "x"}}},
                                    {"tool": {"namespace": "linear", "name": "screenshot", "arguments": {}}},
                                    {"tool": {"arguments": {"summary": "done"}}}])
    assert cli.main(["run", flow("[linear]", harness="codex")]) == 0
    (thread,) = [e["params"] for e in codex_log(log, "request") if e["method"] == "thread/start"]
    submit_result, linear = thread["dynamicTools"]
    assert (linear["type"], linear["name"], linear["description"]) == ("namespace", "linear", "Linear, through wf (read-only)")
    assert [t["name"] for t in linear["tools"]] == ["list_issues", "get_user", "screenshot"]
    read, write, image, result = codex_log(log, "reply")
    assert read == {"success": True, "contentItems": [{"type": "inputText", "text": "list_issues answered 'open'"}]}
    assert write["success"] is False and "isn't one of the Linear operations" in write["contentItems"][0]["text"]
    assert image == {"success": True, "contentItems": [{"type": "inputImage", "imageUrl": f"data:image/png;base64,{png_base64()}"}]}
    assert result["success"] is True
    assert vendor.calls == [("list_issues", {"query": "open"}), ("screenshot", {"query": ""})]


def test_codex_answers_other_requests_while_a_slow_tool_call_runs(repo, monkeypatch):
    vendor = FakeVendor(["list_issues", "get_user"], delays={"list_issues": 1.0}).start()
    monkeypatch.setitem(TOOLS, "linear", vendor.tool("linear"))
    try:
        sign_in(vendor)
        log = codex(monkeypatch, repo, [
            {"tool": {"namespace": "linear", "name": "list_issues", "arguments": {"query": "slow"}}, "background": True},
            {"approval": "item/commandExecution/requestApproval"},
            {"tool": {"arguments": {"summary": "done"}}}])
        assert cli.main(["run", flow("[linear]", harness="codex")]) == 0
    finally:
        vendor.stop()
    approval, submitted, slow = codex_log(log, "reply")  # in the order they came back
    assert approval == {"decision": "decline"} and submitted["success"] is True
    assert slow["contentItems"][0]["text"] == "list_issues answered 'slow'"


class _Blocked:
    """A ToolServer whose one operation waits until it's cancelled."""

    name, description = "linear", "Linear, through wf (read-only)"
    operations = [relay.Operation("list_issues", "", {"type": "object", "properties": {}})]

    def __init__(self):
        self.started, self.cancelled = asyncio.Event(), False

    async def call(self, name, arguments):
        self.started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            self.cancelled = True
            raise


def test_stopping_codex_cancels_the_tool_calls_it_was_answering(tmp_path):
    scenario, log = tmp_path / "scenario.json", tmp_path / "requests.jsonl"
    scenario.write_text(json.dumps({"turns": [[{"tool": {"namespace": "linear", "name": "list_issues", "arguments": {}}}]]}))
    blocked = _Blocked()

    async def scenario_run():
        harness = Codex(command=[sys.executable, str(FAKE_CODEX), str(scenario), str(log)], request_timeout=10)
        await harness.open(tmp_path)
        session = await harness.session(spec(tmp_path, [blocked]))
        turn = asyncio.ensure_future(session.send("go"))
        await asyncio.wait_for(blocked.started.wait(), 10)
        rpc = harness.rpc
        await harness.close()
        turn.cancel()
        await asyncio.gather(turn, return_exceptions=True)
        return rpc

    rpc = asyncio.run(scenario_run())
    assert blocked.cancelled and not rpc.answering


def test_codex_transcripts_leave_out_what_a_wf_tool_returned(vendor, repo, monkeypatch):
    sign_in(vendor)
    codex(monkeypatch, repo, [{"item": {"type": "dynamicToolCall", "namespace": "linear", "tool": "list_issues",
                                        "arguments": {"query": "open"}, "status": "completed", "success": True,
                                        "contentItems": [{"type": "inputText", "text": "private: the CEO's notes"}]}},
                              {"tool": {"arguments": {"summary": "done"}}}])
    assert cli.main(["run", flow("[linear]", harness="codex")]) == 0
    logs = attempt_logs()
    assert "private" not in logs
    (recorded,) = [json.loads(line) for line in logs.splitlines() if json.loads(line)["kind"] == "tool"]
    assert recorded["item"]["arguments"] == {"query": "open"} and recorded["kept"] is False
    assert recorded["characters"] == len("private: the CEO's notes")
    assert [e["tool"] for e in events() if e["event"] == "agent_tool"][0] == "linear.list_issues"


# ---------- Cursor ----------


def cursor(monkeypatch, tmp_path: Path, *turns) -> Path:
    """Use the fake `cursor-agent`; returns the log of everything wf sent it and answered."""
    scenario, log = tmp_path / "acp-scenario.json", tmp_path / "acp-requests.jsonl"
    scenario.write_text(json.dumps({"turns": list(turns)}))
    monkeypatch.setitem(harnesses.FACTORIES, "cursor",
                        lambda: Cursor(command=[sys.executable, str(FAKE_ACP), str(scenario), str(log)], request_timeout=10))
    return log


def test_cursor_gets_an_mcp_server_per_tool_and_its_calls_are_relayed(vendor, repo, monkeypatch):
    sign_in(vendor)
    log = cursor(monkeypatch, repo, [{"tool": {"server": "linear", "name": "list_issues", "arguments": {"query": "open"}}},
                                     {"tool": {"server": "linear", "name": "save_issue", "arguments": {"query": "x"}}},
                                     {"tool": {"server": "linear", "name": "screenshot", "arguments": {}}},
                                     {"tool": {"arguments": {"summary": "done"}}}])
    assert cli.main(["run", flow("[linear]", harness="cursor")]) == 0
    (new,) = [e["params"] for e in codex_log(log, "request") if e["method"] == "session/new"]
    wf_server, linear = new["mcpServers"]
    assert (wf_server["name"], linear["name"], linear["type"]) == ("wf", "linear", "http")
    assert linear["url"].startswith("http://127.0.0.1:") and linear["url"] != wf_server["url"]
    assert codex_log(log, "server_tools") == [{"server": "linear", "names": ["list_issues", "get_user", "screenshot"]}]
    assert [answer["outcome"]["optionId"] for answer in codex_log(log, "permission")] == ["allow-once"] * 4
    read, write, image, result = codex_log(log, "reply")
    assert read == {"content": [{"type": "text", "text": "list_issues answered 'open'"}], "isError": False}
    assert write["isError"] is True  # not among the operations this read-only node may use
    assert image == {"content": [{"type": "image", "data": png_base64(), "mimeType": "image/png"}], "isError": False}
    assert result["isError"] is False
    assert "list_issues answered" not in attempt_logs()  # the transcript leaves out what the tool returned


def test_cursor_refuses_an_mcp_server_wf_did_not_give_the_session(tmp_path):
    call = {"kind": "other", "rawInput": {"providerIdentifier": "slack", "toolName": "slack_send_message"}}
    assert allows("full", False, tmp_path, call, {"wf", "slack"})  # full allows everything anyway
    assert not allows("read-only", False, tmp_path, call, {"wf", "linear"})
    assert allows("read-only", False, tmp_path, call, {"wf", "slack"})


# ---------- secrets ----------


def test_no_secret_reaches_an_agent_or_the_run_folder(vendor, repo, wf_home, monkeypatch, tmp_path):
    sign_in(vendor, access_token=SECRET)
    secrets = [SECRET, signin.keychain.load("linear")["refresh_token"]]
    log = codex(monkeypatch, repo, [{"tool": {"namespace": "linear", "name": "list_issues", "arguments": {"query": "open"}}},
                                    {"tool": {"arguments": {"summary": "done"}}}])
    assert cli.main(["run", flow("[linear]", harness="codex")]) == 0
    acp = cursor(monkeypatch, repo, [{"tool": {"server": "linear", "name": "list_issues", "arguments": {"query": "open"}}},
                                     {"tool": {"arguments": {"summary": "done"}}}])
    assert cli.main(["run", flow("[linear]", harness="cursor")]) == 0
    assert vendor.requests("/mcp")[-1]["authorization"] == f"Bearer {SECRET}"  # wf used it, on the agents' behalf
    sent_to_agents = log.read_text() + acp.read_text()
    assert sent_to_agents.count("list_issues answered") == 2
    written = [path.read_bytes() for path in [*Path(".wf").rglob("*"), *wf_home.rglob("*")] if path.is_file()]
    for secret in secrets:
        assert secret not in sent_to_agents  # session settings, environments, tool replies: everything Codex and Cursor got
        assert not any(secret.encode() in data for data in written)  # the run folders and ~/.wf
        assert not any(secret in value for value in os.environ.values())

    from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport

    async def claude_command(tools) -> str:
        o = replace(options(spec(tmp_path, tools, "full")), cli_path="claude")
        return json.dumps([SubprocessCLITransport(prompt="", options=o)._build_command(), o.env])

    command = attached([ToolUse(name="linear")], claude_command)  # Claude Code's command line and the environment wf adds
    assert "linear" in command and not any(secret in command for secret in secrets)


# ---------- real agents ----------


@pytest.mark.harness
@pytest.mark.parametrize("harness", ["claude-code", "codex", "cursor"])
def test_a_real_agent_reads_through_a_tool(harness, vendor, repo):
    sign_in(vendor)
    Path("flow.yaml").write_text(dedent("""
        version: 1
        name: real-tools
        defaults:
          harnesses: {claude-code: {model: claude-haiku-5-5, effort: low}, codex: {effort: low}, cursor: {model: composer-2.5}}
        start: {input: {}}
        nodes:
          read:
            needs: [start]
            harness: HARNESS
            tools: [linear]
            instruction: >-
              Use the Linear tool's list_issues operation once, with the query "open".
              Then submit exactly the text it returned as your summary.
        end: {output: {summary: "{{ read.summary }}"}}
        """).replace("HARNESS", harness))
    assert cli.main(["run", "flow.yaml"]) == 0
    assert ("list_issues", {"query": "open"}) in vendor.calls
    assert "list_issues answered 'open'" in json.loads((run_dir() / "output.json").read_text())["summary"]
