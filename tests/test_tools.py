"""wf tools: setup, the Keychain, refreshing sign-ins, doctor, `wf tools list`, and validation.
test_tool_sessions.py covers the tools in agent sessions.

Every test runs against FakeVendor (fake_vendor.py), an OAuth and MCP server on 127.0.0.1,
with the in-memory keyring from conftest.py: nothing here signs in anywhere real, and nothing
touches your Keychain.
"""

import asyncio
import io
import json
import subprocess
import sys
import time
from pathlib import Path
from textwrap import dedent
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest
from keyring.errors import KeyringLocked, PasswordSetError

from fake_vendor import FakeVendor, browser, free_port, sign_in
from workflow import cli, runner, validate
from workflow.spec import ToolUse
from workflow.tools import doctor, keychain, relay, signin
from workflow.tools.catalog import TOOLS


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


class Terminal(io.StringIO):
    def isatty(self) -> bool:
        return True


def setup(tool, **changes) -> dict:
    """signin.setup at a free port, with a browser that signs in, and no one at the terminal."""
    arguments = {"port": free_port(), "token": False, "write": False, "say": lambda text: None,
                 "ask": lambda prompt: "", "ask_secret": lambda prompt: "", "open_url": browser(), **changes}
    return asyncio.run(signin.setup(tool, **arguments))


def flow(tools: str = "[linear]", nodes: str = "", top: str = "") -> str:
    Path("flow.yaml").write_text(dedent("""
        version: 1
        name: tooled
        defaults:
          harness: claude-code
          harnesses: {{claude-code: {{model: opus}}}}
        {top}
        start: {{input: {{}}}}
        nodes:
          read:
            needs: [start]
            instruction: Read the open issues
            tools: {tools}
        end: {{output: {{}}}}
        """).format(tools=tools, top=top).replace("end: {output: {}}", nodes + "end: {output: {}}"))
    return "flow.yaml"


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
    assert stored["write"] is False  # Linear's read scope only
    (registered,) = vendor.requests("/register")
    assert registered["json"]["redirect_uris"] == [f"http://localhost:{port}/callback"]
    assert registered["json"]["token_endpoint_auth_method"] == "none"
    (asked,) = vendor.requests("/authorize")
    query = asked["query"]
    assert (query["client_id"], query["code_challenge_method"]) == (stored["client_id"], "S256")
    assert (query["scope"], query["resource"]) == ("read", f"{vendor.base}/mcp")
    (traded,) = vendor.requests("/token")  # the code, traded with the PKCE verifier the vendor checked
    assert traded["form"]["grant_type"] == "authorization_code" and "client_secret" not in traded["form"]


