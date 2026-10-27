import { fail } from "./protocol.ts";

export class RequestBudget {
  private end: number;
  private now: () => number;
  private retryable = false;

  constructor(ms = 54_000, now = () => performance.now()) {
    this.now = now;
    this.end = now() + ms;
  }

  private expired() {
    return fail(this.retryable ? "transient" : "unavailable", "cursor.request_timeout", "Cursor's request time limit has expired. Try again.",
      { retryable: this.retryable, ...(this.retryable ? { retry_after_s: 30 } : {}) });
  }

  retrying(): void {
    this.retryable = true;
  }

  async run<T>(action: () => Promise<T>): Promise<T> {
    const remaining = this.end - this.now();
    if (remaining <= 0) throw this.expired();
    let timer: ReturnType<typeof setTimeout> | undefined;
    const expired = new Promise<never>((_, reject) => {
      timer = setTimeout(() => reject(this.expired()), Math.ceil(remaining)).unref();
    });
    try {
      return await Promise.race([action(), expired]);
    } finally {
      clearTimeout(timer);
    }
  }

  async wait(ms: number, pause: (ms: number) => Promise<unknown>): Promise<void> {
    if (ms >= this.end - this.now()) throw this.expired();
    await this.run(() => pause(ms));
  }
}
