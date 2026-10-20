"""A fake `cursor-agent` for tests: `--list-models`, and `acp`, the Agent Client Protocol over stdio, scripted.

    python fake_acp.py scenario.json requests.jsonl [cursor-agent arguments]

scenario.json: {"models": [...], "version": "...", "logged_out": false, "silent": [method, ...],
"turns": [[action, ...], ...]}. `--version` prints the version, and `--list-models` the models as
cursor-agent does, or fails when "logged_out". Requests for a
"silent" method never get an answer. Every session is its own process, so the turns are shared
through a counter file next to requests.jsonl: each session/prompt takes the next list of actions:
    {"text": "..."}                                    an agent_message_chunk
    {"tool": {"name": "submit_result", "arguments": {...}}}  call a tool on the session's MCP server as
                                                       Cursor does: tool call updates, a permission
                                                       request, then tools/call over HTTP if allowed
    {"tool": {"server": "linear", "name": "list_issues", "arguments": {...}}}  the same, on another
                                                       MCP server that session/new named
    {"permission": {"kind": "execute", "title": "`ls`", ...}}  ask for permission, record the answer
    {"error": "Upgrade your plan to continue"}         how Cursor reports a failed request: "\n\n" + text
    {"error": ["Error: [resource", "_exhausted] ..."]}  the same, sent in several chunks
    {"stop": "cancelled"}                              the turn's stopReason (default end_turn)
    {"rpc_error": {"code": -32000, "message": "..."}}  answer session/prompt with this error
    {"child": true}                                    start a process that ignores SIGTERM, and log its pid
    {"hang": true}                                     wait for session/cancel
    {"exit": true}                                     die mid-turn
Sessions are kept in CURSOR_CONFIG_DIR/acp-sessions/<id>, as Cursor keeps them, so session/load
works across processes; it replays the history first, as Cursor does. A new or loaded session
reports its model and parameters in configOptions: the effort in a model id only when initialize
asked for parameterized models, as with Cursor. Every request, the arguments,
folder (and the files in it), environment and cli-config.json each process started with, the MCP tools offered (`tools` for wf's server,
`server_tools` for the others), and every answer to the fake's own requests are appended to requests.jsonl.
"""

import asyncio
import itertools
import json
import os
import subprocess
import sys
import urllib.request
import uuid
from pathlib import Path

scenario = json.load(open(sys.argv[1]))
log_path = Path(sys.argv[2])
args = sys.argv[3:]
models = scenario.get("models") or ["composer-2.5", "gpt-5.6-sol-max-fast"]
EFFORT = {"gpt-5.6-sol-max-fast": ("gpt-5.6-sol", {"reasoning": "max", "fast": "true"})}


def config_options(model: str, parameterized: bool) -> list[dict]:
    name, parameters = EFFORT.get(model, (model, {}))
    if not parameterized and parameters:
        parameters = {"reasoning": "medium", "fast": "false"}  # the model's defaults, not the id's
    return [{"id": "mode", "currentValue": "agent"}, {"id": "model", "currentValue": name},
            *({"id": key, "currentValue": value} for key, value in parameters.items())]


def log(kind: str, data) -> None:
    with log_path.open("a") as file:
        file.write(json.dumps({"kind": kind, "data": data}) + "\n")


def write(message: dict) -> None:
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", **message}) + "\n")
    sys.stdout.flush()


def next_turn() -> list:
    counter = log_path.with_suffix(".turn")
    index = int(counter.read_text()) if counter.exists() else 0
    counter.write_text(str(index + 1))
    turns = scenario.get("turns", [])
    return turns[index] if index < len(turns) else []


