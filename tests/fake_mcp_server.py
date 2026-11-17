"""A tiny MCP server over stdio, for tests. Speaks just enough of the protocol."""
import json
import os
import sys

TOOLS = [
    {"name": "echo", "description": "Repeat the text back.",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string", "description": "what to repeat"}}}},
    {"name": "write_outside", "description": "Try to write a file outside this server's folder.",
     "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}}},
    {"name": "show_env", "description": "Report which named variables are set.",
     "inputSchema": {"type": "object", "properties": {"names": {"type": "string"}}}},
    {"name": "bad name!", "description": "A name no client should accept."},
]

for line in sys.stdin:
    try:
        msg = json.loads(line)
    except ValueError:
        continue
    method, mid = msg.get("method"), msg.get("id")
    if mid is None:
        continue                                   # a notification
    if method == "initialize":
        result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                  "serverInfo": {"name": "fake-mcp", "version": "1.0"}}
    elif method == "tools/list":
        print("this is a stray log line, not JSON", flush=True)
        result = {"tools": TOOLS}
    elif method == "tools/call":
        name, args = msg["params"]["name"], msg["params"].get("arguments") or {}
        if name == "echo":
            result = {"content": [{"type": "text", "text": "echo: " + str(args.get("text", ""))}]}
        elif name == "write_outside":
            try:
                with open(args["path"], "w") as f:
                    f.write("escaped")
                result = {"content": [{"type": "text", "text": "WROTE"}]}
            except OSError as exc:
                result = {"content": [{"type": "text", "text": f"blocked: {exc.strerror}"}], "isError": True}
        elif name == "hang":                       # unlisted: a call that never answers
            import time
            time.sleep(60)
            continue
        elif name == "show_env":
            names = str(args.get("names", "")).split(",")
            result = {"content": [{"type": "text", "text": ";".join(
                f"{n}={os.environ.get(n, '<unset>')}" for n in names)}]}
        else:
            print(json.dumps({"jsonrpc": "2.0", "id": mid,
                              "error": {"code": -32602, "message": "unknown tool"}}), flush=True)
            continue
    else:
        print(json.dumps({"jsonrpc": "2.0", "id": mid,
                          "error": {"code": -32601, "message": "method not found"}}), flush=True)
        continue
    print(json.dumps({"jsonrpc": "2.0", "id": mid, "result": result}), flush=True)
