// One Cursor agent session through @cursor/sdk: wf's settings as SDK options, the SDK's messages as
// wf-session events, and the SDK's errors as the protocol's error classes.
//
// Access levels (the SDK has no answer per call, as ACP had, so the options decide up front):
// - read-only: none of Cursor's own tools but "mcp", which carries the session's tools: wf gives a
//   read-only session its own file tools and `git`, which read only the workspace and the folders
//   the step names, because Cursor's read, grep, glob and ls take any path. No subagents, Cursor's
//   sandbox on, and no MCP server but the session's own tools. A call of any other tool cancels the turn.
// - edit: the default tools, Cursor's sandbox on, subagents get the same lists. wf runs `edit`
//   through ACP for now, because no test has shown that the sandbox keeps writes in the workspace.
// - full: the default tools, the sandbox off.
// At every level the web tools go when `web` is false, and askQuestion goes, since no person answers.
// A session's tools (submit_result, git, wf tools) are the SDK's custom tools: callbacks in this
// process that send a `call` frame and wait for the client's `ret`. They need "mcp" in the tool list.
// With only_given_tools, or below full, the session gets `mcpServers: {}` on every send, which
// replaces the MCP servers Cursor would load, and a call to another MCP server cancels the turn.
//
// enableAgentRetries is set: the SDK then rides out a dropped stream by itself. Its default for it
// differs between code paths in 1.0.38, so it is never left to the default.

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import type { ModelListItem, Run, SDKAgent, SDKCustomTool, SDKMessage, TokenUsage } from "@cursor/sdk";
import {
  addUsage, fail, HostError, newId, noUsage,
  type Content, type ErrorInfo, type Usage, type Wire,
} from "./protocol.ts";
import { effective, resolveModel, type Resolved } from "./models.ts";

type Sdk = typeof import("@cursor/sdk");

export const HOST_DIR = fileURLToPath(new URL("..", import.meta.url));
const READ_ONLY = ["mcp"];
const WEB = ["webSearch", "webFetch"];
const NOBODY_ANSWERS = ["askQuestion"];
const STEER_MS = 8_000; // within the 10 seconds wf waits for turn.steer
const CLOSE_MS = 5_000;
const USAGE_MS = 3_000;
const RESULT_LIMIT = 512 * 1024; // characters of a tool's result in an event
const RETRY_AFTER_S = 30;
const OWN_SERVER = "custom-user-tools"; // the MCP server that holds the session's custom tools
const NOTICES: Array<[string, "auth" | "usage_limit"]> = [
  ["Please sign in to continue", "auth"], ["Upgrade your plan to continue", "usage_limit"],
  ["Add a payment method to continue", "usage_limit"], ["Check your settings to continue", "usage_limit"],
];

let loading: Promise<Sdk> | null = null;

export function loadSdk(): Promise<Sdk> {
  loading ??= import("@cursor/sdk").catch((error: Error) => {
    loading = null;
    throw fail("unavailable", "cursor.sdk_missing",
      `the Cursor SDK (@cursor/sdk) isn't installed beside the host in ${HOST_DIR}: ${error.message}`);
  });
  return loading;
}

export function sdkVersion(): string | null {
  try {
    return JSON.parse(readFileSync(`${HOST_DIR}/node_modules/@cursor/sdk/package.json`, "utf8")).version ?? null;
  } catch {
    return null;
  }
}

function needKey(apiKey: string | null): string {
  if (!apiKey) throw fail("auth", "cursor.no_api_key", "no Cursor API key was given to the Cursor host");
  return apiKey;
}

async function catalog(sdk: Sdk, apiKey: string): Promise<ModelListItem[]> {
  try {
    return await sdk.Cursor.models.list({ apiKey });
  } catch (error) {
    throw new HostError(classify(error));
  }
}

// runtime.check: is the key good, and can Cursor run this model?
export async function check(args: Record<string, unknown>, apiKey: string | null): Promise<Record<string, unknown>> {
  const sdk = await loadSdk();
  const key = needKey(apiKey);
  const models = await catalog(sdk, key);
  const result: Record<string, unknown> = { runtime: "cursor", sdk: sdkVersion(), models: models.map((m) => m.id) };
  if (typeof args.model === "string" && args.model) result.model = resolveModel(args.model, models);
  try {
    const me = await sdk.Cursor.me({ apiKey: key });
    result.account = { api_key_name: me.apiKeyName, email: me.userEmail ?? null };
  } catch {
    result.account = null;
  }
  return result;
}

