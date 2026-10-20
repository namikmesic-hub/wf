// A fake @cursor/sdk for wf's tests. The tests copy the Cursor host into a folder with this
// package as node_modules/@cursor/sdk, so the real host runs on it.
//
// FAKE_CURSOR_SDK_SCENARIO names a JSON file: {"key": "key_test", "catalog": [...], "create_error":
// {...}, "silent_create": false, "steer": "delivered" | "revert" | "hang" | "throw" | "none",
// "usage": true, "billed": {...}, "turns": [[action, ...], ...]}. Each send() takes the next list
// of actions. A counter file next to the log shares them across host processes:
//   {"text": "..."}                                   an assistant message
//   {"tool": {"name": "submit_result", "server": null, "arguments": {...}}}  call one of the
//                                                     session's custom tools, as Cursor does
//   {"call": {"name": "shell", "args": {...}, "late_args": {...}, "result": ..., "status": "completed"}}
//                                                     one of Cursor's own tools
//   {"mcp": {"server": "github", "tool": "create_issue", "args": {...}}}  another MCP server's tool
//   {"usage": {"inputTokens": 1, ...}}                a usage message
//   {"error": {"name": "RateLimitError", "message": "...", "isRetryable": false}}  the stream throws it
//   {"run_error": {"message": "...", "code": "..."}}  the run ends with status "error"
//   {"hang": true}                                    wait until the run is cancelled
//   {"hang": true, "usage": {...}}                    the same, and report that usage as it ends
//   {"pause": "p"}                                    write p.paused next to the log, wait for p.go
//   {"exit": true} | {"child": true} | {"unhandled": true} | {"sleep": 100}
// Without "usage": false each turn ends with a usage message of USAGE.
// FAKE_CURSOR_SDK_LOG names a JSON lines file: every call the host makes, with the options it
// gave (functions as names), each custom tool's answer, and the environment the host runs in.

