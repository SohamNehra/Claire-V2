/* Claire's app. One page, no libraries.
 *
 * Everything shown here arrives as the same events the terminal draws from:
 * the page loads a thread's stored events, then follows the live stream, and
 * both go through one function (applyEvent) that folds them into state.
 *
 * Nothing in this file assigns HTML from a string. Text written by a model --
 * which may have just read a web page -- is only ever placed with textContent,
 * so it cannot become markup or script on a page that can approve commands.
 */
"use strict";

const $ = (id) => document.getElementById(id);

function el(tag, props, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child.nodeType ? child : document.createTextNode(String(child)));
  }
  return node;
}

/* ---- state ---------------------------------------------------------------- */

const S = {
  threads: new Map(),       // id -> thread
  projects: new Map(),      // id -> project summary
  projectDetail: new Map(), // id -> {project, loaded, loading, error}
  approvals: new Map(),     // id -> {id, thread, kind, title, flag}
  view: { kind: "dashboard", name: "overview" },
  status: null,
  notes: [],
  note: null,               // {path, text} open in the memory panel
  tools: [],
  reports: [],
  lastSeq: 0,
  connected: false,
  error: "",
};

function thread(id) {
  let t = S.threads.get(id);
  if (!t) {
    t = { id, kind: id === "home" || /^c\d{4}-/.test(id) ? "home" : "task", title: id === "home" ? "Claire" : "New chat", status: "idle",
          workdir: "", trace: [], loaded: false, loading: false, maxSeq: 0,
          live: null, usage: null, speed: 0, thinking: false, busy: false, updated: 0,
          pending: [], project: "" };
    S.threads.set(id, t);
  }
  return t;
}

/* ---- server --------------------------------------------------------------- */

async function api(path, body, method) {
  const res = await fetch(path, {
    method: method || (body === undefined ? "GET" : "POST"),
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
    credentials: "same-origin",
  });
  let data = {};
  try { data = await res.json(); } catch (_) { /* empty body */ }
  if (!res.ok) throw Object.assign(new Error(data.error || `request failed (${res.status})`), { status: res.status });
  return data;
}

async function refreshThreads() {
  const { threads } = await api("/threads?limit=500");
  for (const row of threads) {
    const t = thread(row.id);
    Object.assign(t, { kind: row.kind, title: row.title, status: row.status, parent: row.parent,
                       skills: row.skills || "", workdir: row.workdir, updated: row.updated, summary: row.summary || "",
                       project: row.project || "" });
  }
  draw();
}

async function loadThread(id) {
  const t = thread(id);
  if (t.loaded || t.loading) return;
  t.loading = true;
  t.loadError = "";
  try {
    const [{ events }, info] = await Promise.all([
      api(`/threads/${encodeURIComponent(id)}/events`),
      api(`/threads/${encodeURIComponent(id)}`),
    ]);
    t.trace = [];
    t.thinking = !!info.thread.thinking;
    Object.assign(t, { title: info.thread.title, status: info.thread.status,
                       workdir: info.thread.workdir, kind: info.thread.kind,
                       parent: info.thread.parent, skills: info.thread.skills || "",
                       project: info.thread.project || "" });
    for (const e of events) fold(t, e);
    if(info.thread.usage)t.usage=info.thread.usage;
    // A stored trace has no live tail: whatever was streaming is over.
    t.live = null;
    t.busy = ["running", "queued", "waiting"].includes(t.status);
    t.loaded = true;
    // Whatever was published while the history was in flight.
    for (const e of t.pending.splice(0)) {
      if (!e.seq || e.seq > t.maxSeq) fold(t, e);
    }
  } catch (err) {
    S.error = err.message;
    t.loadError = err.message;
  } finally {
    t.loading = false;
    draw();
  }
}

/* Each part loads on its own: one that fails leaves the rest on screen. A
   part the service does not know means the service is older than this page. */
const STALE_SERVICE = "Claire’s service is running older code than this page. Restart it: claire2 stop, then claire2 app.";
let sideError = "";
function sideProblem(errors) {
  const problem = errors.some((e) => e.status === 404) ? STALE_SERVICE : (errors[0]?.message || "");
  if (problem) S.error = problem;
  else if (S.error && S.error === sideError) S.error = "";
  sideError = problem;
}
/* The service reports a fingerprint of the app's files. A page that loaded
   different ones reloads itself when nothing would be lost, so it never runs
   older code than the service (as after an update, or a restart). */
let uiBuild = "";
function checkBuild(status) {
  if (!status || !status.ui) return;
  if (!uiBuild) { uiBuild = status.ui; return; }
  if (status.ui === uiBuild) return;
  const unsaved = $("input").value.trim() || $("task-dialog").open || $("setup-dialog").open;
  if (!unsaved) { location.reload(); return; }
  S.error = "Claire was updated. Reload this page to use the new version.";
}

async function toggleHumanFree() {
  const on = !(S.status && S.status.human_free);
  if (on && !confirm("Turn on human-free mode?\n\n"
      + "Claire goes ahead without asking: installing packages, commands outside the sandbox, "
      + "browser and connector steps, schedules, and integrations she built.\n\n"
      + "She still asks before anything that deletes, git push or hard resets, stopping processes, "
      + "sending, paying or signing in, closing your tabs, and requests to local addresses. "
      + "Forbidden commands stay refused in every mode.")) return;
  try {
    const { human_free } = await api("/mode", { human_free: on });
    S.status = Object.assign(S.status || {}, { human_free });
    toast(human_free ? "Human-free mode is on. Claire asks only before deleting or anything dangerous."
                     : "Human-free mode is off. Claire asks before risky steps again.");
  } catch (err) { toast(`Could not switch modes: ${err.message}`); }
  draw();
}

async function refreshSide() {
  const [status, vault, tools, projects] = await Promise.allSettled(
    ["/status", "/vault", "/tools", "/projects"].map((path) => api(path)));
  if (status.status === "fulfilled") { S.status = status.value; checkBuild(status.value); }
  if (vault.status === "fulfilled") S.notes = vault.value.notes;
  if (tools.status === "fulfilled") S.tools = tools.value.tools;
  if (projects.status === "fulfilled") S.projects = new Map(projects.value.projects.map((p) => [p.id, p]));
  sideProblem([status, vault, tools, projects].filter((r) => r.status === "rejected").map((r) => r.reason));
  draw();
}

function connect() {
  const source = new EventSource(`/events?after=${S.lastSeq}`);
  source.onopen = () => {
    S.connected = true; S.error = "";
    refreshSide();                       // after a restart: the new model, a newer page
    if (S.view.kind === "panel" && S.view.name === "models") loadModels();
    draw();
  };
  source.onmessage = (msg) => {
    let e;
    try { e = JSON.parse(msg.data); } catch (_) { return; }
    applyEvent(e);
  };
  source.onerror = () => {
    // EventSource retries by itself and resumes from the last event it saw.
    S.connected = false;
    draw();
  };
}

/* ---- folding events into state ------------------------------------------- */

function applyEvent(e) {
  if (e.seq) S.lastSeq = Math.max(S.lastSeq, e.seq);
  if (!e.thread) return;
  const known = S.threads.has(e.thread);
  const t = thread(e.thread);
  if (!known) refreshThreads().catch(() => {});

  if (e.type === "approval_request") {
    S.approvals.set(e.id, { id: e.id, thread: e.thread, kind: e.kind, title: e.title, flag: e.flag });
  } else if (e.type === "approval_resolved") {
    S.approvals.delete(e.id);
  } else if (["schedule_changed", "integrations_changed", "skills_changed"].includes(e.type)) {
    if(e.type === "schedule_changed" && S.view.kind === "dashboard") fetchExtra("scheduler");
    if (S.view.kind === "panel") {
      if (e.type === "skills_changed" && S.view.name === "skills") loadSkills();
      if (e.type === "schedule_changed" && S.view.name === "scheduler") fetchExtra("scheduler");
      if (e.type === "integrations_changed" && S.view.name === "integrations") fetchExtra("integrations");
    }
  } else if (e.type === "tool_fault" || e.type === "turn_end") {
    refreshSide();          // tool health, inbox and task notes may have moved
    if(e.type==='turn_end' && S.view.kind==='panel' && S.view.name==='scheduler') fetchExtra('scheduler');
  }

  if(e.type === "thread_status") refreshSide();
  if (e.type === "mode_changed") S.status = Object.assign(S.status || {}, { human_free: !!e.human_free });
  if (e.type === "projects_changed") {
    refreshProjects();
    refreshThreads().catch(() => {});      // Claire may have moved a chat
    if (S.view.kind === "project") loadProject(S.view.id);
  }
  if (e.type === "turn_end" && S.view.kind === "project" && S.threads.get(e.thread)?.project === S.view.id) loadProject(S.view.id);
  if (e.type === "thread_deleted") {
    // Leave the chat before forgetting it: looking a thread up creates it.
    if (S.view.kind === "thread" && S.view.id === e.thread) show({ kind: "dashboard", name: "overview" });
    S.threads.delete(e.thread);
    inputDrafts.delete(e.thread);
    draw();
    return;
  }

  // A thread that has not been opened keeps only its summary up to date; its
  // trace is read from storage when it is opened.
  if (t.loaded) {
    if (!e.seq || e.seq > t.maxSeq) fold(t, e);
  } else {
    if (t.loading) t.pending.push(e);
    summarise(t, e);
  }
  draw();
}

function summarise(t, e) {
  if (e.type === "thread_status") t.status = e.status;
  else if (e.type === "turn_start") { t.busy = true; if (e.title) t.title = e.title; }
  else if (e.type === "turn_end") { t.busy = false; t.live = null; }
  else if (e.type === "step") t.live = { label: "preparing request", preview: "" };
  else if (e.type === "status") t.live = { label: e.label || "working", preview: e.preview || "" };
  else if (e.type === "tool_call") t.last = `${e.name} ${e.detail || ""}`;
  else if (e.type === "end") t.live = null;
  t.updated = e.ts || t.updated;
}

function fold(t, e) {
  if (e.seq) t.maxSeq = Math.max(t.maxSeq, e.seq);
  summarise(t, e);
  const beforeLength = t.trace.length;
  const trace = t.trace;
  const last = trace[trace.length - 1];
  switch (e.type) {
    case "reset":
      t.trace = [{ t: "note", level: "info", text: "conversation cleared; memory kept" }];
      break;
    case "turn_start": {
      const text = e.text || "";
      if (text.startsWith("[task report]")) trace.push({ t: "report", text, open: false, ts: e.ts });
      else trace.push({ t: "user", text, ts: e.ts });
      break;
    }
    case "thinking":
      if ((e.text || "").trim()) trace.push({ t: "think", text: e.text.trim() });
      break;
    case "tool_call":
      trace.push({ t: "tool", name: e.name, detail: e.detail || "", elapsed: e.elapsed || 0,
                   boosted: !!e.boosted, result: null, failed: false, fault: false });
      break;
    case "tool_result": {
      const call = [...trace].reverse().find((i) => i.t === "tool" && i.result === null);
      if (call) { call.result = e.first || ""; call.failed = !!e.failed; }
      break;
    }
    case "tool_fault": {
      const call = [...trace].reverse().find((i) => i.t === "tool");
      if (call) call.fault = true;
      break;
    }
    case "answer_start":
      if ((e.reasoning || "").trim()) trace.push({ t: "think", text: e.reasoning.trim() });
      trace.push({ t: "answer", text: "", streaming: true, elapsed: 0, ts: e.ts });
      break;
    case "answer_delta":
      if (last && last.t === "answer" && last.streaming) last.text += e.text || "";
      break;
    case "answer_discard":
      if (last && last.t === "answer" && last.streaming) trace.pop();
      break;
    case "answer":
      if (last && last.t === "answer" && last.streaming) {
        Object.assign(last, { text: e.text || last.text, streaming: false, elapsed: e.elapsed || 0 });
      } else {
        trace.push({ t: "answer", text: e.text || "", streaming: false, elapsed: e.elapsed || 0, ts: e.ts });
      }
      break;
    case "notice":
      trace.push({ t: "note", level: e.level || "info", text: e.text || "" });
      break;
    case "compacted":
      trace.push({ t: "note", level: "info",
                   text: `older turns summarised to save context (${fmtTokens(e.before)} → ${fmtTokens(e.after)})` });
      break;
    case "approval_request":
      trace.push({ t: "note", level: "warn", text: `waiting for your approval: ${e.title}` });
      break;
    case "approval_resolved":
      trace.push({ t: "note", level: e.allowed ? "info" : "warn",
                   text: e.allowed ? "approved" : (e.expired ? "approval timed out; treated as no" : "denied") });
      break;
    case "usage":
      t.usage = { used: e.used, limit: e.limit };
      break;
    case "model_call":
      if (["brain", "talk"].includes(e.role) && e.gen_seconds > 0.3 && e.completion_tokens > 5) {
        t.speed = e.completion_tokens / e.gen_seconds;
      }
      break;
    case "end":
      // An answer still marked as streaming when the turn ended was cut off.
      if (last && last.t === "answer" && last.streaming) last.streaming = false;
      if (e.status === "cancelled") trace.push({ t: "note", level: "warn", text: "stopped" });
      break;
  }
  for (const item of t.trace.slice(e.type === "reset" ? 0 : beforeLength)) item.seq = e.seq || t.maxSeq;
}

/* ---- small renderers ------------------------------------------------------ */

function fmtTokens(n) {
  if (n < 1000) return String(n);
  if (n < 100000) return (n / 1000).toFixed(1).replace(".0", "") + "k";
  return Math.floor(n / 1000) + "k";
}

function tilde(path) {
  const home = S.status && S.status.home;
  return home && path && path.startsWith(home) ? "~" + path.slice(home.length) : path;
}

/* Note titles are markdown headings; show them without the backticks. */
const plain = (title) => (title || "").replace(/`/g, "");

function ago(ts) {
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 90) return "just now";
  if (s < 5400) return `${Math.round(s / 60)} min ago`;
  if (s < 129600) return `${Math.round(s / 3600)} h ago`;
  return new Date(ts * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

/* Inline `code` and **bold**, as nodes. */
function inline(text) {
  const out = [];
  const pattern = /(`[^`\n]+`|\*\*[^*\n]+\*\*)/g;
  let at = 0, m;
  while ((m = pattern.exec(text))) {
    if (m.index > at) out.push(text.slice(at, m.index));
    const token = m[0];
    out.push(token[0] === "`" ? el("code", { text: token.slice(1, -1) })
                              : el("strong", { text: token.slice(2, -2) }));
    at = m.index + token.length;
  }
  if (at < text.length) out.push(text.slice(at));
  return out;
}

/* A small, safe subset of markdown: paragraphs, lists, headings, code. */
function prose(text) {
  const root = el("div", { class: "prose" });
  const lines = (text || "").replace(/\r/g, "").split("\n");
  let i = 0;
  const isList = (l) => /^\s*([-*•]|\d+[.)])\s+/.test(l);
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) { i++; continue; }
    if (line.trim().startsWith("```")) {
      const code = [];
      i++;
      while (i < lines.length && !lines[i].trim().startsWith("```")) code.push(lines[i++]);
      i++;
      root.append(el("pre", {}, el("code", { text: code.join("\n") })));
    } else if (/^( {4}|\t)/.test(line)) {
      const code = [];
      while (i < lines.length && (/^( {4}|\t)/.test(lines[i]) || !lines[i].trim())) {
        code.push(lines[i++].replace(/^( {4}|\t)/, ""));
      }
      while (code.length && !code[code.length - 1].trim()) code.pop();
      root.append(el("pre", {}, el("code", { text: code.join("\n") })));
    } else if (/^#{1,6}\s/.test(line)) {
      root.append(el("h3", {}, inline(line.replace(/^#{1,6}\s+/, ""))));
      i++;
    } else if (isList(line)) {
      const ordered = /^\s*\d+[.)]\s/.test(line);
      const list = el(ordered ? "ol" : "ul");
      while (i < lines.length && (isList(lines[i]) || /^\s{2,}\S/.test(lines[i]))) {
        if (isList(lines[i])) {
          list.append(el("li", {}, inline(lines[i].replace(/^\s*([-*•]|\d+[.)])\s+/, ""))));
        } else if (list.lastChild) {
          list.lastChild.append(" ", ...inline(lines[i].trim()));
        }
        i++;
      }
      root.append(list);
    } else {
      const para = [];
      while (i < lines.length && lines[i].trim() && !lines[i].trim().startsWith("```")
             && !isList(lines[i]) && !/^#{1,6}\s/.test(lines[i])) {
        para.push(lines[i++].trim());
      }
      root.append(el("p", {}, inline(para.join(" "))));
    }
  }
  return root;
}

function meter(usage) {
  if (!usage || !usage.limit) return null;
  const pct = Math.min(1, usage.used / usage.limit);
  const fill = el("div", { class: "fill" + (pct >= 0.8 ? " hot" : pct >= 0.5 ? " warm" : "") });
  fill.style.width = `${Math.max(2, pct * 100)}%`;
  return el("div", { class: "meter", title: "Context used by this thread" },
            el("div", { class: "bar" }, fill),
            `${fmtTokens(usage.used)}/${fmtTokens(usage.limit)}`);
}