interface ClientTool {
  name: string;
  server: string | null;
  private: boolean;
}

interface Access {
  tools?: string[];
  disallowedTools: string[];
  sandbox: boolean;
  inherit: boolean;
}

export function accessOptions(access: string, web: boolean): Access {
  const noWeb = web ? [] : WEB;
  if (access === "read-only") {
    return { tools: [...READ_ONLY, ...(web ? WEB : [])], disallowedTools: ["task", ...NOBODY_ANSWERS], sandbox: true, inherit: false };
  }
  if (access === "edit") return { disallowedTools: [...NOBODY_ANSWERS, ...noWeb], sandbox: true, inherit: true };
  if (access === "full") return { disallowedTools: [...NOBODY_ANSWERS, ...noWeb], sandbox: false, inherit: true };
  throw fail("invalid", "protocol.access", `access must be read-only, edit or full, not ${JSON.stringify(access)}`);
}

export class CursorSession {
  readonly id = newId("ses");
  private wire: Wire;
  private apiKey: string | null;
  private agent: SDKAgent | null = null;
  private run: Run | null = null;
  private turnId: string | null = null;
  private turnDone: Promise<void> | null = null;
  private cancelled = false;
  private outside: string | null = null;
  private force = false;
  private allowed: Set<string> | null = null;
  private ownMcpOnly = false;
  private clients = new Map<string, ClientTool>();
  private customTools: Record<string, SDKCustomTool> = {};
  private calls = new Map<string, string>();
  private turns: Usage[] = [];
  private total: Usage = noUsage();
  private resolved: Resolved | null = null;

  constructor(wire: Wire, apiKey: string | null) {
    this.wire = wire;
    this.apiKey = apiKey;
  }

  async open(args: Record<string, unknown>): Promise<Record<string, unknown>> {
    const sdk = await loadSdk();
    const apiKey = needKey(this.apiKey);
    const access = accessOptions(String(args.access), args.web === true);
    if (typeof args.cwd !== "string" || !args.cwd) throw fail("invalid", "protocol.cwd", "session.open needs a cwd");
    if (typeof args.model !== "string" || !args.model) throw fail("unavailable", "cursor.no_model", "Cursor needs a model");
    this.allowed = access.tools ? new Set(access.tools) : null;
    this.ownMcpOnly = args.only_given_tools === true || args.access !== "full";
    this.tools(Array.isArray(args.tools) ? args.tools : []);
    this.resolved = resolveModel(args.model, await catalog(sdk, apiKey));
    const start = (args.start ?? { kind: "fresh" }) as Record<string, unknown>;
    const options = {
      apiKey,
      model: { id: this.resolved.id, ...(this.resolved.params.length ? { params: this.resolved.params } : {}) },
      mode: "agent" as const,
      ...(access.tools ? { tools: access.tools } : {}),
      disallowedTools: access.disallowedTools,
      ...(this.ownMcpOnly ? { mcpServers: {} } : {}),
      local: {
        cwd: args.cwd,
        ...(args.project_instructions === false ? {} : { settingSources: ["project" as const] }),
        sandboxOptions: { enabled: access.sandbox },
        customTools: this.customTools,
        enableAgentRetries: true,
        ...(access.inherit ? { subagentInherit: {} } : {}),
        ...(typeof args.state_dir === "string" ? { store: new sdk.JsonlLocalAgentStore(args.state_dir) } : {}),
      },
    };
    try {
      if (start.kind === "load") {
        if (typeof start.from !== "string") throw fail("invalid", "protocol.start", "start: load needs `from`");
        this.agent = await sdk.Agent.resume(start.from, options);
        this.force = true; // a host that died mid-turn leaves that run active in the store
      } else {
        this.agent = await sdk.Agent.create(options);
      }
    } catch (error) {
      throw new HostError(classify(error));
    }
    return {
      session: this.id, state: "open", runtime_session_id: this.agent.agentId,
      model: this.resolved, effective: effective(this.resolved), sdk: sdkVersion(),
    };
  }

  opened(): void {
    this.wire.event(this.id, null, "session.opened", {
      runtime_session_id: this.agent?.agentId ?? null, model: this.resolved?.requested ?? null, effort: null,
      effective: this.resolved ? effective(this.resolved) : {}, waited_s: 0,
    });
  }

