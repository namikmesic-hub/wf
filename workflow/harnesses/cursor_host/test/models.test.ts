import assert from "node:assert/strict";
import { test } from "node:test";
import type { ModelListItem } from "@cursor/sdk";
import { effective, resolveModel } from "../src/models.ts";
import { HostError } from "../src/protocol.ts";

function model(id: string, parameters: Record<string, string[]>, defaults: Record<string, string>, aliases?: string[]): ModelListItem {
  let combos: Array<Array<{ id: string; value: string }>> = [[]];
  for (const [name, values] of Object.entries(parameters)) {
    combos = combos.flatMap((params) => values.map((value) => [...params, { id: name, value }]));
  }
  return {
    id, displayName: id, ...(aliases ? { aliases } : {}),
    parameters: Object.entries(parameters).map(([name, values]) => ({ id: name, values: values.map((value) => ({ value })) })),
    variants: combos.map((params) => ({ params, displayName: "", isDefault: params.every((p) => defaults[p.id] === p.value) })),
  };
}

const CATALOG = [
  model("gpt-5.6-sol", { reasoning: ["none", "low", "medium", "high", "xhigh", "max"], fast: ["false", "true"], context: ["272k", "1m"] },
    { reasoning: "medium", fast: "false", context: "1m" }),
  model("claude-opus-5", { thinking: ["false", "true"], effort: ["low", "medium", "high", "xhigh", "max"], fast: ["false", "true"] },
    { thinking: "false", effort: "high", fast: "false" }),
  model("composer-2.5", { fast: ["false", "true"] }, { fast: "false" }, ["composer"]),
  { id: "default", displayName: "Auto" },
];

const params = (id: string) => {
  const resolved = resolveModel(id, CATALOG);
  return [resolved.id, Object.fromEntries(resolved.params.map((p) => [p.id, p.value]))];
};

test("a catalog id or alias runs the catalog's default variant", () => {
  assert.deepEqual(resolveModel("composer-2.5", CATALOG), { requested: "composer-2.5", id: "composer-2.5", params: [] });
  assert.equal(resolveModel("composer", CATALOG).id, "composer-2.5");
  assert.equal(resolveModel("auto", CATALOG).id, "default");
});

test("cursor-agent's ids name a catalog model and its parameters", () => {
  assert.deepEqual(params("gpt-5.6-sol-max-fast"), ["gpt-5.6-sol", { reasoning: "max", fast: "true", context: "1m" }]);
  assert.deepEqual(params("gpt-5.6-sol-medium"), ["gpt-5.6-sol", { reasoning: "medium", fast: "false", context: "1m" }]);
  assert.deepEqual(params("gpt-5.6-sol-extra-high"), ["gpt-5.6-sol", { reasoning: "xhigh", fast: "false", context: "1m" }]);
  assert.deepEqual(params("gpt-5.6-sol-xhigh-fast"), ["gpt-5.6-sol", { reasoning: "xhigh", fast: "true", context: "1m" }]);
  assert.deepEqual(params("claude-opus-5-thinking-high-fast"), ["claude-opus-5", { thinking: "true", effort: "high", fast: "true" }]);
  assert.deepEqual(params("composer-2.5-fast"), ["composer-2.5", { fast: "true" }]);
});

test("the bracket form gives the parameters as they are", () => {
  assert.deepEqual(params("gpt-5.6-sol[reasoning=high, context=272k]"), ["gpt-5.6-sol", { reasoning: "high", context: "272k" }]);
  assert.throws(() => resolveModel("gpt-5.6-sol[reasoning=huge]", CATALOG), /has no parameter reasoning=huge/);
});

test("an id that names no model, or no single variant, is refused", () => {
  const refused = (id: string, pattern: RegExp) =>
    assert.throws(() => resolveModel(id, CATALOG), (error: unknown) =>
      error instanceof HostError && error.info.class === "unavailable" && pattern.test(error.info.message));
  refused("claude-fable-9-max", /Cursor has no model `claude-fable-9-max`/);
  refused("gpt-5.6-sol-turbo", /names none of its variants of gpt-5.6-sol: give it as one of gpt-5.6-sol\[/);
  refused("gpt-5.6-sol-high-high", /names none of its variants/);
});

test("the transcript's record of a model holds its parameters", () => {
  assert.deepEqual(effective(resolveModel("composer-2.5-fast", CATALOG)), { model: "composer-2.5", fast: "true" });
});