/* ---- views ---------------------------------------------------------------- */

/* ---- conversations ---------------------------------------------------------
 * A thread's trace becomes a conversation: what you said, what Claire said,
 * and in between a folded record of the work. When Claire starts a task, the
 * main chat shows a card that opens that task's own chat; when the task
 * reports back, its report arrives as a card too. */

const clock = (ts) => ts ? new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "";
const CARD_TOOLS = new Set(["spawn_task", "schedule_task", "message_task"]);

function stepNode(item) {
  if (item.t === "think") return el("div", { class: "think", text: item.text });
  return el("div", { class: "step" + (item.failed ? " failed" : "") },
    el("div", { class: "call" },
       el("span", { class: "tool", text: item.name }),
       el("span", { class: "detail", text: item.detail, title: item.detail }),
       item.boosted && el("span", { class: "boost", title: "thinking was boosted for this step", text: "◆" }),
       item.elapsed >= 1 && el("span", { class: "time", text: `${Math.round(item.elapsed)}s` })),
    item.result !== null && el("div", { class: "result", text: item.result, title: item.result }),
    item.fault && el("div", { class: "fault", text: "⚑ this tool is broken; a repair note is in the inbox" }));
}

/* Consecutive reasoning and tool steps, folded into one line. */
function workGroup(items, t, isLast) {
  const head = items[0];
  const tools = items.filter((i) => i.t === "tool");
  const failed = tools.filter((i) => i.failed).length;
  const running = isLast && t.busy;
  const open = head.open !== undefined ? head.open : (t.kind === "task" || running);
  const names = [...new Set(tools.map((i) => i.name))].slice(0, 4).join(" · ");
  const label = running ? "Working" : tools.length ? `${tools.length} step${tools.length > 1 ? "s" : ""}` : "Thought it through";
  const summary = el("summary", {},
    el("span", { class: "work-mark" + (running ? " live" : ""), "aria-hidden": "true" }),
    el("span", { class: "work-label", text: label }),
    names && el("span", { class: "work-names", text: names }),
    failed > 0 && el("span", { class: "work-failed", text: `${failed} failed` }),
    el("span", { class: "work-chevron", "aria-hidden": "true", text: "›" }));
  const box = el("details", { class: "work", open }, summary,
                 el("div", { class: "work-body" }, items.map(stepNode)));
  // Remember what the reader chose, so a redraw mid-stream does not undo it.
  summary.addEventListener("click", () => { head.open = !box.open; });
  return box;
}

function openChat(id) { show({ kind: "thread", id }); }

/* A fresh conversation with Claire. The others stay where they are. */
async function newChat(project = "") {
  try {
    const { thread: row } = await api("/threads", { kind: "chat", project });
    Object.assign(thread(row.id), { kind: row.kind, title: row.title, status: row.status, updated: row.updated,
                                    workdir: row.workdir, parent: row.parent, project: row.project || "" });
    show({ kind: "thread", id: row.id });
    requestAnimationFrame(() => $("input").focus());
  } catch (err) { toast(`Could not start a new chat: ${err.message}`); }
}

async function clearChat(t) {
  if (!confirm("Clear this chat? Its messages are removed and Claire starts it fresh. Tasks, schedules, approvals and her memory are not touched.")) return;
  try {
    await api(`/threads/${encodeURIComponent(t.id)}/reset`, {});
    if (t.id === "home" && hiddenBefore) { hiddenBefore = 0; savePrefs(); }
  } catch (err) { toast(`Could not clear this chat: ${err.message}`); }
}

async function deleteChat(t) {
  if (!confirm(`Delete the chat “${t.title}”? Its messages are removed. Claire's memory and any tasks it started are kept.`)) return;
  try { await api(`/threads/${encodeURIComponent(t.id)}`, undefined, "DELETE"); }
  catch (err) { toast(`Could not delete this chat: ${err.message}`); }
}

function taskCard(item) {
  const m = /Started task (\S+):/.exec(item.result || "");
  if (item.result !== null && !m) return null;            // it did not start; show the step
  const task = m ? S.threads.get(m[1]) : null;
  return el("button", { class: "chat-card task", type: "button", disabled: !m,
                        onclick: () => m && openChat(m[1]) },
    el("span", { class: "card-kicker", text: "Claire opened a new chat" }),
    el("span", { class: "card-title", text: item.detail || (task && task.title) || "Task" }),
    el("span", { class: "card-foot" },
       task ? el("span", { class: `pill ${task.status}`, text: task.status }) : el("span", { class: "pill", text: "starting" }),
       task && task.live ? el("span", { class: "card-live", text: task.live.label }) : null,
       el("span", { class: "card-open", text: "Open chat →" })));
}

function scheduleCard(item) {
  const first = (item.result || "").split("\n")[0];
  const ok = first.startsWith("Scheduled");
  if (item.result !== null && !ok && !/did not approve/.test(first)) return null;
  return el("div", { class: "chat-card schedule" + (item.result !== null && !ok ? " declined" : "") },
    el("span", { class: "card-kicker", text: item.result === null ? "Waiting for your approval" : ok ? "New habit scheduled" : "Schedule not approved" }),
    el("span", { class: "card-title", text: item.detail || "Recurring job" }),
    ok && el("span", { class: "card-foot" },
       el("span", { class: "card-live", text: (item.result.split("\n")[1] || "").replace(/ Next run.*/, "") }),
       el("button", { class: "card-open", type: "button", text: "Scheduler →",
                      onclick: () => show({ kind: "panel", name: "scheduler" }) })));
}

function reportCard(item) {
  const m = /^\[task report\] Task "(.*)" \((\S+)\) ended with status: (\w+)/.exec(item.text) || [];
  const [, title, id, status] = m;
  const body = item.text.split("\n").slice(1).join("\n").trim();
  const more = el("details", { class: "card-more", open: item.open },
                  el("summary", { text: "Read the report" }), prose(body));
  more.querySelector("summary").addEventListener("click", () => { item.open = !more.open; });
  return el("div", { class: `chat-card report ${status || ""}` },
    el("span", { class: "card-kicker", text: "A task reported back" }),
    el("span", { class: "card-title", text: title || "Task report" }),
    el("span", { class: "card-foot" },
       status && el("span", { class: `pill ${status}`, text: status }),
       id && el("button", { class: "card-open", type: "button", text: "Open chat →", onclick: () => openChat(id) })),
    more);
}

function traceNodes(t) {
  const out = [];
  const shown = visibleTrace(t);
  const items = shown.length > 500 ? shown.slice(-500) : shown;
  if (items.length < shown.length) out.push(el("div", { class: "note", text: `${shown.length - items.length} earlier steps not shown` }));
  let firstUser = true;
  let group = [];
  const flush = (isLast) => { if (group.length) { out.push(workGroup(group, t, isLast)); group = []; } };

  items.forEach((item, index) => {
    const card = item.t === "tool" && CARD_TOOLS.has(item.name)
      ? (item.name === "spawn_task" ? taskCard(item) : item.name === "schedule_task" ? scheduleCard(item) : null)
      : null;
    if ((item.t === "think" || item.t === "tool") && !card) { group.push(item); return; }
    // A line of reasoning just before a card belongs with it, not on its own.
    if (card && group.length && group.every((g) => g.t === "think")) group = [];
    flush(false);
    if (card) {
      out.push(card);
    } else if (item.t === "user") {
      if (t.kind === "home") {
        out.push(el("div", { class: "msg you" },
          el("div", { class: "chat-meta" }, el("span", { text: "You" }), el("span", { class: "chat-time", text: clock(item.ts) })),
          el("div", { class: "bubble", text: item.text })));
      } else {
        const fromClaire = !!t.parent;
        out.push(el("div", { class: "brief" },
          el("div", { class: "who", text: firstUser ? (fromClaire ? "Brief from Claire" : "Your brief") : "Follow-up" }),
          el("div", { class: "brief-text", text: item.text })));
      }
      firstUser = false;
    } else if (item.t === "report") {
      out.push(reportCard(item));
    } else if (item.t === "answer") {
      const body = item.streaming
        ? el("div", { class: "bubble body prose caret", text: item.text })
        : el("div", { class: "bubble body" }, prose(item.text));
      out.push(el("div", { class: "msg claire" },
        el("div", { class: "chat-avatar", "aria-hidden": "true", text: "C" }),
        el("div", { class: "chat-main" },
          el("div", { class: "chat-meta who" }, el("span", { text: "Claire" }),
             el("span", { class: "chat-time", text: clock(item.ts) }),
             !item.streaming && item.elapsed >= 1 && el("span", { class: "chat-time", text: `${Math.round(item.elapsed)}s` })),
          body)));
    } else if (item.t === "note") {
      out.push(el("div", { class: `note ${item.level}`, text: item.text.split("\n")[0] }));
    }
    if (index === items.length - 1) return;
  });
  flush(true);
  return out;
}

/* ---- the sidebar ------------------------------------------------------------
 * One list, as in a code workspace: projects as folders holding their chats
 * and tasks, then chats that belong to no project, with the main chat first. */

function icon(name, cls = "svg-icon") {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", cls);
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", "#" + name);
  svg.append(use);
  return svg;
}

function shortAgo(ts) {
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return "now";
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h`;
  if (s < 604800) return `${Math.floor(s / 86400)}d`;
  return new Date(ts * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

const isActive = (t) => t.kind === "task" && (t.busy || ["running", "queued", "waiting"].includes(t.status));
const projectOf = (t) => (t && t.project ? S.projects.get(t.project) || null : null);
const projectThreads = (pid) => [...S.threads.values()].filter((t) => t.project === pid && t.id !== "home");

function threadRow(t, current) {
  const main = t.id === "home";
  const chat = t.kind === "home";
  const busy = t.busy || isActive(t);
  return el("button", { class: "thread-row" + (current === t ? " active" : "") + (main ? " main" : "") + (chat ? " chat" : " task"),
                        type: "button", title: main ? "Claire · main chat" : t.title, "data-thread": t.id,
                        "data-view": main ? "thread:home" : null,
                        "aria-current": current === t ? "true" : null, onclick: () => openChat(t.id) },
    chat ? el("span", { class: "thread-icon" }, icon("i-chat")) : el("span", { class: `dot ${t.status}` }),
    el("span", { class: "thread-title", text: main ? "Claire" : t.title }),
    el("span", { class: "thread-side" },
      busy ? el("span", { class: "work-mark live", title: t.live ? t.live.label : t.status })
           : main ? el("span", { class: "thread-tag", text: "Main" })
           : el("span", { class: "thread-time", text: t.updated ? shortAgo(t.updated) : "" })));
}

function sideHead(label, ...actions) {
  return el("div", { class: "side-head" }, el("span", { class: "side-label", text: label }),
            el("div", { class: "history-actions" }, actions));
}

function projectActivity(p) {
  return Math.max(p.last_activity || 0, ...projectThreads(p.id).map((t) => t.updated || 0));
}

function sortedProjects() {
  return [...S.projects.values()].sort((a, b) =>
    (projectThreads(b.id).some(isActive) - projectThreads(a.id).some(isActive)) || projectActivity(b) - projectActivity(a));
}

function projectFolder(p, current) {
  const threads = projectThreads(p.id).sort((a, b) => b.updated - a.updated);
  const visible = threads.filter((t) => historyVisible(t, current));
  const active = threads.filter(isActive).length;
  const open = !collapsedProjects[p.id];
  const here = S.view.kind === "project" && S.view.id === p.id;
  const shown = visible.slice(0, 6);
  return el("div", { class: "project" + (open ? " open" : "") + (here ? " here" : ""), "data-project": p.id },
    el("div", { class: "project-row" },
      el("button", { class: "project-toggle", type: "button", "aria-expanded": String(open),
                     "aria-label": `${open ? "Collapse" : "Expand"} ${p.name}`, onclick: () => toggleProject(p.id) },
         icon("i-chevron", "svg-icon chevron")),
      el("button", { class: "project-name", type: "button", title: p.path ? tilde(p.path) : p.name,
                     onclick: () => show({ kind: "project", id: p.id }) },
         icon("i-folder"), el("span", { class: "label", text: p.name }),
         active ? el("span", { class: "project-active", text: String(active), title: `${active} active` }) : null),
      el("button", { class: "project-add", type: "button", title: `New chat in ${p.name}`, "aria-label": `New chat in ${p.name}`,
                     onclick: () => newChat(p.id) }, icon("i-plus"))),
    open && el("div", { class: "project-threads" },
      shown.map((t) => threadRow(t, current)),
      !threads.length ? el("div", { class: "side-empty", text: "No chats yet" }) : null,
      threads.length && !visible.length ? el("div", { class: "side-empty", text: "Previous items hidden" }) : null,
      visible.length > shown.length ? el("button", { class: "side-more", type: "button", text: `Show all ${visible.length}`,
                                                     onclick: () => show({ kind: "project", id: p.id }) }) : null));
}

function sideThreads(current) {
  const all = [...S.threads.values()].filter((t) => t.id !== "home");
  const filed = (t) => t.project && S.projects.has(t.project);
  const chats = all.filter((t) => t.kind === "home" && !filed(t)).sort((a, b) => b.updated - a.updated);
  const tasks = all.filter((t) => t.kind === "task").sort((a, b) => b.updated - a.updated);
  const looseTasks = tasks.filter((t) => !filed(t));
  const projects = sortedProjects();
  const visibleTasks = looseTasks.filter((t) => historyVisible(t, current));
  return [
    sideHead("Projects", historyToggle("tasks", tasks, current),
      el("button", { class: "side-icon", type: "button", title: "New project", "aria-label": "New project",
                     onclick: () => projectDialog() }, icon("i-plus"))),
    projects.length ? el("div", { class: "project-list" }, projects.map((p) => projectFolder(p, current)))
                    : el("div", { class: "side-empty", text: "A project appears when Claire works in a folder, or add one with +." }),
    sideHead("Chats", historyToggle("chats", chats, current)),
    el("div", { class: "thread-list" },
       chats.filter((t) => historyVisible(t, current)).slice(0, 60).map((t) => threadRow(t, current)),
       !chats.length ? el("div", { class: "side-empty", text: "No chats yet. Start one with New chat." }) : null,
       chats.length && !chats.some((t) => historyVisible(t, current)) ? el("div", { class: "side-empty", text: "Previous chats are hidden." }) : null),
    looseTasks.length ? sideHead("Other tasks") : null,
    looseTasks.length ? el("div", { class: "thread-list" },
      visibleTasks.length ? visibleTasks.slice(0, 40).map((t) => threadRow(t, current))
                          : el("div", { class: "side-empty", text: "Previous tasks are hidden." })) : null,
  ];
}

let sideSignature = "";
function drawSide(current) {
  const key = JSON.stringify([S.view, current && current.id, S.connected, hiddenThreadHistory, collapsedProjects,
    [...S.projects.values()].map((p) => [p.id, p.name, p.path, p.last_activity]),
    [...S.threads.values()].map((t) => [t.id, t.title, t.status, t.busy, t.project, t.updated && shortAgo(t.updated)]),
    [...S.approvals.keys()]]);
  if (key === sideSignature) return;
  sideSignature = key;
  $("side-threads").replaceChildren(...sideThreads(current).filter(Boolean));
}

function toggleProject(pid) {
  if (collapsedProjects[pid]) delete collapsedProjects[pid]; else collapsedProjects[pid] = true;
  savePrefs(); draw();
}

/* ---- a conversation ------------------------------------------------------ */

function chatLayout(current) {
  const skills = (current.skills || "").split(",").filter(Boolean);
  return el("section", { class: "chat-pane" },
    current.kind === "task" && el("div", { class: "chat-pane-head" },
       el("span", { class: `pill ${current.status}`, text: current.status }),
       el("span", { class: "chat-pane-where", text: tilde(current.workdir), title: current.workdir }),
       skills.map((s) => el("span", { class: "chat-skill", text: s }))),
    el("div", { class: "chat-scroll", id: "chat-scroll" }, viewThread(current)));
}

/* ---- projects ------------------------------------------------------------- */

async function refreshProjects() {
  try {
    const { projects } = await api("/projects");
    S.projects = new Map(projects.map((p) => [p.id, p]));
  } catch (err) { sideProblem([err]); }
  draw();
}

async function loadProject(pid) {
  const state = S.projectDetail.get(pid) || {};
  if (state.loading) return;
  S.projectDetail.set(pid, Object.assign(state, { loading: true }));
  try {
    const data = await api(`/projects/${encodeURIComponent(pid)}`);
    S.projects.set(pid, Object.assign(S.projects.get(pid) || {}, data.project));
    for (const row of data.threads) {
      const t = thread(row.id);
      Object.assign(t, { kind: row.kind, title: row.id === "home" ? "Claire" : row.title, status: row.status, parent: row.parent,
                         skills: row.skills || "", workdir: row.workdir, updated: row.updated,
                         summary: row.summary || "", project: row.project || "" });
    }
    S.projectDetail.set(pid, { project: data.project, loaded: true, error: "" });
  } catch (err) {
    S.projectDetail.set(pid, { error: err.status === 404 ? "This project no longer exists." : err.message, loaded: true });
  }
  draw();
}

function projectDialog(existing = null) {
  const root = showSetup(existing ? `Rename “${existing.name}”` : "New project",
    existing ? "Only the name changes. Its chats, tasks and memory stay where they are."
             : "A project groups chats and tasks, and keeps its own memory: what it is, and what was last done in it.");
  const name = el("input", { maxlength: 80, placeholder: existing ? "" : "Shop redesign", value: existing ? existing.name : null });
  const folder = el("input", { placeholder: "~/code/shop", spellcheck: "false", autocomplete: "off" });
  const browse = el("button", { type: "button", class: "ghost", text: S.status?.platform === "win32" ? "Type folder path" : "Browse…", disabled: S.status?.platform === "win32", title: S.status?.platform === "win32" ? "Paste the Windows folder path into the field" : "Choose the folder in the Finder" });
  browse.addEventListener("click", async () => {
    browse.disabled = true; browse.textContent = "Choose in the Finder…"; error.hidden = true;
    try {
      const { path } = await api("/folders/pick", {});
      if (path) {
        folder.value = tilde(path);
        if (!name.value.trim()) name.value = path.split("/").filter(Boolean).pop() || "";
      }
    } catch (err) { error.textContent = err.message; error.hidden = false; }
    finally { browse.disabled = false; browse.textContent = "Browse…"; }
  });
  const error = el("p", { class: "error", role: "alert", hidden: true });
  const submit = el("button", { type: "submit", class: "primary", text: existing ? "Rename" : "Create project" });
  const form = el("form", { class: "setup-form" }, field("Name", name),
    existing ? null : el("label", {}, "Folder (optional)",
      el("div", { class: "folder-row" }, folder, browse)),
    existing ? null : el("p", { class: "field-help", text: "A folder in your home folder, usually a repository. Tasks in this project work there. Leave it empty for a project that only groups chats; the name is then required." }),
    error, el("div", { class: "row end" }, actionButton("Cancel", closeSetup), submit));
  form.addEventListener("submit", async (event) => {
    event.preventDefault(); error.hidden = true; submit.disabled = true;
    try {
      const { project } = existing
        ? await api(`/projects/${encodeURIComponent(existing.id)}/rename`, { name: name.value.trim() })
        : await api("/projects", { name: name.value.trim(), path: folder.value.trim() });
      S.projects.set(project.id, project);
      closeSetup();
      await refreshProjects();
      show({ kind: "project", id: project.id });
    } catch (err) { error.textContent = err.message; error.hidden = false; }
    finally { submit.disabled = false; }
  });
  root.append(form);
  name.focus();
}

function briefDialog(p) {
  const detail = S.projectDetail.get(p.id)?.project || p;
  const root = showSetup("PROJECT.md", `Claire reads this at the start of every chat and task in ${p.name}. Say what the project is, how work is done there, and anything she must not touch.`);
  const text = el("textarea", { rows: 16, maxlength: 40000, spellcheck: "true" });
  text.value = detail.brief || `# ${p.name}\n\n`;
  const error = el("p", { class: "error", role: "alert", hidden: true });
  const submit = el("button", { type: "submit", class: "primary", text: "Save" });
  const form = el("form", { class: "setup-form" }, field("Instructions and context", text),
    el("p", { class: "field-help", text: `Saved as projects/${p.slug}/PROJECT.md in Claire’s vault. You can also edit it there by hand.` }),
    error, el("div", { class: "row end" }, actionButton("Cancel", closeSetup), submit));
  form.addEventListener("submit", async (event) => {
    event.preventDefault(); error.hidden = true; submit.disabled = true;
    try {
      await api(`/projects/${encodeURIComponent(p.id)}/brief`, { text: text.value });
      closeSetup(); toast("PROJECT.md saved. Claire uses it from her next reply.");
      await loadProject(p.id);
    } catch (err) { error.textContent = err.message; error.hidden = false; }
    finally { submit.disabled = false; }
  });
  root.append(form);
  text.focus();
}

