import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { mkdtemp, readFile, readdir, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { promisify } from "node:util";
import { cachedCatalog, CATALOG_TTL_MS } from "../src/catalog.ts";
import { retryRateLimit } from "../src/cursor.ts";

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