import { spawn } from "node:child_process";
import { appendFileSync, existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";

const SCENARIO = JSON.parse(readFileSync(process.env.FAKE_CURSOR_SDK_SCENARIO, "utf8"));
const LOG = process.env.FAKE_CURSOR_SDK_LOG;
const KEY = SCENARIO.key ?? "key_test";
const USAGE = { inputTokens: 1000, outputTokens: 200, cacheReadTokens: 600, cacheWriteTokens: 0, totalTokens: 1200 };

function log(kind, data = {}) {
  appendFileSync(LOG, JSON.stringify({ kind, data }) + "\n");
}

log("loaded", {
  api_key_env: "CURSOR_API_KEY" in process.env, auth_token_env: "CURSOR_AUTH_TOKEN" in process.env,
  no_browser: process.env.NO_OPEN_BROWSER ?? null, cwd: process.cwd(), argv: process.argv.slice(1),
});

export class CursorSdkError extends Error {
  constructor(message, options = {}) {
    super(message);
    this.name = options.errorName ?? "CursorSdkError";
    this.code = options.code;
    this.status = options.status;
    this.isRetryable = options.isRetryable ?? false;
  }
}

function errorClass(name) {
  return class extends CursorSdkError {
    constructor(message, options = {}) {
      super(message, { ...options, errorName: name });
    }
  };
}

export const AuthenticationError = errorClass("AuthenticationError");
export const RateLimitError = errorClass("RateLimitError");
export const ConfigurationError = errorClass("ConfigurationError");
export const AgentBusyError = errorClass("AgentBusyError");
export const NetworkError = errorClass("NetworkError");
export const AgentNotFoundError = errorClass("AgentNotFoundError");
const CLASSES = { AuthenticationError, RateLimitError, ConfigurationError, AgentBusyError, NetworkError, AgentNotFoundError, CursorSdkError };

function made(spec) {
  const Made = CLASSES[spec.name] ?? Error;
  return new Made(spec.message ?? "fake error", spec);
}

function variants(parameters, defaults) {
  let all = [[]];
  for (const parameter of parameters) {
    all = all.flatMap((params) => parameter.values.map((v) => [...params, { id: parameter.id, value: v.value }]));
  }
  return all.map((params) => ({
    params, displayName: params.map((p) => p.value).join(" "),
    isDefault: params.every((p) => defaults[p.id] === p.value),
  }));
}

function model(id, parameters, defaults) {
  return { id, displayName: id, parameters, variants: variants(parameters, defaults) };
}

const values = (...list) => list.map((value) => ({ value }));
const CATALOG = [
  model("composer-2.5", [{ id: "fast", values: values("false", "true") }], { fast: "false" }),
  model("gpt-5.6-sol", [
    { id: "reasoning", values: values("none", "low", "medium", "high", "xhigh", "max") },
    { id: "fast", values: values("false", "true") },
    { id: "context", values: values("272k", "1m") },
  ], { reasoning: "medium", fast: "false", context: "1m" }),
  { id: "default", displayName: "Auto" },
];

function checkKey(apiKey) {
  if (apiKey !== KEY) throw new AuthenticationError("Invalid User API Key", { status: 401, code: "error" });
}

function plain(value) {
  return JSON.parse(JSON.stringify(value ?? null, (key, inner) => {
    if (typeof inner === "function") return `[function ${key}]`;
    if (inner instanceof JsonlLocalAgentStore) return { store: inner.rootDir };
    return inner;
  }));
}

export class JsonlLocalAgentStore {
  constructor(rootDir) {
    this.rootDir = rootDir;
  }

  save(agentId) {
    mkdirSync(this.rootDir, { recursive: true });
    appendFileSync(join(this.rootDir, "agents.ndjson"), JSON.stringify({ agentId }) + "\n");
  }

  has(agentId) {
    const file = join(this.rootDir, "agents.ndjson");
    return existsSync(file) && readFileSync(file, "utf8").split("\n").some((line) => line && JSON.parse(line).agentId === agentId);
  }
}

export const Cursor = {
  models: {
    async list(options = {}) {
      log("models.list", { api_key: options.apiKey ?? null });
      checkKey(options.apiKey);
      return SCENARIO.catalog ?? CATALOG;
    },
  },
  async me(options = {}) {
    checkKey(options.apiKey);
    return { apiKeyName: "wf tests", userEmail: "someone@example.com", createdAt: "2026-10-20T00:00:00Z" };
  },
};

function never() {
  return new Promise(() => setInterval(() => {}, 1000));
}

export const Agent = {
  async create(options) {
    log("create", plain(options));
    if (SCENARIO.silent_create) await never();
    checkKey(options.apiKey);
    if (SCENARIO.create_error) throw made(SCENARIO.create_error);
    const catalog = SCENARIO.catalog ?? CATALOG;
    if (!catalog.some((m) => m.id === options.model?.id)) {
      throw new ConfigurationError(`Cannot use this model: ${options.model?.id}. Available models: ${catalog.map((m) => m.id).join(", ")}.`);
    }
    const agentId = `agent-${Math.random().toString(36).slice(2, 10)}`;
    options.local?.store?.save(agentId);
    return new FakeAgent(agentId, options);
  },

  async resume(agentId, options) {
    log("resume", { agentId, ...plain(options) });
    checkKey(options.apiKey);
    if (!options.local?.store?.has(agentId)) throw new AgentNotFoundError(`Agent ${agentId} not found`, { code: "agent_not_found" });
    return new FakeAgent(agentId, options);
  },
};

function nextTurn() {
  const counter = `${LOG}.turn`;
  const index = existsSync(counter) ? Number(readFileSync(counter, "utf8")) : 0;
  writeFileSync(counter, String(index + 1));
  return (SCENARIO.turns ?? [])[index] ?? [];
}

class FakeAgent {
  constructor(agentId, options) {
    this.agentId = agentId;
    this.options = options;
    this.model = options.model;
  }

  async send(text, sendOptions = {}) {
    log("send", { agentId: this.agentId, text, options: plain(sendOptions) });
    return new FakeRun(this, nextTurn(), sendOptions);
  }

  close() {
    log("close", { agentId: this.agentId });
  }

  async getUsage() {
    log("getUsage", { agentId: this.agentId });
    return SCENARIO.billed ?? { usage: USAGE, cost: { rawCostCents: 1.5, chargedCents: 0 }, runs: [] };
  }
}

let calls = 0;

class FakeRun {
  constructor(agent, actions, sendOptions) {
    this.id = `run-${Math.random().toString(36).slice(2, 8)}`;
    this.agentId = agent.agentId;
    this.agent = agent;
    this.tools = sendOptions.local?.customTools ?? agent.options.local?.customTools ?? {};
    this.status = "running";
    this.messages = [];
    this.finished = false;
    this.wake = () => {};
    this.cancelled = false;
    this.onCancel = () => {};
    if (SCENARIO.steer === "none") this.steer = undefined;
    this.done = this.play(actions);
  }

  push(message) {
    this.messages.push({ agent_id: this.agentId, run_id: this.id, ...message });
    this.wake();
  }

  async play(actions) {
    try {
      for (const action of actions) {
        if (this.cancelled) break;
        await this.act(action);
      }
      if (!this.cancelled && SCENARIO.usage !== false && this.status === "running" && !this.thrown) {
        this.push({ type: "usage", usage: USAGE });
      }
      if (this.cancelled && this.cancelUsage) this.push({ type: "usage", usage: this.cancelUsage });
    } catch (error) {
      this.thrown = error;
    }
    if (this.status === "running") this.status = this.cancelled ? "cancelled" : this.thrown ? "error" : "finished";
    this.finished = true;
    this.wake();
  }

  async act(action) {
    if ("text" in action) this.push({ type: "assistant", message: { role: "assistant", content: [{ type: "text", text: action.text }] } });
    if ("tool" in action) await this.custom(action.tool);
    if ("call" in action) {
      const call = action.call;
      const id = `call-${++calls}`;
      this.push({ type: "tool_call", call_id: id, name: call.name, status: "running", args: call.args ?? {} });
      if (call.late_args) this.push({ type: "tool_call", call_id: id, name: call.name, status: "running", args: call.late_args });
      this.push({ type: "tool_call", call_id: id, name: call.name, status: call.status ?? "completed", args: call.late_args ?? call.args ?? {}, result: call.result ?? { ok: true } });
    }
    if ("mcp" in action) {
      const id = `call-${++calls}`;
      const args = { providerIdentifier: action.mcp.server, toolName: action.mcp.tool, args: action.mcp.args ?? {} };
      this.push({ type: "tool_call", call_id: id, name: "mcp", status: "running", args });
      await new Promise((resolve) => setTimeout(resolve, 200));
      if (!this.cancelled) this.push({ type: "tool_call", call_id: id, name: "mcp", status: "completed", args, result: { content: [] } });
    }
    if ("usage" in action && !("hang" in action)) this.push({ type: "usage", usage: action.usage });
    if ("error" in action) throw made(action.error);
    if ("run_error" in action) {
      this.status = "error";
      this.error = action.run_error;
    }
    if ("hang" in action) {
      this.cancelUsage = action.usage;
      await new Promise((resolve) => { this.onCancel = resolve; });
    }
    if ("pause" in action) {
      const folder = dirname(LOG);
      writeFileSync(join(folder, `${action.pause}.paused`), "");
      const deadline = Date.now() + 30_000;
      while (!existsSync(join(folder, `${action.pause}.go`)) && Date.now() < deadline && !this.cancelled) {
        await new Promise((resolve) => setTimeout(resolve, 50));
      }
    }
    if ("sleep" in action) await new Promise((resolve) => setTimeout(resolve, action.sleep));
    if ("exit" in action) process.exit(1);
    if ("child" in action) {
      const child = spawn(process.execPath, ["-e", "process.on('SIGTERM', () => {}); setInterval(() => {}, 100)"], { stdio: "ignore" });
      log("child", { pid: child.pid });
    }
    if ("unhandled" in action) Promise.reject(new Error("a promise that nothing waits for"));
  }

  async custom(tool) {
    const name = tool.server ? `${tool.server}__${tool.name}` : tool.name;
    const id = `call-${++calls}`;
    const args = { providerIdentifier: "custom-user-tools", toolName: name, args: tool.arguments ?? {} };
    this.push({ type: "tool_call", call_id: id, name: "mcp", status: "running", args });
    const offered = this.tools[name];
    if (!offered) {
      log("tool_result", { name, offered: false });
      this.push({ type: "tool_call", call_id: id, name: "mcp", status: "error", args, result: `no tool ${name}` });
      return;
    }
    const result = await offered.execute(tool.arguments ?? {}, { toolCallId: id });
    log("tool_result", { name, result });
    this.push({ type: "tool_call", call_id: id, name: "mcp", status: result.isError ? "error" : "completed", args, result });
  }

  async *stream() {
    let index = 0;
    for (;;) {
      while (index < this.messages.length) yield this.messages[index++];
      if (this.finished) {
        if (this.thrown) throw this.thrown;
        return;
      }
      await new Promise((resolve) => { this.wake = resolve; });
    }
  }

  async wait() {
    await this.done;
    return { id: this.id, status: this.status, ...(this.error ? { error: this.error } : {}) };
  }

  async cancel() {
    log("cancel", { run: this.id });
    this.cancelled = true;
    this.onCancel();
  }

  async steer(text) {
    log("steer", { text });
    const mode = SCENARIO.steer ?? "delivered";
    if (mode === "hang") await never();
    if (mode === "throw") throw new CursorSdkError("steer failed");
    return mode === "delivered" ? "complete_delivered" : "revert_to_followup";
  }

  supports() {
    return true;
  }
}