async function removeProject(p) {
  if (!confirm(`Remove “${p.name}” from your projects? Its chats and tasks are kept and move to the general list. Its memory stays in the vault at projects/${p.slug}.`)) return;
  // Leave its page first, so nothing asks for it once it is gone.
  if (S.view.kind === "project" && S.view.id === p.id) show({ kind: "dashboard", name: "overview" });
  try {
    await api(`/projects/${encodeURIComponent(p.id)}`, undefined, "DELETE");
    S.projects.delete(p.id);
    S.projectDetail.delete(p.id);
    for (const t of S.threads.values()) if (t.project === p.id) t.project = "";
    await refreshProjects();
  } catch (err) { toast(`Could not remove the project: ${err.message}`); }
}

function moveDialog(t) {
  const root = showSetup("Move chat", "The chat keeps its messages. From its next reply Claire uses the memory of the project you choose.");
  const choice = (pid, label, sub) => el("button", { type: "button", class: "move-option" + ((t.project || "") === pid ? " selected" : ""),
    onclick: async () => {
      try {
        const { thread: row } = await api(`/threads/${encodeURIComponent(t.id)}/project`, { project: pid });
        t.project = row.project || "";
        closeSetup(); toast(pid ? `Moved to ${label}.` : "Moved out of its project.");
        refreshProjects();
      } catch (err) { toast(`Could not move this chat: ${err.message}`); }
    } }, icon(pid ? "i-folder" : "i-chat"), el("span", {}, el("strong", { text: label }), sub ? el("small", { text: sub }) : null));
  root.append(el("div", { class: "move-list" }, choice("", "No project", "A general chat"),
    sortedProjects().map((p) => choice(p.id, p.name, p.path ? tilde(p.path) : "Group"))),
    el("div", { class: "row end" }, actionButton("Cancel", closeSetup)));
}

function threadLine(t) {
  const lastAnswer = [...(t.trace || [])].reverse().find((i) => i.t === "answer");
  const preview = t.busy && t.live ? t.live.label
    : t.kind === "home" ? (lastAnswer ? lastAnswer.text.replace(/\s+/g, " ").slice(0, 110) : "Chat")
    : (t.summary || t.last || "").replace(/\s+/g, " ").slice(0, 110) || tilde(t.workdir);
  return el("button", { class: "list-row", type: "button", onclick: () => openChat(t.id) },
    t.kind === "home" ? el("span", { class: "thread-icon" }, icon("i-chat")) : el("span", { class: `dot ${t.status}` }),
    el("span", { class: "list-text" }, el("span", { class: "list-title", text: t.title }),
       el("span", { class: "list-sub", text: preview })),
    t.kind === "task" ? statusPill(t.status) : null,
    el("span", { class: "thread-time", text: t.updated ? ago(t.updated) : "" }));
}

