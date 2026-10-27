import assert from "node:assert/strict";
import { execFile, spawn } from "node:child_process";
import { once } from "node:events";
import { mkdir, mkdtemp, readFile, readdir, rm, utimes, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { mock, test } from "node:test";
import { setTimeout as pause } from "node:timers/promises";
import { promisify } from "node:util";
import { cachedCatalog, CATALOG_TTL_MS, LOCK_MAX_AGE_MS } from "../src/catalog.ts";
import { retryRateLimit } from "../src/cursor.ts";
import { RequestBudget } from "../src/budget.ts";
import { HostError } from "../src/protocol.ts";

const MODELS = [{ id: "composer-2.5", displayName: "Composer" }];
const exec = promisify(execFile);

test("the cache shares one fetch across processes and keeps keys apart", async () => {
  const root = await mkdtemp(join(tmpdir(), "wf-catalog-"));
  try {
    const script = `
      import { appendFile } from 'node:fs/promises';
      import { cachedCatalog } from ${JSON.stringify(new URL("../src/catalog.ts", import.meta.url).href)};
      await cachedCatalog('key-a', async () => {
        await appendFile(${JSON.stringify(join(root, "fetches"))}, 'fetch\\n');
        await new Promise(resolve => setTimeout(resolve, 50));
        return ${JSON.stringify(MODELS)};
      }, ${JSON.stringify(root)});
    `;
    await Promise.all(Array.from({ length: 4 }, () => exec(process.execPath, ["--input-type=module", "-e", script])));
    assert.equal(await readFile(join(root, "fetches"), "utf8"), "fetch\n");
    assert.deepEqual(await cachedCatalog("key-a", async () => { throw Error("no fetch"); }, root), MODELS);
    let other = 0;
    await cachedCatalog("key-b", async () => { other += 1; return []; }, root);
    assert.equal(other, 1);
    assert.equal((await readdir(root)).filter(name => name.endsWith(".json")).length, 2);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("expired or damaged data is fetched again and a failed fetch releases the lock", async () => {
  const root = await mkdtemp(join(tmpdir(), "wf-catalog-"));
  try {
    let calls = 0;
    const fetch = async () => { calls += 1; return MODELS; };
    await cachedCatalog("key", fetch, root);
    const [name] = await readdir(root);
    const file = join(root, name);
    await writeFile(file, JSON.stringify({ fetched_at: Date.now() - CATALOG_TTL_MS, models: MODELS }));
    await cachedCatalog("key", fetch, root);
    assert.equal(calls, 2);
    await writeFile(file, "broken");
    await assert.rejects(cachedCatalog("key", async () => { throw Error("offline"); }, root), /offline/);
    assert.deepEqual(await cachedCatalog("key", fetch, root), MODELS);
    assert.equal(calls, 3);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("a dead process leaves a lock that the next fetch reclaims at once", async () => {
  const root = await mkdtemp(join(tmpdir(), "wf-dead-catalog-"));
  const script = `
    import { writeFile } from 'node:fs/promises';
    import { cachedCatalog } from ${JSON.stringify(new URL("../src/catalog.ts", import.meta.url).href)};
    await cachedCatalog('key', async () => {
      await writeFile(${JSON.stringify(join(root, "fetching"))}, 'started');
      await new Promise(() => setInterval(() => {}, 1000));
      return [];
    }, ${JSON.stringify(root)});
  `;
  const child = spawn(process.execPath, ["--input-type=module", "-e", script], { stdio: "ignore" });
  try {
    for (let tries = 0; ; tries += 1) {
      if ((await readdir(root)).includes("fetching")) break;
      assert.ok(tries < 200, "the child did not take the lock");
      await pause(10);
    }
    const ended = once(child, "exit");
    child.kill("SIGKILL");
    await ended;
    let calls = 0;
    const fetch = async () => { calls += 1; return MODELS; };
    const found = await Promise.all(Array.from({ length: 4 }, () => cachedCatalog("key", fetch, root, new RequestBudget(2_000))));
    assert.equal(calls, 1);
    assert.ok(found.every(models => models[0].id === MODELS[0].id));
    assert.equal((await readdir(root)).filter(name => name.includes(".lock")).length, 0);
  } finally {
    if (child.exitCode === null && child.signalCode === null) {
      const ended = once(child, "exit");
      child.kill("SIGKILL");
      await ended;
    }
    await rm(root, { recursive: true, force: true });
  }
});

test("an old lock cannot block the cache when its process id is in use", async () => {
  const root = await mkdtemp(join(tmpdir(), "wf-old-catalog-"));
  try {
    await cachedCatalog("key", async () => MODELS, root);
    const [name] = await readdir(root);
    await rm(join(root, name));
    const lock = join(root, name + ".lock");
    await mkdir(lock);
    await writeFile(join(lock, `owner-${process.pid}-old`), "");
    const old = new Date(Date.now() - LOCK_MAX_AGE_MS - 1_000);
    await utimes(lock, old, old);
    let calls = 0;
    const fetch = async () => { calls += 1; return MODELS; };
    const found = await Promise.all(Array.from({ length: 4 }, () => cachedCatalog("key", fetch, root, new RequestBudget(2_000))));
    assert.equal(calls, 1);
    assert.ok(found.every(models => models[0].id === MODELS[0].id));
    assert.equal((await readdir(root)).filter(name => name.includes(".lock")).length, 0);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("cache waits and both retry loops share one request budget", async () => {
  const rate = Object.assign(new Error("rate limit"), { name: "RateLimitError" });
  for (const cacheWait of [0, 10_000, 50_000]) {
    let elapsed = 0;
    const budget = new RequestBudget(54_000, () => elapsed);
    const wait = async (ms: number) => { elapsed += ms; };
    let catalogCalls = 0;
    let sessionCalls = 0;
    const open = async () => {
      await budget.wait(cacheWait, wait);
      await retryRateLimit(async () => { if (++catalogCalls < 4) throw rate; return []; }, wait, budget);
      await retryRateLimit(async () => { if (++sessionCalls < 4) throw rate; return "open"; }, wait, budget);
    };
    await assert.rejects(open(), error => error instanceof HostError
      && error.info.code === "cursor.request_timeout" && error.info.retryable);
    assert.ok(elapsed < 54_000);
    assert.ok(sessionCalls < 4);
  }
});

test("a pending SDK call cannot hold the request past its budget", async () => {
  mock.timers.enable({ apis: ["setTimeout"] });
  try {
    const budget = new RequestBudget(100);
    budget.retrying();
    const pending = budget.run(() => new Promise(() => {}));
    const refused = assert.rejects(pending, error => error instanceof HostError && error.info.retryable);
    mock.timers.tick(100);
    await refused;
  } finally {
    mock.timers.reset();
  }
});

test("Cursor rate limits wait with a bounded backoff and usage limits stop at once", async () => {
  const delays: number[] = [];
  const rate = Object.assign(new Error("You have exceeded the rate limit of 30 requests per minute for the get_models endpoint"),
    { name: "ConfigurationError", code: "resource_exhausted" });
  let calls = 0;
  assert.equal(await retryRateLimit(async () => { if (++calls < 3) throw rate; return "ok"; }, async ms => { delays.push(ms); }), "ok");
  assert.deepEqual(delays, [5_000, 10_000]);
  calls = 0;
  delays.length = 0;
  await assert.rejects(retryRateLimit(async () => { calls += 1; throw rate; }, async ms => { delays.push(ms); }), /rate limit/);
  assert.equal(calls, 4);
  assert.deepEqual(delays, [5_000, 10_000, 20_000]);
  await assert.rejects(retryRateLimit(async () => { throw Object.assign(new Error("usage limit"), { name: "RateLimitError" }); },
    async () => { assert.fail("a usage limit must not wait"); }), /usage limit/);
});
