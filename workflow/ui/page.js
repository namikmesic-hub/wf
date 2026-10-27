// wf ui: the page. It reads the JSON of workflow/ui.py and puts what runs wrote into the page as text only.
"use strict";

const STATES = [
  ["waiting", "○", "waiting"], ["running", "▶", "running"], ["paused", "‖", "paused"],
  ["done", "✓", "done"], ["failed", "✗", "failed"], ["skipped", "–", "skipped"],
];
const SYMBOL = Object.fromEntries(STATES.map(([state, symbol]) => [state, symbol]));
const BOX_W = 176, BOX_H = 50, ROW_H = 88, COL_W = 204, SIDE = 150, TOP = 22, END_W = 74, END_H = 26;
const SVG = "http://www.w3.org/2000/svg";

let selected = null;
let eventsNext = null;
let shownFile = null;
let shownBrief = null;
const busy = new Set();

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value);
  }
  node.append(...children.filter((c) => c !== null && c !== undefined));
  return node;
}

function svg(tag, attrs = {}, ...children) {
  const node = document.createElementNS(SVG, tag);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
  node.append(...children);
  return node;
}

function text(id, value) { document.getElementById(id).textContent = value; }

// The key comes in the address that wf ui prints (#token=...). The tab keeps it when the address shows a run.
const token = new URLSearchParams(location.hash.slice(1)).get("token") || sessionStorage.getItem("wf-token") || "";
sessionStorage.setItem("wf-token", token);

async function getJSON(url) {
  const response = await fetch(url, { cache: "no-store", headers: { "X-WF-Token": token } });
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || response.statusText);
  return body;
}

async function once(name, work) {
  if (busy.has(name)) return;
  busy.add(name);
  try {
    await work();
    text("problem", "");
  } catch (error) {
    text("problem", `Can't read ${name}: ${error.message}`);
  } finally {
    busy.delete(name);
  }
}

function clock(when) {
  if (!when) return "";
  const date = new Date(when);
  return Number.isNaN(date.getTime()) ? when : date.toLocaleTimeString([], { hour12: false });
}

function duration(seconds) {
  seconds = Math.round(seconds);
  if (seconds < 60) return `${seconds} s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)} min`;
  const minutes = Math.round(seconds / 60);
  return `${Math.floor(minutes / 60)} h ${minutes % 60} min`;
}

function dot(kind) { return el("span", { class: `dot ${kind}`, "aria-hidden": "true" }); }

// ---------- the runs ----------

async function refreshRuns() {
  const found = await getJSON("/api/runs");
  text("folder", found.folder);
  const list = document.getElementById("run-list");
  list.replaceChildren(...found.runs.map((run) => {
    const line = el("span", { class: "run-line" }, dot(`run-${run.status}`), el("span", { text: run.status }));
    if (run.progress) {
      const meter = el("span", { class: "mini-meter", "aria-hidden": "true" }, el("div"));
      meter.firstChild.style.width = `${run.progress.percent}%`;
      line.append(meter, el("span", { text: `about ${run.progress.percent}%` }));
    }
    const button = el("button", { type: "button", "data-id": run.id, "aria-current": String(run.id === selected), onclick: () => select(run.id) },
      el("span", { class: "run-name", text: run.run }),
      run.summary ? el("span", { class: "run-summary", text: run.summary, title: run.summary }) : null,
      el("span", { class: "run-line", text: `${run.workflow || "?"}${run.dry_run ? " (dry run)" : ""}` }), line);
    return el("li", {}, button);
  }));
  document.getElementById("no-runs").hidden = found.runs.length > 0;
  if (!selected && found.runs.length) select(fromHash() || found.runs[0].id);
}

function fromHash() {
  return new URLSearchParams(location.hash.slice(1)).get("run");
}