function projectView(p) {
  const detail = S.projectDetail.get(p.id) || {};
  if (!detail.loaded && !detail.loading) loadProject(p.id);
  if (detail.error) return el("div", { class: "empty-state" }, el("div", { class: "big", text: "Project unavailable" }), el("p", { text: detail.error }));
  const threads = projectThreads(p.id).sort((a, b) => b.updated - a.updated);
  const active = threads.filter(isActive);
  const info = detail.project || p;
  const brief = (info.brief || "").replace(/^---\n[\s\S]*?\n---\n/, "").trim();
  const work = info.work || [];
  return el("div", { class: "project-page" },
    el("section", { class: "page-section" },
      el("div", { class: "section-heading" }, el("h2", { text: "Active work" }), el("span", { class: "section-note", text: active.length ? `${active.length} running` : "" })),
      active.length ? el("div", { class: "row-list" }, active.map(threadLine))
                    : el("div", { class: "quiet-empty", text: "Nothing is running in this project." })),
    el("div", { class: "project-grid" },
      el("section", { class: "page-section" },
        el("div", { class: "section-heading" }, el("h2", { text: "Chats and tasks" }),
           el("button", { class: "link", type: "button", text: "New chat", onclick: () => newChat(p.id) })),
        threads.length ? el("div", { class: "row-list" }, threads.map(threadLine))
                       : el("div", { class: "quiet-empty", text: "No chats or tasks yet. A chat started here uses this project’s memory, and its tasks work in the project’s folder." })),
      el("section", { class: "page-section project-memory" },
        el("div", { class: "section-heading" }, el("h2", { text: "Project memory" }),
           el("button", { class: "link", type: "button", text: "Edit PROJECT.md", onclick: () => briefDialog(p) })),
        el("div", { class: "memory-card" },
          el("div", { class: "memory-path", text: `projects/${info.slug || p.slug}/PROJECT.md` }),
          brief ? prose(brief) : el("p", { class: "dim", text: detail.loaded ? "Nothing written yet. Describe the project so every chat and task starts out knowing it." : "Loading…" })),
        el("h3", { class: "memory-subhead", text: "What was last done" }),
        work.length ? el("div", { class: "work-log" }, work.slice(0, 8).map((entry, i) => {
          const body = entry.text.replace(/^## .*\n+/, "").replace(/^Updated: .*\n(Project folder: .*\n)?/, "").trim();
          const first = body.split("\n").find((line) => line.trim()) || "Update";
          return el("details", { class: "log-entry", open: i === 0 },
            el("summary", {}, el("span", { class: "log-title", text: plain(first.replace(/^Task:\s*/, "")) }),
               el("span", { class: "thread-time", text: ago(entry.updated) })),
            prose(body.split("\n").slice(1).join("\n").trim() || body));
        })) : el("div", { class: "quiet-empty", text: detail.loaded ? "No work recorded yet. Each task in this project adds an entry when it starts and when it ends." : "Loading…" }))));
}

function viewThread(t) {
  if (!t.loaded) {
    if (t.loadError) return el("div", { class: "empty-state" },
      el("div", { class: "big", text: "Couldn’t load this channel." }),
      el("p", { text: t.loadError }),
      el("button", { class: "ghost small", text: "Retry", onclick: () => loadThread(t.id) }));
    loadThread(t.id);
    return el("div", { class: "empty-state", text: "Loading…" });
  }
  if (t.id === "home" && hiddenBefore && !visibleTrace(t).length) return el("div", {class:"empty-state"},
    el("div", {class:"big",text:"A fresh view. The same Claire."}), el("p", {text:"Earlier messages are hidden in this browser. Claire still has the conversation."}),
    actionButton("Show full history", hideHistory));
  if (!t.trace.length) {
    return t.kind === "home"
      ? el("div", { class: "empty-state" },
           el("div", { class: "big", text: projectOf(t) ? `New chat in ${projectOf(t).name}` : "What are we working on?" }),
           projectOf(t) ? "Claire starts this chat knowing the project’s memory, and its tasks work in the project’s folder."
                        : "Ask anything, or give Claire real work: she opens a task for it and reports back here.")
      : el("div", { class: "empty-state", text: "Nothing has happened in this task yet." });
  }
  return el("div", { class: "column" }, traceNodes(t));
}

function panelInbox() {
  const open = S.notes.filter((n) => n.folder === "inbox" && !["done", "closed"].includes(n.status));
  const root = el("div", { class: "panel" });
  if (!open.length) {
    root.append(el("div", { class: "empty-state" }, el("div", { class: "big", text: "Nothing waiting." }),
                   "When a tool breaks or something needs your hands, the note lands here."));
    return root;
  }
  root.append(el("p", { class: "dim", text:
    "Notes Claire has left for you. Mark one done by setting status: done at the top of the file, or delete it." }));
  const rows = open.map((n) => el("tr", { class: "click", onclick: () => openNote(n.path) },
    el("td", {}, el("span", { class: "warn", text: "⚑ " }), plain(n.title)),
    el("td", { class: "dim", text: ago(n.modified) })));
  root.append(el("table", { class: "table" }, el("tbody", {}, rows)));
  return root;
}

function panelMemory() {
  const projectFolders = [...new Set(S.notes.map((n) => n.folder).filter((f) => f === "projects" || f.startsWith("projects/")))].sort();
  const folders = [".", "identity", "work", ...projectFolders, "learned", "tasks", "sessions", "inbox"];
  const tree = el("div", { class: "tree" });
  for (const folder of folders) {
    const dated = /(^|\/)(tasks|sessions|work)$/.test(folder);
    const notes = S.notes.filter((n) => n.folder === folder)
      .sort((a, b) => dated ? b.modified - a.modified : a.title.localeCompare(b.title));
    if (!notes.length) continue;
    const label = folder === "." ? "shared index" : folder.startsWith("projects/") ? folder.slice(9).replace("/", " / ") : folder;
    tree.append(el("div", { class: "folder" + (folder.startsWith("projects/") ? " project-folder" : ""), text: label }));
    for (const n of notes) {
      tree.append(el("button", { class: S.note && S.note.path === n.path ? "active" : "",
                                 title: n.path, text: plain(n.title), onclick: () => openNote(n.path) }));
    }
  }
  let box;
  if (S.note) {
    const body = S.note.text.replace(/^---\n[\s\S]*?\n---\n/, "").replace(/^<!-- claire-work \{[^\n]*\} -->\n/gm, "");
    box = el("div", { class: "notebox" }, el("div", { class: "path", text: S.note.path }), prose(body));
  } else {
    box = el("div", { class: "notebox dim", text:
      "Everything Claire remembers is a markdown file in her vault. Pick a note to read it. "
      + "Edit the files in Obsidian or any editor; she picks changes up on her next turn." });
  }
  return el("div", { class: "panel" }, el("div", { class: "split" }, tree, box));
}

function panelTools() {
  const rows = S.tools.map((t) => {
    const h = t.health || {};
    let state = el("span", { class: "ok", text: "ok" });
    if (h.substitute) state = el("span", { class: "warn", text: `broken → using ${h.substitute}` });
    else if (h.consecutive > 0) state = el("span", { class: "bad", text: `failing (${h.consecutive} in a row)` });
    else if (h.faults > 0) state = el("span", { class: "dim", text: `ok (${h.faults} past fault${h.faults > 1 ? "s" : ""})` });
    const acts = [];
    if (h.faults > 0 || h.substitute) {
      acts.push(el("button", { class: "ghost small", text: "Mark repaired", onclick: () => toolAction(t.name, "reset") }));
    }
    if (t.source === "workshop") {
      acts.push(el("button", { class: "ghost small", text: "Remove", onclick: () => {
        if (confirm(`Remove the workshop tool "${t.name}"? Its code stays in the workshop's git history.`)) toolAction(t.name, "remove");
      } }));
    }
    return el("tr", {},
      el("td", { class: "mono", text: t.name }),
      el("td", {}, t.source === "workshop" ? el("span", { class: "tag", text: "workshop" }) : el("span", { class: "dim", text: "built-in" })),
      el("td", {}, state, h.last_error && (h.consecutive > 0 || h.substitute)
         ? el("div", { class: "dim mono", text: h.last_error.split("\n")[0].slice(0, 140) }) : null),
      el("td", {}, el("div", { class: "row" }, acts)));
  });
  return el("div", { class: "panel" },
    el("p", { class: "dim", text:
      "When a tool breaks, Claire works around it in her sandboxed workshop and leaves you a repair note. "
      + "Built-in tools can only be fixed by you." }),
    el("div", { class: "scroll-x" }, el("table", { class: "table" },
      el("thead", {}, el("tr", {}, ["Tool", "Kind", "State", ""].map((h) => el("th", { text: h })))),
      el("tbody", {}, rows))));
}

function panelBench() {
  const s = S.status;
  const root = el("div", { class: "panel" });
  if (s) {
    const m = s.model;
    root.append(el("div", { class: "statgrid" },
      stat("Brain", m.model + (m.live ? "" : "  (not loaded)")),
      stat("Context per thread", `${m.ctx.toLocaleString()} tokens`),
      stat("Sandbox", s.sandbox ? "on" : "off — pattern guards only", s.sandbox ? "ok" : "warn"),
      stat("Service", s.version)));
  }
  root.append(el("h3", { text: "Report cards" }));
  if (!S.reports.length) {
    root.append(el("p", { class: "dim", text: "No results yet. Run `claire2 bench` to measure a model on Claire's own tasks." }));
    return root;
  }
  const rows = [...S.reports].reverse().map((r) => el("tr", {},
    el("td", { class: "dim", text: r.when }),
    el("td", { text: r.suite }),
    el("td", { class: "mono", text: r.model }),
    el("td", { class: "num " + (r.passed === r.total ? "ok" : r.passed ? "warn" : "bad"),
               text: `${r.passed}/${r.total}` + (r.complete === false ? " *" : "") }),
    el("td", { class: "num", text: r.tok_s.toFixed(1) }),
    el("td", { class: "num", text: `${Math.round(r.seconds)}s` }),
    el("td", { class: "num", text: String(r.retries) })));
  root.append(el("div", { class: "scroll-x" }, el("table", { class: "table" },
    el("thead", {}, el("tr", {},
      el("th", { text: "When" }), el("th", { text: "Suite" }), el("th", { text: "Model" }),
      el("th", { class: "num", text: "Passed" }), el("th", { class: "num", text: "Tokens/s" }),
      el("th", { class: "num", text: "Time" }), el("th", { class: "num", text: "Bad replies" }))),
    el("tbody", {}, rows))));
  return root;
}

function stat(k, v, tone) {
  return el("div", { class: "stat" }, el("div", { class: "k", text: k }),
            el("div", { class: "v " + (tone || ""), text: v }));
}

const PANELS = {
  inbox: ["Repair notes", panelInbox], memory: ["Memory", panelMemory],
  tools: ["Capabilities", panelTools], bench: ["Benchmark reports", panelBench],
  models: ["Models", panelModelSetup],
  tasks: ["All tasks", panelTasks], integrations: ["Integrations", panelIntegrations],
  scheduler: ["Scheduler", panelScheduler], controller: ["Controller settings", panelController],
  skills: ["Skills", panelSkills],
};

/* ---- command center: presentation only, using existing state -------------- */

const activeTasks = () => [...S.threads.values()].filter((t) => t.kind === "task"
  && (t.busy || ["running", "queued", "waiting"].includes(t.status)));
const inputThread = () => thread(S.view.kind === "thread" ? S.view.id : "home");

function openTaskDialog(project = "") {
  $("task-error").hidden = true;
  const select = $("task-project");
  select.replaceChildren(el("option", { value: "", text: "No project" }),
    ...sortedProjects().map((p) => el("option", { value: p.id, text: p.path ? `${p.name} — ${tilde(p.path)}` : p.name })));
  select.value = typeof project === "string" ? project : "";
  $("task-dialog").showModal();
}

function syncDrawers() {
  $("side").inert = matchMedia("(max-width: 760px)").matches && !$("side").classList.contains("open");
  $("rail").inert = matchMedia("(max-width: 1180px)").matches && !$("rail").classList.contains("open");
  $("drawer-backdrop").hidden = !(matchMedia("(max-width: 760px)").matches && $("side").classList.contains("open"))
    && !(matchMedia("(max-width: 1180px)").matches && $("rail").classList.contains("open"));
}

function openActivity() {
  $("side").classList.remove("open");
  $("rail").classList.add("open");
  syncDrawers();
  $("approvals").scrollIntoView({ block: "nearest" });
  const target = $("approvals").querySelector("button") || $("rail-close");
  if (target && target.getClientRects().length) target.focus();
}

function taskBucket(status){
  if(['idle','queued','running','waiting'].includes(status))return 'active';
  if(status==='done')return 'completed';
  if(['error','stopped','interrupted'].includes(status))return 'failed';
  return 'other';
}
function dashboard() {
  const view=$('view');
  if(!$('task-board')){
    view.replaceChildren($('overview-template').content.cloneNode(true));
    $('dashboard-new-task').addEventListener('click',()=>openTaskDialog());
    $('dashboard-new-project').addEventListener('click',()=>projectDialog());
    $('dashboard-all-tasks').addEventListener('click',()=>{taskFilter='all';show({kind:'panel',name:'tasks'});});
    for(const filter of ['total','active','completed','failed'])$('stat-'+filter).addEventListener('click',()=>{
      taskFilter=filter==='total'?'all':filter;show({kind:'panel',name:'tasks'});
    });
    for(const item of view.querySelectorAll('[data-open-panel]'))item.addEventListener('click',()=>show({kind:'panel',name:item.dataset.openPanel}));
  }
  const active=activeTasks().sort((a,b)=>b.updated-a.updated);
  const busy=active.length>0||thread('home').busy;
  document.querySelector('.dashboard').dataset.state=!S.connected?'offline':S.approvals.size?'waiting':busy?'working':'ready';
  $('core-state-label').textContent=!S.connected?'Reconnecting':S.approvals.size?'Needs your approval':busy?'Working':'Ready';
  const counts=S.status?.task_counts;
  for(const key of ['total','active','completed','failed'])$('metric-'+key).textContent=counts?String(counts[key]):'—';
  // Active work across every project: the place to follow several at once.
  const board=$('task-board');
  const signature=JSON.stringify(active.map(t=>[t.id,t.title,t.status,t.live?.label,projectOf(t)?.name]));
  if(board.dataset.signature!==signature){
    board.dataset.signature=signature;
    board.replaceChildren(...(active.length?active.slice(0,20).map(t=>{
      const project=projectOf(t);
      const detail=t.live?.label && t.live.label!==t.status ? t.live.label : (t.last || tilde(t.workdir));
      return el('button',{class:'task-row',onclick:()=>show({kind:'thread',id:t.id})},
        el('span',{class:`dot ${t.status}`}),
        el('span',{class:'task-text'},el('span',{class:'task-title',text:t.title}),detail?el('span',{class:'task-description',text:detail}):null),
        el('span',{class:'task-project'+(project?'':' none'),text:project?project.name:'No project'}),
        statusPill(t.status));})
      :[el('div',{class:'quiet-empty',text:'Nothing is running. Work started in any project shows up here while it runs.'})]));
  }
  const projects=sortedProjects().slice(0,8);
  const pboard=$('project-board');
  const pkey=JSON.stringify(projects.map(p=>[p.id,p.name,p.path,projectThreads(p.id).filter(isActive).length,p.last_title,p.last_status,shortAgo(projectActivity(p))]));
  if(pboard.dataset.signature!==pkey){
    pboard.dataset.signature=pkey;
    pboard.replaceChildren(...(projects.length?projects.map(p=>{
      const running=projectThreads(p.id).filter(isActive).length;
      return el('button',{class:'project-card',onclick:()=>show({kind:'project',id:p.id})},
        el('span',{class:'project-card-top'},icon('i-folder'),el('span',{class:'task-title',text:p.name}),
           running?el('span',{class:'project-active',text:`${running} active`}):el('span',{class:'thread-time',text:shortAgo(projectActivity(p))})),
        el('span',{class:'task-description',text:p.last_title?`Last: ${p.last_title}`:(p.path?tilde(p.path):'Group')}));})
      :[el('div',{class:'quiet-empty',text:'No projects yet. A project appears when Claire works in a folder, or create one.'})]));
  }
  const state=extras.scheduler;
  const upcoming=state.items.filter(s=>s.status==='active' && s.next_run_at).sort((a,b)=>new Date(a.next_run_at)-new Date(b.next_run_at)).slice(0,3);
  const next=$('upcoming-board');
  const key=JSON.stringify([state.state,upcoming]);
  if(next.dataset.signature!==key){next.dataset.signature=key;next.replaceChildren(...(upcoming.length?upcoming.map(s=>el('button',{class:'task-row upcoming-row',onclick:()=>{show({kind:'panel',name:'scheduler'});scheduleDetails(s);}},
    icon('i-clock'),el('span',{class:'task-text'},el('span',{class:'task-title',text:s.title}),el('span',{class:'task-description',text:formatDate(s.next_run_at,s.timezone)+' · '+s.timezone}))))
    :[el('div',{class:'quiet-empty',text:state.state==='ready'?'Nothing scheduled.':['idle','loading'].includes(state.state)?'Checking schedules…':'Schedules unavailable.'})]));}
}

function drawSystem() {
  const s = S.status;
  const selected = inputThread();
  const model = (selected.kind === "home" && !selected.thinking && s?.controller) || s?.model;
  const busy = [...S.threads.values()].some((t) => t.busy || ["running", "queued"].includes(t.status));
  document.body.dataset.connected = String(S.connected);
  $("connection-chip").classList.toggle("online", S.connected);
  $("connection-label").textContent = S.connected ? "Service connected" : "Reconnecting";
  $("engine-name").textContent = model?.model || "Awaiting local service";
  $("engine-state").textContent = !S.connected ? "Link offline" : busy ? "Processing" : s?.power?.state === "asleep" ? "Asleep · wakes on request" : s?.power?.state === "sleeping" ? "Going to sleep" : model?.live ? "Model ready" : "On standby";
  const current=inputThread();
  const usage=current.usage || (current.id==='home'?s?.context:null);
  const ratio=usage?.limit?Math.max(0,Math.min(1,usage.used/usage.limit)):null;
  $('engine-context').textContent=ratio===null?'Context unavailable':`${(ratio*100).toFixed(1)}% used`;
  const bars=document.querySelector('.engine-bars');
  bars.classList.remove('working','ready');
  bars.setAttribute('aria-valuenow',String(ratio===null?0:Math.round(ratio*100)));
  bars.setAttribute('aria-valuetext',ratio===null?'Unavailable':`${fmtTokens(usage.used)} of ${fmtTokens(usage.limit)} tokens, estimated`);
  [...bars.children].forEach((bar,i)=>bar.classList.toggle('filled',ratio!==null && i<Math.ceil(ratio*bars.children.length)));
  $('engine-caption').textContent=ratio===null?'Open a chat to see its context':`${current.id==='home'?'Claire':current.title} · ~${fmtTokens(usage.used)} / ${fmtTokens(usage.limit)} tokens`;
  const capacity=s?.capacity;
  $('memory-summary').textContent=capacity?.memory?.available_bytes!==null && capacity?.memory?.available_bytes!==undefined
    ? `${(capacity.memory.available_bytes/1073741824).toFixed(1)} GB available / ${(capacity.memory.total_bytes/1073741824).toFixed(0)} GB RAM`:'Memory unavailable';
  $('worker-summary').textContent=capacity?`${capacity.running_tasks} running · ${capacity.task_limit} task slot${capacity.task_limit===1?'':'s'} · ${capacity.reason}`:'Reading task capacity…';
  $("guard-label").textContent = !s ? "Checking containment" : s.sandbox ? "Sandbox enabled" : "Sandbox unavailable";
  $("guard-label").className = s && !s.sandbox ? "warn" : "";
  $("guard-detail").textContent = !S.connected ? "Status unavailable while disconnected" : !s ? "Waiting for service status"
    : s.sandbox ? "Task commands are confined to their workspace." : "Pattern guards only. OS isolation is not active.";
}

/* ---- drawing -------------------------------------------------------------- */

/* Redraws rebuild the view. While Claire streams that happens many times a
   second, and a rebuild between pressing and releasing a button swallows the
   click; one while scrolling yanks the page. So: never redraw while a button
   is held or the page is being scrolled, and at most every 150 ms otherwise. */
let queued = false, lastRender = 0, pointerDown = false, scrolledAt = 0;
function draw() {
  if (queued) return;
  queued = true;
  const attempt = () => {
    const now = performance.now();
    const busy = pointerDown || now - scrolledAt < 350;
    const wait = busy ? 80 : 150 - (now - lastRender);
    if (wait > 0) { setTimeout(attempt, wait); return; }
    requestAnimationFrame(() => { queued = false; lastRender = performance.now(); render(); });
  };
  attempt();
}
function watchInput() {
  document.addEventListener("pointerdown", () => { pointerDown = true; }, true);
  for (const ev of ["pointerup", "pointercancel", "dragend"]) {
    document.addEventListener(ev, () => { pointerDown = false; }, true);
  }
  for (const ev of ["wheel", "touchmove", "keydown"]) {
    document.addEventListener(ev, (e) => {
      if (ev !== "keydown" || ["PageUp", "PageDown", "ArrowUp", "ArrowDown", "Home", "End", " "].includes(e.key)) scrolledAt = performance.now();
    }, { capture: true, passive: true });
  }
}

function render() {
  const view = $("view");
  const overview = S.view.kind === "dashboard";
  const current = S.view.kind === "thread" ? thread(S.view.id) : null;
  view.dataset.mode = overview ? "dashboard" : S.view.kind;
  drawSystem();
  drawSide(current);
  $("new-chat").disabled = !S.connected;
  const humanFree = !!(S.status && S.status.human_free);
  $("human-free").setAttribute("aria-checked", String(humanFree));
  $("human-free-detail").textContent = humanFree ? "On · asks only before deleting or danger" : "Off · asks before risky steps";
  $("mode-chip").hidden = !humanFree;

  for (const item of document.querySelectorAll(".nav-item[data-view]")) {
    const [kind, name] = item.dataset.view.split(":");
    const selected = S.view.kind === kind && (S.view.id || S.view.name) === name;
    item.classList.toggle("active", selected);
    if (selected) item.setAttribute("aria-current", "page"); else item.removeAttribute("aria-current");
  }
  const inbox = S.notes.filter((n) => n.folder === "inbox" && !["done", "closed"].includes(n.status));
  $("inbox-count").textContent = inbox.length ? String(inbox.length) : "";
  $("inbox-count").className = "meta" + (inbox.length ? " alert" : "");
  const broken = S.tools.filter((t) => t.health && (t.health.substitute || t.health.consecutive > 0)).length;
  $("tools-state").textContent = broken ? `${broken} broken` : "";
  $("tools-state").className = "meta" + (broken ? " alert" : "");
  const anyBusy = [...S.threads.values()].some((t) => t.busy);
  $("pulse").className = "pulse" + (!S.connected ? " down" : anyBusy ? " live" : "");
  const running = activeTasks().sort((a, b) => b.updated - a.updated);
  $("active-count").textContent = running.length ? String(running.length) : "";

  // header + main view
  $("banner").hidden = !S.error;
  $("banner").textContent = S.error;
  const tools = [];
  const crumb = $("view-category");
  crumb.replaceChildren();
  if (overview) {
    $("title").textContent = "Command center";
    const where = new Set(running.map((t) => t.project || "-")).size;
    $("subtitle").textContent = running.length
      ? `${running.length} task${running.length === 1 ? "" : "s"} running in ${where} ${where === 1 ? "place" : "places"}`
      : "Everything Claire is working on, in every project";
    dashboard();
  } else if (current) {
    const project = projectOf(current);
    const mainChat = current.id === "home";
    if (project) crumb.append(el("button", { class: "crumb-link", type: "button", onclick: () => show({ kind: "project", id: project.id }) },
                                 icon("i-folder"), el("span", { text: project.name })));
    else crumb.append(current.kind === "home" ? "Chats" : "Tasks");
    $("title").textContent = mainChat ? "Claire" : current.title;
    $("subtitle").textContent = current.kind === "home"
      ? (current.speed ? `${mainChat ? "Main chat" : "Chat"} · ${current.speed.toFixed(0)} tokens/s`
         : mainChat ? "Main chat · follows the work in every project" : project ? "Uses this project’s memory" : "A separate chat · its own context")
      : (current.parent ? "A task Claire started" : "A task you started");
    const m = meter(current.usage);
    if (m) tools.push(m);
    if (current.kind === "home" && !mainChat) {
      tools.push(el("button", { class: "ghost small", text: project ? "Move…" : "Add to project…", disabled: current.busy, onclick: () => moveDialog(current) }));
      tools.push(el("button", { class: "ghost small", text: "Delete chat", disabled: current.busy, onclick: () => deleteChat(current) }));
    }
    if (current.kind === "home" && current.trace.length) {
      tools.push(el("button", { class: "ghost small", id: "clear-chat", text: "Clear chat", title: "Remove this chat’s messages. Tasks and memory are kept.",
                                disabled: current.busy, onclick: () => clearChat(current) }));
    }
    if (mainChat && current.trace.length) {
      tools.push(el("button", { class: "ghost small", text: hiddenBefore ? "Show full history" : "Hide earlier messages", title: "Only changes this browser’s view. The conversation is retained.",
                                disabled: current.busy, onclick: hideHistory }));
    }
    if (current.kind !== "home") tools.push(actionButton("Ask for status", () => askForUpdate(current)));
    // The conversation scrolls inside the page, and is rebuilt on every
    // event; carry its scroll position across the rebuild.
    const was = $("chat-scroll");
    const stick = !was || S.jump || was.scrollHeight - was.scrollTop - was.clientHeight < 90;
    const at = was ? was.scrollTop : 0;
    view.replaceChildren(chatLayout(current));
    const now = $("chat-scroll");
    now.scrollTop = stick ? now.scrollHeight : at;
    S.jump = false;
  } else if (S.view.kind === "project") {
    const p = S.projects.get(S.view.id) || S.projectDetail.get(S.view.id)?.project;
    crumb.append("Projects");
    if (!p) {
      const detail = S.projectDetail.get(S.view.id);
      if (!detail) loadProject(S.view.id);
      $("title").textContent = "Project";
      $("subtitle").textContent = "";
      view.replaceChildren(el("div", { class: "empty-state", text: detail?.error || "Loading…" }));
    } else {
      $("title").textContent = p.name;
      $("subtitle").textContent = p.path ? tilde(p.path) : "A group with no folder of its own";
      tools.push(actionButton("New chat", () => newChat(p.id), true),
                 actionButton("New task", () => openTaskDialog(p.id)),
                 actionButton("Rename", () => projectDialog(p)),
                 actionButton("Remove", () => removeProject(p)));
      view.replaceChildren(projectView(p));
    }
  } else {
    const [title, build] = PANELS[S.view.name];
    $("title").textContent = title;
    $("subtitle").textContent = S.view.name === "memory" && S.status ? tilde(S.status.vault) : "";
    view.replaceChildren(build());
  }
  $("head-tools").replaceChildren(...tools);

  // In a chat the composer continues it; on the command center or a project
  // page it starts a new chat there.
  $("composer").hidden = !["thread", "dashboard", "project"].includes(S.view.kind);
  const starting = !current;
  const scope = current ? projectOf(current) : S.view.kind === "project" ? S.projects.get(S.view.id) || null : null;
  $("input").placeholder = starting
    ? (scope ? `Start a new chat in ${scope.name}…` : "Start a new chat: ask anything, or give Claire work")
    : current.kind === "home" ? (scope ? `Message Claire about ${scope.name}` : "Message Claire")
    : "Send this task a follow-up…";
  $("composer-scope").replaceChildren(...(scope ? [icon("i-folder"), el("span", { text: scope.name })] : []));
  $("composer-scope").hidden = !scope;
  $("stop").hidden = starting || !current.busy;
  $("deep").hidden = starting;
  $("deep").setAttribute("aria-pressed", String(!starting && current.thinking));
  $("send").disabled = !S.connected || (!starting && current.busy);
  $("deep").disabled = !S.connected;
  const live = !starting && current.busy ? current.live : null;
  $("live").hidden = !live;
  if (live) {
    $("live-label").textContent = live.label;
    $("live-preview").textContent = live.preview || "";
  }

  // right rail
  const asks = [...S.approvals.values()];
  $("approval-count").textContent = String(asks.length);
  $("approval-count").classList.toggle("attention", asks.length > 0);
  $("mobile-approval-count").textContent = String(asks.length);
  $("mobile-approval-count").hidden = asks.length === 0;
  $("approvals").replaceChildren(...(asks.length ? asks.map((a) => {
    const asker = a.thread === "home" ? null : thread(a.thread);
    const where = asker && projectOf(asker) ? ` · ${projectOf(asker).name}` : "";
    return el("div", { class: "card ask" },
       el("div", { class: "from", text: `${asker ? asker.title : "Main chat"}${where} wants to ${verb(a.kind)}` }),
       a.flag && el("div", { class: "flag", text: a.flag }),
       el("div", { class: "what", text: a.title }),
       el("div", { class: "acts" },
          el("button", { class: "no", text: "Deny", onclick: () => answer(a.id, false) }),
          el("button", { class: "yes", text: "Allow", onclick: () => answer(a.id, true) })));
  }) : [el("div", { class: "empty", text: "None pending." })]));
  $("running-count").textContent = String(running.length);
  $("signal-count").textContent = String(inbox.length);
  $("running").replaceChildren(...(running.length ? running.slice(0, 12).map((t) =>
    el("button", { class: "card run", onclick: () => show({ kind: "thread", id: t.id }) },
       el("div", { class: "from" }, el("span", { class: `dot ${t.status}` }), el("span", { text: t.title })),
       el("div", { class: "line", text: [projectOf(t)?.name || "No project", t.live ? t.live.label : (t.last || t.status)].join(" · ") })))
    : [el("div", { class: "empty", text: "Nothing running." })]));
  $("rail-inbox").replaceChildren(...(inbox.length ? inbox.map((n) =>
    el("button", { class: "card inbox", text: plain(n.title), onclick: () => openNote(n.path) }))
    : [el("div", { class: "empty", text: "No repair notes." })]));

  document.title = (asks.length ? `(${asks.length}) ` : "") + "Claire";
}

function verb(kind) {
  return { command: "run a command", search: "search the web", fetch: "open a page",
           network: "use the network", connector: "use a connected service",
           integration: "install an integration she built", schedule: "set up a schedule" }[kind] || "do something";
}

/* ---- actions -------------------------------------------------------------- */

function fromHash() {
  let hash;
  try { hash = decodeURIComponent(location.hash.slice(1)); } catch (_) { hash = "overview"; }
  const [kind, ...rest] = hash.split("/");
  const name = rest.join("/");
  if ((kind === "task" || kind === "chat") && name) return { kind: "thread", id: name };
  if (kind === "project" && name) return { kind: "project", id: name };
  if (kind === "panel" && PANELS[name]) return { kind: "panel", name };
  if (kind === "home") return { kind: "thread", id: "home" };
  return { kind: "dashboard", name: "overview" };
}

function toHash(view) {
  if (view.kind === "dashboard") return "#overview";
  if (view.kind === "panel") return `#panel/${view.name}`;
  if (view.kind === "project") return `#project/${encodeURIComponent(view.id)}`;
  if (view.id === "home") return "#home";
  const known = S.threads.get(view.id);
  return `#${known && known.kind === "home" ? "chat" : "task"}/${encodeURIComponent(view.id)}`;
}

function show(view) {
  inputDrafts.set(inputThread().id, $("input").value);
  S.view = view;
  S.jump = true;                       // open a chat at its latest message
  $("input").value = inputDrafts.get(inputThread().id) || "";
  resize();
  S.error = "";
  if (toHash(view) !== (location.hash || "#")) history.pushState(null, "", toHash(view));
  $("side").classList.remove("open");
  $("rail").classList.remove("open");
  syncDrawers();
  if(view.kind === "dashboard") { refreshSide(); fetchExtra("scheduler"); }
  if (view.kind === "project") loadProject(view.id);
  if (view.kind === "panel") {
    refreshSide();
    if (view.name === "tasks") refreshThreads().catch(err => { S.error=err.message; draw(); });
    if (["integrations", "scheduler"].includes(view.name)) fetchExtra(view.name);
    if (view.name === "skills") loadSkills();
    if (view.name === "memory" && !S.note) {
      // Open on the shared index: what every chat and task is given.
      api("/vault/MEMORY.md").then((note) => { if (!S.note) { S.note = note; draw(); } }).catch(() => {});
    }
    if (view.name === "controller") loadController();
    if (view.name === "bench") api("/bench").then((d) => { S.reports = d.reports; draw(); }).catch(() => {});
    if (view.name === "models") loadModels();
  }
  draw();
  requestAnimationFrame(() => {
    const v = $("view");
    v.scrollTop = view.kind === "thread" ? v.scrollHeight : 0;
    if (view.kind === "thread") $("input").focus();
    else $("view").focus({ preventScroll: true });
  });
}

async function openNote(path) {
  try {
    S.note = await api("/vault/" + path.split("/").map(encodeURIComponent).join("/"));
    show({ kind: "panel", name: "memory" });
  } catch (err) { S.error = err.message; draw(); }
}

/* Sending from a chat continues it. Sending from the command center or a
   project page starts a new chat (in that project), as in a code workspace. */
async function send(event) {
  event.preventDefault();
  const input = $("input");
  const text = input.value.trim();
  let t = S.view.kind === "thread" ? thread(S.view.id) : null;
  if (!text || !S.connected || (t && t.busy)) return;
  const previousBusy = t ? t.busy : false;
  input.value = "";
  inputDrafts.delete(inputThread().id);
  resize();
  try {
    if (!t) {
      const project = S.view.kind === "project" ? S.view.id : "";
      const { thread: row } = await api("/threads", { kind: "chat", project });
      t = thread(row.id);
      Object.assign(t, { kind: row.kind, title: row.title, status: row.status, updated: row.updated,
                         workdir: row.workdir, parent: row.parent, project: row.project || "" });
      show({ kind: "thread", id: row.id });
    }
    t.busy = true;
    draw();
    await api(`/threads/${encodeURIComponent(t.id)}/messages`, { text });
  } catch (err) {
    S.error = err.message;
    if (t) t.busy = previousBusy;
    input.value = text;
    resize();
    draw();
  }
  requestAnimationFrame(() => { $("view").scrollTop = $("view").scrollHeight; });
}

async function act(fn) {
  try { await fn(); } catch (err) { S.error = err.message; }
  draw();
}

const stop = () => act(() => api(`/threads/${encodeURIComponent(inputThread().id)}/cancel`, {}));
const answer = (id, allow) => act(async () => {
  await api(`/approvals/${encodeURIComponent(id)}`, { allow });
  S.approvals.delete(id);
});
const toolAction = (name, action) => act(async () => {
  await api(`/tools/${encodeURIComponent(name)}/${action}`, {});
  await refreshSide();
});
const toggleDeep = () => act(async () => {
  const t = inputThread();
  const { thinking } = await api(`/threads/${encodeURIComponent(t.id)}/mode`, { thinking: !t.thinking });
  t.thinking = thinking;
});

function resize() {
  const input = $("input");
  input.style.height = "auto";
  input.style.height = Math.min(220, input.scrollHeight + 2) + "px";
}

async function startTask(event) {
  event.preventDefault();
  const error = $("task-error");
  error.hidden = true;
  try {
    const { thread: row } = await api("/threads", {
      title: $("task-title").value.trim(), workdir: $("task-workdir").value.trim(),
      task: $("task-brief").value.trim(), project: $("task-project").value,
      skills: $("task-skills").value.split(/[,\s]+/).filter(Boolean) });
    $("task-dialog").close();
    $("task-form").reset();
    Object.assign(thread(row.id), { kind: "task", title: row.title, status: row.status,
                                    parent: row.parent, skills: row.skills || "", project: row.project || "",
                                    workdir: row.workdir, updated: row.updated, summary: row.summary || "" });
    refreshProjects();
    show({ kind: "thread", id: row.id });
  } catch (err) {
    error.textContent = err.message;
    error.hidden = false;
  }
}

/* ---- start ---------------------------------------------------------------- */

let lastUserActivity = -Infinity;
function noteUserActivity() {
  if (document.hidden || performance.now() - lastUserActivity < 60000) return;
  lastUserActivity = performance.now();
  // Register use of the workspace without loading a sleeping model.
  api('/activity', {}).catch(() => {});
}

function wire() {
  for (const name of ["pointerdown", "keydown", "input", "wheel", "touchstart"]) {
    document.addEventListener(name, noteUserActivity, {passive:true, capture:true});
  }
  noteUserActivity();
  for (const item of document.querySelectorAll(".nav-item[data-view]")) {
    const [kind, name] = item.dataset.view.split(":");
    item.addEventListener("click", () => show(kind === "thread" ? { kind, id: name } : { kind, name }));
  }
  $("composer").addEventListener("submit", send);
  $("input").addEventListener("input", resize);
  $("input").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); $("composer").requestSubmit(); }
  });
  $("stop").addEventListener("click", stop);
  $("deep").addEventListener("click", toggleDeep);
  $("new-chat").addEventListener("click", () => newChat());
  $("human-free").addEventListener("click", toggleHumanFree);
  $("mode-chip").addEventListener("click", toggleHumanFree);
  $("task-cancel").addEventListener("click", () => $("task-dialog").close());
  $("task-form").addEventListener("submit", startTask);
  $("side-open").addEventListener("click", () => {
    $("rail").classList.remove("open"); $("side").classList.add("open"); syncDrawers(); $("side-close").focus();
  });
  $("side-close").addEventListener("click", () => {
    $("side").classList.remove("open"); syncDrawers(); $("side-open").focus();
  });
  $("rail-open").addEventListener("click", openActivity);
  $("rail-close").addEventListener("click", () => { $("rail").classList.remove("open"); syncDrawers(); $("rail-open").focus(); });
  $("drawer-backdrop").addEventListener("click", () => {
    const trigger = $("side").classList.contains("open") ? $("side-open") : $("rail-open");
    $("side").classList.remove("open"); $("rail").classList.remove("open"); syncDrawers(); trigger.focus();
  });
  window.addEventListener("resize", syncDrawers);
  syncDrawers();
  window.addEventListener("hashchange", () => {
    if (location.hash === "#main") { $("main").focus(); return; }
    show(fromHash());
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      const railOpen = $("rail").classList.contains("open");
      const sideOpen = $("side").classList.contains("open");
      $("side").classList.remove("open");
      $("rail").classList.remove("open");
      syncDrawers();
      if (railOpen) $("rail-open").focus();
      else if (sideOpen) $("side-open").focus();
    }
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k" && !$("task-dialog").open && !$("setup-dialog").open) {
      e.preventDefault();
      // The composer is on every page that can start a chat.
      if (!["thread", "dashboard", "project"].includes(S.view.kind)) show({ kind: "dashboard", name: "overview" });
      requestAnimationFrame(() => $("input").focus());
    }
  });
  setInterval(()=>{if(!document.hidden){refreshSide();if(S.view.kind==="dashboard")fetchExtra("scheduler");}},15000);
}

