from _assert import check, done, read_lines
import json, os, shutil, subprocess, sys
ROOT = "/tmp/acp_perm"
shutil.rmtree(ROOT, ignore_errors=True); os.makedirs(ROOT)
p = subprocess.Popen([sys.executable, "%s/drivers/driver_tools.py" % os.path.dirname(os.path.abspath(__file__))], cwd=ROOT,
                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                     text=True, encoding="utf-8", bufsize=1)
seq = [0]

def send(obj):
    p.stdin.write(json.dumps(obj) + "\n"); p.stdin.flush()

read = read_lines(p)

def request(method, params):
    seq[0] += 1; rid = seq[0]
    send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
    while True:
        m = read()
        if m.get("id") == rid and "method" not in m: return m
        collect(m)

def collect(m):
    if m.get("method") == "session/request_permission": return m
    if m.get("method") == "session/update": updates.append(m["params"]["update"])
    return m

def turn(text, permission=None):
    updates.clear()
    seq[0] += 1; rid = seq[0]
    send({"jsonrpc": "2.0", "id": rid, "method": "session/prompt",
          "params": {"sessionId": sid, "prompt": [{"type": "text", "text": text}]}})
    asked = []
    while True:
        m = read(12)
        if m.get("id") == rid and "method" not in m: break
        if m.get("method") == "session/request_permission":
            asked.append(m["params"])
            send({"jsonrpc": "2.0", "id": m["id"], "result": {"outcome": {"outcome": "selected", "optionId": permission}}})
        else:
            collect(m)
    return m, asked, list(updates)

init = request("initialize", {"protocolVersion": 1, "clientCapabilities": {}})
check("initialize: negotiated", "result" in init, repr(init)[:80])
new = request("session/new", {"cwd": ROOT, "mcpServers": []})
sid = new["result"]["sessionId"]
check("session/new: returns an id", bool(sid), repr(sid)[:40])
check("session/new: advertises config options", len(new["result"].get("configOptions") or []) >= 1, repr(new["result"].get("configOptions"))[:80])
updates = []
req, asked, u = turn("/approval ask")
check("approval: a command answers", any("/approval" in str(x) or "approval:" in str(x) for x in u), repr(u)[-120:])

req, asked, u = turn("make one", "allow_once")
check("allow_once: turn completes", req["result"]["stopReason"] == "end_turn", repr(req["result"]))
check("allow_once: a permission request arrived", len(asked) == 1, "%d requests" % len(asked))
check("allow_once: it names the tool", bool(asked) and "write" in json.dumps(asked[0]).lower(), repr(asked)[:100])
check("allow_once: it offers options", bool(asked) and len(asked[0]["toolCall"].get("options") or asked[0].get("options") or []) >= 1,
      repr(asked[0])[:150])
check("allow_once: the tool ran", os.path.exists(ROOT + "/out1.txt"), "no file")
check("allow_once: a tool_call_update carried content",
      any(x.get("sessionUpdate") == "tool_call_update" and x.get("content") for x in u), repr(u)[-120:])
titles = [x.get("title") for x in u if x.get("sessionUpdate") == "tool_call"]
statuses = [asked[0]["toolCall"].get("status")] if asked else []
check("allow_once: the request reports a status", statuses != [None], repr(statuses))

req, asked, u = turn("make two", "reject_once")
failed = [x for x in u if x.get("sessionUpdate") == "tool_call_update" and x.get("status") == "failed"]
check("reject_once: turn completes", req["result"]["stopReason"] == "end_turn", repr(req["result"]))
check("reject_once: the file was NOT written", not os.path.exists(ROOT + "/out2.txt"), "it was written")
check("reject_once: the tool call is marked failed", len(failed) >= 1, "%d failed updates" % len(failed))
check("reject_once: the model is told it was denied",
      any("permission denied" in json.dumps(x).lower() for x in failed), repr(failed)[:150])

req, asked, u = turn("make three", "allow_always")
check("allow_always: turn completes", req["result"]["stopReason"] == "end_turn", repr(req["result"]))
check("allow_always: the tool ran", os.path.exists(ROOT + "/out3.txt"), "no file")

req, asked, u = turn("make four", "allow_once")
check("always: not asked a second time", len(asked) == 0, "%d asks" % len(asked))
check("always: the second tool ran", os.path.exists(ROOT + "/out4.txt"), "no file")

req, asked, u = turn("/cost")
check("cost: /cost answers", any("usage:" in json.dumps(x) or "cost:" in json.dumps(x) for x in u), repr(u)[-120:])
check("cost: no model turn was needed", req["result"]["stopReason"] == "end_turn", repr(req["result"]))

p.stdin.close()
p.wait(timeout=10)
check("process: exit 0", p.returncode == 0, repr(p.returncode))
done("ACP PERMISSIONS")
