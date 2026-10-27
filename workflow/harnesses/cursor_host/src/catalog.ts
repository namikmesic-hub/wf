import { createHash, randomUUID } from "node:crypto";
import { mkdir, readFile, rename, rm, stat, writeFile } from "node:fs/promises";
import { homedir } from "node:os";
import { join } from "node:path";
import { setTimeout as pause } from "node:timers/promises";
import type { ModelListItem } from "@cursor/sdk";
import { fail } from "./protocol.ts";

export const CATALOG_TTL_MS = 15 * 60_000;
const LOCK_MS = 120_000;

export async function cachedCatalog(
  apiKey: string, fetch: () => Promise<ModelListItem[]>,
  root = join(process.env.WF_HOME || join(homedir(), ".wf"), "cache", "cursor-models"),
): Promise<ModelListItem[]> {
  const file = join(root, createHash("sha256").update(apiKey).digest("hex") + ".json");
  const lock = file + ".lock";
  await mkdir(root, { recursive: true, mode: 0o700 });
  const until = Date.now() + 55_000;
  for (;;) {
    const cached = await readCatalog(file);
    if (cached !== null) return cached;
    try {
      await mkdir(lock, { mode: 0o700 });
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "EEXIST") throw error;
      try {
        if (Date.now() - (await stat(lock)).mtimeMs > LOCK_MS) await rm(lock, { recursive: true, force: true });
      } catch (error) {
        if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
      }
      if (Date.now() >= until) throw fail("transient", "cursor.catalog_busy", "Cursor's model cache is busy. Try again.", { retryable: true });
      await pause(50);
      continue;
    }
    const temp = file + "." + randomUUID();
    try {
      const cached = await readCatalog(file);
      if (cached !== null) return cached;
      const models = await fetch();
      await writeFile(temp, JSON.stringify({ fetched_at: Date.now(), models }), { mode: 0o600 });
      await rename(temp, file);
      return models;
    } finally {
      await rm(temp, { force: true });
      await rm(lock, { recursive: true, force: true });
    }
  }
}

async function readCatalog(file: string): Promise<ModelListItem[] | null> {
  try {
    const saved = JSON.parse(await readFile(file, "utf8"));
    if (!saved || typeof saved.fetched_at !== "number") return null;
    const age = Date.now() - saved.fetched_at;
    if (Number.isFinite(age) && age >= 0 && age < CATALOG_TTL_MS && Array.isArray(saved.models)
        && saved.models.every((m: ModelListItem) => m && typeof m.id === "string")) return saved.models;
  } catch (error) {
    if (!(error instanceof SyntaxError) && (error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
  }
  return null;
}
