"""A typed, scoped computer API; app permissions remain in connectors."""
import json
from ..computer import Computer, ComputerError
from ..connectors import ConnectorError
from . import Tool


def computer(ctx,args):
    core=ctx.session.core
    if not hasattr(core,'computer'):
        core.computer=Computer(core)
    try:
        result=core.computer.call(ctx,args)
        return ('' if result['ok'] else 'ERROR: ')+json.dumps(result,ensure_ascii=False)
    except ComputerError as exc:
        result={'ok':False,'code':exc.code,'message':str(exc),'performed':exc.performed,**exc.details}
    except ConnectorError as exc:
        text=str(exc)
        code='permission_required' if 'macOS has not allowed' in text else 'stale_observation' if 'window changed' in text else 'connector_unavailable'
        result={'ok':False,'code':code,'message':text,'performed':False}
    if hasattr(core,'computer'):core.computer.receipts[ctx.thread_id]=result
    return ('REFUSED: ' if result['code']=='denied' else 'ERROR: ')+json.dumps(result,ensure_ascii=False)


TOOLS=[Tool('computer',computer,needs=('connectors',),kinds=('home','agent'),params=('app','tab','observation','target','text','key','modifiers','direction','amount','x','y','quality'))]
