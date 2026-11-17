"""Direct macOS Accessibility access, avoiding System Events' UI-tree bridge.

Only the trusted connector calls this module. App allow-lists and approvals stay
in the connector/computer layer. Every AX message has a bounded timeout.
"""
import ctypes as C
import re
import time

CF=C.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
AX=C.CDLL('/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices')
CG=C.CDLL('/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics')
P=C.c_void_p

def bind(lib,name,result,args):
    f=getattr(lib,name);f.restype=result;f.argtypes=args;return f

retain=bind(CF,'CFRetain',P,[P]);release=bind(CF,'CFRelease',None,[P])
typeid=bind(CF,'CFGetTypeID',C.c_ulong,[P])
cfhash=bind(CF,'CFHash',C.c_ulong,[P])
stringid=bind(CF,'CFStringGetTypeID',C.c_ulong,[])()
arrayid=bind(CF,'CFArrayGetTypeID',C.c_ulong,[])()
dictid=bind(CF,'CFDictionaryGetTypeID',C.c_ulong,[])()
numberid=bind(CF,'CFNumberGetTypeID',C.c_ulong,[])()
boolid=bind(CF,'CFBooleanGetTypeID',C.c_ulong,[])()
axid=bind(AX,'AXUIElementGetTypeID',C.c_ulong,[])()
valueid=bind(AX,'AXValueGetTypeID',C.c_ulong,[])()
make_string=bind(CF,'CFStringCreateWithCString',P,[P,C.c_char_p,C.c_uint32])
strlen=bind(CF,'CFStringGetLength',C.c_long,[P])
getstr=bind(CF,'CFStringGetCString',C.c_bool,[P,C.c_char_p,C.c_long,C.c_uint32])
arraycount=bind(CF,'CFArrayGetCount',C.c_long,[P]);arrayget=bind(CF,'CFArrayGetValueAtIndex',P,[P,C.c_long])
dictcount=bind(CF,'CFDictionaryGetCount',C.c_long,[P]);dictget=bind(CF,'CFDictionaryGetKeysAndValues',None,[P,P,P])
getnumber=bind(CF,'CFNumberGetValue',C.c_bool,[P,C.c_int,P]);getbool=bind(CF,'CFBooleanGetValue',C.c_bool,[P])
valuekind=bind(AX,'AXValueGetType',C.c_int,[P]);valueget=bind(AX,'AXValueGetValue',C.c_bool,[P,C.c_int,P])
create_app=bind(AX,'AXUIElementCreateApplication',P,[C.c_int])
copyattr=bind(AX,'AXUIElementCopyAttributeValue',C.c_int,[P,P,C.POINTER(P)])
setattr_ax=bind(AX,'AXUIElementSetAttributeValue',C.c_int,[P,P,P])
perform=bind(AX,'AXUIElementPerformAction',C.c_int,[P,P])
timeout_ax=bind(AX,'AXUIElementSetMessagingTimeout',C.c_int,[P,C.c_float])
trusted=bind(AX,'AXIsProcessTrusted',C.c_bool,[])
TRUE=P.in_dll(CF,'kCFBooleanTrue')
_STRINGS={}

def string(value):
    if value not in _STRINGS:_STRINGS[value]=make_string(None,value.encode('utf-8'),0x08000100)
    return _STRINGS[value]


def decode(ref):
    if not ref:return None
    kind=typeid(ref)
    if kind==stringid:
        size=strlen(ref)*4+1;buf=C.create_string_buffer(size)
        return buf.value.decode('utf-8','replace') if getstr(ref,buf,size,0x08000100) else ''
    if kind==boolid:return bool(getbool(ref))
    if kind==numberid:
        value=C.c_double();getnumber(ref,6,C.byref(value));return int(value.value) if value.value.is_integer() else value.value
    if kind==arrayid:return [decode(arrayget(ref,i)) for i in range(arraycount(ref))]
    if kind==dictid:
        count=dictcount(ref);keys=(P*count)();values=(P*count)();dictget(ref,keys,values)
        return {decode(keys[i]):decode(values[i]) for i in range(count)}
    if kind==axid:return Element(ref,retain_ref=True)
    if kind==valueid:
        k=valuekind(ref)
        if k in (1,2):
            values=(C.c_double*2)();valueget(ref,k,values);return tuple(values)
    return None