function select(id) {
  if (id === selected) return;
  selected = id;
  eventsNext = null;
  shownFile = null;
  shownBrief = null;
  history.replaceState(null, "", `#run=${encodeURIComponent(id)}`);
  document.getElementById("events").replaceChildren();
  document.getElementById("viewer").hidden = true;
  document.querySelectorAll("#run-list button").forEach((b) => b.setAttribute("aria-current", String(b.dataset.id === id)));
  once("the run", refreshRun);
  once("the events", refreshEvents);
}

// ---------- one run ----------

async function refreshRun() {
  if (!selected) return;
  const id = selected;
  const found = await getJSON(`/api/run?id=${encodeURIComponent(id)}`);
  if (id !== selected) return;
  document.getElementById("run").hidden = false;
  const state = found.state;
  text("run-id", state.run);
  text("run-workflow", `${state.workflow || ""}${state.dry_run ? " (dry run)" : ""}`);
  document.getElementById("run-status").replaceChildren(dot(`run-${state.status}`), el("span", { text: state.status }));
  text("run-detail", detail(state));
  document.getElementById("run-detail").hidden = !detail(state);
  showBrief(found.brief);
  showProgress(found.progress, state.status);
  showGraph(found.graph, found.progress, found.states);
  showSessions(found.sessions);
  showMetrics(found.metrics, found.states);
  showAttempts(found.attempts);
  if (shownFile) await showFile(shownFile, false);
}

function detail(state) {
  if (state.status === "waiting") return `Waits for a person: ${(state.nodes || []).join(", ")}. Answer with: ${state.answer}`;
  if (state.status === "failed") return `Failed at ${state.node || "?"}: ${state.error || ""}. Continue with: ${state.retry}`;
  if (state.status === "stopped") return `${state.reason}. Continue with: ${state.retry}`;
  if (state.status === "limit") return `Paused at a limit: ${state.reason}. Continue with: ${state.retry}`;
  if (state.agents_waiting && state.agents_waiting.length) {
    return `Waits for an agent slot: ${state.agents_waiting.map((w) => w.node).join(", ")}`;
  }
  return "";
}