  private tools(given: unknown[]): void {
    for (const raw of given) {
      const tool = raw as Record<string, unknown>;
      if (tool.kind !== "client" || typeof tool.name !== "string") {
        throw fail("invalid", "protocol.tool", `phase 0 tools are of kind client, with a name: ${JSON.stringify(raw)}`);
      }
      const server = typeof tool.server === "string" ? tool.server : null;
      const name = server ? `${server}__${tool.name}` : tool.name;
      const client: ClientTool = { name: tool.name, server, private: tool.private === true };
      this.clients.set(name, client);
      this.customTools[name] = {
        ...(typeof tool.description === "string" ? { description: tool.description } : {}),
        ...(tool.input_schema && typeof tool.input_schema === "object" ? { inputSchema: tool.input_schema as Record<string, never> } : {}),
        ...(tool.annotations && typeof tool.annotations === "object" ? { annotations: tool.annotations as Record<string, boolean> } : {}),
        execute: async (callArgs, context) => {
          const ret = await this.wire.call(this.id, this.turnId, client.name, client.server, context?.toolCallId ?? null, callArgs);
          return { content: ret.content.map(sdkContent), isError: ret.isError };
        },
      };
    }
  }

  // turn.start: the turn's id at once, then its events, and turn.ended when it ends.
  startTurn(args: Record<string, unknown>): [Record<string, unknown>, () => void] {
    if (this.agent === null) throw fail("invalid", "protocol.session", "the session isn't open");
    if (this.turnDone !== null) throw fail("invalid", "protocol.turn", "a turn of this session is still running");
    if (typeof args.text !== "string") throw fail("invalid", "protocol.text", "turn.start needs a text");
    const turn = newId("trn");
    const kind = typeof args.kind === "string" ? args.kind : "message";
    const begin = () => {
      this.turnDone = this.runTurn(turn, kind, args.text as string).finally(() => {
        this.turnDone = null;
      });
    };
    return [{ turn }, begin];
  }

  private async runTurn(turn: string, kind: string, text: string): Promise<void> {
    this.turnId = turn;
    this.cancelled = false;
    this.outside = null;
    this.wire.event(this.id, turn, "turn.started", { kind, text });
    let reported: Usage | null = null;
    let status = "done";
    let error: ErrorInfo | null = null;
    try {
      const run = await this.agent!.send(text, {
        ...(this.ownMcpOnly ? { mcpServers: {} } : {}),
        local: { customTools: this.customTools, ...(this.force ? { force: true } : {}) },
      });
      this.force = false;
      this.run = run;
      if (this.cancelled) await run.cancel();
      for await (const message of run.stream()) {
        if (message.type === "usage") {
          reported = addUsage(reported ?? noUsage(), fromTokens(message.usage));
          this.wire.event(this.id, turn, "turn.usage", { usage: reported });
        } else {
          this.message(turn, message);
        }
      }
      const result = await run.wait();
      reported ??= result.usage ? fromTokens(result.usage) : null;
      if (result.status === "cancelled") status = "cancelled";
      else if (result.status === "error") error = classifyRunError(result.error);
    } catch (thrown) {
      error = classify(thrown);
    } finally {
      this.run = null;
    }
    if (this.outside !== null) error = { class: "failed", code: "cursor.outside_access", message: this.outside, retryable: false };
    else if (status === "cancelled" || (error === null && this.cancelled)) {
      status = "cancelled";
      error = { class: "cancelled", code: "cursor.cancelled", message: "the turn was cancelled", retryable: false };
    }
    if (error !== null && error.class !== "cancelled") {
      status = ["unavailable", "auth", "usage_limit", "not_found"].includes(error.class) ? "unavailable" : "failed";
    }
    const usage = reported ?? noUsage();
    this.turns.push(usage);
    this.total = addUsage(this.total, usage);
    this.wire.event(this.id, turn, "turn.ended", { status, error, usage, result_accepted: false, queued_steers: [] });
  }

  private message(turn: string, message: SDKMessage): void {
    if (message.type === "assistant") {
      for (const block of message.message.content) {
        if (block.type === "text" && block.text) this.wire.event(this.id, turn, "turn.text", { text: block.text });
      }
    } else if (message.type === "tool_call") {
      this.tool(turn, message);
    }
  }