async function start() {
  wire();
  watchInput();
  thread("home");
  S.view = fromHash();
  draw();
  try {
    const [status, pending] = await Promise.all([api("/status"), api("/approvals")]);
    S.status = status;
    checkBuild(status);
    S.lastSeq = status.last_event;
    for (const a of pending.approvals) {
      S.approvals.set(a.id, { id: a.id, thread: a.thread_id, kind: a.kind, title: a.title, flag: a.flag });
    }
    await refreshThreads();
    connect();
    await refreshSide();
    window.addEventListener("popstate", () => show(fromHash()));
    show(fromHash());
  } catch (err) {
    S.error = `Could not reach Claire's service: ${err.message}`;
    draw();
  }
}

/* ---- workspace extensions: optional backend services + explicit local drafts ---- */
const PREF_KEY = 'claire.workspace.v2';
function loadPrefs() {
  try { const value = JSON.parse(localStorage.getItem(PREF_KEY) || '{}'); return value && typeof value === 'object' && !Array.isArray(value) ? value : {}; }
  catch (_) { return {}; }
}
const prefs = loadPrefs();
let hiddenBefore = Number(prefs.hiddenBefore) || 0;
function historyPreference(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return {};
  return Object.fromEntries(Object.entries(value).filter(([id, updated]) => typeof updated === 'number' && Number.isFinite(updated) && updated >= 0));
}
let hiddenThreadHistory = {
  chats: historyPreference(prefs.hiddenThreadHistory?.chats),
  tasks: historyPreference(prefs.hiddenThreadHistory?.tasks),
};
let collapsedProjects = prefs.collapsedProjects && typeof prefs.collapsedProjects === 'object' && !Array.isArray(prefs.collapsedProjects)
  ? Object.fromEntries(Object.entries(prefs.collapsedProjects).filter(([, v]) => v === true)) : {};
let scheduleDrafts = Array.isArray(prefs.scheduleDrafts) ? prefs.scheduleDrafts.filter(d => d && typeof d.id === 'string' && typeof d.title === 'string') : [];
let integrationDrafts = Array.isArray(prefs.integrationDrafts) ? prefs.integrationDrafts.filter(d => d && typeof d.id === 'string') : [];
let controllerDraft = prefs.controllerDraft && typeof prefs.controllerDraft === 'object' ? prefs.controllerDraft : null;
const extras = {
  integrations: { state: 'idle', items: [], error: '' },
  scheduler: { state: 'idle', items: [], error: '' },
};
let taskFilter = 'all';
let scheduleFilter = 'all';
const inputDrafts = new Map();
let toastTimer;
function toast(message) { $('toast').textContent = message; $('toast').hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => { $('toast').hidden = true; }, 4500); }
function savePrefs() {
  try {
    localStorage.setItem(PREF_KEY, JSON.stringify({ hiddenBefore, hiddenThreadHistory, collapsedProjects, scheduleDrafts, integrationDrafts, controllerDraft }));
    return true;
  } catch (_) { toast('Browser storage is unavailable. This change will last only in this tab.'); return false; }
}
function historyPinned(t, current) {
  return t.id === 'home' || current?.id === t.id || t.busy
    || ['running','queued','waiting'].includes(t.status)
    || (t.kind === 'task' && t.status === 'idle')
    || [...S.approvals.values()].some(a => a.thread === t.id);
}
function historyVisible(t, current = null) {
  if (historyPinned(t, current)) return true;
  const hidden = hiddenThreadHistory[t.kind === 'home' ? 'chats' : 'tasks'];
  if (!Object.prototype.hasOwnProperty.call(hidden, t.id)) return true;
  // A hidden conversation resurfaces when new work/messages update it.
  return (Number(t.updated) || 0) > hidden[t.id];
}
function togglePrevious(kind) {
  const current = S.view.kind === 'thread' ? S.threads.get(S.view.id) : null;
  if (Object.keys(hiddenThreadHistory[kind]).length) {
    hiddenThreadHistory[kind] = {};
  } else {
    const items = [...S.threads.values()].filter(t => (kind === 'chats' ? t.kind === 'home' : t.kind === 'task') && !historyPinned(t, current));
    hiddenThreadHistory[kind] = Object.fromEntries(items.map(t => [t.id, Number(t.updated) || 0]));
    if (!items.length) { toast(`No previous ${kind} to hide.`); return; }
  }
  savePrefs(); draw();
}
function historyToggle(kind, items, current = null) {
  const enabled = Object.keys(hiddenThreadHistory[kind]).length > 0;
  const count = items.filter(t => !historyVisible(t, current)).length;
  return el('button', {class:'ghost small history-toggle', type:'button', 'data-history-kind':kind,
    'aria-pressed':String(enabled), 'aria-label':`${enabled ? 'Show' : 'Hide'} previous ${kind}`,
    title:'Keeps the open conversation and active work visible. You can restore previous items any time.',
    text:enabled ? `Show previous${count ? ` (${count})` : ''}` : 'Hide previous',
    onclick:()=>togglePrevious(kind)});
}