function urls(value) {
  if (typeof value === "string") return (value.match(/https?:\/\/[^\s<>"'`]+/gi) || []).map((url) => url.replace(/[.,!?;:)\]}]+$/, ""));
  if (value && typeof value === "object") return Object.values(value).flatMap(urls);
  return [];
}

function linkLabel(url) {
  const parsed = new URL(url);
  const ticket = parsed.hostname === "linear.app" && parsed.pathname.match(/\/issue\/([a-z]+-\d+)/i);
  if (ticket) return ticket[1].toUpperCase();
  const pr = parsed.hostname === "github.com" && parsed.pathname.match(/^\/([^/]+)\/([^/]+)\/pull\/(\d+)/);
  return pr ? `${pr[1]}/${pr[2]}#${pr[3]}` : `${parsed.hostname}${parsed.pathname === "/" ? "" : parsed.pathname}`;
}

function link(url, label = url) {
  try {
    const parsed = new URL(url);
    if (parsed.protocol === "http:" || parsed.protocol === "https:") {
      return el("a", { href: url, target: "_blank", rel: "noopener noreferrer", text: label });
    }
  } catch (_) {}
  return document.createTextNode(label);
}

function linkedText(value) {
  const node = el("span");
  const pattern = /https?:\/\/[^\s<>"'`]+/gi;
  let at = 0;
  for (const match of value.matchAll(pattern)) {
    const url = urls(match[0])[0];
    node.append(value.slice(at, match.index), link(url));
    at = match.index + url.length;
  }
  node.append(value.slice(at));
  return node;
}

function taskText(value) {
  const node = el("div");
  const split = /\r?\n[ \t]*\r?\n/.exec(value);
  const first = split ? value.slice(0, split.index) : value;
  node.append(el("p", { class: "task-text" }, linkedText(first)));
  if (split) node.append(el("details", {}, el("summary", { text: "show all" }),
    el("p", { class: "task-text" }, linkedText(value.slice(split.index + split[0].length)))));
  return node;
}

function showBrief(brief) {
  const signature = JSON.stringify(brief);
  if (signature === shownBrief) return;
  shownBrief = signature;
  const box = document.getElementById("brief");
  box.replaceChildren();
  if (!brief) return;
  const inputs = brief.inputs || {};
  if (brief.description) box.append(el("p", { class: "task-description secondary", text: brief.description, title: brief.description }));
  if (brief.main_input) box.append(taskText(inputs[brief.main_input]));
  const found = [...new Set(urls([inputs, brief.follow_ups, brief.output]))];
  if (found.length) {
    const line = el("p", {}, el("strong", { text: "Links: " }));
    found.forEach((url, i) => {
      if (i) line.append(" · ");
      let label = url;
      try { label = linkLabel(url); } catch (_) {}
      line.append(link(url, label));
    });
    box.append(line);
  }
  const fields = [
    [inputs, "branch", "Branch"], [inputs, "pr", "Pull request"],
    [inputs, "delivery_mode", "Delivery mode"], [inputs, "delivery", "Delivery mode"],
    [brief.output, "pr_url", "Pull request"], [brief.output, "pr", "Pull request"],
    [brief.output, "pull_request_url", "Pull request"], [brief.output, "head", "Head"],
    [brief.output, "not_ready_reason", "Not ready"],
  ];
  const deliverables = el("p", {}, el("strong", { text: "Deliverables: " }));
  let count = 0;
  for (const [source, key, label] of fields) {
    if (!source || source[key] === undefined || source[key] === null || source[key] === "") continue;
    if (count++) deliverables.append(" · ");
    deliverables.append(`${label}: `, linkedText(String(source[key])));
  }
  if (count) box.append(deliverables);
  for (const follow of brief.follow_ups || []) {
    const time = follow.time ? new Date(follow.time) : null;
    const when = time && !Number.isNaN(time.getTime()) ? time.toLocaleString() : follow.time || "";
    box.append(el("section", { class: "follow-up" }, el("h4", { text: `Follow-up ${follow.number}${when ? ` · ${when}` : ""}` }),
      taskText(follow.request || "")));
  }
  const other = el("dl", { class: "task-inputs" });
  for (const [key, value] of Object.entries(inputs)) {
    if (key === brief.main_input || fields.some(([source, field]) => source === inputs && field === key)) continue;
    if (value === null || value === "") continue;
    const shown = typeof value === "string" ? value : JSON.stringify(value);
    if (shown.length > 240 || shown.includes("\n")) continue;
    other.append(el("dt", { text: key }), el("dd", {}, linkedText(shown)));
  }
  if (other.childElementCount) box.append(other);
}

function showProgress(progress, status) {
  const meter = document.getElementById("meter");
  if (!progress) {
    text("percent", "");
    text("progress-text", "The workflow files of this run can't be read, so there is no estimate.");
    text("progress-basis", "");
    return;
  }
  text("percent", status === "succeeded" ? "100%" : `about ${progress.percent}%`);
  document.getElementById("meter-fill").style.width = `${progress.percent}%`;
  meter.setAttribute("aria-valuenow", progress.percent);
  const parts = [];
  if (progress.steps) {
    const at = progress.at.map((name) => where(name, progress.nodes[name]));
    parts.push(`Step ${progress.step} of ${progress.steps}${at.length ? ` (${at.join(", ")})` : ""}`);
  }
  for (const loop of progress.loops) {
    if (loop.max) parts.push(`${loop.from} → ${loop.to} ${loop.taken} of at most ${loop.max}`);
  }
  if (progress.left_s !== null && status !== "succeeded") {
    parts.push(progress.left_s >= 60 ? `about ${duration(progress.left_s)} left` : "less than a minute left");
  }
  text("progress-text", parts.join(" · "));
  text("progress-basis", progress.estimate === "earlier runs"
    ? `An estimate, from the time each step took in ${progress.earlier_runs} earlier finished run(s).`
    : "An estimate, from the steps: each loop counts as if it runs to its max.");
}

function where(name, node) {
  if (node && node.items_done !== undefined) return `${name}: ${node.items_done} of ${node.items} items done`;
  if (node && node.inner) return `${name}: step ${node.inner.step} of ${node.inner.steps}`;
  return name;
}

// ---------- the graph ----------

function showGraph(graph, progress, states) {
  const legend = document.getElementById("legend");
  legend.replaceChildren(...STATES.map(([state, symbol, word]) => el("span", {}, dot(`node-${state}`), el("span", { text: `${symbol} ${word}` }))));
  const box = document.getElementById("graph");
  if (!graph) {
    box.replaceChildren(el("p", { class: "muted", text: "The workflow files of this run can't be read." }));
    return;
  }
  const nodes = progress ? progress.nodes : {};
  const places = graph.nodes.map((n) => n.place);
  const half = Math.max(0, ...places.map(Math.abs)) * COL_W + BOX_W / 2;
  const width = 2 * (half + SIDE);
  const height = TOP * 2 + (graph.rows - 1) * ROW_H + BOX_H / 2 + END_H / 2;
  const at = {};
  for (const node of graph.nodes) {
    const terminal = node.kind === "start" || node.kind === "end";
    at[node.id] = { x: width / 2 + node.place * COL_W, y: TOP + node.row * ROW_H + BOX_H / 2, w: terminal ? END_W : BOX_W, h: terminal ? END_H : BOX_H };
  }
  const drawing = svg("svg", { width, height, viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": "The workflow graph" });
  drawing.append(svg("defs", {}, arrow("arrow", "arrow"), arrow("arrow-loop", "arrow loop")));
  const loops = {};
  for (const loop of progress ? progress.loops : []) loops[`${loop.from}>${loop.to}`] = loop;
  for (const edge of graph.edges) drawing.append(drawEdge(edge, at, loops));
  for (const node of graph.nodes) drawing.append(drawNode(node, at[node.id], nodes[node.id], states[node.id]));
  box.replaceChildren(drawing);
}

function arrow(id, cls) {
  return svg("marker", { id, viewBox: "0 0 10 10", refX: "9", refY: "5", markerWidth: "7", markerHeight: "7", orient: "auto-start-reverse" },
    svg("path", { d: "M0,0 L10,5 L0,10 z", class: cls }));
}

function drawEdge(edge, at, loops) {
  const a = at[edge.from], b = at[edge.to];
  let d, labelAt = null;
  if (edge.kind === "loop") {
    const ax = a.x + a.w / 2, bx = b.x + b.w / 2, reach = 46 + 10 * Math.round((a.y - b.y) / ROW_H);
    d = `M${ax},${a.y} C${ax + reach},${a.y} ${bx + reach},${b.y} ${bx + 2},${b.y}`;
    labelAt = { x: Math.max(ax, bx) + reach * 0.75 + 6, y: (a.y + b.y) / 2 };
  } else {
    const y1 = a.y + a.h / 2, y2 = b.y - b.h / 2 - 2, rows = Math.round((b.y - a.y) / ROW_H);
    const bend = rows > 1 ? -(a.w / 2 + 26 + 8 * rows) : 0;
    d = `M${a.x},${y1} C${a.x + bend},${y1 + ROW_H * 0.6} ${b.x + bend},${y2 - ROW_H * 0.6} ${b.x},${y2}`;
  }
  const group = svg("g");
  const tip = edge.label ? `${edge.from} → ${edge.to}: ${edge.label}` : `${edge.from} → ${edge.to}`;
  group.append(svg("path", { d, class: "hit" }, svg("title", {}, tip)));
  group.append(svg("path", { d, class: `edge ${edge.kind}`, "marker-end": `url(#${edge.kind === "loop" ? "arrow-loop" : "arrow"})` }));
  if (labelAt) {
    const loop = loops[`${edge.from}>${edge.to}`];
    const count = loop ? `${loop.taken} of ${loop.max ?? "?"}` : edge.label;
    group.append(svg("text", { x: labelAt.x, y: labelAt.y, class: "loop-label" }, count));
  }
  return group;
}

function drawNode(node, place, info, state = "waiting") {
  const left = place.x - place.w / 2, top = place.y - place.h / 2;
  if (node.kind === "start" || node.kind === "end") {
    return svg("g", {}, svg("rect", { x: left, y: top, width: place.w, height: place.h, rx: place.h / 2, class: "terminal" }),
      svg("text", { x: place.x, y: place.y + 4, "text-anchor": "middle" }, node.id));
  }
  const facts = [`${SYMBOL[state]} ${state}`];
  if (info && info.passes > 1) facts.push(`pass ${info.passes}`);
  if (info && info.items_done !== undefined) facts.push(`${info.items_done}/${info.items} items`);
  else if (info && info.inner) facts.push(`step ${info.inner.step}/${info.inner.steps}`);
  const group = svg("g", { class: state });
  const tip = [node.id, node.details.join(" · "), node.description].filter(Boolean).join("\n");
  group.append(svg("title", {}, tip));
  group.append(svg("rect", { x: left, y: top, width: place.w, height: place.h, rx: 6, class: `box ${state}` }));
  group.append(svg("rect", { x: left + 1, y: top + 1, width: 6, height: place.h - 2, rx: 3, class: `bar ${state}` }));
  const name = shorten(node.id, 24 - node.kind.length);
  group.append(svg("text", { x: left + 16, y: top + 20 }, name, svg("tspan", { class: "sub", dx: "6" }, node.kind)));
  group.append(svg("text", { x: left + 16, y: top + 38, class: "sub" }, shorten(facts.join(" · "), 28)));
  return group;
}

function shorten(value, most) { return value.length > most ? `${value.slice(0, most - 1)}…` : value; }

// ---------- tables ----------

function table(id, head, rows, empty) {
  const header = el("tr", {}, ...head.map(([name, num]) => el("th", { class: num ? "num" : "", text: name })));
  const body = rows.length ? rows : [el("tr", {}, el("td", { class: "empty", colspan: String(head.length), text: empty }))];
  document.getElementById(id).replaceChildren(el("thead", {}, header), el("tbody", {}, ...body));
}

function row(cells, cls = "") {
  return el("tr", { class: cls }, ...cells.map((cell) => {
    if (cell instanceof Node) return el("td", {}, cell);
    const [value, num] = Array.isArray(cell) ? cell : [cell, false];
    return el("td", { class: num ? "num" : "", text: value === null || value === undefined ? "" : String(value) });
  }));
}

function showSessions(sessions) {
  table("sessions", [["node"], ["pass"], ["attempt"], ["harness"], ["model"], ["started"], ["last tool call"], ["quiet", true]],
    sessions.map((s) => row([
      s.node, s.pass, s.attempt, s.harness, s.model || "", clock(s.started),
      s.last_tool ? `${s.last_tool.tool} ${s.last_tool.detail || ""}`.trim() : "none yet", [duration(s.quiet_s), true],
    ])), "No agent session runs now.");
}

// A step that runs counts what its pass has done so far: the server adds it to the numbers of the passes that ended
function showMetrics(metrics, states) {
  const rows = Object.entries(metrics.nodes).map(([name, n]) => row([
    name, states[name] ? `${SYMBOL[states[name]]} ${states[name]}` : "",
    [n.passes, true], [n.attempts, true], [n.failed_attempts, true], [n.items || "", true],
    [n.tokens.toLocaleString(), true], [n.cost_usd.toFixed(2), true], [duration(n.duration_s), true],
  ]));
  const run = metrics.run;
  rows.push(row(["run", "", "", "", "", "", [run.tokens.toLocaleString(), true], [run.cost_usd.toFixed(2), true],
    [duration(run.duration_s), true]], "total"));
  table("metrics", [["node"], ["state"], ["passes", true], ["attempts", true], ["failed", true], ["items", true],
    ["tokens", true], ["cost $", true], ["time", true]], rows, "");
  document.getElementById("notes").replaceChildren(...metrics.notes.map((note) => el("li", { text: note })));
}

function showAttempts(attempts) {
  table("attempts", [["node"], ["pass"], ["attempt"], ["harness"], ["files"]], attempts.map((a) => {
    const links = el("span");
    for (const [label, path] of [["transcript", a.log], ["instruction", a.instruction], ["output", a.output]]) {
      if (!path) continue;
      if (links.childNodes.length) links.append(" · ");
      links.append(el("button", { type: "button", class: "link", text: label, onclick: () => showFile(path, true) }));
    }
    return row([a.node, a.pass, a.attempt, [a.harness, a.model].filter(Boolean).join(" "), links]);
  }), "No attempt has started yet.");
}

// ---------- files ----------

async function showFile(path, scroll) {
  const id = selected;
  const found = await getJSON(`/api/file?id=${encodeURIComponent(id)}&path=${encodeURIComponent(path)}`);
  if (id !== selected) return;
  shownFile = path;
  const viewer = document.getElementById("viewer");
  viewer.hidden = false;
  text("viewer-title", path);
  text("viewer-cut", found.cut_bytes ? `The first ${found.cut_bytes.toLocaleString()} bytes are left out.` : "");
  const body = document.getElementById("viewer-body");
  const atEnd = body.scrollTop + body.clientHeight >= body.scrollHeight - 4;
  body.replaceChildren(...entries(found.text, path));
  if (scroll) viewer.scrollIntoView({ block: "nearest" });
  if (scroll || atEnd) body.scrollTop = body.scrollHeight;
}

function entries(content, path) {
  if (!path.endsWith(".log")) return [el("div", { class: "entry", text: content })];
  const lines = content.split("\n").filter((line) => line.trim());
  const parsed = lines.map((line) => { try { return JSON.parse(line); } catch { return null; } });
  if (!parsed.length || parsed.some((item) => !item || typeof item !== "object" || !item.kind)) {
    return [el("div", { class: "entry", text: content })];  // a command's output
  }
  return parsed.map((item) => {
    const { kind, ...rest } = item;
    let body;
    if (kind === "sent" || kind === "text") body = rest.text;
    else if (kind === "tool") body = `${rest.name || rest.title || ""} ${JSON.stringify(rest.input ?? rest.item ?? rest, null, 1)}`;
    else if (kind === "tool_result") body = rest.kept === false ? `(${rest.characters} characters, not kept)` : String(rest.content ?? "");
    else if (kind === "result") body = `${rest.subtype || ""} ${rest.result || ""}\n${rest.tokens ?? 0} tokens, $${(rest.cost_usd ?? 0).toFixed(4)}`;
    else body = JSON.stringify(rest, null, 1);
    const cls = kind === "sent" ? "sent" : kind === "result" ? "result" : (rest.is_error || kind === "error") ? "error" : "";
    return el("div", { class: `entry ${cls}` }, el("span", { class: "kind", text: kind === "sent" ? "sent to the agent" : kind }), String(body ?? ""));
  });
}

// ---------- events ----------

async function refreshEvents() {
  if (!selected) return;
  const id = selected;
  const after = eventsNext === null ? "" : `&after=${eventsNext}`;
  const found = await getJSON(`/api/events?id=${encodeURIComponent(id)}${after}`);
  if (id !== selected) return;
  eventsNext = found.next;
  if (!found.events.length) return;
  const list = document.getElementById("events");
  const atEnd = list.scrollTop + list.clientHeight >= list.scrollHeight - 4;
  list.append(...found.events.map((e) => el("li", {}, el("time", { text: clock(e.time) }), e.text.trim())));
  while (list.childElementCount > 1000) list.firstElementChild.remove();
  if (atEnd) list.scrollTop = list.scrollHeight;
}

// ---------- start ----------

window.addEventListener("hashchange", () => { const id = fromHash(); if (id) select(id); });
once("the runs", refreshRuns);
setInterval(() => once("the runs", refreshRuns), 5000);
setInterval(() => once("the run", refreshRun), 2000);
setInterval(() => once("the events", refreshEvents), 1000);
