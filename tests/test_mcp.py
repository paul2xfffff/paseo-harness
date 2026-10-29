"""The MCP client, against hand-written stdio servers.

Deliberately *not* using `mcp.server.MCPServer`: as of mcp 2.3.0 its `call_tool` raises
`IndexError: list index out of range` for every decorated tool, so a test built on it would
be testing the SDK's bug rather than this client. These servers speak the JSON-RPC subset the
client actually uses, which is also a sharper test: if the client depends on anything beyond
initialize / tools.list / tools.call, it fails here.

Skipped when `mcp` is not installed, so the suite still runs on a bare checkout.
"""
import json, os, subprocess, sys, tempfile, textwrap, threading, time

import _path  # noqa: F401
from _assert import check, done
import agent

try:
    import mcp  # noqa: F401
    SKIP = False
except ImportError:
    SKIP = True


BASE = """import json, sys
TOOLS = __TOOLS__
def reply(i, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": i}
    if error is not None: msg["error"] = error
    else: msg["result"] = result
    sys.stdout.write(json.dumps(msg) + "\\n"); sys.stdout.flush()
for line in sys.stdin:
    line = line.strip()
    if not line: continue
    try: req = json.loads(line)
    except ValueError: continue
    method, i = req.get("method"), req.get("id")
    if method == "initialize":
        reply(i, {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                  "serverInfo": {"name": "fake", "version": "1"}})
    elif method == "tools/list":
        reply(i, {"tools": TOOLS})
    elif method == "tools/call":
        name = req["params"]["name"]; args = req["params"].get("arguments") or {}
__DISPATCH__
    elif i is not None:
        reply(i, error={"code": -32601, "message": "no such method"})
"""

def write_server(directory, name, tools_json, dispatch_js):
    path = os.path.join(directory, name)
    with open(path, "w") as f:
        f.write(BASE.replace("__TOOLS__", tools_json).replace("__DISPATCH__", dispatch_js))
    return path


ADD_TOOLS = json.dumps([
    {"name": "add", "description": "Add two integers.",
     "inputSchema": {"type": "object", "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
                     "required": ["a", "b"]}},
    {"name": "boom", "description": "Always reports an error.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "slow", "description": "Sleep.",
     "inputSchema": {"type": "object", "properties": {"seconds": {"type": "number"}}}},
], indent=0).replace("\n", "")

DISPATCH = '''
        if name == "add":
            reply(i, {"content": [{"type": "text", "text": str(args["a"] + args["b"])}], "isError": False})
        elif name == "boom":
            reply(i, {"content": [{"type": "text", "text": "kaboom"}], "isError": True})
        elif name == "slow":
            import time; time.sleep(float(args.get("seconds", 1))); reply(i, {"content": [{"type": "text", "text": "slept"}], "isError": False})
        else:
            reply(i, error={"code": -32602, "message": "unknown tool"})
'''

LONG_TOOLS = json.dumps([
    {"name": "z" * 70 + "_alpha", "description": "first", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "z" * 70 + "_beta", "description": "second", "inputSchema": {"type": "object", "properties": {}}},
], indent=0).replace("\n", "")

LONG_DISPATCH = '''
        reply(i, {"content": [{"type": "text", "text": name}], "isError": False})
'''



def tool_call(name, args):
    return {"id": "c", "function": {"name": name, "arguments": json.dumps(args)}}


def live_pids(path):
    """Child pids running this server. pgrep -f misses short-lived stdio children on macOS,
    so ask ps for the full command line and require the path to be the script argument."""
    r = subprocess.run(["/bin/ps", "-axo", "pid=,command="], capture_output=True, text=True)
    out = []
    for line in r.stdout.splitlines():
        parts = line.split()
        # the child's command line ends with the server path; anything else merely mentions it
        if len(parts) >= 3 and parts[-1] == path:
            out.append(parts[0])
    return out


