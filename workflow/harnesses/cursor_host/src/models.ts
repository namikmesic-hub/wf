// Model ids: wf workflows name Cursor models the way cursor-agent lists them, with the parameters
// in the id (gpt-5.6-sol-max-fast). The SDK takes a catalog id and parameters from
// Cursor.models.list(): { id: "gpt-5.6-sol", params: [{ id: "reasoning", value: "max" }, ...] }.
//
// resolveModel() takes, in this order:
// - a catalog id or one of its aliases, with no parameters: the catalog's default variant.
// - `id[name=value,...]`, the form cursor-agent also takes, with exactly those parameters.
// - an id that starts with a catalog id, followed by words that name one variant of it: a word is
//   a parameter's value (high, max, xhigh, or extra-high for xhigh), or the name of a parameter
//   that is on (fast, thinking). When the words fit several variants, the one whose other
//   parameters have the default variant's values wins.
// Anything else, or words that fit no variant or no single one, is refused, so a session never
// runs on a model that wasn't asked for.

import type { ModelListItem, ModelParameterValue } from "@cursor/sdk";
import { fail } from "./protocol.ts";

export interface Resolved {
  requested: string;
  id: string;
  params: ModelParameterValue[];
}

const SPELLINGS: Record<string, string[][]> = { xhigh: [["xhigh"], ["extra", "high"]] };

export function resolveModel(requested: string, catalog: ModelListItem[]): Resolved {
  const wanted = requested.trim();
  const named = (id: string) => catalog.find((m) => m.id === id || m.aliases?.includes(id) === true);
  if (wanted === "auto" && named("default")) return { requested, id: "default", params: [] };

  const bracket = /^([^[\]]+)\[([^\]]*)\]$/.exec(wanted);
  if (bracket) {
    const model = named(bracket[1].trim());
    if (!model) throw unknown(requested, catalog);
    const params = bracket[2].split(",").map((pair) => pair.trim()).filter(Boolean).map((pair) => {
      const [id, ...rest] = pair.split("=");
      return { id: id.trim(), value: rest.join("=").trim() };
    });
    for (const param of params) {
      const known = model.parameters?.find((p) => p.id === param.id);
      if (!known || !known.values.some((v) => v.value === param.value)) {
        throw fail("unavailable", "cursor.model_parameter",
          `Cursor's model ${model.id} has no parameter ${param.id}=${param.value}: it takes ${describe(model)}`);
      }
    }
    return { requested, id: model.id, params };
  }

  const exact = named(wanted);
  if (exact) return { requested, id: exact.id, params: [] };

  const base = longestPrefix(wanted, catalog);
  if (!base) throw unknown(requested, catalog);
  const words = wanted.slice(base.name.length + 1).toLowerCase().split("-").filter(Boolean);
  const variants = base.model.variants ?? [];
  const fitting = variants.filter((v) => fits(v.params, words));
  const fallback = variants.find((v) => v.isDefault === true);
  const chosen = fitting.length === 1 ? fitting : fitting.filter((v) => sameUnnamed(v.params, fallback?.params ?? [], words));
  if (chosen.length !== 1) {
    const why = fitting.length === 0 ? "none of its variants" : "more than one of its variants";
    throw fail("unavailable", "cursor.model_variant",
      `Cursor model id ${requested} names ${why} of ${base.model.id}: give it as ${forms(base.model)}`);
  }
  return { requested, id: base.model.id, params: chosen[0].params.map((p) => ({ id: p.id, value: p.value })) };
}

function longestPrefix(wanted: string, catalog: ModelListItem[]): { name: string; model: ModelListItem } | null {
  let best: { name: string; model: ModelListItem } | null = null;
  for (const model of catalog) {
    for (const name of [model.id, ...(model.aliases ?? [])]) {
      if (wanted.startsWith(name + "-") && (best === null || name.length > best.name.length)) best = { name, model };
    }
  }
  return best;
}

// Do these words name exactly this variant? Each word must be used once: by a parameter that is
// on (its name), or by a parameter's value. A parameter that is off must not be named.
function fits(params: ModelParameterValue[], words: string[]): boolean {
  let left = [...words];
  for (const param of params) {
    const value = param.value.toLowerCase();
    if (value === "true") {
      if (!left.includes(param.id.toLowerCase())) return false;
      left = without(left, [param.id.toLowerCase()]);
    } else if (value === "false") {
      if (left.includes(param.id.toLowerCase())) return false;
    } else {
      const spelling = (SPELLINGS[value] ?? [[value]]).find((s) => contains(left, s));
      if (spelling) left = without(left, spelling);
    }
  }
  return left.length === 0;
}

// The parameters that no word names have the default variant's values.
function sameUnnamed(params: ModelParameterValue[], defaults: ModelParameterValue[], words: string[]): boolean {
  return params.every((param) => {
    const value = param.value.toLowerCase();
    const named = value === "true" || value === "false" || (SPELLINGS[value] ?? [[value]]).some((s) => contains(words, s));
    return named || defaults.some((d) => d.id === param.id && d.value === param.value);
  });
}

function contains(words: string[], wanted: string[]): boolean {
  const left = [...words];
  for (const word of wanted) {
    const index = left.indexOf(word);
    if (index === -1) return false;
    left.splice(index, 1);
  }
  return true;
}

function without(words: string[], taken: string[]): string[] {
  const left = [...words];
  for (const word of taken) left.splice(left.indexOf(word), 1);
  return left;
}

function describe(model: ModelListItem): string {
  const parameters = model.parameters ?? [];
  if (parameters.length === 0) return "no parameters";
  return parameters.map((p) => `${p.id} (${p.values.map((v) => v.value).join(", ")})`).join(", ");
}

function forms(model: ModelListItem): string {
  const variants = model.variants ?? [];
  if (variants.length === 0) return model.id;
  return "one of " + variants.slice(0, 12).map((v) => `${model.id}[${v.params.map((p) => `${p.id}=${p.value}`).join(",")}]`).join(", ");
}

function unknown(requested: string, catalog: ModelListItem[]) {
  const ids = catalog.map((m) => m.id);
  const listed = ids.length > 40 ? `${ids.slice(0, 40).join(", ")} and ${ids.length - 40} more` : ids.join(", ");
  return fail("unavailable", "cursor.model", `Cursor has no model \`${requested}\`: this account's models are ${listed}. Use id[name=value,...] for explicit parameters.`);
}

// The model a session uses, as the transcript records it: { model, ...parameters }.
export function effective(resolved: Resolved): Record<string, string> {
  return { model: resolved.id, ...Object.fromEntries(resolved.params.map((p) => [p.id, p.value])) };
}