function hideHistory() {
  hiddenBefore = hiddenBefore ? 0 : thread('home').maxSeq;
  savePrefs(); draw();
  toast(hiddenBefore ? 'Earlier messages hidden in this browser. Claire keeps the conversation.' : 'Full conversation restored.');
}
function visibleTrace(t) { return t.id === 'home' && hiddenBefore ? t.trace.filter(item => (item.seq || 0) > hiddenBefore) : t.trace; }
function askForUpdate(t) {
  // Ask in the chat that started the task when there is one; otherwise a new chat.
  const parent = t && t.parent && t.parent !== 'home' ? S.threads.get(t.parent) : null;
  show(parent ? { kind: 'thread', id: parent.id } : { kind: 'dashboard', name: 'overview' });
  $('input').value = t ? `Check the current status of task ${t.id} (${t.title}). Tell me what is done, what remains, and whether it needs my input.`
    : 'Check my background tasks and give me a current status update. What is done, running, or waiting on me?';
  resize(); $('input').focus();
}
function panelIntro(eyebrow, title, description, ...actions) {
  return el('div', { class: 'page-intro' }, el('div', {}, el('div', { class: 'eyebrow', text: eyebrow }),
    el('h2', { text: title }), el('p', { text: description })), el('div', { class: 'page-actions' }, actions));
}
function actionButton(text, fn, primary = false, disabled = false) {
  return el('button', { type: 'button', class: primary ? 'primary small' : 'ghost small', text, onclick: fn, disabled });
}
function notice(title, text, tone = '') {
  return el('div', { class: 'service-notice ' + tone }, el('span', { class: 'notice-symbol', 'aria-hidden': 'true', text: tone === 'error' ? '!' : '◈' }),
    el('div', {}, el('strong', { text: title }), el('p', { text })));
}
function fetchExtra(name) {
  const state=extras[name];
  if(state.request)return state.request;
  state.state='loading';state.error='';draw();
  state.request=(async()=>{
    try {
      const data=await api(name==='scheduler'?'/schedules':'/integrations');
      const rows=name==='scheduler'?data.schedules:data.integrations;
      if(!Array.isArray(rows))throw new Error('The service returned an unsupported response.');
      if(name==='integrations'){const info=await api('/integrations/providers');state.providers=info.providers;state.redirect=info.redirect_uri||'';}
      state.items=rows;state.state='ready';if(name==='integrations')state.installed=data.installed||[];
    }catch(err){
      state.items=[];state.state=[404,501].includes(err.status)?'unavailable':'error';state.error=err.message;
    }finally{state.request=null;draw();}
  })();
  return state.request;
}
function extraNotice(name) {
  const state = extras[name];
  if (state.state === 'loading' || state.state === 'idle') return notice('Checking connection', 'Reading the local service…');
  if (state.state === 'error') return notice('Could not load service data', `${state.error} Saved drafts remain available. Retry to check the service.`, 'error');
  if (state.state === 'unavailable') return notice(name === 'scheduler' ? 'Scheduler service not connected' : 'Connection service not connected',
    name === 'scheduler' ? 'You can prepare schedules below. Drafts do not run; existing cron jobs cannot be listed until the scheduler service is connected.'
      : 'Prepare connections below. Signing in and granting access will be available when the connection service is enabled.');
  return notice(name === 'scheduler' ? 'Schedules from your local service' : 'Connections from your local service',
    name === 'scheduler' ? 'Run times and job states are reported by Claire’s scheduler. Open a run to inspect its task.' : 'Account and connection states below are reported by Claire’s connection service.', 'connected');
}
function statusPill(text) { return el('span', { class: 'pill ' + (['running','waiting','done','error','stopped'].includes(text) ? text : ''), text }); }
function taskCards(tasks) {
  if (!tasks.length) return notice('No tasks in this view', 'Give Claire an objective, or create a task directly.');
  return el('div', { class: 'work-list' }, tasks.map(t => el('article', { class: 'work-card' },
    el('div', { class: 'work-card-top' }, el('span', { class: `dot ${t.status}` }), el('h3', { text: t.title }), statusPill(t.status)),
    el('p', { class: 'work-summary', text: t.live?.label || t.last || t.summary || 'Open this task to inspect its conversation and execution log.' }),
    el('div', { class: 'work-meta' }, el('span', { text: projectOf(t)?.name || 'No project' }), el('span', { text: tilde(t.workdir) || 'Task workspace' }), el('span', { text: t.id }), el('span', { text: t.updated ? ago(t.updated) : '' })),
    el('div', { class: 'work-actions' }, actionButton('Open task chat ↗', () => show({kind:'thread',id:t.id})), actionButton('Ask Claire for an update', () => askForUpdate(t)))
  )));
}
function panelTasks() {
  const tasks = [...S.threads.values()].filter(t => t.kind === 'task').sort((a,b)=>b.updated-a.updated);
  const all = tasks.filter(t => historyVisible(t));
  const filters = { all: () => true, active: t => ['idle','running','queued','waiting'].includes(t.status), completed: t => t.status === 'done', failed: t=>['error','stopped','interrupted'].includes(t.status), attention: t => ['waiting','error','interrupted','stopped'].includes(t.status) };
  return el('div', { class: 'panel extension-panel' }, panelIntro('', 'Tasks',
     'Every task in every project. Open one to follow its progress or continue the conversation.', historyToggle('tasks', tasks), actionButton('Status briefing', () => askForUpdate()), actionButton('New task', () => openTaskDialog(), true)),
    el('div', { class:'filter-tabs', 'aria-label':'Task filters' }, Object.keys(filters).map(key=>el('button',{class: taskFilter===key?'selected':'', 'aria-pressed':String(taskFilter===key), text:`${key}  ${all.filter(filters[key]).length}`, onclick:()=>{taskFilter=key;draw();}}))),
    taskCards(all.filter(filters[taskFilter])));
}
const PROVIDERS = [
  { id:'google', name:'Google account', mark:'G', type:'GOOGLE', description:'One sign-in for Gmail and Calendar: search and read mail, see your events.', auth:'Google sign-in with your OAuth client', permissions:'Read-only. It cannot send mail or change events.', action:'Set up Google', color:'mail' },
  { id:'gmail', hidden:true, name:'Gmail', mark:'G', type:'GOOGLE WORKSPACE', description:'Search messages, read conversations, and bring the right context into your work.', auth:'Google sign-in', permissions:'Read and search mail. This connector cannot send messages.', action:'Set up Gmail', color:'mail' },
  { id:'calendar', hidden:true, name:'Google Calendar', mark:'31', type:'GOOGLE WORKSPACE', description:'Read events and availability to plan your day.', auth:'Google sign-in', permissions:'Read-only calendar access.', action:'Set up Calendar', color:'mail' },
  { id:'browser', name:'Browser', mark:'↗', type:'PLAYWRIGHT', description:'Explore websites, research travel dates, and compare information in a dedicated browser.', auth:'Persistent browser session', permissions:'A dedicated browser profile. Sign in to websites yourself when required.', action:'Set up browser', color:'browser' },
  { id:'notes', name:'Apple Notes', mark:'✎', type:'ON THIS MAC', description:'Find, read and write notes in the Notes app, without touching the screen.', auth:'macOS permission, asked once', permissions:'Reads and searches notes; creates notes and adds text. Deleting a note asks first.', action:'Set up Notes', color:'browser' },
  { id:'mac', name:'Mac apps', mark:'⌘', type:'ON THIS MAC', description:'Let Claire work inside apps you choose: read a window, click, type and use menus. A blue frame shows while she has control.', auth:'macOS Accessibility permission', permissions:'Only the apps you name. Reading, typing and ordinary clicks go ahead; deleting, sending, saving over and quitting ask first.', action:'Choose apps', color:'browser' },
  { id:'built', hidden:true, name:'Built by Claire', mark:'✦', type:'CLAIRE', description:'An integration Claire wrote and you approved.', auth:'Credentials you enter here', permissions:'Actions that change things ask first.', color:'mcp' },
  { id:'mcp', name:'Custom MCP', mark:'⌘', type:'EXTEND CLAIRE', description:'Bring tools from an MCP server into Claire’s workspace.', auth:'Provider-dependent', permissions:'Tools are discovered by the service. Each tool asks before running.', action:'Add MCP connection', color:'mcp' },
  { id:'api', name:'API service', mark:'{ }', type:'CUSTOM CONNECTION', description:'Prepare a service that uses an API token when account sign-in is unavailable.', auth:'Token, when supported', permissions:'Tokens are stored by Claire’s local secret manager, outside the conversation.', action:'Set up API', color:'api' },
];
function panelIntegrations() {
  const state = extras.integrations;
  const live = state.items;
  const root = el('div', {class:'panel extension-panel'}, panelIntro('CONNECTED INTELLIGENCE','Your tools. Claire’s reach.',
    'Connect accounts once, choose what Claire can do, and keep access under your control.', actionButton('Refresh',()=>fetchExtra('integrations'),false,state.state==='loading')),
    extraNotice('integrations'));
  if (live.length) root.append(el('h3',{text:'Your connections'}),el('div',{class:'work-list'},live.map(item=>el('article',{class:'work-card'},
    el('div',{class:'work-card-top'},el('h3',{text:item.name || item.provider || item.id}),
       statusPill((item.credentials||[]).some(c=>!c.provided) ? 'needs your input' : (item.status || 'unknown'))),
    el('p',{class:'work-summary',text:item.provider==='built' ? 'Built by Claire' + (item.status==='connected' ? ' · working' : '')
                                         : (item.account_label || 'No account details reported')}),
    el('div',{class:'work-meta',text:Array.isArray(item.scopes)?item.scopes.join(' · '):'Permissions not reported'}),
    actionButton('Connection details',()=>integrationDialog(PROVIDERS.find(p=>p.id===item.provider)||{id:item.provider||'custom',name:item.name||'Connection',auth:'Provider-managed',permissions:'See granted permissions from the connection service.'},item))
  ))));
  const installed = state.installed || [];
  if (installed.length) root.append(el('h3',{text:'Built by Claire · not connected'}),el('div',{class:'work-list'},installed.map(item=>el('article',{class:'work-card built-card'},
    el('div',{class:'work-card-top'},el('h3',{text:item.title}),statusPill('installed')),
    el('p',{class:'work-summary',text:item.description}),
    el('div',{class:'work-meta',text:(item.needs.length?'Needs: '+item.needs.join(', ')+' · ':'')+'Actions: '+item.actions.join(', ')}),
    actionButton('Set up',async()=>{
      try{
        const made=await api('/integrations',{provider:'built',name:item.title,config:{integration:item.name}});
        await fetchExtra('integrations');
        integrationDialog(PROVIDERS.find(p=>p.id==='built'),made.integration);
      }catch(err){toast('Could not set it up: '+err.message);}
    },true)))));
  root.append(el('div',{class:'integration-grid'},PROVIDERS.filter(p=>!p.hidden).map(provider=>{
    const drafts = integrationDrafts.filter(d=>d.provider===provider.id);
    const spec = extras.integrations.providers?.find(p=>p.provider===provider.id);
    const unavailable = spec?.available === false;
    return el('article',{class:'integration-card'},el('div',{class:'integration-top'},el('span',{class:'provider-mark '+provider.color,text:provider.mark}),el('span',{class:'eyebrow',text:provider.type})),
      el('h3',{text:provider.name}),el('p',{text:unavailable?spec.unavailable_reason:provider.description}),el('div',{class:'integration-auth',text:provider.auth}),
      el('div',{class:'integration-bottom'},actionButton(unavailable?'Unavailable on Windows':provider.action,()=>integrationDialog(provider),false,unavailable),el('span',{class:'draft-count',text:drafts.length?`${drafts.length} setup draft${drafts.length===1?'':'s'}`:''})));
  })));
  if (integrationDrafts.length) root.append(el('h3',{text:'Setup drafts · not connected'}),el('div',{class:'draft-list'},integrationDrafts.map(d=>el('div',{class:'draft-row'},el('div',{},el('strong',{text:d.name||d.provider}),el('small',{text:d.provider==='mcp'?d.endpoint||d.transport:'Waiting for secure connection setup'})),
    actionButton('Edit',()=>integrationDialog(PROVIDERS.find(p=>p.id===d.provider),null,d)),actionButton('Remove draft',()=>{integrationDrafts=integrationDrafts.filter(x=>x.id!==d.id);savePrefs();draw();})))));
  root.append(notice('Your credentials stay out of the conversation','Google uses account sign-in. API and MCP credentials go to the local secret store. Existing setup drafts contain no credentials.'));
  return root;
}
function showSetup(title, subtitle) {
  const content=$('setup-content');
  content.replaceChildren(el('div',{class:'eyebrow',text:'WORKSPACE / SETUP'}),el('h2',{id:'setup-title',text:title}),el('p',{class:'dim',text:subtitle}));
  $('setup-dialog').showModal();
  return content;
}
function field(label, input) { return el('label',{},label,input); }
function closeSetup() { $('setup-dialog').close(); $('setup-content').replaceChildren(); }
function endpointValid(value) {
  try { const url=new URL(value); return !url.username && !url.password && !url.search && !url.hash && (url.protocol==='https:' || (url.protocol==='http:' && ['localhost','127.0.0.1','[::1]'].includes(url.hostname))); } catch(_){return false;}
}
async function integrationAction(item, action, output) {
  try {
    output.replaceChildren(el('p', {class:'dim',text:'Contacting the connection service…'}));
    const result = await api('/integrations/' + encodeURIComponent(item.id) + '/' + action, {});
    await fetchExtra('integrations');
    if(result.integration){
      closeSetup();integrationDialog(PROVIDERS.find(p=>p.id===result.integration.provider),result.integration);
      output=el('div',{'aria-live':'polite'});$('setup-content').append(output);
    }
    output.replaceChildren();
    if (result.auth_url) {
      const url = new URL(result.auth_url);
      if (url.protocol !== 'https:' && !(url.protocol === 'http:' && ['localhost','127.0.0.1'].includes(url.hostname))) throw new Error('The provider returned an unsupported sign-in address.');
      output.append(el('a',{class:'primary small',href:url.href,target:'_blank',rel:'noopener noreferrer',text:'Continue to Google sign-in ↗'}),el('p',{class:'field-help',text:'Complete sign-in in the new tab, then refresh your connections.'}));
    } else {
      const current=result.integration;
      toast(current?.status==='connected'?'Connection verified.':current?.error || 'Connection updated.');
    }
  } catch(err) { output.replaceChildren(notice('Connection needs attention',err.message,'error')); }
}
/* The credentials a connection asks for -- for one Claire built, the steps she
   wrote for getting each. Saved to the keychain, never shown to her. */