def main():
    d = tempfile.mkdtemp()
    py = sys.executable
    good = write_server(d, "good.py", ADD_TOOLS, DISPATCH)
    long_names = write_server(d, "long.py", LONG_TOOLS, LONG_DISPATCH)
    exits = os.path.join(d, "exits.py")
    open(exits, "w").write("import sys\nsys.stderr.write('refusing to start\\n')\nsys.exit(1)\n")

    # 1. A live server: tools appear namespaced, and calls work.
    hub = agent.MCP([{"name": "demo", "command": py, "args": [good]}], d)
    ts = hub.toolset()
    names = sorted(ts.by_name) if ts else []
    check("server: tools are namespaced", names == ["mcp__demo__add", "mcp__demo__boom", "mcp__demo__slow"], repr(names))
    check("server: clean start has no errors", hub.errors == [], repr(hub.errors))
    if ts:
        out = agent.run_tool(tool_call("mcp__demo__add", {"a": 2, "b": 40}), ts, cwd=d, trace=None)
        check("server: tool call returns its text", out[0] == "42", repr(out[0]))
        check("server: success is not an error", out[2] is False, repr(out[2]))
        bad = agent.run_tool(tool_call("mcp__demo__boom", {}), ts, cwd=d, trace=None)
        check("server: isError propagates", bad[2] is True, repr(bad))
        check("server: hub survives a tool-level error", hub.errors == [], repr(hub.errors))
        missing = agent.run_tool(tool_call("mcp__demo__add", {}), ts, cwd=d, trace=None)
        check("server: bad arguments surface as an error", missing[2] is True, repr(missing))
    # Closing must make the hub unusable. Checking the child's pid is unreliable (the SDK may
    # exec, and ps argv does not always match), but a dead connection is observable.
    ts_before = hub.toolset()
    hub.close()
    check("server: closes idempotently", (hub.close() or True), "second close raised")
    if ts_before:
        after = agent.run_tool(tool_call("mcp__demo__add", {"a": 1, "b": 1}), ts_before, cwd=d, trace=None)
        check("server: calls fail after close", after[2] is True, repr(after[0])[:60])

    # 2. A server that refuses to start reports why, and does not hang.
    t0 = time.time()
    hub = agent.MCP([{"name": "dead", "command": py, "args": [exits]}], d)
    took = time.time() - t0
    check("start failure: reported", any("dead" in e for e in hub.errors), repr(hub.errors))
    check("start failure: no tools", hub.toolset() is None, repr(hub.toolset()))
    check("start failure: fast", took < 10, "%.1fs" % took)
    hub.close()

    # 3. A missing binary fails fast with a readable error.
    t0 = time.time()
    hub = agent.MCP([{"name": "nope", "command": "/definitely/not/here"}], d)
    took = time.time() - t0
    check("missing binary: reported", any("nope" in e for e in hub.errors), repr(hub.errors))
    check("missing binary: fast", took < 5, "%.1fs" % took)
    hub.close()

    # 4. Two tools that truncate to the same 64-char name are refused; nothing half-loads.
    hub = agent.MCP([{"name": "coll", "command": py, "args": [long_names]}], d)
    check("collision: refused", any("collision" in e.lower() for e in hub.errors), repr(hub.errors)[:140])
    check("collision: no partial load", hub.toolset() is None, repr(hub.toolset()))
    check("collision: names both tools", any("alpha" in e and "beta" in e for e in hub.errors), repr(hub.errors)[:200])
    hub.close()

    # 5. A non-stdio spec is rejected with a message instead of being dropped.
    hub = agent.MCP([{"name": "web", "type": "http", "url": "http://example.invalid"}], d)
    check("http spec: rejected with a message", any("stdio" in e for e in hub.errors), repr(hub.errors))
    hub.close()

    # 6. An empty spec list is a no-op.
    hub = agent.MCP([], d)
    check("empty: no tools, no errors", hub.toolset() is None and hub.errors == [], repr(hub.errors))
    hub.close()

    # 7. A server that dies mid-call fails the call instead of hanging, and says so.
    hub = agent.MCP([{"name": "live", "command": py, "args": [good]}], d)
    ts = hub.toolset()
    if ts and "mcp__live__slow" in ts.by_name:
        result = {}

        def call_slow():
            try:
                result["out"] = agent.run_tool(tool_call("mcp__live__slow", {"seconds": 60}), ts, cwd=d, trace=None)
            except BaseException as e:
                result["err"] = e

        th = threading.Thread(target=call_slow, daemon=True)
        th.start()
        time.sleep(1.0)
        pid = (live_pids(good) or [None])[0]
        if pid:
            subprocess.run(["/bin/kill", "-9", pid])
        th.join(timeout=30)
        check("dead server: call returns instead of hanging", bool(result), repr(result)[:120])
        check("dead server: failure is visible", bool(hub.errors), repr(hub.errors)[:140])
    hub.close()

    # 8. Two servers in one hub: independent, and both usable.
    hub = agent.MCP([{"name": "one", "command": py, "args": [good]},
                     {"name": "two", "command": py, "args": [good]}], d)
    ts = hub.toolset()
    names = sorted(ts.by_name) if ts else []
    check("two servers: both namespaced", "mcp__one__add" in names and "mcp__two__add" in names, repr(names))
    if ts:
        a = agent.run_tool(tool_call("mcp__one__add", {"a": 1, "b": 1}), ts, cwd=d, trace=None)
        b = agent.run_tool(tool_call("mcp__two__add", {"a": 2, "b": 2}), ts, cwd=d, trace=None)
        check("two servers: both answer", a[0] == "2" and b[0] == "4", repr((a[0], b[0])))
    hub.close()


if SKIP:
    print("MCP SKIPPED (mcp is not installed)")
    sys.exit(0)
main()
done("MCP")