  private tool(turn: string, message: Extract<SDKMessage, { type: "tool_call" }>): void {
    const mcp = mcpCall(message);
    const name = mcp?.tool ?? message.name;
    const client = this.clients.get(name); // wf names no tool as Cursor names its own
    const args = mcp ? mcp.args : message.args;
    const base = {
      call: message.call_id,
      tool: client ? client.name : name,
      server: client ? client.server : mcp?.server === OWN_SERVER ? null : (mcp?.server ?? null),
      args: args ?? null,
    };
    if (message.status === "running") {
      const key = JSON.stringify(args ?? null);
      const seen = this.calls.get(message.call_id);
      if (seen === key) return;
      this.calls.set(message.call_id, key);
      this.wire.event(this.id, turn, "turn.tool", { ...base, status: seen === undefined ? "started" : "updated" });
      if (seen === undefined) this.refuseOutside(message, client, mcp);
      return;
    }
    this.calls.delete(message.call_id);
    const outcome = client?.private ? { kept: false, size: JSON.stringify(message.result ?? null).length } : { result: capped(message.result) };
    this.wire.event(this.id, turn, "turn.tool", {
      ...base, status: message.status === "completed" ? "completed" : "failed", ...outcome,
      ...(message.truncated ? { truncated: message.truncated } : {}),
    });
  }

  // A tool the options should have kept from this session: stop the turn, and fail it.
  private refuseOutside(message: { name: string }, client: ClientTool | undefined, mcp: McpCall | null): void {
    if (client || mcp?.server === OWN_SERVER) return; // only the session's own callbacks answer there
    let why: string | null = null;
    if (mcp && this.ownMcpOnly) why = `Cursor called ${mcp.server ?? "an MCP server"}.${mcp.tool ?? "?"}, an MCP tool wf didn't give this session`;
    else if (!mcp && this.allowed && !this.allowed.has(message.name)) why = `Cursor ran ${message.name}, a tool outside the session's read-only list`;
    if (why === null || this.outside !== null) return;
    this.outside = why;
    this.run?.cancel().catch(() => undefined);
  }

  async steer(text: string): Promise<"delivered" | "declined" | "unconfirmed"> {
    const run = this.run;
    if (run === null || run.status !== "running" || typeof run.steer !== "function") return "declined";
    try {
      const outcome = await Promise.race([run.steer(text), sleep(STEER_MS).then(() => "no answer" as const)]);
      if (outcome === "complete_delivered") return "delivered";
      return outcome === "no answer" ? "unconfirmed" : "declined";
    } catch {
      return "declined";
    }
  }

  async cancel(): Promise<void> {
    if (this.turnDone === null) return;
    this.cancelled = true;
    try {
      await this.run?.cancel();
    } catch (error) {
      log(`cancelling the run failed: ${String(error)}`);
    }
  }

  async close(reason: string): Promise<Record<string, unknown>> {
    if (this.turnDone !== null) {
      await this.cancel();
      await Promise.race([this.turnDone, sleep(CLOSE_MS)]);
    }
    try {
      this.agent?.close();
    } catch (error) {
      log(`closing the agent failed: ${String(error)}`);
    }
    this.wire.event(this.id, null, "session.closed", { reason, usage: this.total });
    return { usage: this.total, dropped_steers: [] };
  }

  async usageRead(): Promise<Record<string, unknown>> {
    let billed: Record<string, unknown> | null = null;
    if (this.agent !== null) {
      try {
        const got = await Promise.race([this.agent.getUsage(), sleep(USAGE_MS).then(() => null)]);
        if (got !== null) {
          billed = {
            usage: fromTokens(got.usage),
            cost: got.cost ? { raw_cost_cents: got.cost.rawCostCents, charged_cents: got.cost.chargedCents } : null,
            runs: got.runs.length,
          };
        }
      } catch (error) {
        billed = { error: classify(error).message };
      }
    }
    return { turns: this.turns, total: this.total, billed };
  }
}

interface McpCall {
  server: string | null;
  tool: string | null;
  args: unknown;
}

function mcpCall(message: { name: string; args?: unknown }): McpCall | null {
  const args = message.args as Record<string, unknown> | undefined;
  const named = args !== null && typeof args === "object" && typeof args.toolName === "string";
  if (message.name !== "mcp" && !named) return null;
  return {
    server: typeof args?.providerIdentifier === "string" ? args.providerIdentifier : null,
    tool: typeof args?.toolName === "string" ? args.toolName : null,
    args: args?.args ?? null,
  };
}

function capped(result: unknown): unknown {
  const text = JSON.stringify(result ?? null);
  return text.length <= RESULT_LIMIT ? (result ?? null) : { truncated: true, characters: text.length, head: text.slice(0, 2000) };
}