function credentialsForm(item, output) {
  const form=el('form',{class:'setup-form credentials-form'});
  const inputs={};
  const waiting=item.credentials.filter(c=>!c.provided).length;
  form.append(el('strong',{text:waiting?`Needs from you (${waiting})`:'Credentials (saved; enter a new value to replace one)'}));
  for(const c of item.credentials){
    const input=el('input',{type:c.secret===false?'url':'password',autocomplete:'off',spellcheck:'false',maxlength:4000,
                            placeholder:c.provided?'Saved':(c.secret===false?'https://…':'Paste it here')});
    inputs[c.key]=input;
    form.append(el('label',{},el('span',{text:`${c.label} `}),el('code',{text:c.key}),input,c.how?el('span',{class:'field-help',text:'How to get it: '+c.how}):null));
  }
  const save=el('button',{class:'primary',type:'submit',text:'Save and test'});
  form.append(el('div',{class:'row end'},save));
  form.addEventListener('submit',async event=>{
    event.preventDefault();save.disabled=true;
    const credentials={};
    for(const [k,input] of Object.entries(inputs)) if(input.value.trim()) credentials[k]=input.value.trim();
    try{
      const result=await api('/integrations/'+encodeURIComponent(item.id)+'/credentials',{credentials});
      for(const input of Object.values(inputs)) input.value='';
      await fetchExtra('integrations');
      closeSetup();integrationDialog(PROVIDERS.find(p=>p.id===result.integration.provider)||{id:result.integration.provider,name:result.integration.name,permissions:''},result.integration);
      toast(result.integration.status==='connected'?'Connected.':'Saved, but the test failed: '+result.integration.error);
    }catch(err){output.replaceChildren(notice('Could not save',err.message,'error'));}finally{save.disabled=false;}
  });
  return form;
}
/* What Google needs from sir before a sign-in can work, said before he tries. */
function googleHelp(redirect, saved) {
  const copy=el('button',{class:'ghost small',type:'button',text:'Copy',onclick:async()=>{
    try{await navigator.clipboard.writeText(redirect);copy.textContent='Copied';}catch(_){copy.textContent='Select and copy it';}
  }});
  return el('div',{class:'service-notice google-help'},el('span',{class:'notice-symbol','aria-hidden':'true',text:'◈'}),el('div',{},
    el('strong',{text:saved?'Your saved Google client is reused':'Before you sign in: an OAuth client of your own'}),
    saved ? el('p',{text:'Leave the client fields empty to use the client you already set up. One client serves every Google connection.'})
          : el('ol',{},
              el('li',{text:'Google Cloud Console → APIs & Services → Library: enable the Gmail API and the Google Calendar API.'}),
              el('li',{text:'OAuth consent screen: add your Google account under Test users.'}),
              el('li',{text:'Credentials → Create credentials → OAuth client ID → Application type: Desktop app. Paste its client id and secret here.'})),
    el('p',{class:'field-help',text:'A Desktop app client needs no redirect address. If you use a Web application client instead, add this exact address under Authorized redirect URIs:'}),
    el('div',{class:'redirect-row'},el('code',{text:redirect||'(start the service to see it)'}),redirect?copy:null)));
}
function integrationDialog(provider, connected=null, draft=null) {
  if(!provider)return;
  if(!connected && !extras.integrations.providers?.length) {
    const root=showSetup('Connection service unavailable','Refresh Integrations when the service is running.');
    root.append(notice('Nothing connected','Account setup requires the local connection service.'),actionButton('Done',closeSetup));return;
  }
  const root=showSetup(connected ? connected.name : provider.name+' setup',provider.permissions);
  if(connected) {
    const output=el('div',{'aria-live':'polite'});
    const waiting=(connected.credentials||[]).some(c=>!c.provided);
    root.append(waiting ? notice('Needs your input','Enter what it asks for below, then Save and test.')
                        : notice(connected.status,connected.error || connected.account_label || 'Saved. Connect to verify access.',connected.status==='error'?'error':''));
    if(connected.credentials?.length) root.append(credentialsForm(connected,output));
    if(connected.operations?.length) root.append(el('div',{class:'run-list'},connected.operations.map(op=>el('div',{class:'run-row'},el('strong',{text:op.name}),el('span',{class:'dim',text:op.writes?'Asks before running':'Allowed without asking'})))));
    root.append(el('div',{class:'work-actions'},actionButton(['google','gmail','calendar'].includes(connected.provider)?'Sign in with Google':'Connect & test',()=>integrationAction(connected,'connect',output),true),actionButton('Test connection',()=>integrationAction(connected,'test',output)),actionButton('Disconnect',()=>{
      if(confirm('Disconnect '+connected.name+' and remove its saved access tokens?')) integrationAction(connected,'disconnect',output);
    }),actionButton('Remove connection',async()=>{
      if(!confirm('Remove '+connected.name+' and its credentials?'))return;
      try{await api('/integrations/'+encodeURIComponent(connected.id),undefined,'DELETE');closeSetup();await fetchExtra('integrations');}catch(err){output.replaceChildren(notice('Could not remove connection',err.message,'error'));}
    })),output,actionButton('Done',closeSetup));return;
  }
  const browserSetup=provider.id==='browser';
  const spec=extras.integrations.providers?.find(p=>p.provider===provider.id);
  if(!spec){root.append(notice('Provider not available','This service does not support that connection type.'),actionButton('Done',closeSetup));return;}
  if(spec.available===false){root.append(notice('Unavailable on this operating system',spec.unavailable_reason),actionButton('Done',closeSetup));return;}
  const form=el('form',{class:'setup-form'});
  const name=el('input',{required:true,maxlength:60,value:draft?.name||provider.name});
  form.append(field('Connection name',name));
  const inputs={};
  for(const f of spec.fields) {
    if(f.key==='auto_allow')continue; // Tools ask first. No permission widening as a setup default.
    let input;
    if(f.key==='transport') input=el('select',{},el('option',{value:'http',text:'Remote server (HTTP)'}),el('option',{value:'stdio',text:'Local program (stdio)'}));
    else if(f.key==='auth') input=el('select',{},el('option',{value:'bearer',text:'Bearer token'}),el('option',{value:'header',text:'Custom token header'}),el('option',{value:'none',text:'No authentication'}));
    else if(['allow_write','keep_alive','headless','ask_every','read_only','gmail','calendar'].includes(f.key)) input=el('input',{type:'checkbox'});
    else if(f.key==='notes') input=el('textarea',{rows:3,maxlength:1500});
    else input=el('input',{type:f.secret?'password':'text',required:f.required,autocomplete:'off',spellcheck:'false',maxlength:2000});
    inputs[f.key]=input;
    if(f.key==='transport')input.value=browserSetup?'stdio':draft?.transport||'http';
    if(f.key==='command' && browserSetup)input.value='npx -y @playwright/mcp@latest --user-data-dir=./browser-profile';
    if(f.key==='keep_alive' || f.key==='headless')input.checked=browserSetup;
    if(f.key==='gmail' || f.key==='calendar')input.checked=true;
    if(f.key==='client_id' && spec.saved_client){input.placeholder=`Leave empty to use your saved client (${spec.saved_client})`;input.required=false;}
    if(f.key==='client_secret' && spec.saved_client)input.placeholder='Leave empty to reuse the saved secret';
    if(f.key==='url' || f.key==='base_url')input.value=draft?.endpoint||'';
    const label=field(f.label,input);
    if(input.type==='checkbox')label.className='check-line';
    form.append(label);
  }
  const transportFields=()=>{
    const local=inputs.transport?.value==='stdio';
    for(const key of ['command','url','token'])if(inputs[key] && inputs.transport){inputs[key].closest('label').hidden=key==='command'?!local:local;inputs[key].disabled=key==='command'?!local:local;}
  };
  inputs.transport?.addEventListener('change',transportFields);transportFields();
  if(spec.oauth) form.append(googleHelp(extras.integrations.redirect, spec.saved_client));
  form.append(notice('Saved access, explicit control',browserSetup?'Connect & test opens a dedicated Chrome profile using Playwright. Chrome keeps its native sandbox; the MCP process stays inside Claire’s sandbox. Opening and reading pages goes ahead; major actions (deleting, sending, paying, signing in, uploading) ask for approval. Chrome runs only while a task is using it and closes when the task ends; sign-ins stay in this separate profile.':spec.oauth?'Use your Google OAuth Desktop client ID and optional secret. Save, then sign in once. Gmail and Calendar request read-only access.':'Credentials go directly to Claire’s local secret store. They are never saved as browser drafts or sent in chat. Local commands run when you choose Connect & test.'));
  const error=el('p',{class:'error',hidden:true,role:'alert'});
  const submit=el('button',{class:'primary',type:'submit',text:'Save connection'});
  form.append(error,el('div',{class:'row end'},actionButton('Cancel',closeSetup),submit));
  form.addEventListener('submit',async event=>{
    event.preventDefault();error.hidden=true;submit.disabled=true;
    try{
      const config={},secrets={};
      for(const f of spec.fields){const input=inputs[f.key];if(!input||input.disabled)continue;const value=input.type==='checkbox'?input.checked:input.value.trim();(f.secret?secrets:config)[f.key]=value;}
      const result=await api('/integrations',{provider:spec.provider,name:name.value.trim(),config,secrets});
      for(const f of spec.fields)if(f.secret && inputs[f.key])inputs[f.key].value='';
      if(draft){integrationDrafts=integrationDrafts.filter(d=>d.id!==draft.id);savePrefs();}
      closeSetup();await fetchExtra('integrations');integrationDialog(provider,result.integration);
    }catch(err){error.textContent=err.message;error.hidden=false;}finally{submit.disabled=false;}
  });root.append(form);
}
function validCron(value) {
  const fields=value.trim().split(/\s+/); const ranges=[[0,59],[0,23],[1,31],[1,12],[0,7]];
  if(fields.length!==5) return false;
  return fields.every((f,i)=>f.split(',').every(part=>{
    if(!/^(\*|\d+(-\d+)?)(\/\d+)?$/.test(part))return false;
    const [base,step]=part.split('/'); if(step!==undefined && (Number(step)<1 || Number(step)>ranges[i][1]-ranges[i][0]+1))return false;
    if(base==='*')return true;
    const [start,end=start]=base.split('-').map(Number);return start>=ranges[i][0] && end<=ranges[i][1] && end>=start;
  }));
}
function validZone(value) { try {new Intl.DateTimeFormat('en',{timeZone:value}).format();return !!value.trim();}catch(_){return false;} }
function formatDate(value, zone) {
  if(!value)return 'Not reported';
  const date=new Date(typeof value==='number'?value*1000:value);
  if(!Number.isFinite(date.getTime()))return 'Not reported';
  try{return new Intl.DateTimeFormat(undefined,{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit',timeZone:zone||undefined}).format(date);}catch(_){return date.toLocaleString();}
}
function panelScheduler() {
  const state=extras.scheduler;
  const all=[...state.items.map(item=>({...item,local:false})),...scheduleDrafts.map(item=>({...item,local:true,status:'draft'}))];
  const filtered=all.filter(item=>scheduleFilter==='all'||(scheduleFilter==='drafts'?item.local:item.status===scheduleFilter));
  const root=el('div',{class:'panel extension-panel'},panelIntro('TIME & AUTOMATION','Work that runs on your time.',
    'Review recurring objectives, their next runs, and the task conversations they create.',actionButton('Refresh',()=>fetchExtra('scheduler'),false,state.state==='loading'),actionButton('+ New schedule',()=>scheduleDialog(),true)),extraNotice('scheduler'));
  root.append(el('div',{class:'statgrid schedule-stats'},stat('Active schedules',state.state==='ready'?String(state.items.filter(x=>x.status==='active').length):'—'),stat('Paused schedules',state.state==='ready'?String(state.items.filter(x=>x.status==='paused').length):'—'),stat('Local drafts',String(scheduleDrafts.length))));
  root.append(el('div',{class:'filter-tabs','aria-label':'Schedule filters'},['all','active','paused','drafts'].map(key=>el('button',{class:scheduleFilter===key?'selected':'','aria-pressed':String(scheduleFilter===key),text:key,onclick:()=>{scheduleFilter=key;draw();}}))));
  if(!filtered.length)root.append(notice(state.state==='ready'?'No schedules in this view':'No schedules to display yet',state.state==='ready'?'Create a recurring objective. New schedules are saved paused until you enable them.':'Prepare an objective and timing. It stays a draft until Claire’s scheduler can activate it.'));
  root.append(el('div',{class:'work-list'},filtered.map(item=>el('article',{class:'work-card schedule-card'},
    el('div',{class:'work-card-top'},el('span',{class:'schedule-clock',text:'◷','aria-hidden':'true'}),el('h3',{text:item.title||'Scheduled task'}),statusPill(item.local?'Draft · not scheduled':item.status||'unknown')),
    el('p',{class:'work-summary',text:item.task||'No objective reported'}),
    el('div',{class:'schedule-details'},el('div',{},el('small',{text:'CRON'}),el('code',{text:item.cron||'Not reported'})),el('div',{},el('small',{text:'TIME ZONE'}),el('span',{text:item.timezone||'Not reported'})),el('div',{},el('small',{text:'NEXT RUN'}),el('span',{text:item.local?'Not scheduled':item.status==='paused'?'Paused':formatDate(item.next_run_at,item.timezone)}))),
    el('div',{class:'work-actions'},item.local?actionButton('Edit draft',()=>scheduleDialog(item)):actionButton('Run history',()=>scheduleDetails(item)),
      !item.local?actionButton(item.status==='active'?'Pause':'Enable',()=>scheduleAction(item,item.status==='active'?'pause':'resume')):null,
      !item.local?actionButton('Run now',()=>scheduleAction(item,'run')):null,
      !item.local?actionButton('Remove',()=>scheduleAction(item,'delete')):null,
      item.local?actionButton('Remove draft',()=>{scheduleDrafts=scheduleDrafts.filter(d=>d.id!==item.id);savePrefs();draw();}):null,
      item.local?actionButton('Save to scheduler',()=>publishSchedule(item),false,state.state!=='ready'):el('span',{class:'field-help',text:item.last_run_at?'Last run: '+formatDate(item.last_run_at,item.timezone):'No last run reported'}))
  ))));
  return root;
}
function scheduleDialog(draft=null) {
  const ready=extras.scheduler.state==='ready';
  const root=showSetup(draft?'Edit schedule draft':'Plan a recurring task',ready?'New schedules are saved paused. Enable one when you want it to run automatically.':'The scheduler is unavailable. You can save a local draft.');
  const form=el('form',{class:'setup-form'});
  const title=el('input',{id:'schedule-title',required:true,maxlength:100,value:draft?.title||'',placeholder:'Morning inbox briefing'});
  const objective=el('textarea',{id:'schedule-objective',required:true,rows:3,maxlength:6000,placeholder:'Describe the task Claire should perform and what to report.'});objective.value=draft?.task||'';
  const presets=el('select',{id:'schedule-preset'},el('option',{value:'0 9 * * *',text:'Every day at 09:00'}),el('option',{value:'0 9 * * 1-5',text:'Weekdays at 09:00'}),el('option',{value:'0 * * * *',text:'Every hour'}),el('option',{value:'custom',text:'Custom cron expression'}));
  const cron=el('input',{id:'schedule-cron',required:true,value:draft?.cron||'0 9 * * *',placeholder:'0 9 * * 1-5'});
  if(draft)presets.value=['0 9 * * *','0 9 * * 1-5','0 * * * *'].includes(draft.cron)?draft.cron:'custom';
  presets.addEventListener('change',()=>{if(presets.value!=='custom')cron.value=presets.value;});cron.addEventListener('input',()=>{presets.value='custom';});
  const timezone=el('input',{id:'schedule-timezone',required:true,value:draft?.timezone||Intl.DateTimeFormat().resolvedOptions().timeZone||'UTC'});
  const folder=el('input',{id:'schedule-folder',value:draft?.workdir||'',placeholder:'Optional project folder'});
  const error=el('p',{class:'error',role:'alert',hidden:true});
  form.append(field('Task title',title),field('Objective',objective),field('Repeat',presets),el('div',{class:'form-grid'},field('Cron expression',cron),field('Time zone',timezone)),el('p',{class:'field-help',text:'Five numeric fields: minute · hour · day of month · month · day of week. Time zone is explicit; next run is calculated by the scheduler.'}),field('Working folder',folder),error,el('div',{class:'row end'},actionButton('Cancel',closeSetup),el('button',{type:'submit',class:'primary',text:ready?'Save paused schedule':'Save draft'})));
  form.addEventListener('submit',async event=>{
    event.preventDefault();error.hidden=true;
    if(!title.value.trim()||!objective.value.trim()){error.textContent='Enter a task title and objective.';error.hidden=false;return;}
    if(!validCron(cron.value)){error.textContent='Enter a valid five-field numeric cron expression, such as 0 9 * * 1-5.';error.hidden=false;return;}
    if(!validZone(timezone.value.trim())){error.textContent='Enter a valid time zone, such as Asia/Kolkata or Europe/London.';error.hidden=false;return;}
    const item={id:draft?.id||crypto.randomUUID(),title:title.value.trim(),task:objective.value.trim(),cron:cron.value.trim(),timezone:timezone.value.trim(),workdir:folder.value.trim()};
    if(ready){
      const submit=form.querySelector('[type=submit]');submit.disabled=true;
      try{await publishSchedule(item,true);closeSetup();}catch(err){error.textContent=err.message;error.hidden=false;}finally{submit.disabled=false;}return;
    }
    scheduleDrafts=[...scheduleDrafts.filter(d=>d.id!==item.id),item];const saved=savePrefs();closeSetup();draw();if(saved)toast('Draft saved. No task has been scheduled.');
  });root.append(form);
}
async function publishSchedule(item, propagate=false){
  try{
    await api('/schedules',{title:item.title,task:item.task,cron:item.cron,timezone:item.timezone,workdir:item.workdir,active:false});
    scheduleDrafts=scheduleDrafts.filter(d=>d.id!==item.id);savePrefs();await fetchExtra('scheduler');toast('Schedule saved paused. Enable it when ready.');
  }catch(err){if(propagate)throw err;toast(err.message);}
}
async function scheduleAction(item,action){
  if(action==='delete'&&!confirm('Remove schedule “'+item.title+'”? Existing task conversations will remain.'))return;
  try{
    const path='/schedules/'+encodeURIComponent(item.id);
    const result=action==='delete'?await api(path,undefined,'DELETE'):await api(path+'/'+action,{});
    await fetchExtra('scheduler');
    if(action==='run')toast(result.started?'Task started. Open Run history to follow it.':'This schedule already has a task in progress.');
  }catch(err){toast(err.message);}
}
async function scheduleDetails(item) {
  await fetchExtra('scheduler');
  item=extras.scheduler.items.find(s=>s.id===item.id)||item;
  const root=showSetup(item.title||'Schedule history','Run times are shown in '+(item.timezone||'the local time zone')+'.');
  const runs=Array.isArray(item.runs)?item.runs:[];
  root.append(runs.length?el('div',{class:'run-list'},runs.map(run=>el('div',{class:'run-row'},el('span',{text:formatDate(run.started_at,item.timezone)}),statusPill(run.status||'unknown'),run.thread_id?actionButton('Open task',()=>{closeSetup();show({kind:'thread',id:run.thread_id});}):el('span',{class:'dim',text:'No task linked'})))):notice('No run history reported','The scheduler has not returned any runs for this job.'));
  root.append(el('div',{class:'row end'},actionButton('Done',closeSetup)));
}
const controllerState={data:null,error:'',loading:false};
async function loadController(){
  if(controllerState.loading)return;
  controllerState.loading=true;controllerState.error='';draw();
  try{controllerState.data=await api('/config');}catch(err){controllerState.error=err.message;}
  controllerState.loading=false;draw();
}
function panelController(){
  const data=controllerState.data;
  const root=el('div',{class:'panel extension-panel'},panelIntro('ORCHESTRATION','One controller. Separate task contexts.',
    'Claire keeps the main conversation, delegates objectives, and brings task reports back here.',actionButton('Refresh',loadController,false,controllerState.loading),actionButton('Back to controller',()=>show({kind:'dashboard',name:'overview'}),true)));
  root.append(notice('What this page controls','This page shows the model roles and limits actually in use. To change them, edit config.py and restart Claire. Opening this page does not create another agent or change the model.'));
  if(controllerState.error)root.append(notice('Could not read runtime configuration',controllerState.error,'error'));
  if(!data){root.append(notice('Loading model configuration','Reading the running service…'));return root;}
  root.append(el('div',{class:'integration-grid'},Object.entries(data.roles).map(([role,info])=>el('article',{class:'integration-card'},
    el('div',{class:'eyebrow',text:role==='brain'?'CONTROLLER + TASKS':role.toUpperCase()}),el('h3',{text:info.model||'Unavailable'}),
    el('p',{text:role==='brain'?'The main conversation and task workers use this model with separate histories.':info.own_model?'A dedicated model for this supporting role.':'Shares the brain model; no separate model is loaded.'}),statusPill(info.live?'live':'unavailable'),el('small',{class:'dim',text:fmtTokens(info.context)+' tokens per context'})))));
  root.append(el('div',{class:'statgrid'},stat('Task workers',String(data.task_workers)),stat('Brain slots',String(data.brain_slots)),stat('Parallel scouts',String(data.scout_parallel)),stat('Steps per turn',String(data.max_steps))),
    notice('Your conversation is retained','Hide earlier messages only changes this browser’s view. Task chats and Claire’s saved conversation stay available.'),
    actionButton('Inspect model reports ↗',()=>show({kind:'panel',name:'models'})));
  if(controllerDraft)root.append(notice('An earlier setup draft was never applied','The running configuration above is authoritative.'),actionButton('Discard old draft',()=>{controllerDraft=null;savePrefs();draw();}));
  return root;
}



/* ---- skills ---------------------------------------------------------------
 * Instructions for one kind of work. You write them; Claire loads the ones a
 * task needs. Stored as markdown files in the vault's skills folder. */

const skillState = { items: [], open: null, error: "", loaded: false };

async function loadSkills() {
  try {
    skillState.items = (await api("/skills")).skills;
    skillState.error = "";
    if (skillState.open && !skillState.items.some((s) => s.name === skillState.open.name)) skillState.open = null;
  } catch (err) { skillState.error = err.message; }
  skillState.loaded = true;
  draw();
}

async function openSkill(name) {
  try { skillState.open = (await api("/skills/" + encodeURIComponent(name))).skill; }
  catch (err) { skillState.error = err.message; }
  draw();
}

async function saveSkill(body, errorNode) {
  try {
    const { skill } = await api("/skills", body);
    closeSetup();
    toast(`Skill “${skill.name}” saved`);
    await loadSkills();
    await openSkill(skill.name);
  } catch (err) { errorNode.textContent = err.message; errorNode.hidden = false; }
}

function skillDialog(existing = null) {
  const content = showSetup(existing ? `Edit “${existing.name}”` : "New skill",
    "Write the instructions the way you would brief a capable colleague. Claire reads the description to decide whether a task needs the skill.");
  const name = el("input", { maxlength: "41", placeholder: "code-review", value: existing ? existing.name : null, readonly: !!existing });
  const description = el("input", { maxlength: "300", placeholder: "What kind of work this covers, in one line", value: existing ? existing.description : null });
  const text = el("textarea", { rows: "14", placeholder: "# How to do it\n\nSteps, standards, what to check before finishing…" });
  if (existing) text.value = existing.text;
  const always = el("input", { type: "checkbox", checked: existing && existing.always });
  const error = el("p", { class: "error", hidden: true });
  const form = el("form", { class: "setup-form" },
    field("Name (lowercase, hyphens)", name), field("Description", description), field("Instructions", text),
    el("label", { class: "check-line" }, always, "Load into every task, without waiting for Claire to choose it"), error,
    el("div", { class: "row end" }, actionButton("Cancel", closeSetup),
       el("button", { type: "submit", class: "primary small", text: existing ? "Save changes" : "Create skill" })));
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    saveSkill({ name: name.value.trim(), description: description.value.trim(), text: text.value,
                always: always.checked }, error);
  });
  content.append(form);
  (existing ? text : name).focus();
}

function uploadSkill() {
  const picker = el("input", { type: "file", accept: ".md,.markdown,.txt,text/markdown,text/plain" });
  picker.addEventListener("change", async () => {
    const file = picker.files && picker.files[0];
    if (!file) return;
    if (file.size > 200000) { skillState.error = "That file is too large to be a skill (the limit is 40,000 characters)."; draw(); return; }
    try {
      const { skill } = await api("/skills", { filename: file.name, text: await file.text() });
      toast(`Skill “${skill.name}” uploaded`);
      await loadSkills();
      await openSkill(skill.name);
    } catch (err) { skillState.error = err.message; draw(); }
  });
  picker.click();
}

async function removeSkill(skill) {
  if (!confirm(`Remove the skill “${skill.name}”? Tasks will no longer be able to load it.`)) return;
  try {
    await api("/skills/" + encodeURIComponent(skill.name), undefined, "DELETE");
    skillState.open = null;
    toast(`Skill “${skill.name}” removed`);
  } catch (err) { skillState.error = err.message; }
  loadSkills();
}

function panelSkills() {
  const root = el("div", { class: "panel extension-panel" },
    panelIntro("WHAT CLAIRE KNOWS HOW TO DO", "Skills.",
      "A skill is a set of instructions for one kind of work. Claire sees this list, and a task loads the skills it needs. Only you can write them.",
      actionButton("Upload .md", uploadSkill), actionButton("+ New skill", () => skillDialog(), true)));
  if (skillState.error) root.append(notice("Something went wrong", skillState.error, "error"));
  if (!skillState.loaded) { root.append(notice("Loading skills", "One moment.")); return root; }
  const list = el("div", { class: "tree skill-list" }, skillState.items.map((s) =>
    el("button", { class: skillState.open && skillState.open.name === s.name ? "active" : "", type: "button", onclick: () => openSkill(s.name) },
      el("span", { class: "skill-name", text: s.name }),
      el("span", { class: "skill-source " + s.source, text: (s.source === "builtin" ? "built-in" : "yours") + (s.always ? " · always" : "") }),
      el("span", { class: "skill-description", text: s.description }))));
  const open = skillState.open;
  const detail = open
    ? el("div", { class: "notebox" },
        el("div", { class: "skill-head" },
           el("div", {}, el("div", { class: "path", text: `${open.source === "builtin" ? "built-in" : "skills/" + open.name + ".md"} · ${open.size.toLocaleString()} characters` }),
              el("p", { class: "dim", text: open.description })),
           el("div", { class: "page-actions" },
              actionButton(open.source === "builtin" ? "Make my own version" : "Edit", () => skillDialog(open)),
              open.source === "user" && actionButton("Remove", () => removeSkill(open)))),
        prose(open.text))
    : el("div", { class: "notebox dim", text: "Pick a skill to read it. Upload a markdown file, or write one here; it is saved to the skills folder in Claire’s vault, where you can also edit it by hand." });
  root.append(el("div", { class: "split" }, list, detail));
  return root;
}

/* ---- models ------------------------------------------------------------------
 * The model Claire thinks with. Add one by choosing its file (or a model in
 * LM Studio), use it, and restart her onto it. Kept in models.json in the
 * state folder; config.py's own models are listed too. */

const modelState = { data: null, error: "", loading: false, restarting: false };
const SERVER_NAMES = { llama: "llama.cpp", lmstudio: "LM Studio", splash: "Splash" };

async function loadModels() {
  if (modelState.loading) return;
  modelState.loading = true;
  try { modelState.data = await api("/models"); modelState.error = ""; }
  catch (err) { modelState.error = err.status === 404 ? STALE_SERVICE : err.message; }
  modelState.loading = false;
  draw();
}

const gib = (bytes) => `${(bytes / 1073741824).toFixed(1)} GB`;

async function useModel(name) {
  try { modelState.data = await api("/models/use", { name }); toast("Saved. Restart Claire to switch to it."); }
  catch (err) { toast(`Could not choose that model: ${err.message}`); }
  draw();
}

async function removeModel(m) {
  if (!confirm(`Remove “${m.label}” from Claire's models? The model file itself is not touched.`)) return;
  try { modelState.data = await api(`/models/${encodeURIComponent(m.name)}`, undefined, "DELETE"); }
  catch (err) { toast(`Could not remove it: ${err.message}`); }
  draw();
}

async function restartClaire() {
  if (!confirm("Restart Claire now?\n\nThe model she is running is unloaded to free its memory, and she starts again with the model you chose. This page reconnects by itself; the first reply may take a little longer while the model loads.")) return;
  try {
    await api("/models/restart", {});
    modelState.restarting = true;
    toast("Restarting Claire…");
    // The service goes away and comes back; check until it does.
    const started = Date.now();
    const wait = async () => {
      try {
        const status = await api("/status");
        if (status.model_choice && status.model_choice.running === status.model_choice.selected) {
          modelState.restarting = false; S.status = status; await loadModels();
          toast("Claire is back, on the model you chose."); return;
        }
      } catch (_) { /* still restarting */ }
      if (Date.now() - started < 120000) setTimeout(wait, 1500);
      else { modelState.restarting = false; draw(); toast("Claire has not come back yet. Check with: claire2 status"); }
    };
    setTimeout(wait, 2500);
  } catch (err) { toast(`Could not restart: ${err.message}`); }
  draw();
}

function modelDialog() {
  const data = modelState.data || {};
  const root = showSetup("Add a model", "Point Claire at a model on this computer. Small models answer faster; large ones reason better. A model needs roughly its file size in free memory, plus room for its context.");
  const server = el("select", {}, el("option", { value: "llama", text: "llama.cpp — a local .gguf file" }),
                                   el("option", { value: "lmstudio", text: "LM Studio — a model in its library" }));
  const file = el("input", { placeholder: "~/models/Qwen3-8B-Q4_K_M.gguf", spellcheck: "false", autocomplete: "off" });
  const browse = el("button", { type: "button", class: "ghost", text: S.status?.platform === "win32" ? "Type model path" : "Browse…", disabled: S.status?.platform === "win32", title: S.status?.platform === "win32" ? "Paste the .gguf path or choose LM Studio" : "Choose the .gguf file in the Finder" });
  const about = el("p", { class: "field-help", text: "" });
  const studioPick = el("select", {}, el("option", { value: "", text: "Checking LM Studio…" }));
  const studioKey = el("input", { placeholder: "publisher/model-name, as LM Studio lists it", spellcheck: "false" });
  const label = el("input", { maxlength: 60, placeholder: "A name you will recognise" });
  const context = el("input", { type: "number", min: 2048, step: 1024, value: 32768 });
  const contextHelp = el("p", { class: "field-help", text: "Tokens the model can read at once. 32,768 is a good start; more uses more memory." });
  const useNow = el("input", { type: "checkbox", checked: true });
  const error = el("p", { class: "error", role: "alert", hidden: true });
  const submit = el("button", { type: "submit", class: "primary", text: "Add model" });
  const llamaPart = el("div", {}, el("label", {}, "Model file", el("div", { class: "folder-row" }, file, browse)), about,
    data.llama_server === false ? notice("llama.cpp is not installed", S.status?.platform === "win32" ? "Choose LM Studio above to use its downloaded models." : "Claire starts models with llama-server. Install it once in Terminal: brew install llama.cpp", "error") : null);
  const studioPart = el("div", { hidden: true }, field("Model in LM Studio", studioPick), field("Or its key", studioKey),
    el("p", { class: "field-help", text: "LM Studio must be open; Claire loads the model through it." }));
  const describe = (info) => {
    about.textContent = `${info.name || "Model"} · ${info.architecture || "unknown architecture"} · ${gib(info.size_bytes)}`
      + (info.context_length ? ` · trained for ${info.context_length.toLocaleString()} tokens` : "");
    if (!label.value.trim() && info.name) label.value = info.name;
    if (info.suggested_context) context.value = info.suggested_context;
    if (info.context_length) {
      context.max = info.context_length;
      contextHelp.textContent = `Up to ${info.context_length.toLocaleString()} tokens for this model. 32,768 is a good start; more uses more memory.`;
    }
  };
  const inspect = async () => {
    if (!file.value.trim()) return;
    try { describe(await api("/models/inspect", { path: file.value.trim() })); error.hidden = true; }
    catch (err) { about.textContent = ""; error.textContent = err.message; error.hidden = false; }
  };
  file.addEventListener("change", inspect);
  browse.addEventListener("click", async () => {
    browse.disabled = true; browse.textContent = "Choose in the Finder…"; error.hidden = true;
    try {
      const info = await api("/models/browse", {});
      if (info.path) { file.value = tilde(info.path); describe(info); }
    } catch (err) { error.textContent = err.message; error.hidden = false; }
    finally { browse.disabled = false; browse.textContent = "Browse…"; }
  });
  server.addEventListener("change", async () => {
    const studio = server.value === "lmstudio";
    llamaPart.hidden = studio; studioPart.hidden = !studio;
    if (!studio) return;
    try {
      const library = await api("/models/lmstudio");
      studioPick.replaceChildren(el("option", { value: "", text: library.running ? (library.models.length ? "Choose a model" : "LM Studio has no models yet") : "LM Studio is not running" }),
        ...library.models.map((m) => el("option", { value: m.key, text: m.name })));
    } catch (_) { studioPick.replaceChildren(el("option", { value: "", text: "LM Studio is not running" })); }
  });
  studioPick.addEventListener("change", () => { if (studioPick.value) { studioKey.value = studioPick.value; if (!label.value.trim()) label.value = studioPick.selectedOptions[0].textContent; } });
  const form = el("form", { class: "setup-form" }, field("Runs with", server), llamaPart, studioPart,
    field("Name", label), field("Context (tokens)", context), contextHelp,
    el("label", { class: "check-line" }, useNow, "Use this model (after a restart)"),
    error, el("div", { class: "row end" }, actionButton("Cancel", closeSetup), submit));
  form.addEventListener("submit", async (event) => {
    event.preventDefault(); error.hidden = true; submit.disabled = true;
    try {
      const made = await api("/models", { server: server.value, path: file.value.trim(), model: studioKey.value.trim(),
                                          label: label.value.trim(), context: Number(context.value) || 0 });
      modelState.data = made;
      if (useNow.checked) modelState.data = await api("/models/use", { name: made.model.name });
      closeSetup(); toast(useNow.checked ? "Added. Restart Claire to switch to it." : "Added.");
    } catch (err) { error.textContent = err.message; error.hidden = false; }
    finally { submit.disabled = false; draw(); }
  });
  root.append(form);
  if (S.status?.platform === "win32") {
    server.value = "lmstudio";
    server.dispatchEvent(new Event("change"));
    studioKey.focus();
  } else file.focus();
}

function panelModelSetup() {
  const data = modelState.data;
  const root = el("div", { class: "panel extension-panel" },
    panelIntro("", "Models", "The model Claire thinks with. Add one by choosing its file, use it, and restart Claire onto it.",
      actionButton("Refresh", loadModels, false, modelState.loading), actionButton("Add model", modelDialog, true)));
  if (modelState.error) root.append(notice("Could not read the models", modelState.error, "error"));
  if (!data) { if (!modelState.error) root.append(notice("Loading models", "Reading the local model setup…")); return root; }
  const byName = Object.fromEntries(data.models.map((m) => [m.name, m]));
  if (modelState.restarting) {
    root.append(notice("Restarting Claire", "She will be back in a moment, on the model you chose."));
  } else if (data.selected && data.running && data.selected !== data.running) {
    root.append(el("div", { class: "service-notice switch-notice" }, el("span", { class: "notice-symbol", "aria-hidden": "true", text: "↻" }),
      el("div", {}, el("strong", { text: "Restart to switch models" }),
        el("p", { text: `Claire is running ${byName[data.running]?.label || data.running}. She will use ${byName[data.selected]?.label || data.selected} after a restart.` })),
      actionButton("Restart Claire now", restartClaire, true)));
  }
  if (!data.models.length) root.append(notice("No models yet", "Add one: choose a .gguf file, or a model from LM Studio."));
  root.append(el("div", { class: "work-list" }, data.models.map((m) => el("article", { class: "work-card model-card" + (m.running ? " running" : "") },
    el("div", { class: "work-card-top" },
      el("h3", { text: m.label }),
      el("span", { class: "model-server", text: SERVER_NAMES[m.server] || m.server }),
      m.running ? statusPill("in use") : m.selected ? statusPill("selected") : null),
    el("p", { class: "model-where", text: m.where || "—" }),
    el("div", { class: "work-meta" },
      el("span", { text: m.context ? `${m.context.toLocaleString()}-token context` : "the model's own context" }),
      el("span", { text: m.source === "added" ? "added in the app" : "from config.py" }),
      m.port ? el("span", { text: `port ${m.port}` }) : null),
    m.problem ? el("p", { class: "model-problem", text: m.problem }) : null,
    el("div", { class: "work-actions" },
      !m.selected ? actionButton("Use this model", () => useModel(m.name), false, !!m.problem) : null,
      m.source === "added" ? actionButton("Remove", () => removeModel(m)) : null)))));
  root.append(notice("Where this is kept", "Models you add, and which one to use, are saved in models.json in Claire's state folder (~/.claire). That choice wins over config.py. From a terminal: claire2 models."));
  return root;
}

start();
