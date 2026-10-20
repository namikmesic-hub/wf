import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { PassThrough } from "node:stream";
import { test } from "node:test";
import { accessOptions, classify, classifyRunError, fromTokens } from "../src/cursor.ts";
import { lines, MAX_FRAME, PROTOCOL, Wire } from "../src/protocol.ts";

const PROTOCOL_DIR = new URL("../protocol/", import.meta.url);

test("every example frame has what the schema requires of its type", () => {
  const schema = JSON.parse(readFileSync(new URL("wf-session-0.schema.json", PROTOCOL_DIR), "utf8"));
  const examples = readFileSync(new URL("examples.jsonl", PROTOCOL_DIR), "utf8").trim().split("\n").map((line) => JSON.parse(line));
  const seen = new Set<string>();
  for (const frame of examples) {
    const shape = schema.$defs[frame.t];
    assert.ok(shape, `no frame type ${frame.t}`);
    for (const key of shape.required) assert.ok(key in frame, `${frame.t} lacks ${key}`);
    seen.add(frame.t);
  }
  assert.deepEqual([...seen].sort(), ["call", "ev", "hello", "req", "res", "ret", "welcome"]);
  assert.equal(examples[0].protocol, PROTOCOL);
});

test("lines are split, and one longer than a frame is dropped", () => {
  const got: string[] = [];
  let dropped = 0;
  const feed = lines((line) => got.push(line), () => { dropped += 1; });
  feed(Buffer.from('{"a":1}\n{"b"'));
  feed(Buffer.from(':2}\n'));
  feed(Buffer.alloc(MAX_FRAME + 10, 120));
  feed(Buffer.from("\nlast\n"));
  assert.deepEqual(got, ['{"a":1}', '{"b":2}', "last"]);
  assert.equal(dropped, 1);
});

test("a call waits for its ret, and every call left gets an error when the client goes", async () => {
  const out = new PassThrough();
  const sent: Array<Record<string, unknown>> = [];
  out.on("data", (chunk: Buffer) => chunk.toString().trim().split("\n").forEach((line) => sent.push(JSON.parse(line))));
  const wire = new Wire(out);
  const answered = wire.call("ses_a", "trn_b", "git", null, "tc_1", { args: ["status"] });
  const left = wire.call("ses_a", "trn_b", "submit_result", null, null, {});
  await new Promise((resolve) => setImmediate(resolve));
  wire.ret({ t: "ret", id: sent[0].id, content: [{ type: "text", text: "clean" }], isError: false });
  assert.deepEqual(await answered, { content: [{ type: "text", text: "clean" }], isError: false });
  wire.close("wf is gone");
  assert.equal((await left).isError, true);
  assert.equal(sent[1].t, "call");
});

test("the access levels keep what each may not do out of the options", () => {
  assert.deepEqual(accessOptions("read-only", false), { tools: ["mcp"], disallowedTools: ["task", "askQuestion"], sandbox: true, inherit: false });
  assert.deepEqual(accessOptions("read-only", true).tools, ["mcp", "webSearch", "webFetch"]);
  assert.deepEqual(accessOptions("edit", false), { disallowedTools: ["askQuestion", "webSearch", "webFetch"], sandbox: true, inherit: true });
  assert.deepEqual(accessOptions("full", true), { disallowedTools: ["askQuestion"], sandbox: false, inherit: true });
  assert.throws(() => accessOptions("root", false), /access must be read-only, edit or full/);
});

test("the SDK's errors get the protocol's classes", () => {
  const error = (name: string, message: string, more: Record<string, unknown> = {}) => Object.assign(new Error(message), { name, ...more });
  const classOf = (thrown: unknown) => classify(thrown).class;
  assert.equal(classOf(error("AuthenticationError", "Invalid User API Key")), "auth");
  assert.match(classify(error("AuthenticationError", "Invalid User API Key")).message, /Cursor refused the API key/);
  assert.equal(classOf(error("RateLimitError", "usage limit")), "usage_limit");
  assert.equal(classOf(error("RateLimitError", "slow down", { isRetryable: true })), "transient");
  assert.equal(classOf(error("ConfigurationError", "Cannot use this model: x")), "unavailable");
  assert.equal(classOf(error("ConfigurationError", "no key", { code: "unauthenticated" })), "auth");
  assert.equal(classOf(error("AgentNotFoundError", "gone")), "not_found");
  assert.equal(classOf(error("NetworkError", "503")), "transient");
  assert.equal(classify(error("NetworkError", "503")).retry_after_s, 30);
  assert.equal(classOf(error("CursorSdkError", "Error: RetriableError: [internal] keepalive ping timed out")), "transient");
  assert.equal(classOf(error("CursorSdkError", "Upgrade your plan to continue")), "usage_limit");
  assert.equal(classOf(new TypeError("x is undefined")), "internal");
  assert.equal(classifyRunError({ message: "Error: ConnectError: [resource_exhausted] You've hit your usage limit" }).class, "usage_limit");
  assert.equal(classifyRunError({ message: "the model broke" }).class, "failed");
});

test("the SDK's token counts become the protocol's usage object, with its cost still to come", () => {
  assert.deepEqual(fromTokens({ inputTokens: 10, outputTokens: 5, cacheReadTokens: 3, cacheWriteTokens: 1, totalTokens: 15, reasoningTokens: 2 }), {
    input_tokens: 10, cached_input_tokens: 3, cache_write_tokens: 1, output_tokens: 5, reasoning_tokens: 2, total_tokens: 15,
    cost_usd: null, cost_source: "pending",
  });
});