def post(url: str, message: dict) -> dict:
    request = urllib.request.Request(url, json.dumps({"jsonrpc": "2.0", **message}).encode(),
                                     {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"})
    with urllib.request.urlopen(request, timeout=10) as reply:
        body = reply.read()
    return json.loads(body) if body else {}


async def main() -> None:
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=64 * 1024 * 1024)
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    ids, calls = itertools.count(1000), itertools.count(1)
    pending: dict[int, asyncio.Future] = {}
    cancelled = asyncio.Event()
    sessions = Path(os.environ["CURSOR_CONFIG_DIR"]) / "acp-sessions"
    state = {"urls": {}, "connected": set(), "parameterized": False}
    model = args[args.index("--model") + 1] if "--model" in args else "default"

    async def ask(method: str, params: dict):
        request_id = next(ids)
        pending[request_id] = loop.create_future()
        write({"id": request_id, "method": method, "params": params})
        return await pending[request_id]

    def update(session: str, body: dict) -> None:
        write({"method": "session/update", "params": {"sessionId": session, "update": body}})

    async def use_tool(session: str, name: str, arguments: dict, server: str = "wf") -> None:
        url = state["urls"][server]
        if server not in state["connected"]:  # connect the way Cursor does, before the first call
            state["connected"].add(server)
            await asyncio.to_thread(post, url, {"id": 0, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}})
            await asyncio.to_thread(post, url, {"method": "notifications/initialized"})
            listed = await asyncio.to_thread(post, url, {"id": 1, "method": "tools/list", "params": {}})
            names = [t["name"] for t in listed["result"]["tools"]]
            log("tools", names) if server == "wf" else log("server_tools", {"server": server, "names": names})
        call = f"call-{next(calls)}"
        update(session, {"sessionUpdate": "tool_call", "toolCallId": call, "title": "MCP: tool", "kind": "other", "status": "pending", "rawInput": {}})
        update(session, {"sessionUpdate": "tool_call_update", "toolCallId": call, "title": f"{server}: {name}",
                         "rawInput": {"providerIdentifier": server, "toolName": name, "args": arguments}})
        update(session, {"sessionUpdate": "tool_call_update", "toolCallId": call, "status": "in_progress"})
        answer = await ask("session/request_permission", {
            "sessionId": session, "toolCall": {"toolCallId": call, "title": f"wf-{name}: {name}", "kind": "other", "status": "pending"},
            "options": [{"optionId": "allow-once", "name": "Allow once", "kind": "allow_once"},
                        {"optionId": "allow-always", "name": "Allow always", "kind": "allow_always"},
                        {"optionId": "reject-once", "name": "Reject", "kind": "reject_once"}]})
        log("permission", answer)
        if answer.get("outcome", {}).get("optionId") == "allow-once":
            reply = await asyncio.to_thread(post, url, {"id": next(ids), "method": "tools/call",
                                                                 "params": {"name": name, "arguments": arguments}})
            log("reply", reply["result"])
        update(session, {"sessionUpdate": "tool_call_update", "toolCallId": call, "status": "completed", "rawOutput": {"success": True}})

    async def run_turn(session: str, request_id: int, actions: list) -> None:
        stop = "end_turn"
        cancelled.clear()
        for action in actions:
            if "rpc_error" in action:
                write({"id": request_id, "error": action["rpc_error"]})
                return
            if "child" in action:
                child = subprocess.Popen([sys.executable, "-c", CHILD])
                log("child", child.pid)
            if "text" in action:
                update(session, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": action["text"]}})
            elif "tool" in action:
                await use_tool(session, action["tool"].get("name", "submit_result"), action["tool"]["arguments"],
                               action["tool"].get("server", "wf"))
            elif "permission" in action:
                asked = dict(action["permission"])
                options = asked.pop("options", None) or [
                    {"optionId": "allow-once", "name": "Allow once", "kind": "allow_once"},
                    {"optionId": "allow-always", "name": "Allow always", "kind": "allow_always"},
                    {"optionId": "reject-once", "name": "Reject", "kind": "reject_once"}]
                call = f"call-{next(calls)}"
                update(session, {"sessionUpdate": "tool_call", "toolCallId": call, "status": "pending", **asked})
                update(session, {"sessionUpdate": "tool_call_update", "toolCallId": call, "status": "in_progress"})
                answer = await ask("session/request_permission", {
                    "sessionId": session, "toolCall": {"toolCallId": call, "status": "pending", **asked}, "options": options})
                log("permission", answer)
            elif "error" in action:
                chunks = action["error"] if isinstance(action["error"], list) else [action["error"]]
                for index, chunk in enumerate(chunks):
                    update(session, {"sessionUpdate": "agent_message_chunk",
                                     "content": {"type": "text", "text": ("\n\n" if index == 0 else "") + chunk}})
            elif "stop" in action:
                stop = action["stop"]
            elif "hang" in action:
                await cancelled.wait()
                stop = "cancelled"
            elif "exit" in action:
                sys.stdout.flush()
                os._exit(1)
        write({"id": request_id, "result": {"stopReason": stop}})

    while line := await reader.readline():
        message = json.loads(line)
        if "method" not in message:  # an answer to one of our requests
            pending.pop(message["id"]).set_result(message.get("result", {"error": message.get("error")}))
            continue
        log("request", message)
        method, params, request_id = message["method"], message.get("params") or {}, message.get("id")
        if method in scenario.get("silent", []):
            continue
        if method == "initialize":
            state["parameterized"] = bool((params.get("clientCapabilities") or {}).get("_meta", {}).get("parameterizedModelPicker"))
            write({"id": request_id, "result": {"protocolVersion": 1, "authMethods": [{"id": "cursor_login", "name": "Cursor Login"}],
                                                "agentCapabilities": {"loadSession": True, "mcpCapabilities": {"http": True, "sse": True}}}})
        elif method == "authenticate":
            write({"id": request_id, "result": {}})
        elif method in ("session/new", "session/load"):
            session = params.get("sessionId") or str(uuid.uuid4())
            if method == "session/load" and not (sessions / session).exists():
                write({"id": request_id, "error": {"code": -32602, "message": "Invalid params", "data": {"message": f'Session "{session}" not found'}}})
                continue
            (sessions / session).mkdir(parents=True, exist_ok=True)
            (sessions / session / "meta.json").write_text(json.dumps({"cwd": params["cwd"]}))
            state["urls"] = {server["name"]: server["url"] for server in params["mcpServers"]}
            if method == "session/load":  # Cursor replays the conversation before it answers
                update(session, {"sessionUpdate": "user_message_chunk", "content": {"type": "text", "text": "an earlier instruction"}})
                update(session, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "an earlier answer"}})
            options = {"configOptions": config_options(model, state["parameterized"])}
            write({"id": request_id, "result": {"sessionId": session, **options} if method == "session/new" else options})
        elif method == "session/set_mode":
            write({"id": request_id, "result": {}})
        elif method == "session/prompt":
            asyncio.ensure_future(run_turn(params["sessionId"], request_id, next_turn()))
        elif method == "session/cancel":
            cancelled.set()
        elif request_id is not None:
            write({"id": request_id, "error": {"code": -32601, "message": f"fake doesn't know {method}"}})


CHILD = "import signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\nwhile True: time.sleep(0.1)"

if args[-1:] == ["--version"]:
    print(scenario.get("version", "0"))
    sys.exit(0)
if args[-1:] == ["--list-models"]:
    log("list-models", {"config": os.environ.get("CURSOR_CONFIG_DIR")})
    if scenario.get("logged_out"):
        print("Error: not logged in. Run `cursor-agent login`.", file=sys.stderr)
        sys.exit(1)
    print("Available models\n\n" + "\n".join(f"{model} - {model.title()}" for model in models))
    sys.exit(0)
config = os.environ.get("CURSOR_CONFIG_DIR")
log("start", {"args": args, "cwd": os.getcwd(), "files": sorted(os.listdir()), "config": config,
              "data": os.environ.get("CURSOR_DATA_DIR"),
              "settings": json.loads(Path(config, "cli-config.json").read_text()),
              "api_key": "CURSOR_API_KEY" in os.environ, "auth_token": "CURSOR_AUTH_TOKEN" in os.environ,
              "no_browser": os.environ.get("NO_OPEN_BROWSER")})
asyncio.run(main())
