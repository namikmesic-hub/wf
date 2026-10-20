// The wf-session/0 protocol: the frames, the error classes and the usage object that wf and a
// session host send each other, one JSON object per line over stdin and stdout.
// protocol/wf-session-0.schema.json is the same contract as JSON Schema, and
// protocol/examples.jsonl holds an example of each frame. Both test suites read them.

import type { Writable } from "node:stream";

export const PROTOCOL = "wf-session/0";
export const MAX_FRAME = 1024 * 1024; // bytes in one line, in both directions
export const MAX_BACKLOG = 64 * 1024 * 1024; // bytes waiting for the client to read, before the host gives up
const BIG_VALUE = 64 * 1024; // characters of one value in an event that is too big as it is

export type ErrorClass =
  | "unavailable" // the runtime can't run here: not installed, can't start, no such model
  | "auth" // the sign-in is missing, expired or refused
  | "usage_limit" // the account's quota is used up
  | "transient" // a dropped connection after the runtime's own retries
  | "failed" // the turn failed in another way
  | "cancelled" // the client cancelled the turn
  | "invalid" // a bad request: a bug in the client
  | "not_found" // no such session or saved agent
  | "internal"; // a bug in the host

export interface ErrorInfo {
  class: ErrorClass;
  code: string;
  message: string;
  retryable: boolean;
  retry_after_s?: number;
  resets_at?: string;
}

export class HostError extends Error {
  info: ErrorInfo;

  constructor(info: ErrorInfo) {
    super(info.message);
    this.info = info;
  }
}

export function fail(errorClass: ErrorClass, code: string, message: string, more: Partial<ErrorInfo> = {}): HostError {
  return new HostError({ class: errorClass, code, message, retryable: false, ...more });
}

export interface Usage {
  input_tokens: number;
  cached_input_tokens: number;
  cache_write_tokens: number;
  output_tokens: number;
  reasoning_tokens: number;
  total_tokens: number;
  cost_usd: number | null;
  cost_source: "reported" | "estimated" | "pending" | "none";
  billed?: { raw_cost_cents: number; charged_cents: number };
}

export function noUsage(): Usage {
  return {
    input_tokens: 0, cached_input_tokens: 0, cache_write_tokens: 0, output_tokens: 0, reasoning_tokens: 0,
    total_tokens: 0, cost_usd: null, cost_source: "none",
  };
}

export function addUsage(total: Usage, more: Usage): Usage {
  return {
    input_tokens: total.input_tokens + more.input_tokens,
    cached_input_tokens: total.cached_input_tokens + more.cached_input_tokens,
    cache_write_tokens: total.cache_write_tokens + more.cache_write_tokens,
    output_tokens: total.output_tokens + more.output_tokens,
    reasoning_tokens: total.reasoning_tokens + more.reasoning_tokens,
    total_tokens: total.total_tokens + more.total_tokens,
    cost_usd: total.cost_usd === null && more.cost_usd === null ? null : (total.cost_usd ?? 0) + (more.cost_usd ?? 0),
    cost_source: more.cost_source === "none" ? total.cost_source : more.cost_source,
  };
}

export interface Content {
  type: string;
  text?: string;
  data?: string;
  mimeType?: string;
}

export interface Ret {
  content: Content[];
  isError: boolean;
}

export type Frame = Record<string, unknown> & { t: string };

// Writes frames, numbers events, and keeps the calls that wait for a `ret`. An event too big for
// a frame keeps its small fields, and each big value becomes { truncated, characters }. When the
// client stops reading and MAX_BACKLOG bytes wait, onOverflow runs, and the host stops.
export class Wire {
  private out: Writable;
  private seq = 0;
  private callIds = 0;
  private waiting = new Map<string, (ret: Ret) => void>();
  private gone: string | null = null;
  onOverflow: () => void = () => undefined;

  constructor(out: Writable) {
    this.out = out;
  }

