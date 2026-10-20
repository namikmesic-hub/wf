// wf's Cursor host: Cursor agent sessions through @cursor/sdk, served over stdin and stdout with
// the wf-session/0 protocol (protocol.ts). wf starts one host per session. stderr is its log.
//
// The client sends `hello` first, with the Cursor API key in `credentials`: the key comes over
// stdin, never from the environment or the command line. The host answers `welcome`, and then
// answers each `req` by its id. turn.start answers at once, and the turn's end comes as a
// turn.ended event, so turn.steer and turn.cancel can run during a turn. When stdin closes, the
// host cancels what still runs and exits.

import { CursorSession, check, classify, log, sdkVersion, sleep } from "./cursor.ts";
import { fail, lines, PROTOCOL, Wire, type Frame } from "./protocol.ts";

const VERSION = "0.1.0";
// Text in the error that the SDK leaves unawaited when a shell can't start (t3code's guard)
const SHELL_SPAWN = ["dump_zsh_state", "dump_bash_state", "__CURSOR_SANDBOX_ENV_RESTORE"];

const wire = new Wire(process.stdout);
const sessions = new Map<string, CursorSession>();
let greeted = false;
let apiKey: string | null = null;
let stopping = false;

process.on("unhandledRejection", (reason) => {
  const spawnargs = (reason as { spawnargs?: unknown })?.spawnargs;
  if (Array.isArray(spawnargs) && spawnargs.some((a) => typeof a === "string" && SHELL_SPAWN.some((m) => a.includes(m)))) {
    log(`a shell failed to start, and the host keeps running: ${String(reason)}`);
    return;
  }
  log(`unhandled rejection: ${reason instanceof Error ? reason.stack : String(reason)}`);
  process.exit(70);
});
process.on("SIGTERM", () => void shutdown("the host was stopped", 2_000));
process.stdout.on("error", () => void shutdown("stdout closed", 2_000));
process.stdin.on("data", lines(receive, () => log("dropped a line longer than 1 MiB")));
process.stdin.on("end", () => void shutdown("the client closed its end", 10_000));

function receive(line: string): void {
  let frame: Frame;
  try {
    frame = JSON.parse(line);
  } catch {
    log("dropped a line that isn't JSON");
    return;
  }
  if (frame === null || typeof frame !== "object" || typeof frame.t !== "string") return;
  if (!greeted) {
    hello(frame);
    return;
  }
  if (frame.t === "req") void request(frame);
  else if (frame.t === "ret") wire.ret(frame);
  else log(`dropped a frame of type ${frame.t}`);
}

function hello(frame: Frame): void {
  if (frame.t !== "hello") {
    log(`the first frame must be hello, not ${frame.t}`);
    process.exit(2);
  }
  greeted = true;
  const credentials = frame.credentials as Record<string, unknown> | undefined;
  apiKey = typeof credentials?.api_key === "string" && credentials.api_key ? credentials.api_key : null;
  wire.send({
    t: "welcome", protocol: PROTOCOL,
    server: { name: "wf-cursor-host", version: VERSION, runtime: "cursor", sdk: sdkVersion(), node: process.version },
    head: wire.head(), replay: "events",
  });
  if (frame.protocol !== PROTOCOL) {
    log(`the client speaks ${String(frame.protocol)}, and this host ${PROTOCOL}`);
    process.exit(2);
  }
}

async function request(frame: Frame): Promise<void> {
  const args = (frame.args !== null && typeof frame.args === "object" ? frame.args : {}) as Record<string, unknown>;
  try {
    const [result, after] = await operation(String(frame.op), args);
    wire.respond(frame.id, result);
    after?.();
  } catch (error) {
    wire.refuse(frame.id, classify(error));
  }
}

type Answer = [Record<string, unknown>, (() => void)?];

async function operation(op: string, args: Record<string, unknown>): Promise<Answer> {
  switch (op) {
    case "runtime.check":
      return [await check(args, apiKey)];
    case "session.open": {
      if (stopping) throw fail("unavailable", "host.stopping", "the Cursor host is stopping");
      const session = new CursorSession(wire, apiKey);
      sessions.set(session.id, session);
      try {
        return [await session.open(args), () => session.opened()];
      } catch (error) {
        sessions.delete(session.id);
        await session.close("it didn't open").catch(() => undefined);
        throw error;
      }
    }
    case "turn.start":
      return find(args).startTurn(args);
    case "turn.steer":
      return [{ status: await find(args).steer(String(args.text ?? "")) }];
    case "turn.cancel":
      await find(args).cancel();
      return [{}];
    case "session.close": {
      const session = find(args);
      sessions.delete(session.id);
      return [await session.close(typeof args.reason === "string" ? args.reason : "closed")];
    }
    case "usage.read":
      return [await find(args).usageRead()];
    default:
      throw fail("invalid", "protocol.op", `the Cursor host has no operation ${op}`);
  }
}

function find(args: Record<string, unknown>): CursorSession {
  const session = sessions.get(String(args.session));
  if (session === undefined) throw fail("not_found", "protocol.session", `there's no session ${String(args.session)}`);
  return session;
}

async function shutdown(reason: string, within: number): Promise<void> {
  if (stopping) return;
  stopping = true;
  wire.close(reason);
  const closing = Promise.all([...sessions.values()].map((s) => s.close(reason).catch(() => undefined)));
  await Promise.race([closing, sleep(within)]);
  process.exit(0);
}
