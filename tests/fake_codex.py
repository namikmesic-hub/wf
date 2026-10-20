"""A fake `codex app-server` for tests: the same line-delimited JSON-RPC over stdio, scripted.

    python fake_codex.py scenario.json requests.jsonl

scenario.json: {"models": [...], "config": {...}, "skills": [...], "silent": [method, ...], "turns": [[action, ...], ...]}.
config/read answers with "config" (none: an empty one), and skills/list with "skills" for each cwd.
Requests for a "silent" method never get an answer (a wedged app server). Each turn/start
takes the next list of actions:
    {"tool": {"arguments": {...}, "name": "submit_result"}}  call a dynamic tool, wait for the reply
    {"tool": {"arguments": {...}, "name": "...", "namespace": "linear"}}   one in a namespace
    {"tool": {...}, "background": true}                      send the call and go on without waiting; the
                                                             turn ends once its reply is in
    {"approval": "item/commandExecution/requestApproval"}  ask for approval, wait for the reply
    {"item": {"type": "commandExecution", "command": "ls"}} item/started + item/completed
    {"started": {"type": "commandExecution", "command": "ls"}}  item/started only: the item never completes
    {"tokens": 1200}                                         a response of 1200 tokens: thread/tokenUsage/updated
                                                             (total: the thread's running total, last: 1200)
    {"error": {"codexErrorInfo": "usageLimitExceeded", "message": "...", "willRetry": false}}
    {"end": "failed", "message": "..."}                      how the turn ends (default: completed)
    {"hang": true}                                           wait for turn/interrupt
    {"exit": true}                                           the app server dies
Every request the client sends, and every reply to the fake's own requests, is
appended to requests.jsonl.
"""

import asyncio
import itertools
import json
import os
import sys

scenario = json.load(open(sys.argv[1]))
log_path = sys.argv[2]
turns = list(scenario.get("turns", []))
models = scenario.get("models") or [{"id": "gpt-default", "model": "gpt-default", "isDefault": True, "hidden": False,
                                      "defaultReasoningEffort": "medium", "supportedReasoningEfforts": [],
                                      "description": "", "displayName": "default"}]


def log(kind: str, data) -> None:
    with open(log_path, "a") as file:
        file.write(json.dumps({"kind": kind, "data": data}) + "\n")


def write(message: dict) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


async def main() -> None:
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    ids, counter = itertools.count(1000), itertools.count(1)
    pending: dict[int, asyncio.Future] = {}
    interrupts: dict[str, asyncio.Event] = {}
    totals: dict[str, int] = {}

    async def call(method: str, params: dict):
        request_id = next(ids)
        pending[request_id] = loop.create_future()
        write({"id": request_id, "method": method, "params": params})
        return await pending[request_id]

    async def tool_call(thread: str, turn: str, tool: dict) -> None:
        log("reply", await call("item/tool/call", {"threadId": thread, "turnId": turn, "callId": f"call-{next(ids)}",
                                                   "tool": tool.get("name", "submit_result"), "namespace": tool.get("namespace"),
                                                   "arguments": tool["arguments"]}))

    async def run_turn(thread: str, turn: str, actions: list) -> None:
        write({"method": "turn/started", "params": {"threadId": thread, "turn": {"id": turn, "items": [], "status": "inProgress"}}})
        status, error, background = "completed", None, []
        for action in actions:
            if "tool" in action and action.get("background"):
                background.append(asyncio.ensure_future(tool_call(thread, turn, action["tool"])))
            elif "tool" in action:
                await tool_call(thread, turn, action["tool"])
            elif "approval" in action:
                log("reply", await call(action["approval"], {"threadId": thread, "turnId": turn, "itemId": "i1"}))
            elif "item" in action or "started" in action:
                item = {"id": f"item-{next(ids)}", **(action.get("item") or action["started"])}
                write({"method": "item/started", "params": {"threadId": thread, "turnId": turn, "item": item}})
                if "item" in action:
                    write({"method": "item/completed", "params": {"threadId": thread, "turnId": turn, "item": item}})
            elif "tokens" in action:
                totals[thread] += action["tokens"]
                def usage(tokens: int) -> dict:
                    return {"totalTokens": tokens, "inputTokens": tokens, "cachedInputTokens": 0,
                            "outputTokens": 0, "reasoningOutputTokens": 0}
                write({"method": "thread/tokenUsage/updated",
                       "params": {"threadId": thread, "turnId": turn,
                                  "tokenUsage": {"total": usage(totals[thread]), "last": usage(action["tokens"])}}})
            elif "error" in action:
                e = action["error"]
                write({"method": "error", "params": {"threadId": thread, "turnId": turn, "willRetry": e.get("willRetry", False),
                                                     "error": {"message": e["message"], "codexErrorInfo": e.get("codexErrorInfo")}}})
            elif "end" in action:
                status = action["end"]
                error = {"message": action.get("message", ""), "codexErrorInfo": action.get("codexErrorInfo")}
            elif "exit" in action:  # the app server dies mid-turn
                sys.stdout.flush()
                os._exit(1)
            elif "hang" in action:
                await interrupts.setdefault(turn, asyncio.Event()).wait()
                status = "interrupted"
        await asyncio.gather(*background)
        write({"method": "turn/completed", "params": {"threadId": thread, "turn": {"id": turn, "items": [], "status": status, "error": error}}})

    while line := await reader.readline():
        message = json.loads(line)
        if "method" not in message:  # a reply to one of our requests
            pending.pop(message["id"]).set_result(message.get("result", {"error": message.get("error")}))
            continue
        log("request", message)
        method, params, request_id = message["method"], message.get("params") or {}, message.get("id")
        if method in scenario.get("silent", []):
            continue
        if method == "initialize":
            write({"id": request_id, "result": {"userAgent": "fake", "codexHome": "/tmp", "platformFamily": "unix", "platformOs": "macos"}})
        elif method == "model/list":
            write({"id": request_id, "result": {"data": models}})
        elif method == "config/read":
            write({"id": request_id, "result": {"config": scenario.get("config", {}), "origins": {}}})
        elif method == "skills/list":
            write({"id": request_id, "result": {"data": [{"cwd": cwd, "skills": scenario.get("skills", []), "errors": []}
                                                         for cwd in params.get("cwds") or []]}})
        elif method == "thread/start":
            thread = f"thread-{next(counter)}"
            totals[thread] = 0
            write({"id": request_id, "result": {"thread": {"id": thread}, "model": params.get("model") or "gpt-default"}})
        elif method == "thread/fork":  # a copy of the thread, history (and token total) included
            thread = f"thread-{next(counter)}"
            totals[thread] = totals.get(params["threadId"], 0)
            write({"id": request_id, "result": {"thread": {"id": thread}, "model": params.get("model") or "gpt-default"}})
        elif method == "turn/start":
            turn = f"turn-{next(counter)}"
            write({"id": request_id, "result": {"turn": {"id": turn, "items": [], "status": "inProgress"}}})
            asyncio.ensure_future(run_turn(params["threadId"], turn, turns.pop(0) if turns else []))
        elif method == "turn/interrupt":
            interrupts.setdefault(params["turnId"], asyncio.Event()).set()
            write({"id": request_id, "result": {}})
        elif request_id is not None:
            write({"id": request_id, "error": {"code": -32601, "message": f"fake doesn't know {method}"}})


asyncio.run(main())