class Element:
    def __init__(self,ref,retain_ref=False):
        self.ref=retain(ref) if retain_ref else ref
        timeout_ax(self.ref,0.5)
    def __del__(self):
        ref=getattr(self,'ref',None)
        if ref:
            try:release(ref)
            except Exception:pass
            self.ref=None
    def get(self,key,default=None):
        ref=P();error=copyattr(self.ref,string(key),C.byref(ref))
        if error or not ref:return default
        try:return decode(ref.value)
        finally:release(ref.value)
    def set(self,key,value):
        if isinstance(value,bool):ref=TRUE if value else P.in_dll(CF,'kCFBooleanFalse');owned=False
        else:ref=make_string(None,str(value).encode('utf-8'),0x08000100);owned=True
        try:
            error=setattr_ax(self.ref,string(key),ref)
            if error:raise RuntimeError(f'AX set {key} failed ({error})')
        finally:
            if owned:release(ref)
    def press(self,action='AXPress'):
        error=perform(self.ref,string(action))
        if error:raise RuntimeError(f'AX action {action} failed ({error})')


def application(pid):
    if not trusted():raise RuntimeError('CLAIRE:NO_ACCESSIBILITY')
    app=Element(create_app(int(pid)));timeout_ax(app.ref,2.0)
    for flag in ('AXManualAccessibility','AXEnhancedUserInterface'):
        try:app.set(flag,True)
        except RuntimeError:pass
    return app


copywindows=bind(CG,'CGWindowListCopyWindowInfo',P,[C.c_uint32,C.c_uint32])

def has_window_candidates(pid):
    raw=copywindows(0,0)
    try:rows=decode(raw) or []
    finally:
        if raw:release(raw)
    return any(r.get('kCGWindowOwnerPID')==pid and r.get('kCGWindowLayer')==0
               and r.get('kCGWindowBounds',{}).get('Width',0)>200
               and r.get('kCGWindowBounds',{}).get('Height',0)>150 for r in rows)


# Some macOS releases omit AXWindowNumber. Resolve the actual AX window
# when the platform exposes its bridge; conservative public metadata is fallback.
_ax_window=getattr(AX,'_AXUIElementGetWindow',None)
if _ax_window is not None:
    _ax_window.restype=C.c_int;_ax_window.argtypes=[P,C.POINTER(C.c_uint32)]


def window_id(window,pid):
    if _ax_window is not None:
        number=C.c_uint32()
        if _ax_window(window.ref,C.byref(number))==0 and number.value:return number.value
    native=window.get('AXWindowNumber')
    if native is not None:return native
    pos=window.get('AXPosition');size=window.get('AXSize')
    if not pos or not size:return None
    raw=copywindows(0,0)
    try:rows=decode(raw) or []
    finally:
        if raw:release(raw)
    matches=[x for x in rows if x.get('kCGWindowOwnerPID')==pid and x.get('kCGWindowLayer')==0
             and all(abs(x.get('kCGWindowBounds',{}).get(k,-9999)-v)<2 for k,v in zip(('X','Y','Width','Height'),pos+size))]
    named=[x for x in matches if x.get('kCGWindowName')==window.get('AXTitle')]
    if len(named)==1:return named[0]['kCGWindowNumber']
    return matches[0]['kCGWindowNumber'] if len(matches)==1 else None


def windows(app):
    all_windows=app.get('AXWindows',[]) or []
    if not all_windows:
        focused=app.get('AXFocusedWindow')
        if isinstance(focused,Element) and focused.get('AXRole')=='AXWindow':all_windows=[focused]
    real=[w for w in all_windows if w.get('AXSubrole') in ('AXStandardWindow','AXDialog','AXSystemDialog','AXFloatingWindow')]
    return real or all_windows


def info(window,pid):
    pos=window.get('AXPosition') or (0,0);size=window.get('AXSize') or (0,0)
    return {'id':window_id(window,pid),'process_id':pid,'title':window.get('AXTitle') or '',
            'x':pos[0],'y':pos[1],'w':size[0],'h':size[1]}


