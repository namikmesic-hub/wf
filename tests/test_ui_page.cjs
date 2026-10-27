const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.attributes = {}; }
  setAttribute(key, value) { this.attributes[key] = value; }
  addEventListener() {}
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  get childElementCount() { return this.children.filter((child) => child instanceof Element).length; }
}

function page() {
  const brief = new Element("div");
  const context = vm.createContext({
    URL, URLSearchParams, location: { hash: "" }, sessionStorage: { getItem() {}, setItem() {} },
    document: {
      createElement: (tag) => new Element(tag), createTextNode: (text) => text, getElementById: () => brief,
    },
  });
  const source = fs.readFileSync(path.join(__dirname, "../workflow/ui/page.js"), "utf8");
  vm.runInContext(source.split("// ---------- start ----------")[0], context);
  return { brief, show(data) { context.data = data; vm.runInContext("showBrief(data)", context); } };
}

function elements(node) {
  return node instanceof Element ? [node, ...node.children.flatMap(elements)] : [];
}

function text(node) {
  return node instanceof Element ? (node.textContent || "") + node.children.map(text).join("") : String(node);
}

test("The Task card shows the deliver-change output", () => {
  const view = page();
  view.show({
    inputs: { branch: "fm/task", delivery_mode: "branch" },
    output: { pr_url: "https://github.com/owner/repo/pull/12", head: "abc123", not_ready: "Tests failed." },
  });
  assert(text(view.brief).includes("Deliverables: Branch: fm/task"));
  assert(text(view.brief).includes("Delivery mode: branch"));
  assert(text(view.brief).includes("Head: abc123"));
  assert(text(view.brief).includes("Not ready: Tests failed."));
  assert(elements(view.brief).some((node) => node.tag === "a" && node.textContent === "owner/repo#12"));
});

test("Task text keeps its line breaks and links", () => {
  const view = page();
  const data = {
    description: "Add a flag.", main_input: "task",
    inputs: { task: "Fix <script>alert(1)</script> at https://linear.app/team/issue/DENG-860/task.\nKeep this line.\n\nAdd a flag." },
  };
  view.show(data);
  const nodes = elements(view.brief);
  const details = nodes.find((node) => node.tag === "details");
  const paragraphs = nodes.filter((node) => node.tag === "p" && node.className === "task-text");
  assert(text(paragraphs[0]).endsWith("\nKeep this line."));
  assert(!text(paragraphs[0]).includes("Add a flag."));
  assert(text(details).endsWith("Add a flag."));
  assert(nodes.some((node) => node.tag === "summary" && node.textContent === "show all"));
  assert(nodes.some((node) => node.tag === "a" && node.textContent === "DENG-860"));
  assert(nodes.filter((node) => node.tag === "a").every((node) => /^https?:/.test(node.attributes.href)));
  assert(text(view.brief).includes("<script>alert(1)</script>"));
  assert(!nodes.some((node) => node.tag === "script"));
  details.open = true;
  view.show(data);
  assert.equal(elements(view.brief).find((node) => node.tag === "details"), details);
  assert.equal(details.open, true);
});

test("The Task card accepts missing data and shows follow-ups", () => {
  const view = page();
  for (const data of [null, {}, { inputs: {}, follow_ups: [] }]) {
    view.show(data);
    assert.equal(view.brief.children.length, 0);
  }
  view.show({
    inputs: { login: "user", count: 2, enabled: false },
    follow_ups: [
      { number: 2, time: "2026-10-27T16:00:00Z", request: "Add a check." },
      { number: 1, time: "2026-10-27T15:00:00Z", request: "Add a flag." },
    ],
  });
  const shown = text(view.brief);
  assert(shown.indexOf("Follow-up 2") < shown.indexOf("Follow-up 1"));
  assert(shown.includes("Add a check."));
  assert(shown.includes("loginuser"));
  assert(shown.includes("count2"));
  assert(shown.includes("enabledfalse"));
});
