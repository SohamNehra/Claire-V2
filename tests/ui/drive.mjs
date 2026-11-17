// Drives headless Chrome over its debugging port to exercise the app, and
// always closes the browser: on success, on error and on timeout. The browser
// has its own profile folder; only processes carrying that folder are killed.
import { spawn, execSync } from "node:child_process";
import { mkdtempSync, readFileSync, writeFileSync, rmSync, existsSync } from "node:fs";
import { tmpdir, homedir } from "node:os";
import { join } from "node:path";

const CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const [url, outDir] = process.argv.slice(2);
const profile = mkdtempSync(join(tmpdir(), "claire-drive-"));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let chrome = null, ws = null, nextId = 1;
const waiting = new Map(), problems = [];

function leftovers() {
  try { return execSync(`pgrep -f ${profile}`, { encoding: "utf8" }).split("\n").filter(Boolean); }
  catch { return []; }
}
function closeBrowser() {
  try { if (ws) ws.close(); } catch {}
  if (chrome && chrome.pid) { try { process.kill(-chrome.pid, "SIGKILL"); } catch {} }
  for (const pid of leftovers()) { try { process.kill(Number(pid), "SIGKILL"); } catch {} }
  execSync("sleep 0.4");
  const left = leftovers().length;
  rmSync(profile, { recursive: true, force: true });
  console.log(`browser closed; processes left: ${left}`);
}
const watchdog = setTimeout(() => { console.log("TIMED OUT"); closeBrowser(); process.exit(2); }, 150000);

function send(method, params = {}) {
  const id = nextId++;
  ws.send(JSON.stringify({ id, method, params }));
  return new Promise((resolve, reject) => waiting.set(id, { resolve, reject }));
}
async function js(expression) {
  const r = await send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
  if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description || r.exceptionDetails.text);
  return r.result.value;
}
async function shot(name) {
  const r = await send("Page.captureScreenshot", { format: "png" });
  writeFileSync(join(outDir, name + ".png"), Buffer.from(r.data, "base64"));
}
async function size(width, height, mobile = false) {
  await send("Emulation.setDeviceMetricsOverride", { width, height, deviceScaleFactor: 1, mobile });
}
const scheme = (value) => send("Emulation.setEmulatedMedia", { features: [{ name: "prefers-color-scheme", value }] });
const click = (sel) => js(`(() => { const n = document.querySelector(${JSON.stringify(sel)}); if (!n) throw new Error("no element: " + ${JSON.stringify(sel)}); n.click(); })()`);
const DEMO = "[...document.querySelectorAll('#side-threads .thread-row.chat')].find(r => r.textContent.includes('hey claire'))";
const openDemo = () => js(DEMO + ".click()");
function check(label, ok, detail = "") {
  console.log(`${ok ? "PASS" : "FAIL"}  ${label}${detail ? "  -- " + detail : ""}`);
  if (!ok) problems.push(label);
}
// No hidden horizontal overflow: nothing wider than the window.
const overflow = () => js(`document.documentElement.scrollWidth - window.innerWidth`);

