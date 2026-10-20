import assert from "node:assert/strict";
import { PassThrough, Writable } from "node:stream";
import { mock, test } from "node:test";
import { CursorSession } from "../src/cursor.ts";
import { MAX_FRAME, Wire } from "../src/protocol.ts";

// What the tests reach inside a session: the run, the guard's settings, and its handlers.
interface Inside {
  run: { status: string; cancel: () => Promise<void>; steer?: (text: string) => Promise<string> } | null;
  agent: unknown;
  allowed: Set<string> | null;
  ownMcpOnly: boolean;
  outside: string | null;
  tools(given: unknown[]): void;
  tool(turn: string, message: Record<string, unknown>): void;
}

function session(): { inside: Inside; frames: Array<Record<string, unknown>>; cancels: () => number } {
  const out = new PassThrough();
  const frames: Array<Record<string, unknown>> = [];
  out.on("data", (chunk: Buffer) => chunk.toString().trim().split("\n").forEach((line) => frames.push(JSON.parse(line))));
  const made = new CursorSession(new Wire(out), "key");
  const inside = made as unknown as Inside;
  let cancelled = 0;
  inside.run = { status: "running", cancel: async () => { cancelled += 1; } };
  inside.allowed = new Set(["mcp"]);
  inside.ownMcpOnly = true;
  inside.tools([{ kind: "client", name: "submit_result" }, { kind: "client", server: "linear", name: "get_issue", private: true }]);
  return { inside, frames, cancels: () => cancelled };
}

const mcp = (status: string, args: Record<string, unknown>) => ({ type: "tool_call", call_id: "c1", name: "mcp", status, args });

test("a call whose MCP server isn't known yet waits for a later message, and a session tool then passes", () => {
  const { inside, cancels } = session();
  inside.tool("trn_1", mcp("running", {}));
  inside.tool("trn_1", mcp("running", { providerIdentifier: "custom-user-tools", toolName: "linear__get_issue", args: { id: "A" } }));
  inside.tool("trn_1", mcp("completed", { providerIdentifier: "custom-user-tools", toolName: "linear__get_issue", args: { id: "A" } }));
  assert.equal(inside.outside, null);
  assert.equal(cancels(), 0);
});

test("another server's tool with a session tool's name is still another server's", () => {
  const { inside, cancels } = session();
  inside.tool("trn_1", mcp("running", { providerIdentifier: "foreign", toolName: "submit_result", args: {} }));
  assert.match(inside.outside ?? "", /Cursor called foreign.submit_result, an MCP tool wf didn't give this session/);
  assert.equal(cancels(), 1);
});

test("a call that names its server only later, or is first seen done, is checked too", () => {
  const late = session();
  late.inside.tool("trn_1", mcp("running", { args: { q: 1 } }));
  late.inside.tool("trn_1", mcp("running", { providerIdentifier: "github", toolName: "create_issue", args: { q: 1 } }));
  assert.match(late.inside.outside ?? "", /github.create_issue/);
  const done = session();
  done.inside.tool("trn_1", { type: "tool_call", call_id: "c2", name: "shell", status: "completed", args: { command: "ls" } });
  assert.match(done.inside.outside ?? "", /Cursor ran shell, a tool outside the session's read-only list/);
  const unnamed = session();
  unnamed.inside.tool("trn_1", mcp("completed", { args: {} }));
  assert.match(unnamed.inside.outside ?? "", /an MCP server it didn't name/);
});

test("a second turn.start fails even before the first turn begins", () => {
  const { inside } = session();
  inside.agent = {};
  const made = inside as unknown as CursorSession;
  const [first] = made.startTurn({ text: "one" });
  assert.ok(first.turn);
  assert.throws(() => made.startTurn({ text: "two" }), /a turn of this session is still running/);
});

test("a late answer to a message comes as a steer.status event", async () => {
  mock.timers.enable({ apis: ["setTimeout"] });
  try {
    const { inside, frames } = session();
    let answer = (outcome: string) => undefined as void;
    inside.run!.steer = () => new Promise((resolve) => { answer = resolve; });
    const status = (inside as unknown as CursorSession).steer("st_1", "Use the v2 API");
    mock.timers.tick(8_000);
    assert.equal(await status, "queued");
    answer("revert_to_followup");
    await new Promise((resolve) => setImmediate(resolve));
    const event = frames.find((frame) => frame.kind === "steer.status");
    assert.deepEqual(event?.data, { id: "st_1", status: "declined" });
  } finally {
    mock.timers.reset();
  }
});

test("an event too big for a frame keeps its small fields", () => {
  const out = new PassThrough();
  let line = "";
  out.on("data", (chunk: Buffer) => { line += chunk.toString(); });
  new Wire(out).event("ses_a", "trn_b", "turn.tool", { call: "c1", tool: "write", server: null, status: "started",
    args: { text: "x".repeat(MAX_FRAME) } });
  const frame = JSON.parse(line);
  assert.deepEqual(frame.data, { call: "c1", tool: "write", server: null, status: "started",
    args: { truncated: true, characters: MAX_FRAME + 11 } });
});

test("a client that stops reading stops the host once the backlog is too big", () => {
  const stuck = new Writable({ highWaterMark: 1024, write() { /* never done */ } });
  const wire = new Wire(stuck);
  let stopped = 0;
  wire.onOverflow = () => { stopped += 1; };
  for (let i = 0; i < 80 && stopped === 0; i += 1) wire.event("ses_a", "trn_b", "turn.text", { text: "x".repeat(MAX_FRAME - 200) });
  assert.equal(stopped, 1);
});
