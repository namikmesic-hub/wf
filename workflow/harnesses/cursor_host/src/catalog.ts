import { createHash, randomUUID } from "node:crypto";
import { mkdir, readFile, readdir, rename, rm, rmdir, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { setTimeout as pause } from "node:timers/promises";
import type { ModelListItem } from "@cursor/sdk";
import { RequestBudget } from "./budget.ts";

export const CATALOG_TTL_MS = 15 * 60_000;

export async function cachedCatalog(
  apiKey: string, fetch: () => Promise<ModelListItem[]>,
  root: string, budget = new RequestBudget(),
): Promise<ModelListItem[]> {
  const file = join(root, createHash("sha256").update(apiKey).digest("hex") + ".json");
  const lock = file + ".lock";
  await mkdir(root, { recursive: true, mode: 0o700 });
  const cached = await readCatalog(file);
  if (cached !== null) return cached;
  const owner = `owner-${process.pid}-${randomUUID()}`;
  const candidate = lock + "." + owner;
  const temp = file + "." + randomUUID();
  let held = false;
  try {
    await mkdir(candidate, { mode: 0o700 });
    await writeFile(join(candidate, owner), "", { mode: 0o600 });
    for (;;) {
      const cached = await readCatalog(file);
      if (cached !== null) return cached;
      try {
        await rename(candidate, lock);
        held = true;
        break;
      } catch (error) {
        if (!["EEXIST", "ENOTEMPTY"].includes((error as NodeJS.ErrnoException).code ?? "")) throw error;
        await clearDeadLock(lock);
        budget.retrying();
        await budget.wait(50, pause);
      }
    }
    const cached = await readCatalog(file);
    if (cached !== null) return cached;
    const models = await budget.run(fetch);
    await writeFile(temp, JSON.stringify({ fetched_at: Date.now(), models }), { mode: 0o600 });
    await rename(temp, file);
    return models;
  } finally {
    await rm(temp, { force: true });
    if (held) {
      await rm(join(lock, owner), { force: true });
      await removeEmptyLock(lock);
    }
    await rm(candidate, { recursive: true, force: true });
  }
}

async function clearDeadLock(lock: string): Promise<void> {
  let owners: string[];
  try {
    owners = await readdir(lock);
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return;
    throw error;
  }
  for (const owner of owners) {
    const pid = /^owner-([1-9]\d*)-/.exec(owner)?.[1];
    if (!pid) continue;
    try {
      process.kill(Number(pid), 0);
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === "ESRCH") await rm(join(lock, owner), { force: true });
    }
  }
  await removeEmptyLock(lock);
}

async function removeEmptyLock(lock: string): Promise<void> {
  try {
    await rmdir(lock);
  } catch (error) {
    if (!["ENOENT", "ENOTEMPTY", "EEXIST"].includes((error as NodeJS.ErrnoException).code ?? "")) throw error;
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
