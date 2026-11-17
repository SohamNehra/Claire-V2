const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const appPath = process.argv[2] || path.resolve(__dirname, '../../claire_core/web/app.js');
const htmlPath = process.argv[3] || path.resolve(__dirname, '../../claire_core/web/index.html');
const source = fs.readFileSync(appPath, 'utf8').replace(/\nstart\(\);\s*$/, '\n');
const ctx = vm.createContext({ console, URL, Intl, localStorage: {getItem:()=>null}, document:{}, setTimeout,clearTimeout });
vm.runInContext(source, ctx);
// User input keeps the workspace awake; periodic rendering does not.
ctx.performance={now:()=>10000};
ctx.document.hidden=false;
vm.runInContext(`const activityCalls=[];api=(...args)=>{activityCalls.push(args);return Promise.resolve({});};`,ctx);
vm.runInContext('noteUserActivity();noteUserActivity();',ctx);
assert.equal(vm.runInContext('activityCalls.length',ctx),1);
ctx.performance.now=()=>70000;
vm.runInContext('noteUserActivity()',ctx);
assert.equal(vm.runInContext('activityCalls.length',ctx),2);
ctx.document.hidden=true;
ctx.performance.now=()=>140000;
vm.runInContext('noteUserActivity()',ctx);
assert.equal(vm.runInContext('activityCalls.length',ctx),2);
ctx.document.hidden=false;
let count=3;
function check(expression,expected){assert.equal(vm.runInContext(expression,ctx),expected,expression);count++;}
for (const cron of ['0 9 * * *','0 9 * * 1-5','*/15 8-18 * * 1,3,5','0 0 1 1 0','59 23 31 12 7'])check(`validCron(${JSON.stringify(cron)})`,true);
for (const cron of ['60 9 * * *','0 24 * * *','0 9 0 * *','0 9 * 13 *','0 9 * * 8','*/0 * * * *','0 9 * *','0 9 * * * *','0 9 * * 5-1','0 9 * * MON','0 9 * * 1,,2'])check(`validCron(${JSON.stringify(cron)})`,false);
for(const zone of ['Asia/Kolkata','Europe/London','UTC'])check(`validZone(${JSON.stringify(zone)})`,true);
check('validZone("Not/AZone")',false);
for(const url of ['https://example.com/mcp','http://127.0.0.1:8000/mcp','http://localhost:3000'])check(`endpointValid(${JSON.stringify(url)})`,true);
for(const url of ['http://example.com','https://user:secret@example.com','https://example.com/?token=secret','https://example.com/#token','javascript:alert(1)','file:///etc/passwd'])check(`endpointValid(${JSON.stringify(url)})`,false);
check("formatDate('invalid')",'Not reported');
// Clearing a chat is destructive: the only call is in clearChat, after a confirmation.
assert(source.split('/reset').length === 2, 'Only one place may clear a conversation');
assert(/async function clearChat\(t\) \{\s*if \(!confirm\([^)]*\)\) return;[\s\S]{0,120}\/reset/.test(source), 'Clearing a conversation must ask first');
assert(!/\bmission(s)?\b/i.test(fs.readFileSync(htmlPath,'utf8')), 'No old terminology in UI HTML');
for(const status of ['idle','queued','running','waiting'])check(`taskBucket(${JSON.stringify(status)})`,'active');
check("taskBucket('done')",'completed');
for(const status of ['error','stopped','interrupted'])check(`taskBucket(${JSON.stringify(status)})`,'failed');
check("taskBucket('cancelled')",'other');
const html=fs.readFileSync(htmlPath,'utf8');
assert(!html.includes('One mind.'),'No decorative headline');
assert(!html.includes('id="controller-feed"'),'No dashboard mini-chat');
assert(html.includes('id="upcoming-board"'),'Upcoming schedules board exists');
count+=3;
// Hiding is a reversible view preference, with active/current work pinned.
vm.runInContext(`
  const savedPrefs = new Map();
  localStorage.setItem = (key,value)=>savedPrefs.set(key,value);
  localStorage.getItem = key=>savedPrefs.get(key)||null;
  draw=()=>{};toast=()=>{};
  S.threads.clear();S.approvals.clear();
  for (const row of [
    {id:'home',kind:'home',status:'idle',updated:1},
    {id:'old-chat',kind:'home',status:'idle',updated:10},
    {id:'open-chat',kind:'home',status:'idle',updated:10},
    {id:'busy-chat',kind:'home',status:'running',updated:10},
    {id:'old-task',kind:'task',status:'done',updated:10},
    {id:'active-task',kind:'task',status:'queued',updated:10},
    {id:'approval-task',kind:'task',status:'error',updated:10}
  ]) S.threads.set(row.id,row);
  S.approvals.set('ask',{thread:'approval-task'});
  S.view={kind:'thread',id:'open-chat'};
  togglePrevious('chats');togglePrevious('tasks');
`,ctx);
check("historyVisible(S.threads.get('home'))",true);
check("historyVisible(S.threads.get('old-chat'))",false);
check("historyVisible(S.threads.get('open-chat'),S.threads.get('open-chat'))",true);
check("historyVisible(S.threads.get('busy-chat'))",true);
check("historyVisible(S.threads.get('old-task'))",false);
check("historyVisible(S.threads.get('active-task'))",true);
check("historyVisible(S.threads.get('approval-task'))",true);
check("loadPrefs().hiddenThreadHistory.tasks['old-task']",10);
check('S.threads.size',7);
vm.runInContext("S.threads.get('old-task').updated=11; S.threads.set('new-chat',{id:'new-chat',kind:'home',status:'idle',updated:1})",ctx);
check("historyVisible(S.threads.get('old-task'))",true);
check("historyVisible(S.threads.get('new-chat'))",true);
vm.runInContext("togglePrevious('chats');togglePrevious('tasks')",ctx);
check("historyVisible(S.threads.get('old-chat'))",true);
check("historyVisible(S.threads.get('old-task'))",true);
check("Object.keys(historyPreference({good:1,bad:'yes',invalid:-1})).join(',')",'good');
vm.runInContext('S.threads.clear();S.approvals.clear();hiddenThreadHistory={chats:{},tasks:{}}',ctx);
// Markdown tables become tables; cell text is never markup.
vm.runInContext(`
  function fakeEl(tag){ return {tag, nodeType:1, children:[], className:'', attrs:{}, text:'', append(...k){ this.children.push(...k.flat()); },
    setAttribute(k,v){ this.attrs[k]=v; }, addEventListener(){}, set textContent(v){ this.text=v; }, get textContent(){ return this.text; } }; }
  document.createElement = fakeEl;
  document.createTextNode = (t) => ({ nodeType: 3, text: t });
  const flat = (n) => [n.tag, ...(n.children || []).flatMap(c => c.nodeType === 3 ? [c.text] : flat(c))];
  globalThis.__table = flat(prose("Results:\\n\\n| Product | Growth |\\n|---|---:|\\n| Notebook <b>Pro</b> | +91.9% |\\n\\nDone."));
`, ctx);
check("__table.includes('table') && __table.includes('th') && __table.includes('td')", true);
check("__table.includes('Notebook <b>Pro</b>')", true);
check("__table.filter(x => x === 'b').length", 0);
// Review: a "|" line that is not a table used to loop forever.
vm.runInContext(`globalThis.__pipe = flat(prose("| not a table, just a pipe\\nand a second line"));`, ctx);
check("__pipe.includes('p') && __pipe.includes('table')", false);
check("__pipe.join(' ').includes('| not a table, just a pipe')", true);
// Projects: what counts as active work, and the remembered folder state.
check("isActive({kind:'task',status:'waiting'})",true);
check("isActive({kind:'task',status:'done',busy:true})",true);
check("isActive({kind:'task',status:'done'})",false);
check("isActive({kind:'home',status:'running'})",false);
vm.runInContext("S.projects=new Map([['p1',{id:'p1',name:'Shop'}]]);S.threads.set('t9',{id:'t9',kind:'task',project:'p1'})",ctx);
check("projectOf(S.threads.get('t9')).name",'Shop');
check("projectOf({project:'gone'})",null);
check("toHash({kind:'project',id:'p1'})",'#project/p1');
vm.runInContext("toggleProject('p1')",ctx);
check("loadPrefs().collapsedProjects.p1",true);
vm.runInContext("toggleProject('p1')",ctx);
check("'p1' in loadPrefs().collapsedProjects",false);
vm.runInContext('S.threads.clear();S.projects.clear()',ctx);
(async()=>{
  vm.runInContext(`
    draw=()=>{};
    let resolveList;
    let apiCalls=0;
    api=path=>{apiCalls++;return path==='/integrations/providers'?Promise.resolve({providers:[]}):new Promise(resolve=>{resolveList=resolve;});};
    const firstLoad=fetchExtra('integrations');
    const secondLoad=fetchExtra('integrations');
  `,ctx);
  check('firstLoad === secondLoad',true);
  vm.runInContext('resolveList({integrations:[]})',ctx);
  await vm.runInContext('secondLoad',ctx);
  check("extras.integrations.state",'ready');
  check('apiCalls',2);
  vm.runInContext("api=()=>Promise.reject(Object.assign(new Error('offline'),{status:503}))",ctx);
  await vm.runInContext("fetchExtra('scheduler')",ctx);
  check('extras.scheduler.state','error');
  vm.runInContext("api=()=>Promise.reject(Object.assign(new Error('missing'),{status:404}))",ctx);
  await vm.runInContext("fetchExtra('scheduler')",ctx);
  check('extras.scheduler.state','unavailable');
  console.log(`${count+2} frontend checks passed (validation, history, terminology, refresh concurrency, service failures).`);
})().catch(err=>{console.error(err);process.exitCode=1;});
