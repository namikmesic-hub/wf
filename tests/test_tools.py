"""wf tools: setup, doctor, validation, read-only operations, and attaching tools to sessions.

Every test runs against FakeVendor (fake_vendor.py), an OAuth and MCP server on 127.0.0.1,
with the in-memory keyring from conftest.py: nothing here signs in anywhere real, and nothing
touches your Keychain.
"""

import asyncio
import io
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
from keyring.errors import KeyringLocked
from mcp import Client

from fake_vendor import FakeVendor, browser, free_port
from fakes import Fake, submit
from workflow import cli, harnesses, runner, schemas, validate
from workflow.harnesses.base import SessionSpec
from workflow.harnesses.claude_code import options
from workflow.harnesses.codex import Codex
from workflow.harnesses.cursor import Cursor, allows
from workflow.output_tool import ResultTool
from workflow.spec import ToolUse
from workflow.tools import doctor, keychain, relay, signin
from workflow.tools.catalog import TOOLS

FAKE_CODEX = Path(__file__).parent / "fake_codex.py"
FAKE_ACP = Path(__file__).parent / "fake_acp.py"
SECRET = "s3cret-access-token-only-wf-may-see"


@pytest.fixture
def vendor(monkeypatch):
    found = FakeVendor(["list_issues", "get_user", "save_issue"]).start()
    monkeypatch.setitem(TOOLS, "linear", found.tool("linear"))
    yield found
    found.stop()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / "repo").mkdir()
    monkeypatch.chdir(tmp_path / "repo")
    monkeypatch.setattr(runner, "interactive", lambda: False)
    subprocess.run(["git", "init", "-q"], check=True)
    return tmp_path


def signed_in(vendor: FakeVendor, name: str = "linear", **changes) -> dict:
    """Keep a sign-in to the vendor in the keyring, as setup does."""
    vendor.clients["client-0"] = ""
    tokens = vendor.issue()
    stored = {"method": "oauth", "access_token": tokens["access_token"], "refresh_token": tokens["refresh_token"],
              "expires_at": int(time.time()) + 3600, "token_endpoint": f"{vendor.base}/token", "client_id": "client-0",
              "client_secret": "", "auth": "none", "resource": f"{vendor.base}/mcp", "set_up": "2026-10-19", **changes}
    vendor.access.add(stored["access_token"])
    keychain.save(name, stored)
    return stored


class Terminal(io.StringIO):
    def isatty(self) -> bool:
        return True


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


# ---------- setup ----------


