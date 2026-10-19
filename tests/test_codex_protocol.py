"""Protocol drift: does the installed Codex still speak what the adapter expects?

The app server is experimental, so this regenerates its JSON Schema
(`codex app-server generate-json-schema --experimental`) and checks every
method and field codex.py relies on. No model call, no quota. Skipped when
`codex` isn't installed.
"""

import json
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.skipif(shutil.which("codex") is None, reason="codex isn't installed")


@pytest.fixture(scope="module")
def schema(tmp_path_factory):
    out = tmp_path_factory.mktemp("codex-schema")
    subprocess.run(["codex", "app-server", "generate-json-schema", "--experimental", "--out", str(out)],
                   check=True, capture_output=True)

    def load(name: str) -> dict:
        path = out / f"{name}.json" if (out / f"{name}.json").exists() else out / "v2" / f"{name}.json"
        return json.loads(path.read_text())

    return load


def methods(spec: dict) -> set[str]:
    return {v["properties"]["method"]["enum"][0] for v in spec.get("oneOf", spec.get("anyOf", []))}


def test_the_methods_wf_uses_exist(schema):
    assert {"initialize", "model/list", "thread/start", "thread/fork", "turn/start", "turn/interrupt"} <= methods(schema("ClientRequest"))
    assert {"item/tool/call", "item/commandExecution/requestApproval", "item/fileChange/requestApproval"} <= methods(schema("ServerRequest"))
    assert {"turn/completed", "item/started", "item/completed", "thread/tokenUsage/updated", "error"} <= methods(schema("ServerNotification"))


def test_thread_start_takes_the_settings_wf_sends(schema):
    params = schema("ThreadStartParams")
    assert {"cwd", "model", "sandbox", "approvalPolicy", "ephemeral", "config", "dynamicTools"} <= set(params["properties"])
    assert params["definitions"]["SandboxMode"]["enum"] == ["read-only", "workspace-write", "danger-full-access"]


def test_dynamic_tool_calls_and_replies_have_the_expected_shape(schema):
    call = schema("DynamicToolCallParams")
    assert {"threadId", "tool", "arguments"} <= set(call["required"])
    reply = schema("DynamicToolCallResponse")
    assert set(reply["required"]) == {"contentItems", "success"}


def test_usage_limits_and_token_counts_are_reported_as_expected(schema):
    error = schema("ErrorNotification")
    info = json.dumps(error["definitions"]["CodexErrorInfo"])
    assert "usageLimitExceeded" in info and "unauthorized" in info and "rateLimitExceeded" in info
    usage = schema("ThreadTokenUsageUpdatedNotification")["definitions"]["TokenUsageBreakdown"]
    assert "totalTokens" in usage["required"]


def test_dynamic_tools_come_in_namespaces_and_calls_name_theirs(schema):
    tools = schema("ThreadStartParams")["definitions"]["DynamicToolSpec"]["oneOf"]
    namespace = next(variant for variant in tools if variant["properties"]["type"]["enum"] == ["namespace"])
    assert {"name", "description", "tools", "type"} <= set(namespace["required"])  # what codex.py's _namespace sends
    assert "namespace" in schema("DynamicToolCallParams")["properties"]  # how a call names its tool's namespace
    items = schema("DynamicToolCallResponse")["definitions"]["DynamicToolCallOutputContentItem"]["oneOf"]
    assert {"inputText", "inputImage"} <= {item["properties"]["type"]["enum"][0] for item in items}