  send(frame: Frame): void {
    if (this.gone !== null) return;
    let line = JSON.stringify(frame);
    if (Buffer.byteLength(line) > MAX_FRAME && frame.t === "ev") {
      line = JSON.stringify({ ...frame, data: shrink(frame.data as Record<string, unknown>) });
      if (Buffer.byteLength(line) > MAX_FRAME) line = JSON.stringify({ ...frame, data: { truncated: true } });
    }
    this.out.write(line + "\n");
    if (this.out.writableLength > MAX_BACKLOG) this.onOverflow();
  }

  respond(id: unknown, result: unknown, session: string | null = null): void {
    this.send({ t: "res", id, ...(session ? { session } : {}), ok: true, result });
  }

  refuse(id: unknown, error: ErrorInfo, session: string | null = null): void {
    this.send({ t: "res", id, ...(session ? { session } : {}), ok: false, error });
  }

  event(session: string, turn: string | null, kind: string, data: Record<string, unknown>): void {
    this.seq += 1;
    this.send({ t: "ev", seq: this.seq, at: new Date().toISOString(), session, ...(turn ? { turn } : {}), kind, data });
  }

  head(): number {
    return this.seq;
  }

  // A tool call for the client to answer. The answer always comes: from the client, or as an
  // error text when the client is gone, because the agent waits for it.
  call(session: string, turn: string | null, tool: string, server: string | null, callId: string | null,
       args: unknown): Promise<Ret> {
    if (this.gone !== null) return Promise.resolve(errorRet(this.gone));
    this.callIds += 1;
    const id = `c${this.callIds}`;
    return new Promise((resolve) => {
      this.waiting.set(id, resolve);
      this.send({ t: "call", id, session, ...(turn ? { turn } : {}), tool, server, callId, args });
    });
  }

  ret(frame: Frame): void {
    const answer = this.waiting.get(String(frame.id));
    if (answer === undefined) return;
    this.waiting.delete(String(frame.id));
    const content = Array.isArray(frame.content) ? (frame.content as Content[]) : [];
    answer({ content, isError: frame.isError === true });
  }

  // The client is gone: every call still waiting gets an error text, and nothing more is sent.
  close(reason: string): void {
    this.gone = reason;
    for (const answer of this.waiting.values()) answer(errorRet(reason));
    this.waiting.clear();
  }
}

function shrink(data: Record<string, unknown>): Record<string, unknown> {
  return Object.fromEntries(Object.entries(data ?? {}).map(([key, value]) => {
    const text = JSON.stringify(value ?? null);
    return [key, text.length > BIG_VALUE ? { truncated: true, characters: text.length } : value];
  }));
}

function errorRet(reason: string): Ret {
  return { content: [{ type: "text", text: `Error: ${reason}, so this tool can't answer.` }], isError: true };
}

// Splits a stream into lines of at most MAX_FRAME bytes. A longer line is dropped, and onTooLong hears of it.
export function lines(onLine: (line: string) => void, onTooLong: () => void): (chunk: Buffer) => void {
  let parts: Buffer[] = [];
  let size = 0;
  let tooLong = false;
  return (chunk: Buffer) => {
    let start = 0;
    for (;;) {
      const end = chunk.indexOf(10, start);
      const piece = chunk.subarray(start, end === -1 ? chunk.length : end);
      if (!tooLong && size + piece.length > MAX_FRAME) {
        tooLong = true;
        parts = [];
      } else if (!tooLong) {
        parts.push(piece);
        size += piece.length;
      }
      if (end === -1) return;
      if (tooLong) onTooLong();
      else onLine(Buffer.concat(parts).toString("utf8"));
      parts = [];
      size = 0;
      tooLong = false;
      start = end + 1;
    }
  };
}

export function newId(prefix: string): string {
  return `${prefix}_${Date.now().toString(36)}${Math.random().toString(36).slice(2, 10)}`;
}
