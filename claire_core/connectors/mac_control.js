// Reads and drives one app's front window through macOS accessibility.
// Fixed script: the request arrives as one JSON argument.
ObjC.import('CoreGraphics');
ObjC.import('AppKit');
ObjC.import('ApplicationServices');
function run(argv) {
  const a = JSON.parse(argv[0]);
  // Permission checks must not send an Apple event or launch System Events:
  // a missing OS grant otherwise looks like a minute-long hung app.
  if (a.op === 'permissions') {
    return JSON.stringify({accessibility: Boolean($.AXIsProcessTrusted())});
  }
  if (a.op === 'apps') {
    const running = $.NSWorkspace.sharedWorkspace.runningApplications.js
      .filter(app => Number(app.activationPolicy) === 0)
      .map(app => ObjC.unwrap(app.localizedName)).filter(Boolean);
    return JSON.stringify({running: running});
  }
  if(a.op==='instances'){
    return JSON.stringify({instances:$.NSWorkspace.sharedWorkspace.runningApplications.js
      .filter(x=>ObjC.unwrap(x.localizedName)===a.app)
      .map(x=>({pid:Number(x.processIdentifier),active:Boolean(x.active)}))});
  }
  if (!$.AXIsProcessTrusted()) throw new Error('CLAIRE:NO_ACCESSIBILITY');
  const se = Application('System Events');
  const KEYS = {return: 36, enter: 36, tab: 48, space: 49, delete: 51, backspace: 51, escape: 53, esc: 53,
                left: 123, right: 124, down: 125, up: 126, home: 115, end: 119, pageup: 116, pagedown: 121,
                forwarddelete: 117};
  const MODS = {cmd: 'command down', command: 'command down', shift: 'shift down', alt: 'option down',
                option: 'option down', ctrl: 'control down', control: 'control down'};
  const safe = (f, d) => { try { const v = f(); return v === undefined ? d : v; } catch (e) { return d; } };
  if (a.op === 'probe') {
    // Counting another app's windows is refused without Accessibility permission.
    return JSON.stringify({windows: se.processes.byName('Finder').windows.length});
  }
  let candidates=$.NSWorkspace.sharedWorkspace.runningApplications.js.filter(x=>ObjC.unwrap(x.localizedName)===a.app);
  if(!candidates.length && a.op==='open'){
    $.NSWorkspace.sharedWorkspace.launchApplication(a.app);delay(.6);
    candidates=$.NSWorkspace.sharedWorkspace.runningApplications.js.filter(x=>ObjC.unwrap(x.localizedName)===a.app);
  }
  if(!candidates.length)throw new Error('CLAIRE:NOT_RUNNING');
  const automated = app => {
    const task=$.NSTask.alloc.init,pipe=$.NSPipe.pipe;
    task.launchPath='/bin/ps';task.arguments=['-p',String(Number(app.processIdentifier)),'-o','command='];
    task.standardOutput=pipe;task.standardError=$.NSFileHandle.fileHandleWithNullDevice;
    task.launch;const data=pipe.fileHandleForReading.readDataToEndOfFile;task.waitUntilExit;
    const command=ObjC.unwrap($.NSString.alloc.initWithDataEncoding(data,$.NSUTF8StringEncoding));
    return /--user-data-dir(?:=|\s)|--remote-debugging-(?:port|pipe)|--headless/.test(command);
  };
  let chosen;
  if(a.process_id!=null){
    chosen=candidates.find(x=>Number(x.processIdentifier)===Number(a.process_id));
    if(!chosen)throw new Error('CLAIRE:CHANGED');
  }else{
    if(candidates.length>1){
      // A browser hosting Claire is the strongest available current-browser
      // context. Otherwise prefer an active instance, then the regular browser.
      const hosting=candidates.filter(x=>{
        const p=se.applicationProcesses.whose({unixId:Number(x.processIdentifier)})[0];
        return safe(()=>p.windows.name(),[]).some(n=>/^Claire(?:$|\s|[-–—])/.test(n||''));
      });
      const active=candidates.filter(x=>Boolean(x.active));
      if(hosting.length===1)candidates=hosting;
      else if(active.length===1)candidates=active;
    }
    if(candidates.length>1){
      const regular=candidates.filter(x=>!automated(x));
      if(regular.length===1)candidates=regular;
      else if(regular.length>1)candidates=regular;
    }
    if(candidates.length!==1)throw new Error('CLAIRE:AMBIGUOUS_APP');
    chosen=candidates[0];
  }
  const pid=Number(chosen.processIdentifier);
  const proc=se.applicationProcesses.whose({unixId:pid})[0];
  if(!safe(()=>proc.exists(),false))throw new Error('CLAIRE:NOT_RUNNING');
  if(a.op==='open'){
    chosen.activateWithOptions(2);delay(.3);
    return JSON.stringify({ok:true,process_id:pid});
  }
  if(a.op==='browser_shortcut'){
    if(['t','w','l','n','url'].indexOf(a.key)<0)throw new Error('CLAIRE:BAD_KEY');
    const check=()=>{
      if(Number($.NSWorkspace.sharedWorkspace.frontmostApplication.processIdentifier)!==pid)return false;
      if(a.expected_window==null)return true;
      const rows=ObjC.deepUnwrap(ObjC.castRefToObject($.CGWindowListCopyWindowInfo($.kCGWindowListOptionOnScreenOnly,$.kCGNullWindowID)));
      const body=rows.find(x=>x.kCGWindowOwnerPID===pid && x.kCGWindowLayer===0 && x.kCGWindowBounds.Width>Math.max(200,(a.expected_size||[0,0])[0]*.8) && x.kCGWindowBounds.Height>Math.max(150,(a.expected_size||[0,0])[1]*.7));
      return body && body.kCGWindowNumber===a.expected_window;
    };
    for(let i=0;i<20 && !check();i++)delay(.05);
    if(!check())throw new Error('CLAIRE:CHANGED');
    if(a.key==='url'){
      if(typeof a.url!=='string')throw new Error('CLAIRE:BAD_KEY');
      se.keystroke(a.url);if(!check())throw new Error('CLAIRE:CHANGED');se.keyCode(36);
    }else se.keystroke(a.key,{using:['command down']});
    return JSON.stringify({ok:true});
  }
  const windowNumber = w => {
    const native=safe(() => w.attributes.byName('AXWindowNumber').value(),null);
    if(native!=null)return Number(native);
    const p=safe(()=>w.position(),[]),z=safe(()=>w.size(),[]),name=safe(()=>w.name(),'');
    if(p.length!==2||z.length!==2)return null;
    const windows=ObjC.deepUnwrap(ObjC.castRefToObject($.CGWindowListCopyWindowInfo($.kCGWindowListOptionOnScreenOnly,$.kCGNullWindowID)));
    const matches=windows.filter(x=>x.kCGWindowOwnerPID===pid && x.kCGWindowLayer===0 &&
      Math.abs(x.kCGWindowBounds.X-p[0])<2 && Math.abs(x.kCGWindowBounds.Y-p[1])<2 &&
      Math.abs(x.kCGWindowBounds.Width-z[0])<2 && Math.abs(x.kCGWindowBounds.Height-z[1])<2);
    const named=matches.filter(x=>x.kCGWindowName===name);
    return named.length===1?named[0].kCGWindowNumber:matches.length===1?matches[0].kCGWindowNumber:null;
  };
  // Chromium browsers publish a page's contents to accessibility only when
  // something asks for them (as VoiceOver does). Ask once; it lasts until the
  // browser quits. The first read after that takes a moment to fill in.
  const CHROMIUM = ['Google Chrome', 'Google Chrome Beta', 'Chromium', 'Brave Browser', 'Microsoft Edge',
                    'Arc', 'Vivaldi', 'Opera'];
  if (CHROMIUM.indexOf(a.app) >= 0 && a.op !== 'windows' && (a.ax_ready_pids||[]).indexOf(pid)<0) {
    const flag = proc.attributes.byName('AXEnhancedUserInterface');
    if ([true,1,'true','1'].indexOf(safe(() => flag.value(), false))<0) {
      safe(() => { flag.value = true; }, null);
      delay(2);
    }
  }
  // Some apps (Chrome) keep invisible helper windows ahead of the one sir sees.
  // Only real windows count, in front-to-back order.
  const real = () => {
    const subs = safe(() => proc.windows.subrole(), []);
    const kinds = ['AXStandardWindow', 'AXDialog', 'AXSystemDialog', 'AXFloatingWindow'];
    const keep = [];
    subs.forEach((s, i) => { if (kinds.indexOf(s) >= 0) keep.push(i); });
    return keep.length ? keep : subs.map((s, i) => i);
  };
  const win = () => {
    // Straight after an action macOS can briefly report no windows at all.
    let n = 0;
    for (let tries = 0; tries < 8; tries++) {
      n = safe(() => proc.windows.length, 0);
      if (n > 0) break;
      delay(0.25);
    }
    if (n === 0) throw new Error('CLAIRE:NO_WINDOW');
    const list = real();
    const i = a.win || 0;
    if (i >= list.length) throw new Error('CLAIRE:CHANGED');
    const w = proc.windows[list[i]];
    const ident = windowNumber(w);
    if (a.expected_window != null && ident !== a.expected_window) throw new Error('CLAIRE:CHANGED');
    return w;
  };
  if (a.op === 'windows') {
    const ws = proc.windows;
    const names = safe(() => ws.name(), []), pos = safe(() => ws.position(), []), size = safe(() => ws.size(), []);
    const mini = safe(() => ws.attributes.byName('AXMinimized').value(), []);
    return JSON.stringify({windows: real().map((i) => ({title: names[i] || '', box: pos[i] && size[i] ? pos[i].concat(size[i]) : null,
                                                        minimized: mini[i] === true}))});
  }
  if (a.op === 'raise') {
    const w = win();
    const wanted = safe(() => w.name(), '');
    if (safe(() => w.attributes.byName('AXMinimized').value(), false) === true) {
      w.attributes.byName('AXMinimized').value = false;
      delay(0.6);
    }
    w.actions.byName('AXRaise').perform();       // raise first; activating the app keeps it in front
    delay(0.3);
    proc.frontmost = true;
    delay(0.4);
    if (safe(() => proc.windows[real()[0]].name(), '') !== wanted) throw new Error('CLAIRE:CHANGED');
    return JSON.stringify({title: safe(() => proc.windows[real()[0]].name(), '')});
  }
  if (a.op === 'scroll_at') {
    // The wheel turns for whatever is under the pointer, so put it there first.
    const w = win(), pos = w.position(), size = w.size();
    if (a.x < pos[0] || a.y < pos[1] || a.x > pos[0] + size[0] || a.y > pos[1] + size[1]) {
      throw new Error('CLAIRE:CHANGED');
    }
    proc.frontmost = true; delay(0.25);
    const p = $.CGPointMake(a.x, a.y);
    $.CGEventPost($.kCGHIDEventTap, $.CGEventCreateMouseEvent($(), $.kCGEventMouseMoved, p, $.kCGMouseButtonLeft));
    delay(0.1);
    const steps = Math.min(Math.abs(a.lines), 60), sign = a.lines > 0 ? -1 : 1;   // down the page is a negative wheel
    for (let done = 0; done < steps; done += 3) {
      const e = $.CGEventCreateScrollWheelEvent2($(), $.kCGScrollEventUnitLine, 2, sign * Math.min(3, steps - done), 0, 0);
      $.CGEventPost($.kCGHIDEventTap, e);
      delay(0.03);
    }
    delay(0.5);
    return JSON.stringify({ok: true});
  }
  const at = (path) => { let el = win(); for (const i of path) el = el.uiElements[i]; return el; };
  const assertFocus = () => { if(Number($.NSWorkspace.sharedWorkspace.frontmostApplication.processIdentifier)!==pid)throw new Error('CLAIRE:CHANGED');if(a.expected_window!=null)win(); };
  const front = () => { proc.frontmost = true; delay(.1);assertFocus(); };

  if (a.op === 'window') {
    front();
    const w = win(), pos = w.position(), size = w.size();
    return JSON.stringify({id:windowNumber(w),process_id:pid,x: pos[0], y: pos[1], w: size[0], h: size[1], title: safe(() => w.name(), '')});
  }
  // Never anything inside.
  const BARE = ['AXStaticText', 'AXImage', 'AXTextField', 'AXTextArea', 'AXScrollBar', 'AXRulerMarker',
                'AXValueIndicator', 'AXSlider', 'AXIncrementor', 'AXListMarker', 'AXSplitter', 'AXGrowArea'];
  // Nothing worth reading inside once it has a name of its own; an unnamed one
  // is opened, because its label is the text inside it.
  const LEAF = ['AXRadioButton', 'AXButton', 'AXCheckBox', 'AXPopUpButton', 'AXMenuButton', 'AXComboBox',
                'AXDisclosureTriangle', 'AXLink', 'AXHeading', 'AXCell', 'AXMenuItem', 'AXTab'];
  // The window's tab strip: a tab group whose children are tabs (radio buttons).
  const tabStrip = () => {
    const look = (el, depth) => {
      if (depth > 7) return null;
      const kids = el.uiElements, roles = safe(() => kids.role(), []);
      for (let i = 0; i < roles.length; i++) {
        if (roles[i] === 'AXTabGroup' && safe(() => kids[i].uiElements.role(), []).indexOf('AXRadioButton') >= 0) return kids[i];
      }
      for (let i = 0; i < roles.length; i++) {
        if (['AXGroup', 'AXToolbar', 'AXSplitGroup', 'AXLayoutArea', 'AXUnknown'].indexOf(roles[i]) >= 0) {
          const hit = look(kids[i], depth + 1);
          if (hit) return hit;
        }
      }
      return null;
    };
    const strip = look(win(), 0);
    if (!strip) throw new Error('CLAIRE:NO_TABS');
    const kids = strip.uiElements, roles = kids.role();
    const names = safe(() => kids.name(), []), descs = safe(() => kids.description(), []), vals = safe(() => kids.value(), []);
    const tabs = [];
    roles.forEach((r, i) => {
      if (r === 'AXRadioButton') tabs.push({at: i, title: names[i] || descs[i] || '', on: vals[i] === true || vals[i] === 1});
    });
    return {strip: strip, tabs: tabs};
  };
  const windowInfo = () => {
    const w = win(), p = safe(() => w.position(), [0, 0]), z = safe(() => w.size(), [0, 0]);
    return {id: windowNumber(w),process_id:pid,
            title: safe(() => w.name(), ''), x:p[0], y:p[1], w:z[0], h:z[1]};
  };
  if (a.op === 'tabs') {
    return JSON.stringify({tabs: tabStrip().tabs.map((t) => ({title: t.title, selected: t.on}))});
  }
  if (a.op === 'pick_tab') {
    const found = tabStrip(), tab = found.tabs[a.index];
    if (!tab || tab.title !== a.title) throw new Error('CLAIRE:CHANGED');
    front();
    found.strip.uiElements[tab.at].actions.byName('AXPress').perform();
    delay(0.6);
    return JSON.stringify({title: tab.title});
  }
  if (a.op === 'snapshot') {
    const out = [];
    const w = win();
    const walk = (el, path, depth) => {
      if (out.length >= a.max || depth > 14) return;
      const kids = el.uiElements;
      const roles = safe(() => kids.role(), []);
      if (!roles.length) return;
      const names = safe(() => kids.name(), []), descs = safe(() => kids.description(), []);
      // Each question is a round trip; ask only what these children can answer.
      const boxes = ['AXGroup', 'AXList', 'AXSplitGroup', 'AXToolbar', 'AXTabGroup', 'AXLayoutArea',
                     'AXWebArea', 'AXScrollArea', 'AXUnknown', 'AXSection', 'AXLandmarkMain'];
      const some = roles.some((r) => boxes.indexOf(r) < 0);
      const vals = some ? safe(() => kids.value(), []) : [];
      const subs = [];
      const on = some ? safe(() => kids.enabled(), []) : [];
      const pos = a.geo ? safe(() => kids.position(), []) : [], size = a.geo ? safe(() => kids.size(), []) : [];
      for (let i = 0; i < roles.length && out.length < a.max; i++) {
        const v = vals[i];
        out.push({path: path.concat(i), depth: depth, role: roles[i] || '', sub: subs[i] || '',
                  name: names[i] || '', desc: descs[i] || '',
                  value: (v === null || v === undefined || typeof v === 'object') ? '' : String(v).slice(0, a.full ? 20000 : 400),
                  enabled: on[i] !== false,
                  box: (pos[i] && size[i]) ? [pos[i][0], pos[i][1], size[i][0], size[i][1]] : null});
        // A named control has nothing more to say inside it; asking costs a
        // round trip per control, which on a browser's tab strip is seconds.
        if (BARE.indexOf(roles[i]) >= 0) continue;
        if (LEAF.indexOf(roles[i]) >= 0 && (names[i] || descs[i])) continue;
        walk(kids[i], path.concat(i), depth + 1);
      }
    };
    walk(w, [], 0);
    return JSON.stringify({title: safe(() => w.name(), ''), window:windowInfo(), nodes: out, full: out.length >= a.max});
  }
  if (a.op === 'click' || a.op === 'set_value') {
    const el = at(a.path);
    if (safe(() => el.role(), null) !== a.role) throw new Error('CLAIRE:CHANGED');
    if (a.expected_label && a.expected_label !== safe(() => el.name(), '') && a.expected_label !== safe(() => el.description(), '')) throw new Error('CLAIRE:CHANGED');
    front();
    if (a.op === 'click') {
      const press = safe(() => el.actions.byName('AXPress').exists(), false);
      if (press) el.actions.byName('AXPress').perform(); else el.click();
    } else {
      safe(() => { el.focused = true; }, null);
      el.value = a.text;
    }
    delay(0.4);
    return JSON.stringify({ok: true});
  }
  if (a.op === 'click_at') {
    // A real mouse click at a point on the screen, inside this app's window.
    const w = win(), pos = w.position(), size = w.size();
    if (a.x < pos[0] || a.y < pos[1] || a.x > pos[0] + size[0] || a.y > pos[1] + size[1]) {
      throw new Error('CLAIRE:CHANGED');
    }
    front();
    const p = $.CGPointMake(a.x, a.y);
    const post = (type, n) => {
      const e = $.CGEventCreateMouseEvent($(), type, p, $.kCGMouseButtonLeft);
      if (n) $.CGEventSetIntegerValueField(e, $.kCGMouseEventClickState, n);
      $.CGEventPost($.kCGHIDEventTap, e);
    };
    post($.kCGEventMouseMoved); delay(0.08);
    post($.kCGEventLeftMouseDown, 1); delay(0.04); post($.kCGEventLeftMouseUp, 1);
    if (a.double) { delay(0.08); post($.kCGEventLeftMouseDown, 2); delay(0.04); post($.kCGEventLeftMouseUp, 2); }
    delay(0.5);
    return JSON.stringify({ok: true});
  }
  if (a.op === 'type') {
    front();
    // A line break is the Return key; keystroke() alone drops it.
    const lines = a.text.split('\n');
    lines.forEach((line, i) => {
      if (line) {assertFocus();se.keystroke(line);}
      if (i < lines.length - 1) {assertFocus();se.keyCode(36);}
    });
    delay(0.3);
    return JSON.stringify({ok: true});
  }
  if (a.op === 'key') {
    front();
    const using = (a.modifiers || []).map((m) => MODS[m]).filter(Boolean);
    const opts = using.length ? {using: using} : {};
    if (KEYS[a.key] !== undefined) se.keyCode(KEYS[a.key], opts);
    else if (a.key.length === 1) se.keystroke(a.key, opts);
    else throw new Error('CLAIRE:BAD_KEY');
    delay(0.4);
    return JSON.stringify({ok: true});
  }
  if (a.op === 'menu') {
    front();
    let item = proc.menuBars[0].menuBarItems.byName(a.path[0]);
    for (const name of a.path.slice(1)) item = item.menus[0].menuItems.byName(name);
    if (!safe(() => item.exists(), false)) throw new Error('CLAIRE:NO_MENU');
    if (safe(() => item.enabled(), true) === false) throw new Error('CLAIRE:MENU_DISABLED');
    item.click();
    delay(0.5);
    return JSON.stringify({ok: true});
  }
  if (a.op === 'menus') {
    const bar = proc.menuBars[0].menuBarItems;
    if (!a.path.length) return JSON.stringify({items: bar.name().filter((n) => n && n !== 'Apple')});
    let item = bar.byName(a.path[0]);
    for (const name of a.path.slice(1)) item = item.menus[0].menuItems.byName(name);
    const names = item.menus[0].menuItems.name(), on = safe(() => item.menus[0].menuItems.enabled(), []);
    return JSON.stringify({items: names.map((n, i) => n ? n + (on[i] === false ? ' (disabled)' : '') : null).filter(Boolean)});
  }
  throw new Error('CLAIRE:UNKNOWN_OP');
}