def test_setup_asks_for_the_write_scopes_only_with_write(vendor, fake_keyring, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", Terminal())
    monkeypatch.setattr("webbrowser.open", browser())
    assert cli.main(["tools", "linear", "setup", "--write", "--port", str(free_port())]) == 0
    assert vendor.requests("/authorize")[0]["query"]["scope"] == "read write"
    assert json.loads(fake_keyring.items[("wf tools", "linear")])["write"] is True
    with pytest.raises(SystemExit):  # Granola's scopes don't tell reads from writes
        cli.main(["tools", "granola", "setup", "--write"])


def test_setup_waits_for_the_redirect_on_signin_port_of_config_toml_unless_port_is_given(vendor, fake_keyring, monkeypatch,
                                                                                       wf_home):
    monkeypatch.setattr(sys, "stdin", Terminal())
    monkeypatch.setattr("webbrowser.open", browser())
    ports = free_port(), free_port()
    (wf_home / "config.toml").write_text(f"[tools]\nsignin_port = {ports[0]}\n")
    assert cli.main(["tools", "linear", "setup"]) == 0
    assert cli.main(["tools", "linear", "setup", "--port", str(ports[1])]) == 0
    assert [asked["json"]["redirect_uris"] for asked in vendor.requests("/register")] == [
        [f"http://localhost:{port}/callback"] for port in ports]


def test_discovery_follows_the_401_and_finds_openid_metadata_appended_to_an_issuer_path():
    vendor = FakeVendor(issuer_path="/tenant").start()
    try:
        stored = setup(vendor.tool("notion"))
    finally:
        vendor.stop()
    asked = vendor.paths[:vendor.paths.index("/authorize")]
    assert asked == ["/mcp",  # the 401 names the resource metadata
                     "/.well-known/oauth-protected-resource/mcp",
                     "/.well-known/oauth-authorization-server/tenant", "/.well-known/openid-configuration/tenant",
                     "/tenant/.well-known/openid-configuration", "/register"]
    assert vendor.requests("/authorize")[0]["query"]["scope"] == "read write"  # Notion's: the server's own list
    assert stored["access_token"] in vendor.access and "write" not in stored  # one sign-in for reads and writes


def test_setup_with_a_client_of_yours_shows_how_to_make_it_and_asks_for_its_id_and_secret(monkeypatch):
    vendor = FakeVendor(register=False, clients={"my-app": "my-secret"}).start()
    said, port = [], free_port()
    try:
        stored = setup(vendor.tool("slack"), port=port, say=said.append, ask=lambda prompt: "my-app",
                       ask_secret=lambda prompt: "my-secret")
    finally:
        vendor.stop()
    assert f"add the redirect URL http://localhost:{port}/callback" in said[0]
    assert f"user token scopes: {' '.join(TOOLS['slack'].scopes)}" in said[0]  # the read scopes only
    assert "chat:write" not in vendor.requests("/authorize")[0]["query"]["scope"]
    assert (stored["client_id"], stored["client_secret"], stored["auth"]) == ("my-app", "my-secret", "client_secret_post")
    assert not vendor.requests("/register")
    (traded,) = vendor.requests("/token")
    assert traded["form"]["client_secret"] == "my-secret"


def test_gmail_asks_google_for_offline_access_and_sends_no_resource():
    vendor = FakeVendor(register=False, clients={"my-client": "shh"}).start()
    try:
        stored = setup(vendor.tool("gmail"), ask=lambda prompt: "my-client", ask_secret=lambda prompt: "shh")
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


def test_setup_that_saves_a_sign_in_doctor_rejects_exits_with_1(vendor, fake_keyring, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", Terminal())
    monkeypatch.setattr("getpass.getpass", lambda prompt: "a-token-linear-never-issued")
    assert cli.main(["tools", "linear", "setup", "--token"]) == 1
    assert ("wf tools", "linear") in fake_keyring.items  # saved, and reported as not working
    assert "linear: fail" in capsys.readouterr().out


def test_only_tools_whose_vendor_gives_out_tokens_take_one():
    with pytest.raises(SystemExit):
        cli.main(["tools", "granola", "setup", "--token"])


def test_setup_needs_a_terminal(capsys):
    assert cli.main(["tools", "linear", "setup"]) == 1
    assert "run it at a terminal" in capsys.readouterr().err


def test_a_request_to_the_callback_without_the_sign_ins_state_is_turned_away_and_the_wait_goes_on(vendor):
    turned_away = []

    def visit(url: str) -> str:
        port = urlsplit(parse_qs(urlsplit(url).query)["redirect_uri"][0]).port
        with httpx2.Client(timeout=10) as http:  # someone else's request reaches the port first
            stray = http.get(f"http://localhost:{port}/callback?code=stray&state=guessed")
        turned_away.append((stray.status_code, stray.text))
        return url

    stored = setup(TOOLS["linear"], open_url=browser(visit))
    assert turned_away[0][0] == 400 and "the sign-in wf is waiting for" in turned_away[0][1]
    (traded,) = vendor.requests("/token")
    assert traded["form"]["code"] != "stray" and stored["access_token"] in vendor.access


@pytest.mark.parametrize("settings, message", [
    ({"deny": True}, "Notion didn't sign you in: access_denied: you said no"),
    ({"no_code": True}, "the sign-in came back without a code: try again"),
    ({"iss": "https://elsewhere.example"}, "the sign-in came back from https://elsewhere.example"),
])
def test_a_sign_in_that_comes_back_wrong_gets_no_token(settings, message, fake_keyring):
    vendor = FakeVendor(**settings).start()
    try:
        with pytest.raises(signin.SetupFailed, match=message):
            setup(vendor.tool("notion"))
    finally:
        vendor.stop()
    assert not vendor.requests("/token") and not fake_keyring.items


def test_the_sign_ins_iss_must_be_the_issuer_exactly(fake_keyring):
    vendor = FakeVendor().start()
    vendor.settings["iss"] = vendor.issuer + "/"  # the same server to a lax reader, not to RFC 9207
    try:
        with pytest.raises(signin.SetupFailed, match="came back from .*/, not "):
            setup(vendor.tool("notion"))
    finally:
        vendor.stop()
    assert not vendor.requests("/token")


def test_a_sign_in_from_another_issuer_isnt_read_even_for_its_error(fake_keyring):
    vendor = FakeVendor(deny=True, iss="https://elsewhere.example").start()
    try:
        with pytest.raises(signin.SetupFailed) as failed:
            setup(vendor.tool("notion"))
    finally:
        vendor.stop()
    assert "came back from https://elsewhere.example" in str(failed.value) and "access_denied" not in str(failed.value)


def test_a_sign_in_without_the_iss_the_server_says_it_sends_is_ignored(fake_keyring):
    vendor = FakeVendor(iss="", iss_supported=True).start()
    try:
        with pytest.raises(signin.SetupFailed, match="came back without `iss`"):
            setup(vendor.tool("notion"))
    finally:
        vendor.stop()
    assert not vendor.requests("/token")


def test_metadata_that_names_another_issuer_isnt_used(fake_keyring):
    vendor = FakeVendor(metadata_issuer="https://attacker.example").start()
    try:
        with pytest.raises(signin.Unreachable, match="metadata names https://attacker.example, not "):
            setup(vendor.tool("notion"))
    finally:
        vendor.stop()
    assert not vendor.requests("/authorize")


def test_an_issuer_written_with_another_last_slash_is_another_issuer_unless_the_catalog_pins_it(fake_keyring):
    vendor = FakeVendor(resource_slash=True).start()  # as Google's resource metadata names https://accounts.google.com/
    try:
        with pytest.raises(signin.Unreachable, match="metadata names http://127.0.0.1:[0-9]+, not http://127.0.0.1:[0-9]+/,"):
            setup(vendor.tool("notion"))
        stored = setup(vendor.tool("notion", issuer=vendor.issuer))  # as the catalog pins Google's
    finally:
        vendor.stop()
    assert stored["access_token"] in vendor.access
    assert TOOLS["gmail"].issuer == "https://accounts.google.com"


def test_setup_gives_up_when_no_sign_in_comes_back(vendor, monkeypatch):
    monkeypatch.setattr(signin, "WAIT", 0.2)
    with pytest.raises(signin.SetupFailed, match="no sign-in came back within 0.2 seconds"):
        setup(TOOLS["linear"], open_url=lambda url: None)
    assert not vendor.requests("/token")


# ---------- the Keychain ----------


@pytest.mark.parametrize("text", ["not json", '{"refresh_token": "but no access token"}', "[1, 2]"])
def test_a_stored_item_wf_didnt_write_counts_as_not_set_up(text, fake_keyring):
    fake_keyring.items[("wf tools", "linear")] = text
    assert keychain.load("linear") is None
    assert asyncio.run(doctor.check(TOOLS["linear"])).status == "not set up"


# ---------- keeping the sign-in fresh ----------


@pytest.mark.parametrize("left, refreshed", [(30, True), (90, False)])
def test_a_token_is_refreshed_within_a_minute_of_expiring(vendor, left, refreshed):
    old = sign_in(vendor, expires_at=int(time.time()) + left)
    token = asyncio.run(signin.access_token(TOOLS["linear"]))
    assert (token != old["access_token"]) is refreshed
    assert len(vendor.requests("/token")) == int(refreshed)


def test_two_refreshes_at_once_take_turns(vendor):
    sign_in(vendor, expires_at=int(time.time()) - 10)

    async def both():
        return await asyncio.gather(signin.access_token(TOOLS["linear"]), signin.access_token(TOOLS["linear"]))

    first, second = asyncio.run(both())
    assert first == second
    assert len(vendor.requests("/token")) == 1  # the second found the new token: a rotated refresh token is used once


def test_a_refresh_finishes_and_is_saved_when_the_session_that_asked_is_cancelled(monkeypatch):
    vendor = FakeVendor(token_delay=0.5).start()
    monkeypatch.setitem(TOOLS, "linear", vendor.tool("linear"))
    try:
        old = sign_in(vendor, expires_at=int(time.time()) - 10)

        async def cancelled_meanwhile():
            asked = asyncio.ensure_future(signin.access_token(TOOLS["linear"]))
            while not vendor.requests("/token"):
                await asyncio.sleep(0.01)
            asked.cancel()  # a node's timeout, while the vendor is rotating the refresh token
            with pytest.raises(asyncio.CancelledError):
                await asked
            await asyncio.sleep(1)  # the refresh goes on, in a task of its own

        asyncio.run(cancelled_meanwhile())
        new = keychain.load("linear")
        assert new["refresh_token"] != old["refresh_token"] and new["refresh_token"] in vendor.refresh
        assert asyncio.run(signin.access_token(TOOLS["linear"])) == new["access_token"]
    finally:
        vendor.stop()


def test_new_tokens_the_keychain_refuses_are_kept_and_saved_before_another_refresh(vendor, fake_keyring, monkeypatch):
    monkeypatch.setattr(signin, "_unsaved", {})
    old = sign_in(vendor, expires_at=int(time.time()) - 10)
    save = fake_keyring.set_password

    def refuse(service, name, password):
        raise PasswordSetError("the Keychain is locked")

    monkeypatch.setattr(fake_keyring, "set_password", refuse)
    token = asyncio.run(signin.access_token(TOOLS["linear"]))
    assert token != old["access_token"] and keychain.load("linear") == old  # not saved: kept in memory instead
    monkeypatch.setattr(fake_keyring, "set_password", save)
    assert asyncio.run(signin.access_token(TOOLS["linear"])) == token  # no second refresh with the dead token
    assert keychain.load("linear")["access_token"] == token and len(vendor.requests("/token")) == 1


def test_the_refresh_lock_is_the_users_whatever_wf_home_says(monkeypatch, tmp_path):
    first = signin.lock_folder()
    monkeypatch.setenv("WF_HOME", str(tmp_path / "another-home"))
    assert signin.lock_folder() == first and not first.is_relative_to(tmp_path)


def test_a_refused_refresh_reads_the_keychain_again_for_one_saved_meanwhile(vendor, monkeypatch):
    sign_in(vendor, expires_at=int(time.time()) - 10)
    newer = vendor.issue()

    async def refreshed_elsewhere(tool, stored):  # another wf saved a new sign-in, and the old token is dead
        keychain.save("linear", {**stored, "access_token": newer["access_token"], "refresh_token": newer["refresh_token"],
                                 "expires_at": int(time.time()) + 3600})
        raise signin.NotSetUp(tool, "Linear didn't refresh the sign-in (invalid_grant)")

    monkeypatch.setattr(signin, "_refresh", refreshed_elsewhere)
    assert asyncio.run(signin.access_token(TOOLS["linear"])) == newer["access_token"]


def test_a_vendor_that_keeps_the_refresh_token_is_refreshed_with_it_again(monkeypatch):
    vendor = FakeVendor(rotate=False).start()
    monkeypatch.setitem(TOOLS, "linear", vendor.tool("linear"))
    try:
        old = sign_in(vendor, expires_at=int(time.time()) - 10)
        asyncio.run(signin.access_token(TOOLS["linear"]))
        keychain.save("linear", {**keychain.load("linear"), "expires_at": int(time.time()) - 10})
        asyncio.run(signin.access_token(TOOLS["linear"]))
    finally:
        vendor.stop()
    assert [r["form"]["refresh_token"] for r in vendor.requests("/token")] == [old["refresh_token"]] * 2
    assert keychain.load("linear")["refresh_token"] == old["refresh_token"]


@pytest.mark.parametrize("status", [503, 429])
def test_a_refresh_the_vendor_is_too_busy_for_can_be_tried_again(status, monkeypatch):
    vendor = FakeVendor(token_status=status).start()
    monkeypatch.setitem(TOOLS, "linear", vendor.tool("linear"))
    try:
        sign_in(vendor, expires_at=int(time.time()) - 10)
        with pytest.raises(signin.Unreachable, match=f"HTTP {status}|temporarily_unavailable"):
            asyncio.run(signin.access_token(TOOLS["linear"]))
    finally:
        vendor.stop()


# ---------- doctor ----------


def test_doctor_passes_when_the_sign_in_works_and_a_read_call_answers(vendor, capsys):
    sign_in(vendor)
    assert cli.main(["tools", "linear", "doctor"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("linear: pass\n")
    assert "offers 2 read operations" in out and "`get_user` answered" in out
    assert vendor.calls == [("get_user", {"query": "me"})]


def test_doctor_for_every_tool_reports_one_that_is_not_set_up_without_failing(vendor, capsys):
    sign_in(vendor)
    assert cli.main(["tools", "doctor"]) == 0
    out = capsys.readouterr().out
    assert "linear: pass" in out and "slack: not set up\n  next step  run `wf tools slack setup`" in out
    assert cli.main(["tools", "slack", "doctor"]) == 1  # asked about, and not usable


def test_doctor_refreshes_a_sign_in_that_has_expired(vendor):
    old = sign_in(vendor, expires_at=int(time.time()) - 10)
    assert asyncio.run(doctor.check(TOOLS["linear"])).status == "pass"
    new = keychain.load("linear")
    assert new["access_token"] != old["access_token"] and new["refresh_token"] != old["refresh_token"]
    assert old["refresh_token"] not in vendor.refresh  # the vendor rotated it


def test_doctor_says_to_sign_in_again_when_the_vendor_refuses(vendor):
    sign_in(vendor)
    vendor.access.clear()
    vendor.refresh.clear()  # revoked
    report = asyncio.run(doctor.check(TOOLS["linear"]))
    assert report.status == "fail"
    assert dict(report.lines)["problem"] == "Linear didn't refresh the sign-in (invalid_grant: unknown refresh token)"
    assert dict(report.lines)["next step"] == "run `wf tools linear setup`"


def test_doctor_fails_when_the_read_call_fails(vendor):
    sign_in(vendor)
    vendor.errors["get_user"] = "this workspace has no MCP access"
    report = asyncio.run(doctor.check(TOOLS["linear"]))
    assert report.status == "fail"
    assert "the read call `get_user` failed" in dict(report.lines)["problem"]
    assert "this workspace has no MCP access" in dict(report.lines)["problem"]


def test_doctor_says_to_update_wf_when_the_server_no_longer_offers_its_read_call(monkeypatch):
    vendor = FakeVendor(["list_issues"]).start()
    monkeypatch.setitem(TOOLS, "linear", vendor.tool("linear"))
    try:
        sign_in(vendor)
        report = asyncio.run(doctor.check(TOOLS["linear"]))
    finally:
        vendor.stop()
    assert report.status == "fail"
    assert dict(report.lines)["problem"] == "Linear no longer offers `get_user`"
    assert dict(report.lines)["next step"].startswith("update wf")


def test_doctor_names_the_operations_it_keeps_back_and_the_reads_not_offered(vendor):
    sign_in(vendor)
    report = asyncio.run(doctor.check(TOOLS["linear"]))
    lines = dict(report.lines)
    assert report.status == "pass"
    assert lines["kept back"] == "save_issue: only for nodes with `write: true`"
    assert lines["missing"].startswith("extract_images, get_agent_skill, ") and "list_issues" not in lines["missing"]


def test_doctor_notes_an_operation_the_server_marks_read_only_that_wf_keeps_back(monkeypatch):
    vendor = FakeVendor(["list_issues", "get_user", "list_reports"], hints={"list_reports": {"readOnlyHint": True}}).start()
    monkeypatch.setitem(TOOLS, "linear", vendor.tool("linear"))
    try:
        sign_in(vendor)
        report = asyncio.run(doctor.check(TOOLS["linear"]))
    finally:
        vendor.stop()
    assert report.status == "pass"
    assert dict(report.lines)["note"] == ("Linear marks list_reports read-only, but wf keeps them back: "
                                          "if they only read, add them to workflow/tools/catalog.py")


def test_doctor_fails_when_a_read_on_wfs_list_is_marked_as_changing_things(monkeypatch):
    vendor = FakeVendor(["list_issues", "get_user"], hints={"list_issues": {"destructiveHint": True}}).start()
    monkeypatch.setitem(TOOLS, "linear", vendor.tool("linear"))
    try:
        sign_in(vendor)
        report = asyncio.run(doctor.check(TOOLS["linear"]))
    finally:
        vendor.stop()
    assert report.status == "fail"
    assert dict(report.lines)["problem"] == "wf counts list_issues as reads, but Linear marks them as changing things"


def test_doctor_reports_a_server_it_cannot_reach(vendor, monkeypatch):
    sign_in(vendor)
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


# ---------- docs/tools.md ----------


def test_the_documented_reads_are_the_catalogs():
    table = Path(__file__).parent.parent.joinpath("docs", "tools.md").read_text().split("## What counts as a read")[1]
    documented = {}
    for row in table.splitlines():
        cells = [cell.strip() for cell in row.strip("|").split("|")]
        if row.startswith("| `") and len(cells) == 3:
            documented[cells[0].strip("`")] = {name.strip(" `") for name in cells[1].split(",")}
    assert documented == {name: set(tool.reads) for name, tool in TOOLS.items()}


# ---------- wf tools list ----------


def test_list_shows_each_tool_and_its_sign_in(vendor, fake_keyring, capsys):
    sign_in(vendor)  # linear: reads and writes
    keychain.save("slack", {"method": "oauth", "access_token": "x", "write": False})
    assert cli.main(["tools", "list"]) == 0
    rows = {line.split()[0]: line for line in capsys.readouterr().out.splitlines() if not line.startswith(" ")}
    assert rows["linear"].split()[1:3] == ["set", "up"]
    assert rows["slack"].split()[1:3] == ["reads", "only"]
    assert rows["granola"].split()[1:4] == ["not", "set", "up"]


def test_list_says_when_it_cant_tell(fake_keyring, monkeypatch, capsys):
    def locked(service, name):
        raise KeyringLocked("locked")

    monkeypatch.setattr(fake_keyring, "get_password", locked)
    assert cli.main(["tools", "list"]) == 0
    assert all("can't tell" in line for line in capsys.readouterr().out.splitlines() if not line.startswith(" "))


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


def test_a_node_that_writes_with_a_read_only_sign_in_gets_a_warning(repo):
    keychain.save("linear", {"method": "oauth", "access_token": "x", "write": False})
    assert problems(flow("[{name: linear, write: true}]")) == [
        ("warning", "nodes.read.tools", "`linear` is set up to read only, so a node that writes with it fails when it runs: "
                                        "run `wf tools linear setup --write`")]


def shell_flow(harness: str, access: str, extra: str = "", tools: str = "[linear]") -> str:
    Path("flow.yaml").write_text(dedent(f"""
        version: 1
        name: shell
        defaults:
          harnesses: {{claude-code: {{model: opus}}, cursor: {{model: composer-2.5}}}}
        start: {{input: {{}}}}
        nodes:
          brief:
            needs: [start]
            harness: {harness}
            access: {access}
            tools: {tools}
            instruction: Write up the open issues
        """) + "".join(f"    {line}\n" for line in extra.splitlines() if line) + "end: {output: {}}\n")
    return "flow.yaml"


@pytest.mark.parametrize("harness, access, reaches", [
    ("claude-code", "read-only", False), ("claude-code", "edit", True), ("claude-code", "full", True),
    ("codex", "read-only", False), ("codex", "edit", False), ("codex", "full", True),
    ("cursor", "read-only", False), ("cursor", "edit", True), ("cursor", "full", True),
])
def test_a_node_with_tools_and_a_shell_that_reaches_the_keychain_needs_the_opt_in(repo, harness, access, reaches):
    keychain.save("linear", {"method": "token", "access_token": "x"})
    found = problems(shell_flow(harness, access))
    expected = [("error", "nodes.brief", f"this node has linear, and can run on {harness} at `{access}` access, where its "
                                          "commands could read every stored sign-in: give the tools to a read-only node, "
                                          "or one on Codex at `edit`, or set `tools_with_shell: true` to accept that")]
    assert found == (expected if reaches else [])


def test_a_node_that_opts_in_gets_a_warning_naming_it_and_its_tools(repo):
    keychain.save("linear", {"method": "token", "access_token": "x"})
    keychain.save("slack", {"method": "token", "access_token": "x"})
    assert problems(shell_flow("cursor", "full", "tools_with_shell: true", tools="[linear, slack]")) == [
        ("warning", "nodes.brief", "`tools_with_shell: true`: the agent of `brief` has linear, slack, and its commands on "
                                   "cursor at `full` access could read every stored sign-in")]


def test_a_harness_list_names_each_harness_whose_shell_reaches_the_keychain(repo):
    keychain.save("linear", {"method": "token", "access_token": "x"})
    flow_text = Path(shell_flow("[codex, claude-code, cursor]", "edit")).read_text().replace(
        "harnesses: {claude-code: {model: opus}, cursor: {model: composer-2.5}}",
        "harnesses: {claude-code: {model: opus}, cursor: {model: composer-2.5}}\n  model: {claude-code: opus, codex: gpt-6, cursor: composer-2.5}")
    Path("flow.yaml").write_text(flow_text)
    (level, where, message), = problems("flow.yaml")
    assert (level, where) == ("error", "nodes.brief") and "can run on claude-code and cursor at `edit` access" in message


@pytest.mark.parametrize("extra, tools, found", [
    ("", "[]", []),  # no tools: nothing to read
    ("tools_with_shell: \"yes\"", "[linear]", [("error", "nodes.brief.tools_with_shell", "Input should be a valid boolean")]),
])
def test_tools_with_shell_takes_a_boolean_and_matters_only_with_tools(repo, extra, tools, found):
    keychain.save("linear", {"method": "token", "access_token": "x"})
    assert problems(shell_flow("claude-code", "full", extra, tools=tools)) == found


def test_tools_with_shell_goes_on_the_node_not_in_defaults(repo):
    text = Path(shell_flow("claude-code", "read-only")).read_text().replace("defaults:\n", "defaults:\n  tools_with_shell: true\n")
    Path("flow.yaml").write_text(text)
    assert ("error", "defaults.tools_with_shell", "unknown key") in problems("flow.yaml")


def test_tools_in_defaults_reach_every_agent_node(repo):
    text = Path(flow("[linear]")).read_text().replace("    tools: [linear]\n", "").replace(
        "defaults:\n", "defaults:\n  tools: [notion]\n")
    Path("flow.yaml").write_text(text)
    assert problems("flow.yaml") == [("warning", "defaults.tools", "`notion` isn't set up on this machine, so the node fails "
                                                                    "when it runs: run `wf tools notion setup`")]


def test_tools_in_defaults_are_checked_even_without_an_agent_node(repo):
    Path("flow.yaml").write_text(dedent("""
        version: 1
        name: commands
        defaults: {tools: [nope, linear, linear]}
        start: {input: {}}
        nodes:
          build: {needs: [start], command: "true"}
        end: {output: {}}
        """))
    assert [(level, where) for level, where, _ in problems("flow.yaml")] == [
        ("error", "defaults.tools[0]"), ("warning", "defaults.tools"), ("error", "defaults.tools[2]")]


@pytest.mark.parametrize("tools, where, message", [
    ("[{name: linear, wirte: true}]", "nodes.read.tools[0].wirte", "unknown key"),
    ("[{name: linear, write: \"no\"}]", "nodes.read.tools[0].write", "Input should be a valid boolean"),
    ("[3]", "nodes.read.tools[0]", "expected a tool name, or {name: ..., write: true}"),
    ("linear", "nodes.read.tools", "Input should be a valid list"),
])
def test_tools_entries_have_a_shape(repo, tools, where, message):
    assert problems(flow(tools)) == [("error", where, message)]


def test_only_agent_nodes_take_tools(repo):
    flow("[linear]", nodes="  check:\n    needs: [read]\n    command: \"true\"\n    tools: [linear]\n")
    assert ("error", "nodes.check.tools", "unknown key") in problems("flow.yaml")


def test_a_full_access_agent_that_reads_what_a_tool_using_agent_wrote_gets_a_warning(repo):
    keychain.save("linear", {"method": "token", "access_token": "x"})
    flow("[linear]", nodes=("  act:\n    needs: [read]\n    access: full\n"
                            "    instruction: \"Do what this says: {{ read.summary }}\"\n"))
    assert problems("flow.yaml") == [
        ("warning", "nodes.act.instruction", "an agent with `access: full` reads output from `read`, which reads linear; "
                                             "messages, notes and mail that other people write can carry instructions "
                                             "aimed at agents")]


def test_attach_refuses_a_writing_node_a_read_only_sign_in(vendor):
    sign_in(vendor, write=False)

    async def attach():
        async with relay.attach([ToolUse(name="linear", write=True)]):
            pass

    with pytest.raises(signin.NotSetUp) as refused:
        asyncio.run(attach())
    assert refused.value.fix == "run `wf tools linear setup --write`"