def run(app,pid,request):
    op=request['op'];all_windows=windows(app)
    if op=='windows':return {'windows':[{'title':w.get('AXTitle') or '',
            'box':list((w.get('AXPosition') or (0,0))+(w.get('AXSize') or (0,0))),
            'minimized':bool(w.get('AXMinimized'))} for w in all_windows]}
    if not all_windows:raise RuntimeError('CLAIRE:NO_WINDOW')
    if request.get('win') is not None:
        index=int(request['win'])
        if not 0<=index<len(all_windows):raise RuntimeError('CLAIRE:CHANGED')
        window=all_windows[index]
    else:window=next((w for w in all_windows if w.get('AXMain')),all_windows[0])
    meta=info(window,pid)
    expected=request.get('expected_window')
    if expected is not None and expected!=meta['id']:raise RuntimeError('CLAIRE:CHANGED')
    if op=='window':
        try:window.press('AXRaise');app.set('AXFrontmost',True)
        except RuntimeError:pass
        return info(window,pid)
    if op=='snapshot':
        nodes=[];limit=int(request.get('max') or 120);deadline=time.monotonic()+3
        leaf={'AXStaticText','AXImage','AXTextField','AXTextArea','AXScrollBar','AXSlider',
              'AXRadioButton','AXButton','AXCheckBox','AXPopUpButton','AXLink','AXMenuItem','AXTab'}
        def walk(parent,path,depth):
            if depth>14 or len(nodes)>=limit or time.monotonic()>deadline:return
            children=parent.get('AXChildren',[]) or []
            for i,child in enumerate(children):
                if len(nodes)>=limit or time.monotonic()>deadline:break
                role=child.get('AXRole') or ''
                value=child.get('AXValue')
                if len(children)>12 and role in ('AXRadioButton','AXTab') and not value:continue
                name=child.get('AXTitle') or '';description=child.get('AXDescription') or ''
                text=('true' if value else 'false') if isinstance(value,bool) else str(value) if isinstance(value,(str,int,float)) else ''
                box=None
                if request.get('geo'):
                    p=child.get('AXPosition');z=child.get('AXSize')
                    if p and z:box=list(p+z)
                nodes.append({'path':path+[i],'depth':depth,'role':role,'sub':child.get('AXSubrole') or '',
                              'name':name,'desc':description,'value':text[:20000 if request.get('full') else 400],
                              'enabled':child.get('AXEnabled',True) is not False,'box':box})
                if role in leaf and (name or description or role in ('AXStaticText','AXTextField','AXTextArea')):continue
                walk(child,path+[i],depth+1)
        walk(window,[],0)
        return {'title':meta['title'],'window':meta,'nodes':nodes,'full':len(nodes)>=limit or time.monotonic()>deadline}
    if op in ('click','set_value'):
        node=window
        for index in request['path']:
            children=node.get('AXChildren',[]) or []
            if index<0 or index>=len(children):raise RuntimeError('CLAIRE:CHANGED')
            node=children[index]
        if node.get('AXRole')!=request['role']:raise RuntimeError('CLAIRE:CHANGED')
        if node.get('AXEnabled',True) is False:raise RuntimeError('CLAIRE:CHANGED')
        if request.get('expected_label') and request['expected_label'] not in (node.get('AXTitle'),node.get('AXDescription')):raise RuntimeError('CLAIRE:CHANGED')
        if op=='click':node.press()
        else:
            try:node.set('AXFocused',True)
            except RuntimeError:pass
            node.set('AXValue',request.get('text',''))
        return {'ok':True}
    raise RuntimeError('Unsupported native AX operation')


# Browser chrome is read through AX, never by traversing the page's DOM.
_GROUPS={'AXGroup','AXToolbar','AXSplitGroup','AXLayoutArea','AXUnknown'}

def browser_state(app,pid,expected=None):
    ws=windows(app)
    if not ws:raise RuntimeError('CLAIRE:NO_WINDOW')
    window=next((w for w in ws if w.get('AXMain')),ws[0]);meta=info(window,pid)
    if expected is not None and expected!=meta['id']:raise RuntimeError('CLAIRE:CHANGED')
    deadline=time.monotonic()+3
    def find(parent,depth=0):
        if depth>7 or time.monotonic()>deadline:return None
        kids=parent.get('AXChildren',[]) or []
        for child in kids:
            if child.get('AXRole')=='AXTabGroup':
                tabs=[n for n in child.get('AXChildren',[]) or [] if n.get('AXRole') in ('AXRadioButton','AXTab')]
                if tabs:return tabs
        for child in kids:
            if child.get('AXRole') in _GROUPS:
                found=find(child,depth+1)
                if found:return found
        return None
    tabs=find(window)
    if tabs is None:raise RuntimeError('CLAIRE:NO_TABS')
    def address(parent,depth=0):
        if depth>7 or time.monotonic()>deadline:return ''
        kids=parent.get('AXChildren',[]) or []
        for child in kids:
            if child.get('AXRole') in ('AXTextField','AXComboBox') and re.search(r'address|search or enter|search the web',str(child.get('AXTitle') or '')+' '+str(child.get('AXDescription') or ''),re.I):
                return str(child.get('AXValue') or '')
        for child in kids:
            if child.get('AXRole') in _GROUPS:
                found=address(child,depth+1)
                if found:return found
        return ''
    state={'window':meta,'tabs':[{'title':t.get('AXTitle') or t.get('AXDescription') or '',
                                 'selected':t.get('AXValue') in (True,1,'true','1'),'identity':str(cfhash(t.ref))} for t in tabs],
           'url':address(window)}
    return state,tabs,window
