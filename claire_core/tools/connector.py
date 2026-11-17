"""The one tool through which a model reaches a connected service."""

import json
import threading

from ..connectors import ConnectorError
from . import Tool


def use_connector(ctx, args):
    connectors = ctx.session.core.connectors
    iid = (args.get("connector") or "").strip()
    action = (args.get("action") or "help").strip()
    if not iid:
        rows = connectors.connected()
        if not rows:
            return "ERROR: no services are connected. Sir connects them; you cannot."
        return ("ERROR: say which connector. Connected: "
                + ", ".join(f"`{r['id']}` ({r['name']})" for r in rows))
    raw = (args.get("input") or "").strip()
    payload = {}
    if raw:
        try:
            payload = json.loads(raw)
        except ValueError as exc:
            return f"ERROR: <input> is not valid JSON ({exc}). Send a JSON object."
        if not isinstance(payload, dict):
            return "ERROR: <input> must be a JSON object, like {\"query\": \"...\"}"
    row=connectors.store.integration(iid)
    if (row and row['provider']=='mac' and action in ('look','screenshot','see','view','screen')
            and ctx.settings.roles.get(ctx.settings.vision_role)):
        from .computer import computer
        return computer(ctx,{'action':'look','app':str(payload.get('app') or ''),
                             'query':str(payload.get('question') or getattr(ctx.session,'_active_request','') or 'Describe the visible window.')})
    try:
        if action == "help":
            if getattr(ctx.session, "_connector_help", None) is None:
                ctx.session._connector_help = set()
            ctx.session._connector_help.add(iid)
            return connectors.help(iid, compact=getattr(ctx.session, "profile", "") == "controller")
        # Reading goes ahead: sir chose what this connector may read when he
        # connected it. Anything that changes data asks, in every mode.
        session = ctx.session
        if session.cancel.is_set():
            return "ERROR: this task was cancelled."
        finished = threading.Event()

        def watch():
            # A cancelled task must not wait out a stuck call, and must not
            # leave a browser running behind it.
            while not finished.wait(0.2):
                if session.cancel.is_set():
                    connectors.abort(session)
                    return
        threading.Thread(target=watch, name="claire-connector-watch", daemon=True).start()
        try:
            text = connectors.call(
                iid, action, payload, owner=session,
                ask=lambda title, flag: ctx.ask("connector", title, flag, always=True))
            pictures=connectors.take_images()
            if pictures and ctx.settings.roles.get(ctx.settings.vision_role) and not ctx.session.model.get('vision'):
                from ..vision import inspect_images
                from ..computer import ComputerError
                try:
                    seen=inspect_images(ctx.session.core,pictures,getattr(ctx.session,'_active_request','') or 'Describe the screenshot.',session.cancel)
                    text+='\n\nVisual model observation from '+seen['model']+':\n'+seen['text']
                except ComputerError as exc:
                    return f'ERROR: visual inspection failed ({exc.code}): {exc}'
            else:
                ctx.images.extend(pictures)
            return text
        finally:
            finished.set()
    except ConnectorError as exc:
        text = str(exc)
        # A call that failed before this connector's help was ever read: hand
        # the manual over now, once, instead of letting the guessing go on.
        read = getattr(ctx.session, "_connector_help", None)
        if read is None:
            read = ctx.session._connector_help = set()
        if action != "help" and iid not in read:
            read.add(iid)
            try:
                text += ("\n\nYou had not read this connector's help. Here it is; follow it "
                         "exactly for your next call.\n\n" + connectors.help(iid, compact=getattr(ctx.session, "profile", "") == "controller"))
            except ConnectorError:
                pass
        # The service being down or refusing us is not something a different
        # request will fix; say so once and let the task carry on without it.
        if text.startswith(("could not reach", "the service is failing",
                            "the server stopped", "the server did not answer")):
            return (f"ERROR: connector {iid}: {text}\n"
                    "The service is unavailable; report the failure or retry later. "
                    "This does not establish a bug in the connector code.")
        return f"ERROR: {text}"


TOOLS = [Tool("use_connector", use_connector, needs=("connectors",),
              kinds=("agent", "home"))]