def test_setup_signs_in_in_the_browser_and_keeps_the_secret_in_the_keychain(vendor, fake_keyring, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", Terminal())
    monkeypatch.setattr("webbrowser.open", browser())
    port = free_port()
    assert cli.main(["tools", "linear", "setup", "--port", str(port)]) == 0
    out = capsys.readouterr().out
    assert "Saved the Linear sign-in in the macOS Keychain" in out and "linear: pass" in out
    stored = json.loads(fake_keyring.items[("wf tools", "linear")])
    assert stored["access_token"] in vendor.access and stored["refresh_token"] in vendor.refresh
    (registered,) = vendor.requests("/register")
    assert registered["json"]["redirect_uris"] == [f"http://localhost:{port}/callback"]
    assert registered["json"]["token_endpoint_auth_method"] == "none"
    (asked,) = vendor.requests("/authorize")
    query = asked["query"]
    assert (query["client_id"], query["code_challenge_method"]) == (stored["client_id"], "S256")
    assert (query["scope"], query["resource"]) == ("read write", f"{vendor.base}/mcp")  # from the server's metadata
    (traded,) = vendor.requests("/token")  # the code, traded with the PKCE verifier the vendor checked
    assert traded["form"]["grant_type"] == "authorization_code" and "client_secret" not in traded["form"]


def test_setup_with_a_client_of_yours_shows_how_to_make_it_and_asks_for_its_id_and_secret():
    vendor = FakeVendor(register=False, clients={"my-app": "my-secret"}).start()
    said, port = [], free_port()
    try:
        stored = asyncio.run(signin.setup(vendor.tool("slack"), port=port, token=False, say=said.append,
                                          ask=lambda prompt: "my-app", ask_secret=lambda prompt: "my-secret",
                                          open_url=browser()))
    finally:
        vendor.stop()
    assert f"add the redirect URL http://localhost:{port}/callback" in said[0]
    assert "user token scopes: read write" in said[0]
    assert (stored["client_id"], stored["client_secret"], stored["auth"]) == ("my-app", "my-secret", "client_secret_post")
    assert not vendor.requests("/register")
    (traded,) = vendor.requests("/token")
    assert traded["form"]["client_secret"] == "my-secret"


def test_gmail_asks_google_for_offline_access_and_sends_no_resource():
    vendor = FakeVendor(register=False, clients={"my-client": "shh"}).start()
    try:
        stored = asyncio.run(signin.setup(vendor.tool("gmail"), port=free_port(), token=False, say=lambda text: None,
                                          ask=lambda prompt: "my-client", ask_secret=lambda prompt: "shh",
                                          open_url=browser()))
    finally:
        vendor.stop()
    (asked,) = vendor.requests("/authorize")
    query = asked["query"]
    assert (query["access_type"], query["prompt"]) == ("offline", "consent")
    assert query["scope"] == "https://www.googleapis.com/auth/gmail.readonly https://www.googleapis.com/auth/gmail.compose"
    assert "resource" not in query and "resource" not in vendor.requests("/token")[0]["form"]
    assert stored["resource"] is None


def test_setup_takes_a_pasted_token(vendor, fake_keyring, monkeypatch, capsys):
    token = vendor.issue()["access_token"]
    monkeypatch.setattr(sys, "stdin", Terminal())
    monkeypatch.setattr("getpass.getpass", lambda prompt: token)
    assert cli.main(["tools", "linear", "setup", "--token"]) == 0
    stored = json.loads(fake_keyring.items[("wf tools", "linear")])
    assert (stored["method"], stored["access_token"]) == ("token", token)
    assert "linear: pass" in capsys.readouterr().out


def test_only_tools_whose_vendor_gives_out_tokens_take_one():
    with pytest.raises(SystemExit):
        cli.main(["tools", "granola", "setup", "--token"])


def test_setup_needs_a_terminal(capsys):
    assert cli.main(["tools", "linear", "setup"]) == 1
    assert "run it at a terminal" in capsys.readouterr().err


def test_a_sign_in_that_comes_back_with_another_state_is_ignored(vendor, fake_keyring):
    forged = browser(lambda url: url.replace("state=", "state=forged"))
    with pytest.raises(signin.SetupFailed, match="wrong `state`"):
        asyncio.run(signin.setup(TOOLS["linear"], port=free_port(), token=False, say=lambda text: None,
                                 ask=input, ask_secret=input, open_url=forged))
    assert not vendor.requests("/token") and not fake_keyring.items


# ---------- doctor ----------


def test_doctor_passes_when_the_sign_in_works_and_a_read_call_answers(vendor, capsys):
    signed_in(vendor)
    assert cli.main(["tools", "linear", "doctor"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("linear: pass\n")
    assert "offers 2 read operations" in out and "`get_user` answered" in out
    assert vendor.calls == [("get_user", {"query": "me"})]


def test_doctor_for_every_tool_reports_one_that_is_not_set_up_without_failing(vendor, capsys):
    signed_in(vendor)
    assert cli.main(["tools", "doctor"]) == 0
    out = capsys.readouterr().out
    assert "linear: pass" in out and "slack: not set up\n  next step  run `wf tools slack setup`" in out
    assert cli.main(["tools", "slack", "doctor"]) == 1  # asked about, and not usable


def test_doctor_refreshes_a_sign_in_that_has_expired(vendor):
    old = signed_in(vendor, expires_at=int(time.time()) - 10)
    assert asyncio.run(doctor.check(TOOLS["linear"])).status == "pass"
    new = keychain.load("linear")
    assert new["access_token"] != old["access_token"] and new["refresh_token"] != old["refresh_token"]
    assert old["refresh_token"] not in vendor.refresh  # the vendor rotated it


def test_doctor_says_to_sign_in_again_when_the_vendor_refuses(vendor):
    signed_in(vendor)
    vendor.access.clear()
    vendor.refresh.clear()  # revoked
    report = asyncio.run(doctor.check(TOOLS["linear"]))
    assert report.status == "fail"
    assert dict(report.lines)["problem"] == "Linear didn't refresh the sign-in (invalid_grant: unknown refresh token)"
    assert dict(report.lines)["next step"] == "run `wf tools linear setup`"


def test_doctor_fails_when_the_read_call_fails(vendor):
    signed_in(vendor)
    vendor.errors["get_user"] = "this workspace has no MCP access"
    report = asyncio.run(doctor.check(TOOLS["linear"]))
    assert report.status == "fail"
    assert "the read call `get_user` failed" in dict(report.lines)["problem"]
    assert "this workspace has no MCP access" in dict(report.lines)["problem"]


def test_doctor_reports_a_server_it_cannot_reach(vendor, monkeypatch):
    signed_in(vendor)
    monkeypatch.setitem(TOOLS, "linear", vendor.tool("linear", url=f"http://127.0.0.1:{free_port()}/mcp"))
    report = asyncio.run(doctor.check(TOOLS["linear"]))
    assert report.status == "fail"
    assert dict(report.lines)["problem"].startswith("can't use Linear's server at http://127.0.0.1:")
    assert dict(report.lines)["next step"] == "check your network connection, then run `wf tools linear doctor` again"


def test_doctor_says_when_the_keyring_cannot_be_read(fake_keyring, monkeypatch):
    def locked(service, name):
        raise KeyringLocked("the Keychain is locked")

    monkeypatch.setattr(fake_keyring, "get_password", locked)
    report = asyncio.run(doctor.check(TOOLS["slack"]))
    assert report.status == "fail"
    assert dict(report.lines)["problem"] == "can't read the slack sign-in from the keyring: the Keychain is locked"


# ---------- keeping the sign-in fresh ----------


def test_a_token_refused_in_the_middle_of_a_session_is_refreshed_once(vendor):
    signed_in(vendor)

    async def call():
        async with relay.attach([ToolUse(name="linear")]) as (server,):
            vendor.access.clear()  # the token stops working, as an early expiry would
            return await server.call("list_issues", {"query": "open"})

    assert asyncio.run(call()) == ([{"type": "text", "text": "list_issues answered 'open'"}], False)
    assert keychain.load("linear")["access_token"] in vendor.access


def test_two_refreshes_at_once_take_turns(vendor):
    signed_in(vendor, expires_at=int(time.time()) - 10)

    async def both():
        return await asyncio.gather(signin.access_token(TOOLS["linear"]), signin.access_token(TOOLS["linear"]))

    first, second = asyncio.run(both())
    assert first == second
    assert len(vendor.requests("/token")) == 1  # the second found the new token: a rotated refresh token is used once


# ---------- wf validate ----------


def problems(path: str) -> list[tuple[str, str, str]]:
    return [(p.level, p.where, p.message) for p in validate.check(path).problems]


def test_validate_knows_the_tools_and_warns_about_one_not_set_up(repo):
    found = problems(flow("[linear, slak, {name: linear, write: true}]"))
    assert found == [
        ("warning", "nodes.read.tools", "`linear` isn't set up on this machine, so the node fails when it runs: run `wf tools linear setup`"),
        ("error", "nodes.read.tools[1]", "unknown tool `slak` (choose from slack, granola, linear, notion, gmail) (did you mean `slack`?)"),
        ("error", "nodes.read.tools[2]", "`linear` is listed twice"),
    ]


def test_a_tool_that_is_set_up_gets_no_warning(repo):
    keychain.save("linear", {"method": "token", "access_token": "x"})
    assert problems(flow("[linear]")) == []


def test_tools_in_defaults_reach_every_agent_node(repo):
    text = Path(flow("[linear]")).read_text().replace("    tools: [linear]\n", "").replace(
        "defaults:\n", "defaults:\n  tools: [notion]\n")
    Path("flow.yaml").write_text(text)
    assert problems("flow.yaml") == [("warning", "defaults.tools", "`notion` isn't set up on this machine, so the node fails "
                                                                    "when it runs: run `wf tools notion setup`")]


@pytest.mark.parametrize("tools, where, message", [
    ("[{name: linear, wirte: true}]", "nodes.read.tools[0].wirte", "unknown key"),
    ("[3]", "nodes.read.tools[0]", "expected a tool name, or {name: ..., write: true}"),
    ("linear", "nodes.read.tools", "Input should be a valid list"),
])
def test_tools_entries_have_a_shape(repo, tools, where, message):
    assert problems(flow(tools)) == [("error", where, message)]


def test_only_agent_nodes_take_tools(repo):
    flow("[linear]", nodes="  check:\n    needs: [read]\n    command: \"true\"\n    tools: [linear]\n")
    assert ("error", "nodes.check.tools", "unknown key") in problems("flow.yaml")


# ---------- attaching tools to sessions ----------


def test_each_node_gets_its_own_tools_and_only_their_reads_unless_it_opts_in(repo, monkeypatch):
    vendor = FakeVendor(["list_issues", "get_user", "save_issue", "archive_everything"]).start()
    monkeypatch.setitem(TOOLS, "linear", vendor.tool("linear"))
    try:
        signed_in(vendor)
        fake = Fake("claude-code", *[submit({"summary": "ok"})] * 3)
        monkeypatch.setitem(harnesses.FACTORIES, "claude-code", fake.factory)
        flow("[linear]", nodes=("  write:\n    needs: [read]\n    instruction: File the issue\n"
                                "    tools: [{name: linear, write: true}]\n"
                                "  plain:\n    needs: [write]\n    instruction: Sum up\n"))
        assert cli.main(["run", "flow.yaml"]) == 0
    finally:
        vendor.stop()
    attached = [{server.name: [op.name for op in server.operations] for server in spec.tools} for spec in fake.specs]
    assert attached == [
        {"linear": ["list_issues", "get_user"]},  # a new operation counts as a write
        {"linear": ["list_issues", "get_user", "save_issue", "archive_everything"]},
        {},
    ]


def test_a_read_only_node_cannot_call_a_write(vendor):
    signed_in(vendor)

    async def call():
        async with relay.attach([ToolUse(name="linear")]) as (server,):
            return await server.call("save_issue", {"query": "a new issue"})

    content, is_error = asyncio.run(call())
    assert is_error and content[0]["text"] == "Error: `save_issue` isn't one of the Linear operations this step may use."
    assert vendor.calls == []  # it never reached Linear


def test_a_tool_that_is_not_set_up_fails_the_node_without_a_retry(repo, monkeypatch, capsys):
    fake = Fake("claude-code")
    monkeypatch.setitem(harnesses.FACTORIES, "claude-code", fake.factory)
    assert cli.main(["run", flow("[linear]")]) == 1
    assert "read: Linear isn't set up: run `wf tools linear setup`" in capsys.readouterr().err
    assert fake.specs == []  # no session started
    assert not [e for e in events() if e["event"] == "attempt_failed"]


def test_a_server_that_does_not_answer_fails_the_attempt_and_the_retry_tries_again(vendor, repo, monkeypatch, capsys):
    signed_in(vendor)
    monkeypatch.setitem(TOOLS, "linear", vendor.tool("linear", url=f"http://127.0.0.1:{free_port()}/mcp"))
    monkeypatch.setitem(harnesses.FACTORIES, "claude-code", Fake("claude-code").factory)
    assert cli.main(["run", flow("[linear]")]) == 1
    failed = [e for e in events() if e["event"] == "attempt_failed"]
    assert [e["attempt"] for e in failed] == [1, 2]
    assert failed[0]["error"].startswith("can't use Linear's server at http://127.0.0.1:")


def test_a_dry_run_attaches_no_tools(repo, monkeypatch):
    monkeypatch.setitem(harnesses.FACTORIES, "claude-code", lambda: pytest.fail("a dry run called a real harness"))
    Path("script.yaml").write_text(yaml.safe_dump({"agents": {"read": [{"summary": "read them"}]}}))
    assert cli.main(["run", flow("[linear]"), "--dry-run", "script.yaml"]) == 0  # linear isn't even set up


def spec(cwd: Path, tools: list, access: str = "read-only") -> SessionSpec:
    return SessionSpec(cwd=cwd, access=access, web=False, model="opus", effort="high",
                       tool=ResultTool(schemas.object_schema({"summary": {"type": "string"}})),
                       log=cwd / "attempt-1.log", emit=lambda event: None, tools=tools)


@pytest.mark.parametrize("access", ["read-only", "edit", "full"])
def test_claude_code_gets_an_mcp_server_per_tool_that_relays_its_calls(vendor, tmp_path, access):
    signed_in(vendor)

    async def check():
        async with relay.attach([ToolUse(name="linear")]) as tools:
            o = options(spec(tmp_path, tools, access))
            async with Client(o.mcp_servers["linear"]["instance"]) as claude:
                listed = [t.name for t in (await claude.list_tools()).tools]
                answer = await claude.call_tool("list_issues", {"query": "mine"})
            return o, listed, answer

    o, listed, answer = asyncio.run(check())
    assert list(o.mcp_servers) == ["wf", "linear"]
    assert [name for name in o.allowed_tools if "linear" in name] == ["mcp__linear__list_issues", "mcp__linear__get_user"]
    assert listed == ["list_issues", "get_user"]  # what Claude is told about: no writes, at any access level
    assert answer.content[0].text == "list_issues answered 'mine'"
    assert vendor.calls == [("list_issues", {"query": "mine"})]


def codex(monkeypatch, tmp_path: Path, *turns) -> Path:
    """Use the fake Codex app server; returns the log of everything wf sent it."""
    scenario, log = tmp_path / "scenario.json", tmp_path / "requests.jsonl"
    scenario.write_text(json.dumps({"turns": list(turns)}))
    monkeypatch.setitem(harnesses.FACTORIES, "codex",
                        lambda: Codex(command=[sys.executable, str(FAKE_CODEX), str(scenario), str(log)], request_timeout=10))
    return log


def test_codex_gets_a_namespace_per_tool_and_its_calls_are_relayed(vendor, repo, monkeypatch):
    signed_in(vendor)
    log = codex(monkeypatch, repo, [{"tool": {"namespace": "linear", "name": "list_issues", "arguments": {"query": "open"}}},
                                    {"tool": {"namespace": "linear", "name": "save_issue", "arguments": {"query": "x"}}},
                                    {"tool": {"arguments": {"summary": "done"}}}])
    assert cli.main(["run", flow("[linear]", harness="codex")]) == 0
    lines = [json.loads(line) for line in log.read_text().splitlines()]
    (thread,) = [e["data"]["params"] for e in lines if e["kind"] == "request" and e["data"]["method"] == "thread/start"]
    submit_result, linear = thread["dynamicTools"]
    assert (linear["type"], linear["name"], linear["description"]) == ("namespace", "linear", "Linear, through wf (read-only)")
    assert [t["name"] for t in linear["tools"]] == ["list_issues", "get_user"]
    read, write, result = [e["data"] for e in lines if e["kind"] == "reply"]
    assert read == {"success": True, "contentItems": [{"type": "inputText", "text": "list_issues answered 'open'"}]}
    assert write["success"] is False and "isn't one of the Linear operations" in write["contentItems"][0]["text"]
    assert result["success"] is True
    assert vendor.calls == [("list_issues", {"query": "open"})]


def cursor(monkeypatch, tmp_path: Path, *turns) -> Path:
    """Use the fake `cursor-agent`; returns the log of everything wf sent it and answered."""
    scenario, log = tmp_path / "acp-scenario.json", tmp_path / "acp-requests.jsonl"
    scenario.write_text(json.dumps({"turns": list(turns)}))
    monkeypatch.setitem(harnesses.FACTORIES, "cursor",
                        lambda: Cursor(command=[sys.executable, str(FAKE_ACP), str(scenario), str(log)], request_timeout=10))
    return log


def logged(log: Path, kind: str) -> list:
    return [json.loads(line)["data"] for line in log.read_text().splitlines() if json.loads(line)["kind"] == kind]


def test_cursor_gets_an_mcp_server_per_tool_and_its_calls_are_relayed(vendor, repo, monkeypatch):
    signed_in(vendor)
    log = cursor(monkeypatch, repo, [{"tool": {"server": "linear", "name": "list_issues", "arguments": {"query": "open"}}},
                                     {"tool": {"server": "linear", "name": "save_issue", "arguments": {"query": "x"}}},
                                     {"tool": {"arguments": {"summary": "done"}}}])
    assert cli.main(["run", flow("[linear]", harness="cursor")]) == 0
    (new,) = [e["params"] for e in logged(log, "request") if e["method"] == "session/new"]
    wf_server, linear = new["mcpServers"]
    assert (wf_server["name"], linear["name"], linear["type"]) == ("wf", "linear", "http")
    assert linear["url"].startswith("http://127.0.0.1:") and linear["url"] != wf_server["url"]
    assert logged(log, "server_tools") == [{"server": "linear", "names": ["list_issues", "get_user"]}]
    assert [answer["outcome"]["optionId"] for answer in logged(log, "permission")] == ["allow-once"] * 3
    read, write, result = logged(log, "reply")
    assert read == {"content": [{"type": "text", "text": "list_issues answered 'open'"}], "isError": False}
    assert write["isError"] is True  # not among the operations this read-only node may use
    assert result["isError"] is False
    assert vendor.calls == [("list_issues", {"query": "open"})]


def test_cursor_refuses_an_mcp_server_wf_did_not_give_the_session(tmp_path):
    call = {"kind": "other", "rawInput": {"providerIdentifier": "slack", "toolName": "slack_send_message"}}
    assert allows("full", False, tmp_path, call, {"wf", "slack"})  # full allows everything anyway
    assert not allows("read-only", False, tmp_path, call, {"wf", "linear"})
    assert allows("read-only", False, tmp_path, call, {"wf", "slack"})


def test_no_secret_reaches_an_agent_or_the_run_folder(vendor, repo, wf_home, monkeypatch, tmp_path):
    signed_in(vendor, access_token=SECRET)
    stored = keychain.load("linear")
    secrets = [SECRET, stored["refresh_token"]]
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

    async def claude_command() -> str:
        async with relay.attach([ToolUse(name="linear")]) as tools:
            o = replace(options(spec(tmp_path, tools, "full")), cli_path="claude")
            return json.dumps([SubprocessCLITransport(prompt="", options=o)._build_command(), o.env])

    command = asyncio.run(claude_command())  # Claude Code's command line and the environment wf adds
    assert "linear" in command and not any(secret in command for secret in secrets)


@pytest.mark.harness
@pytest.mark.parametrize("harness", ["claude-code", "codex", "cursor"])
def test_a_real_agent_reads_through_a_tool(harness, vendor, repo):
    signed_in(vendor)
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