try {
  chrome = spawn(CHROME, ["--headless=new", `--user-data-dir=${profile}`, "--remote-debugging-port=0",
    "--no-first-run", "--no-default-browser-check", "--disable-extensions", "--hide-scrollbars", "about:blank"],
    { detached: true, stdio: "ignore" });
  const portFile = join(profile, "DevToolsActivePort");
  for (let i = 0; i < 100 && !existsSync(portFile); i++) await sleep(100);
  const port = readFileSync(portFile, "utf8").split("\n")[0];
  const pages = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
  ws = new WebSocket(pages.find((p) => p.type === "page").webSocketDebuggerUrl);
  await new Promise((r, j) => { ws.onopen = r; ws.onerror = j; });
  ws.onmessage = (m) => {
    const msg = JSON.parse(m.data);
    if (msg.id && waiting.has(msg.id)) {
      const w = waiting.get(msg.id); waiting.delete(msg.id);
      msg.error ? w.reject(new Error(msg.error.message)) : w.resolve(msg.result);
    } else if (msg.method === "Runtime.exceptionThrown") {
      problems.push("js exception"); console.log("JS EXCEPTION:", msg.params.exceptionDetails.exception?.description);
    } else if (msg.method === "Log.entryAdded" && ["error", "warning"].includes(msg.params.entry.level)) {
      console.log(`BROWSER ${msg.params.entry.level}:`, msg.params.entry.text, msg.params.entry.url || "");
      if (msg.params.entry.level === "error") problems.push("browser error");
    }
  };
  for (const d of ["Page", "Runtime", "Log", "Network"]) await send(d + ".enable");

  // -- sign in, home, dark, desktop
  await size(1440, 900); await scheme("dark");
  await send("Page.navigate", { url });
  await sleep(2500);
  check("the token is gone from the address after sign-in", !(await js("location.href")).includes("token"));
  check("the sign-in cookie is not readable by scripts", (await js("document.cookie")) === "");
  check("overview is the default landing page", (await js("location.hash")) === "#overview");
  check("overview shows actual active tasks and approvals",
        await js("document.querySelector('#metric-active').textContent === '1' && document.querySelector('#metric-total').textContent === '2' && document.querySelector('#approvals .card') !== null"),
        `active ${await js("document.querySelector('#metric-active').textContent")}, total ${await js("document.querySelector('#metric-total').textContent")}, approval cards ${await js("document.querySelectorAll('#approvals .card').length")}`);
  check("the core reflects pending authorization", await js("document.querySelector('.dashboard').dataset.state === 'waiting'"));
  check("active work names the project each task belongs to",
        await js("[...document.querySelectorAll('#task-board .task-project')].map(n => n.textContent).join() === 'Uploader'"),
        await js("[...document.querySelectorAll('#task-board .task-row')].map(n => n.textContent).join(' | ')"));
  check("the command center lists projects", await js("[...document.querySelectorAll('#project-board .project-card')].some(c => c.textContent.includes('Uploader'))"));
  await shot("0-command-center");
  check("there is no main chat, only chats", await js("!document.querySelector('.thread-row.main') && !document.querySelector('[data-view=\"thread:home\"]')"));
  await openDemo(); await sleep(500);
  check("the chat shows the conversation", (await js("document.querySelectorAll('.msg').length")) >= 5,
        `${await js("document.querySelectorAll('.msg.you').length")} from sir, ${await js("document.querySelectorAll('.msg.claire').length")} from claire`);
  check("the answer renders a list, bold and a code block",
        await js("!!document.querySelector('.prose ul li strong') && !!document.querySelector('.prose pre code')"));
  check("the pending approval is on screen with its warning",
        await js("document.querySelector('#approvals .what')?.textContent === 'rm -rf build' && document.querySelector('#approvals .flag')?.textContent === 'DANGEROUS'"));
  check("the tab title counts what is waiting", (await js("document.title")) === "(1) Claire");
  check("the sidebar files both tasks in their project folder",
        await js("document.querySelectorAll('#side-threads .project').length === 1 && document.querySelector('.project .project-name .label').textContent === 'Uploader' && document.querySelectorAll('.project .thread-row.task').length === 2"),
        `${await js("document.querySelectorAll('#side-threads .project').length")} projects, ${await js("document.querySelectorAll('.project .thread-row.task').length")} tasks inside`);
  check("no sideways overflow at 1440", (await overflow()) <= 0);
  await shot("1-home-dark");

  // -- a task's trace
  await js(`[...document.querySelectorAll('#side-threads .thread-row.task')].find(n => n.textContent.includes('retry')).click()`);
  await sleep(900);
  check("the task view shows its brief, steps and report",
        await js("!!document.querySelector('.brief') && document.querySelectorAll('.step').length === 5 && !!document.querySelector('.msg.claire')"),
        `${await js("document.querySelectorAll('.step').length")} steps`);
  check("the failed step is marked", (await js("document.querySelectorAll('.step.failed').length")) === 1);
  check("the address is linkable", (await js("location.hash")).startsWith("#task/"));
  check("the task's header leads back to its project", (await js("document.querySelector('#view-category .crumb-link')?.textContent")) === "Uploader");
  await shot("2-task-dark");

  // -- panels
  await click('[data-view="panel:tools"]'); await sleep(700);
  check("tools panel lists tools and flags the failing one",
        await js("document.querySelectorAll('.table tbody tr').length > 10 && !!document.querySelector('.table .bad')"));
  await shot("3-tools-dark");
  await click('[data-view="panel:memory"]'); await sleep(900);
  check("Memory opens on the shared index, one line per memory with its file",
        await js("document.querySelector('.notebox .path')?.textContent === 'MEMORY.md' && /→ learned\\/python3-only\\.md/.test(document.querySelector('.notebox').textContent) && document.querySelector('.tree .folder')?.textContent === 'shared index'"),
        await js("document.querySelector('.notebox .path')?.textContent"));
  await click('[data-view="panel:inbox"]'); await sleep(500);
  await js("document.querySelector('.table tr.click').click()"); await sleep(700);
  check("opening an inbox note shows it in memory",
        await js("document.querySelector('.notebox .path')?.textContent === 'inbox/tool-web-search.md' && document.querySelector('.notebox').textContent.includes('layout this tool cannot read')"));
  await shot("4-memory-dark");
  await click('[data-view="panel:bench"]'); await sleep(900);
  check("models panel shows the report cards", (await js("document.querySelectorAll('.table tbody tr').length")) >= 1);
  await shot("5-models-dark");

  // -- light, then narrow
  await scheme("light"); await openDemo(); await sleep(700);
  await shot("6-home-light");
  await scheme("dark"); await size(390, 844, true); await sleep(600);
  check("no sideways overflow at 390", (await overflow()) <= 0, `overflow ${await overflow()}px`);
  check("the sidebar is off-screen until opened", await js("document.getElementById('side').getBoundingClientRect().right <= 0"));
  await shot("7-home-phone");
  await click("#side-open"); await sleep(400);
  await shot("8-menu-phone");
  await click("#side-close"); await size(1440, 900); await sleep(400);

  // -- act: deny the delete, and watch the report come back live
  await js("document.querySelector('#approvals .no').click()");
  await sleep(3500);
  check("denying clears the request and the tab title", (await js("document.title")) === "Claire"
        && (await js("document.querySelectorAll('#approvals .card').length")) === 0);
  check("the task's report reached home, live, and she relayed it",
        await js("[...document.querySelectorAll('.msg.claire')].some(n => n.textContent.includes('left it alone'))"));

  // -- type a message that is markup, and make sure it stays text
  await js(`(() => { const i = document.getElementById('input'); i.value = '<img src=x onerror="document.title=\\'pwned\\'"> hi'; i.dispatchEvent(new Event('input')); document.getElementById('composer').requestSubmit(); })()`);
  await sleep(3500);
  check("markup from sir and from the model is shown as text, never run",
        await js("document.title === 'Claire' && !document.querySelector('#view img') && !document.querySelector('#view script') && document.querySelector('#view').textContent.includes('<script>document.title')"));
  check("the composer cleared and she answered", await js("document.getElementById('input').value === '' && [...document.querySelectorAll('.msg.claire')].some(n => n.textContent.includes('not running it'))"));
  await shot("9-after-actions");

  // -- the stylesheet and script cannot be fetched without signing in
  // -- chats: one main conversation, and a chat for every task it opened
  await size(1440, 900); await scheme("dark");
  await openDemo(); await sleep(900);
  check("the sidebar lists the chat and each task chat",
        await js("document.querySelectorAll('#side-threads .thread-row').length === 3 && " + DEMO + " !== undefined"),
        `${await js("document.querySelectorAll('#side-threads .thread-row').length")} rows`);
  check("where Claire started a task, the main chat shows a card for the new chat",
        (await js("document.querySelectorAll('.chat-card.task').length")) === 2);
  check("a finished task's report arrives as a card",
        await js("!!document.querySelector('.chat-card.report .card-title')"));
  check("the steps she took are folded away in the main chat",
        await js("[...document.querySelectorAll('.chat-scroll details.work')].every(d => !d.open)"));
  await js("document.querySelector('.chat-card.task').click()"); await sleep(900);
  check("the card opens that task's own chat",
        await js("location.hash.startsWith('#task/') && !!document.querySelector('.brief') && document.querySelector('.brief .who').textContent === 'Brief from Claire'"));
  check("the open chat is marked in the list",
        await js("document.querySelector('.thread-row.active .thread-title').textContent.includes('retry')"));
  await shot("10-task-chat");
  await openDemo(); await sleep(600);
  const demoHash = await js("location.hash");
  check("and the list leads back to the chat", demoHash.startsWith("#chat/"));

  check("any chat can be cleared, moved into a project, or deleted",
        await js("!!document.querySelector('#clear-chat') && ['Delete chat', 'Add to project…'].every(t => [...document.querySelectorAll('#head-tools button')].some(b => b.textContent === t))"));
  // -- clicks land and scrolling holds while Claire is streaming (redraws every frame)
  await js("window.__spam = setInterval(() => draw(), 10)");
  await js("document.querySelector('#side-threads .thread-row.task').click()"); await sleep(600);
  const row = JSON.parse(await js("JSON.stringify(" + DEMO + ".getBoundingClientRect())"));
  const at = { x: Math.round(row.x + row.width / 2), y: Math.round(row.y + row.height / 2), button: "left", clickCount: 1 };
  await send("Input.dispatchMouseEvent", { type: "mousePressed", ...at });
  await sleep(250);                                         // a slow, human press while redraws keep coming
  await send("Input.dispatchMouseEvent", { type: "mouseReleased", ...at });
  await sleep(500);
  check("a click lands even while the page is redrawing constantly", (await js("location.hash")) === demoHash);
  await js("clearInterval(window.__spam)");

  // -- a second chat with Claire
  await js("S.status.power={state:'asleep'};draw()"); await sleep(250);
  check('Sleeping models are shown as asleep while the service stays connected',
    await js("document.querySelector('#engine-state').textContent==='Asleep · wakes on request' && document.querySelector('#connection-label').textContent==='Service connected'"));
  await js("S.status.power={state:'awake'};draw()");
  await click("#new-chat"); await sleep(900);
  const chatId = await js("location.hash.startsWith('#chat/') ? decodeURIComponent(location.hash.slice(6)) : ''");
  check("+ New chat opens a fresh, empty chat of its own", !!chatId
        && await js("document.querySelector('.thread-row.active.chat .thread-title').textContent === 'New chat' && document.querySelectorAll('.msg').length === 0 && document.querySelectorAll('#side-threads .thread-row').length === 4"),
        chatId);
  check("the earlier chat is still there, untouched", await js(DEMO + " !== undefined"));
  await shot("10b-new-chat");
  // Previous chats/tasks can be hidden without deleting them or the open chat.
  await js("fetch('/threads',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify({kind:'chat'})}).then(r=>r.json()).then(r=>{localStorage.setItem('claire.ui.previousChat',r.thread.id);return refreshThreads();})");
  await sleep(500);
  await click('#side-threads [data-history-kind="chats"]'); await sleep(350);
  check("hide previous chats keeps the open chat and active work",
    await js("document.querySelectorAll('#side-threads .thread-row').length===3 && !!document.querySelector('.thread-row.active.chat')"));
  await click('#side-threads [data-history-kind="tasks"]'); await sleep(350);
  check("hide previous tasks removes finished rows without deleting threads",
    await js("document.querySelectorAll('#side-threads .thread-row').length===1 && [...S.threads.values()].filter(t=>t.kind==='task').length===2"));
  await shot("10c-hidden-history");
  await send('Page.reload'); await sleep(1800);
  check("chat and task visibility preferences survive reload",
    await js("document.querySelectorAll('#side-threads .thread-row').length===1 && [...document.querySelectorAll('#side-threads .history-toggle')].every(b=>b.getAttribute('aria-pressed')==='true')"));
  await click('#side-threads [data-history-kind="tasks"]');
  await click('#side-threads [data-history-kind="chats"]'); await sleep(350);
  check("show previous restores all conversations and tasks",
    await js("document.querySelectorAll('#side-threads .thread-row').length===5"));
  await js("fetch('/threads/'+localStorage.getItem('claire.ui.previousChat'),{method:'DELETE',credentials:'same-origin'})");
  await sleep(500);

  await js(`fetch('/threads/${chatId}', { method: 'DELETE', credentials: 'same-origin' }).then(r => { window.__deleted = r.status; })`); await sleep(1200);
  check("deleting it returns to the command center", (await js("location.hash")) === "#overview"
        && (await js("document.querySelectorAll('#side-threads .thread-row').length")) === 3,
        `HTTP ${await js("window.__deleted")}, at ${await js("location.hash")}, ${await js("document.querySelectorAll('#side-threads .thread-row').length")} rows`);

  // -- projects: a folder of chats and tasks with a memory of its own
  await js("document.querySelector('.project .project-name').click()"); await sleep(1200);
  check("a project page shows its PROJECT.md and what was last done there",
        await js("location.hash.startsWith('#project/') && document.querySelector('.memory-card').textContent.includes('python3 -m unittest -q') && document.querySelectorAll('.log-entry').length >= 2 && document.querySelectorAll('.project-page .list-row').length === 2"),
        `${await js("document.querySelectorAll('.log-entry').length")} log entries, ${await js("document.querySelectorAll('.project-page .list-row').length")} rows`);
  await shot("12-project");
  await js("document.querySelector('.project .project-add').click()"); await sleep(1000);
  const projectChat = await js("location.hash.startsWith('#chat/') ? decodeURIComponent(location.hash.slice(6)) : ''");
  check("a chat started in a project is filed there and says so",
        !!projectChat && await js("document.querySelector('#view-category .crumb-link')?.textContent === 'Uploader' && document.querySelector('#composer-scope').textContent === 'Uploader' && document.querySelectorAll('.project .thread-row.chat').length === 1"));
  const filed = await js(`fetch('/threads/${projectChat}',{credentials:'same-origin'}).then(r=>r.json()).then(r=>r.thread.project)`);
  check("the service files the chat under the project", !!filed);
  await js("[...document.querySelectorAll('#head-tools button')].find(b => b.textContent === 'Move…').click()"); await sleep(400);
  await js("[...document.querySelectorAll('.move-option')].find(b => b.textContent.startsWith('No project')).click()"); await sleep(900);
  check("a chat can be moved out of its project",
        (await js(`fetch('/threads/${projectChat}',{credentials:'same-origin'}).then(r=>r.json()).then(r=>r.thread.project)`)) === ""
        && await js("document.querySelectorAll('.project .thread-row.chat').length === 0"));
  await js(`fetch('/threads/${projectChat}', { method: 'DELETE', credentials: 'same-origin' })`); await sleep(700);
  await js("document.querySelector('#side-threads .side-icon[aria-label=\"New project\"]').click()"); await sleep(400);
  await js("(() => { const i = document.querySelector('#setup-content input'); i.value = 'Reading list'; document.querySelector('#setup-content form').requestSubmit(); })()"); await sleep(1200);
  check("a project can be created by name, without a folder",
        await js("location.hash.startsWith('#project/') && document.getElementById('title').textContent === 'Reading list' && document.querySelectorAll('#side-threads .project').length === 2"));
  await js("window.confirm = () => true");
  await js("[...document.querySelectorAll('#head-tools button')].find(b => b.textContent === 'Remove').click()"); await sleep(1200);
  check("removing a project takes it off the list", await js("location.hash === '#overview' && document.querySelectorAll('#side-threads .project').length === 1"));

  // -- setting up Google says what Google needs, including the sign-in address
  await click('[data-view="panel:integrations"]'); await sleep(1200);
  check("one Google card, not separate Gmail and Calendar ones",
        await js("[...document.querySelectorAll('.integration-card h3')].map(h => h.textContent).filter(t => /Google|Gmail|Calendar/.test(t)).join() === 'Google account'"));
  await js("[...document.querySelectorAll('.integration-card button')].find(b => b.textContent === 'Set up Google').click()"); await sleep(800);
  check("the Google setup shows the redirect address and lets sir pick services",
        await js("/^http:\\/\\/127\\.0\\.0\\.1:\\d+\\/oauth\\/callback$/.test(document.querySelector('.redirect-row code')?.textContent || '') && document.querySelectorAll('#setup-content input[type=checkbox]:checked').length === 2 && /Desktop app/.test(document.querySelector('.google-help').textContent)"),
        await js("document.querySelector('.redirect-row code')?.textContent"));
  await shot("10d-google-setup");
  await js("document.getElementById('setup-dialog').close()"); await sleep(300);

  // -- an integration Claire built: its credential is entered in a form, and works
  await js("[...document.querySelectorAll('.work-card')].find(c => c.querySelector('h3').textContent === 'Weather').querySelector('button').click()"); await sleep(800);
  check("a connection Claire built asks for its key with her how-to steps",
        await js("!!document.querySelector('.credentials-form input[type=password]') && /weather.example → Account → API keys/.test(document.querySelector('.credentials-form').textContent)"));
  await shot("10e-credentials");
  await js("document.querySelector('.credentials-form input[type=password]').value = 'demo-key'");
  await js("document.querySelector('.credentials-form button[type=submit]').click()"); await sleep(3500);
  check("saving the key tests it and the connection works",
        await js("[...document.querySelectorAll('#setup-content .service-notice strong')].some(s => s.textContent === 'connected')"),
        await js("document.querySelector('#setup-content .service-notice')?.textContent"));
  await js("window.confirm = () => true");
  await js("[...document.querySelectorAll('#setup-content button')].find(b => b.textContent === 'Remove connection').click()"); await sleep(1500);
  check("a removed one stays installed and can be set up again",
        await js("[...document.querySelectorAll('.built-card h3')].some(h => h.textContent === 'Weather') && [...document.querySelectorAll('.built-card button')].some(b => b.textContent === 'Set up')"));
  await js("document.getElementById('setup-dialog').open && document.getElementById('setup-dialog').close()");
  await openDemo(); await sleep(600);

  // -- clearing a chat leaves the tasks alone
  await js("window.confirm = () => true"); await click("#clear-chat"); await sleep(1200);
  check("Clear chat empties the chat and keeps every task",
        await js("document.querySelectorAll('.msg').length === 0 && document.querySelectorAll('#side-threads .thread-row').length === 3"),
        `${await js("document.querySelectorAll('.msg').length")} messages, ${await js("document.querySelectorAll('#side-threads .thread-row').length")} rows`);
  await click('[data-view="dashboard:overview"]').catch(() => {}); await sleep(700);
  await shot("10c-cleared");
  // -- the command center's composer starts a new chat
  await js(`(() => { const i = document.getElementById('input'); i.value = 'what is new?'; i.dispatchEvent(new Event('input')); document.getElementById('composer').requestSubmit(); })()`);
  await sleep(3000);
  check("sending from the command center starts a new chat with the message",
        await js("location.hash.startsWith('#chat/') && document.querySelector('.msg.you .bubble')?.textContent === 'what is new?' && [...document.querySelectorAll('.msg.claire')].some(n => n.textContent.includes('started from the command center'))"),
        await js("location.hash"));

  // -- skills
  await click('[data-view="panel:skills"]'); await sleep(900);
  check("the skills page lists the built-in skills",
        await js("['coding', 'integrations', 'building-integrations'].every(n => [...document.querySelectorAll('.skill-list .skill-name')].map(x => x.textContent).includes(n))"));
  await js("[...document.querySelectorAll('.skill-list button')].find(b => b.querySelector('.skill-name')?.textContent === 'coding').click()"); await sleep(600);
  check("a skill can be read", await js("document.querySelector('.notebox').textContent.includes('Root cause')"));
  await js(`(async () => { await fetch('/skills', { method: 'POST', headers: {'Content-Type': 'application/json'}, credentials: 'same-origin',
      body: JSON.stringify({ filename: 'report-writing.md', text: '---\\nname: report-writing\\ndescription: Writing a clear report.\\n---\\n# Reports\\n\\nLead with the outcome. <img src=x onerror=alert(1)>' }) }); })()`);
  await sleep(1200);
  check("a new skill appears without reloading the page",
        await js("[...document.querySelectorAll('.skill-list .skill-name')].map(n => n.textContent).includes('report-writing')"));
  await js("[...document.querySelectorAll('.skill-list button')].find(b => b.querySelector('.skill-name')?.textContent === 'report-writing').click()"); await sleep(600);
  check("your skill shows as yours, can be removed, and its text is never treated as markup",
        await js("document.querySelector('.skill-source.user') && [...document.querySelectorAll('.notebox button')].some(b => b.textContent === 'Remove') && !document.querySelector('.notebox img')"));
  await shot("11-skills");
  check("no sideways overflow on the chats or skills pages", (await overflow()) <= 0);

  // -- models: add one by its file, choose it, and Claire says a restart switches
  await click('[data-view="panel:models"]'); await sleep(1000);
  check("the Models page lists the models config.py sets up", await js("document.querySelectorAll('.model-card').length >= 1 && [...document.querySelectorAll('.model-card')].some(c => c.textContent.includes('in use'))"));
  await js("[...document.querySelectorAll('.page-actions button')].find(b => b.textContent === 'Add model').click()"); await sleep(500);
  const tiny = join(homedir(), ".claire-ui-test/models/tiny-chat.gguf");
  await js(`(() => { const i = document.querySelector('#setup-content .folder-row input'); i.value = ${JSON.stringify(tiny)}; i.dispatchEvent(new Event('change')); })()`); await sleep(900);
  check("choosing a model file reads its name and how much context it was trained for",
        await js("document.querySelector('#setup-content .field-help').textContent.includes('trained for 8,192 tokens') && [...document.querySelectorAll('#setup-content input')].some(i => i.value === 'Tiny Chat')"),
        await js("document.querySelector('#setup-content .field-help').textContent"));
  await js("document.querySelector('#setup-content form').requestSubmit()"); await sleep(1300);
  check("the added model is chosen, and Claire says a restart switches to it",
        await js("[...document.querySelectorAll('.model-card h3')].some(h => h.textContent === 'Tiny Chat') && !!document.querySelector('.switch-notice') && [...document.querySelectorAll('.switch-notice button')].some(b => b.textContent === 'Restart Claire now')"));
  await shot("13-models");

  // -- human-free mode: one switch, confirmed, shown while on
  await js("window.confirm = () => true");
  await click("#human-free"); await sleep(700);
  check("human-free mode switches on, and says so in the header",
        await js("document.getElementById('human-free').getAttribute('aria-checked') === 'true' && !document.getElementById('mode-chip').hidden")
        && (await js("fetch('/mode',{credentials:'same-origin'}).then(r=>r.json()).then(r=>r.human_free)")) === true);
  await click("#mode-chip"); await sleep(700);
  check("and off again", await js("document.getElementById('human-free').getAttribute('aria-checked') === 'false' && document.getElementById('mode-chip').hidden"));
  // -- a page older than the service reloads itself
  await js("window.__stale = 1; checkBuild({ ui: 'a-newer-build' })"); await sleep(2000);
  check("a page running older code than the service reloads itself", await js("typeof window.__stale === 'undefined' && !!document.getElementById('side-threads')"));

  const bare = await fetch(url.split("/?")[0] + "/app.js");
  check("the app's files need a signed-in browser", bare.status === 403);
  console.log(problems.length ? `\n${problems.length} PROBLEM(S): ${[...new Set(problems)].join(", ")}` : "\nALL CHECKS PASSED");
  if (problems.length) process.exitCode = 1;
} catch (err) {
  console.log("DRIVER ERROR:", err.message);
  process.exitCode = 1;
} finally {
  clearTimeout(watchdog);
  closeBrowser();
}
