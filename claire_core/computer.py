"""Scoped native-app control with verified receipts and owned browser tabs."""
import copy
import json
import re
import threading
import time
import uuid
from urllib.parse import urlparse

from .connectors import ConnectorError


class ComputerError(Exception):
    def __init__(self, code, message, performed=False, **details):
        super().__init__(message)
        self.code, self.performed, self.details = code, performed, details


def token(prefix):
    return prefix + '_' + uuid.uuid4().hex[:12]


def checked_url(raw):
    raw=str(raw or '').strip()
    if not raw or any(c.isspace() for c in raw):
        raise ComputerError('invalid_url','Supply the requested website URL without spaces or keyboard commands.')
    if '://' not in raw:
        if re.match(r'^[a-zA-Z][a-zA-Z0-9+.-]*:',raw) and not re.match(r'^[^/:]+:\d+(?:/|$)',raw):
            raise ComputerError('invalid_url','Executable URL schemes are not supported.')
        raw='https://'+raw
    try:
        parsed=urlparse(raw)
        parsed.port
    except ValueError:raise ComputerError('invalid_url','Malformed website URL or port.')
    if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password:
        raise ComputerError('invalid_url','Only HTTP/HTTPS website URLs without embedded credentials are supported.')
    return raw


class Computer:
    def __init__(self, core):
        self.core=core
        self.lock=threading.RLock()
        self.observations={}
        self.tabs={}
        self.receipts={}
        self.targets={}

    def forget(self, owner):
        with self.lock:
            self.observations={k:v for k,v in self.observations.items() if v['owner']!=owner}
            self.tabs={k:v for k,v in self.tabs.items() if v['owner']!=owner}
            self.targets={k:v for k,v in self.targets.items() if k[0]!=owner}
            self.receipts.pop(owner,None)

    def state(self, session):
        return self.receipts.get(session.thread_id)

    def _resolve(self, session, args):
        app=str(args.get('app') or '').strip()
        observation=self.observations.get(args.get('observation'))
        if not app and observation and observation['owner']==session.thread_id:app=observation['app']
        ref=args.get('tab')
        if ref:
            item=self.tabs.get(ref)
            if not item or item['owner']!=session.thread_id:
                raise ComputerError('unknown_tab','That tab reference was not issued to this conversation. Inspect tabs again.')
            app=item['app']
        rows=self.core.connectors.connected()+self.core.connectors.failing()
        choices=[(r,a) for r in rows if r['provider']=='mac' for a in r['config'].get('apps',[])
                 if not app or str(a).lower()==app.lower()]
        if not app:
            from .connectors.mac import BROWSERS
            choices=[(r,a) for r,a in choices if a.lower() in BROWSERS]
        live=[(r,a) for r,a in choices if r['status']=='connected']
        if live:choices=live
        if len(choices)!=1:
            raise ComputerError('choose_app','Name one connected, allowed app.', choices=[a for _,a in choices])
        row,app=choices[0]
        row=self.core.connectors._usable(row['id'])
        return row,self.core.connectors.providers['mac'],app

    def _native(self, provider, app, op, **args):
        return provider._run(op=op,app=app,**args)

    def _remember_tab(self, session, row, app, state, index, owned=False):
        ref=token('tab')
        self.targets[(session.thread_id,app)]=state['window'].get('process_id')
        self.tabs[ref]={'owner':session.thread_id,'connector':row['id'],'app':app,
                        'state':copy.deepcopy(state),'index':index,'owned':owned}
        return ref

    @staticmethod
    def _expected(item):
        state=item['state']
        result={'expected_window':state['window'].get('id'),
                'process_id':state['window'].get('process_id'),
                'expected_tabs':[t['title'] for t in state['tabs']], 'expected_index':item['index']}
        if all(t.get('identity') for t in state['tabs']):result['expected_tab_ids']=[t['identity'] for t in state['tabs']]
        return result

    def _receipt(self, session, action, app, performed=False, verified=True, **data):
        result={'ok':bool(verified),'action':action,'app':app,'performed':bool(performed),
                'verified':bool(verified),**data}
        self.receipts[session.thread_id]=result
        return result

    def call(self, ctx, args):
        session=ctx.session
        action=str(args.get('action') or '')
        fields={'apps':set(),'open':set(),'inspect':{'query','observation'},'new_tab':{'url'},'tabs':set(),
                'navigate':{'tab','url'},'select_tab':{'tab'},'close_tab':{'tab'},'look':{'query','quality'},
                'click':{'observation','target'},'set_text':{'observation','target','text'},
                'type':{'observation','text'},'key':{'observation','key','modifiers'},
                'scroll':{'observation','direction','amount'},'click_at':{'observation','x','y','query','target'}}
        if action not in fields:raise ComputerError('unknown_action','Choose a documented computer action.')
        extra=set(args)-fields[action]-{'action','app'}
        if extra:raise ComputerError('invalid_arguments','Unexpected computer parameters: '+', '.join(sorted(extra)))
        with self.lock:
            now=time.monotonic()
            self.observations={k:v for k,v in self.observations.items() if now-v['at']<=60}
            if action=='apps':
                rows=self.core.connectors.connected()+self.core.connectors.failing()
                return self._receipt(session,action,'',apps=[{'app':a,'connector':r['id'],'status':r['status'],'error':r.get('error') or ''}
                    for r in rows if r['provider']=='mac' for a in r['config'].get('apps',[])])
            row,provider,app=self._resolve(session,args)
            lock=getattr(provider,'_interaction_lock',self.lock)
            observed=self.observations.get(args.get('observation')) or self.tabs.get(args.get('tab')) or {}
            scoped=observed.get('window') or observed.get('state',{}).get('window',{})
            with lock, provider.control_scope(scoped.get('process_id') or self.targets.get((session.thread_id,app)),scoped.get('id')):
                if row['config'].get('ask_every') and action not in ('apps',):
                    if not ctx.ask('computer',f'{action} in {app}','CHANGES DATA',always=True):
                        raise ComputerError('denied','User denied this action. Do not retry.')
                provider.claim(row,session)
                if action in ('new_tab','navigate','select_tab','close_tab','tabs'):
                    return self._browser(ctx,row,provider,app,action,args)
                if action in ('inspect','open'):
                    if action=='open':self._native(provider,app,'open')
                    found=self._native(provider,app,'snapshot',max=400 if args.get('query') else 120)
                    if args.get('query'):
                        needle=str(args['query']).casefold()
                        selected=[n for n in found['nodes'] if needle in ' '.join(str(n.get(k) or '') for k in ('name','desc','value')).casefold()]
                        paths=[n['path'] for n in selected]
                        found['nodes']=[n for n in found['nodes'] if any(n['path']==p[:len(n['path'])] or n['path'][:len(p)]==p for p in paths)][:120]
                    text=provider._format_snapshot(row['id'],app,found)
                    self.targets[(session.thread_id,app)]=(found.get('window') or {}).get('process_id')
                    obs=token('view')
                    self.observations[obs]={'owner':session.thread_id,'app':app,'connector':row['id'],
                        'at':time.monotonic(),'window':found.get('window') or {},
                        'refs':copy.deepcopy(provider._seen[(row['id'],app)])}
                    return self._receipt(session,action,app,action=='open',observation=obs,
                                         window=found.get('window'),controls=text[:6500])
                if action=='look':
                    from .vision import inspect_window
                    session.emit('status',label='inspecting screenshot',preview=str(args.get('quality') or 'fast')+' vision specialist')
                    question=str(args.get('query') or 'Describe the visible screen relevant to the user request.')
                    task=str(getattr(session,'_active_request','') or '')[:2500]
                    if task and not re.search(r'\b(click|press|type|fill|set|open|close|navigate|save|submit|delete|send|scroll)\b',task,re.I):
                        question='User visual question: '+task+'\nCurrent visual question: '+question
                    analysis=inspect_window(self.core,provider,row['id'],app,question,session.cancel,str(args.get('quality') or 'fast'))
                    obs=token('view')
                    self.observations[obs]={'owner':session.thread_id,'app':app,'at':analysis.get('captured_at',time.monotonic()),'window':analysis['window'],'visual':True}
                    return self._receipt(session,action,app,observation=obs,analysis=analysis)
                if action in ('click','set_text','type','key','scroll','click_at'):
                    obs=self.observations.get(args.get('observation'))
                    if not obs or obs['owner']!=session.thread_id or obs['app']!=app or time.monotonic()-obs['at']>60:
                        raise ComputerError('stale_observation','Inspect the current window first; use its fresh observation ID.')
                    if action=='click_at':
                        if not obs.get('visual'):
                            raise ComputerError('vision_required','Use look to locate the actual visual target before clicking coordinates.')
                        current=self._native(provider,app,'window')
                        if any(current.get(k)!=obs['window'].get(k) for k in ('id','title','x','y','w','h')):
                            raise ComputerError('stale_observation','The pictured window changed. Look again before clicking.')
                        raw_x,raw_y=args.get('x'),args.get('y')
                        if raw_x is None and raw_y is None:
                            pair=re.fullmatch(r'\s*[\[(]?\s*([0-9]+(?:\.[0-9]+)?)\s*,\s*([0-9]+(?:\.[0-9]+)?)\s*[\])]?\s*',str(args.get('target') or ''))
                            if pair:raw_x,raw_y=pair.groups()
                        try:x,y=float(raw_x),float(raw_y)
                        except (TypeError,ValueError):raise ComputerError('invalid_point','Use separate x and y arguments, e.g. x=420, y=395, from the observed 0-1000 image grid.')
                        if not(0<=x<=1000 and 0<=y<=1000):raise ComputerError('invalid_point','Point is outside the observed image.')
                        payload={'app':app,'x':x,'y':y,'what':str(args.get('query') or args.get('target') or '')}
                        op=next(o for o in provider.operations(row) if o.name=='click_at')
                        why=provider.review(row,op,payload)
                        if why and not ctx.ask('computer',why,'MAJOR ACTION',always=True):
                            raise ComputerError('denied','User denied this click. Do not retry.')
                        provider.claim(row,session)
                        self._native(provider,app,'click_at',x=round(current['x']+x/1000*current['w'],1),y=round(current['y']+y/1000*current['h'],1),double=False)
                        self.observations.pop(args['observation'],None)
                        return self._receipt(session,action,app,True,requires_inspection=True)
                    current=self._native(provider,app,'snapshot',max=120)
                    old=obs['window'];new=current.get('window') or {}
                    if (old.get('id') is not None and new.get('id')!=old['id']) or new.get('title')!=old.get('title'):
                        raise ComputerError('stale_observation','The window changed. Inspect it again before acting.')
                    target=args.get('target')
                    if not obs.get('refs'):
                        raise ComputerError('needs_control_references','This is a visual observation, not accessibility control references. Use inspect for target refs, or look to locate a point and click_at with its x/y coordinates.')
                    if action in ('click','set_text'):
                        node=obs['refs'].get(target)
                        match=next((n for n in current['nodes'] if node and n['path']==node['path']),None)
                        if match and not match.get('enabled',True):
                            raise ComputerError('disabled_control','That control is disabled; nothing was clicked.')
                        if not match or match['role']!=node['role'] or (node.get('label') and node['label'] not in (match.get('name'),match.get('desc'))):
                            raise ComputerError('stale_observation','That control changed; inspect again. Nothing was clicked.')
                    provider._seen[(row['id'],app)]=copy.deepcopy(obs['refs'])
                    payload={'app':app}
                    for key in ('target','text','key','direction','amount'):
                        if key in args:payload[key]=args[key]
                    if args.get('modifiers'):
                        payload['modifiers']=re.split(r'[+,\s]+',args['modifiers'])
                        if any(m not in ('cmd','shift','alt','ctrl') for m in payload['modifiers']):
                            raise ComputerError('invalid_key','Modifiers must be cmd, shift, alt or ctrl.')
                    try:
                        text=self.core.connectors.call(row['id'],action,payload,owner=session,
                            ask=lambda title,flag:ctx.ask('computer',title,flag,always=True))
                    except ConnectorError as exc:
                        raise ComputerError('uncertain_action',f'The action did not return a verified receipt: {exc}. Inspect before repeating anything.',True)
                    if text.startswith(('Sir did not approve','User DENIED','ERROR:')):
                        raise ComputerError('denied' if 'approve' in text else 'action_failed',text)
                    self.observations.pop(args['observation'],None)
                    observed=getattr(provider,'_snapshots',{}).get((row['id'],app))
                    if 'Could not read the window afterwards' in text or observed is None:
                        return self._receipt(session,action,app,True,False,post_action=text[:5500],requires_inspection=True)
                    fresh=token('view')
                    self.observations[fresh]={'owner':session.thread_id,'app':app,'connector':row['id'],
                        'at':time.monotonic(),'window':copy.deepcopy(observed),
                        'refs':copy.deepcopy(provider._seen[(row['id'],app)])}
                    return self._receipt(session,action,app,True,observation=fresh,post_action=text[:5500])
                raise ComputerError('unknown_action','Choose a documented computer action.')

    def _browser(self,ctx,row,provider,app,action,args):
        from .connectors.mac import BROWSERS
        session=ctx.session
        if app.lower() not in BROWSERS:
            raise ComputerError('not_browser','Tab operations require an allowed browser app.')
        if action=='new_tab':
            url=checked_url(args.get('url')) if args.get('url') else None
            opened=self._native(provider,app,'open')
            try:result=self._native(provider,app,'browser_new',process_id=opened.get('process_id'))
            except ConnectorError as exc:
                raise ComputerError('uncertain_action',f'The tab operation did not return a verified receipt: {exc}. Inspect tabs before trying again.',True)
            ref=self._remember_tab(session,row,app,result['after'],result['index'],True)
            if url:
                try:return self._navigate(ctx,provider,app,ref,url,'new_tab')
                except Exception as exc:
                    raise ComputerError('navigation_failed',f'The new tab exists but navigation failed: {exc}',True,tab=ref)
            return self._receipt(session,action,app,True,tab=ref,window=result['after']['window'])
        if action=='tabs':
            state=self._native(provider,app,'browser_state')
            entries=[]
            for i,t in enumerate(state['tabs']):
                ref=self._remember_tab(session,row,app,state,i)
                entries.append({'tab':ref,**t})
            return self._receipt(session,action,app,tabs=entries,window=state['window'])
        ref=args.get('tab');item=self.tabs.get(ref)
        if not item or item['owner']!=session.thread_id or item['app']!=app:
            raise ComputerError('unknown_tab','Use a tab reference from this conversation; never invent a tab ID.')
        if action=='select_tab':
            result=self._native(provider,app,'browser_select',**self._expected(item))
            item['state']=result['after']
            return self._receipt(session,action,app,True,bool(result.get('verified')),tab=ref,title=result['after']['window']['title'])
        if action=='navigate':return self._navigate(ctx,provider,app,ref,checked_url(args.get('url')),'navigate')
        if not item['owned'] and not ctx.ask('computer',f'Close existing tab in {app}: '+item['state']['tabs'][item['index']]['title'],'CLOSE_EXISTING_TAB',always=True):
            raise ComputerError('denied','User denied closing the existing tab. Do not retry.')
        try:result=self._native(provider,app,'browser_close_owned',**self._expected(item))
        except ConnectorError as exc:
            raise ComputerError('uncertain_action',f'Close was not verified: {exc}. Inspect tabs before trying anything else.',True,tab=ref)
        if not result.get('verified'):
            raise ComputerError('verification_failed','A close action ran, but its result could not be verified. Inspect before doing anything else.',True)
        self.tabs.pop(ref,None)
        return self._receipt(session,action,app,True,closed_tab=ref,remaining_tabs=len(result['after']['tabs']))

    def _navigate(self,ctx,provider,app,ref,url,action):
        item=self.tabs[ref]
        try:result=self._native(provider,app,'browser_navigate',url=url,**self._expected(item))
        except ConnectorError as exc:
            raise ComputerError('uncertain_action',f'Navigation was not verified: {exc}. Inspect this tab before retrying.',True,tab=ref)
        item['state']=result['after']
        return self._receipt(ctx.session,action,app,True,bool(result.get('verified')),
            tab=ref,window=result['after']['window'],
            native_tab_identity=result['after']['tabs'][item['index']].get('identity'),
            requested_url=url,observed_url=result['after'].get('url'),title=result['after']['window']['title'],
            note='' if result.get('verified') else 'Navigation was sent but the address was not verified. Inspect this tab; do not open another.')