function sdkContent(block: Content): { type: "text"; text: string } | { type: "image"; data: string; mimeType?: string } {
  if (block.type === "image" && typeof block.data === "string") {
    return { type: "image", data: block.data, ...(block.mimeType ? { mimeType: block.mimeType } : {}) };
  }
  return { type: "text", text: block.text ?? "" };
}

export function fromTokens(tokens: TokenUsage | undefined): Usage {
  if (!tokens) return noUsage();
  return {
    input_tokens: tokens.inputTokens ?? 0,
    cached_input_tokens: tokens.cacheReadTokens ?? 0,
    cache_write_tokens: tokens.cacheWriteTokens ?? 0,
    output_tokens: tokens.outputTokens ?? 0,
    reasoning_tokens: tokens.reasoningTokens ?? 0,
    total_tokens: tokens.totalTokens ?? 0,
    cost_usd: null,
    cost_source: "pending", // Cursor bills later: usage.read has its cents once they land
  };
}

// The protocol's class for an error the SDK threw.
export function classify(error: unknown): ErrorInfo {
  if (error instanceof HostError) return error.info;
  const e = (error ?? {}) as { name?: string; message?: string; code?: string; isRetryable?: boolean };
  const name = typeof e.name === "string" ? e.name : "";
  const message = typeof e.message === "string" && e.message ? e.message : String(error);
  const code = typeof e.code === "string" ? e.code : "";
  const retryable = e.isRetryable === true;
  switch (name) {
    case "AuthenticationError":
      return { class: "auth", code: "cursor.auth", message: `Cursor refused the API key: ${message}`, retryable: false };
    case "RateLimitError":
      return retryable
        ? { class: "transient", code: "cursor.rate_limit", message, retryable, retry_after_s: RETRY_AFTER_S }
        : { class: "usage_limit", code: "cursor.usage_limit", message, retryable: false };
    case "ConfigurationError":
    case "IntegrationNotConnectedError":
    case "UnsupportedRunOperationError":
      if (code === "unauthenticated") return { class: "auth", code: "cursor.auth", message, retryable: false };
      return {
        class: "unavailable", code: message.startsWith("Cannot use this model") ? "cursor.model" : "cursor.configuration",
        message, retryable: false,
      };
    case "AgentNotFoundError":
      return { class: "not_found", code: "cursor.agent_not_found", message, retryable: false };
    case "AgentBusyError":
      return { class: "failed", code: "cursor.busy", message, retryable: false };
    case "NetworkError":
      return { class: "transient", code: "cursor.network", message, retryable: true, retry_after_s: RETRY_AFTER_S };
  }
  const fromText = classifyText(message, code);
  if (fromText) return fromText;
  if (retryable) return { class: "transient", code: "cursor.retryable", message, retryable, retry_after_s: RETRY_AFTER_S };
  if (/^(CursorSdkError|CursorAgentError|UnknownAgentError)$/.test(name)) return { class: "failed", code: "cursor.error", message, retryable: false };
  return { class: "internal", code: "host.error", message: `${name || "Error"}: ${message}`, retryable: false };
}

// The class for the error a run ended with.
export function classifyRunError(error: { message?: string; code?: string } | undefined): ErrorInfo {
  const message = error?.message || "the run failed";
  return classifyText(message, error?.code ?? "") ?? { class: "failed", code: "cursor.run_failed", message, retryable: false };
}

// Cursor's own words for a usage limit, a sign-in problem or a request that a retry can clear.
function classifyText(message: string, code: string): ErrorInfo | null {
  for (const [notice, errorClass] of NOTICES) {
    if (message.includes(notice)) return { class: errorClass, code: `cursor.${errorClass}`, message, retryable: false };
  }
  if (/RetriableError/.test(message)) {
    return { class: "transient", code: "cursor.retriable", message, retryable: true, retry_after_s: RETRY_AFTER_S };
  }
  if (code === "unauthenticated" || message.includes("[unauthenticated]")) return { class: "auth", code: "cursor.auth", message, retryable: false };
  if (code === "resource_exhausted" || message.includes("[resource_exhausted]")) {
    return { class: "usage_limit", code: "cursor.usage_limit", message, retryable: false };
  }
  return null;
}

export function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms).unref());
}

export function log(text: string): void {
  process.stderr.write(`${new Date().toISOString()} ${text}\n`);
}
