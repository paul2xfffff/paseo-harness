from _assert import check, done
import json, os, queue, shutil, subprocess, sys, threading
ROOT = "/tmp/acp_autocompact"
shutil.rmtree(ROOT, ignore_errors=True); os.makedirs(ROOT)
env = dict(os.environ, AGENT_MODEL="small", AGENT_MODELS=json.dumps([
    {"id": "small", "context": 4000, "reserve": 800, "keep_recent": 300, "max_output": 400}]))
p = subprocess.Popen([sys.executable, "%s/drivers/driver_autocompact.py" % os.path.dirname(os.path.abspath(__file__))], cwd=ROOT, env=env,
                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
lines = queue.Queue(); threading.Thread(target=lambda: [lines.put(l) for l in p.stdout], daemon=True).start()
seq = [0]; updates = []
def send(o): p.stdin.write(json.dumps(o) + "\n"); p.stdin.flush()
def read(t=20): return json.loads(lines.get(timeout=t))
def req(method, params):
    seq[0] += 1; send({"jsonrpc": "2.0", "id": seq[0], "method": method, "params": params})
    while True:
        m = read()
        if m.get("method") == "session/update": updates.append(m["params"]["update"])
        if m.get("id") == seq[0] and "method" not in m: return m
req("initialize", {"protocolVersion": 1, "clientCapabilities": {"session": {"compaction": {}}}})
sid = req("session/new", {"cwd": ROOT, "mcpServers": []})["result"]["sessionId"]
res = req("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "go"}]})["result"]
check("turn: completes", res.get("stopReason") == "end_turn", repr(res))
kinds = [u.get("sessionUpdate") for u in updates]
# `compaction_update` is not in ACP's SessionUpdate union (a validating client drops it),
# so the signal is an agent notice plus a usage reset. Assert what a client really sees.
check("auto: no unsupported update is sent", kinds.count("compaction_update") == 0, repr(kinds))
check("auto: the client is told the window is clear",
      any("window is clear" in json.dumps(u) for u in updates), repr([u for u in updates if u.get("sessionUpdate") == "agent_message_chunk"])[:200])
check("auto: usage is reset to zero after compaction",
      any(u.get("sessionUpdate") == "usage_update" and u.get("used") == 0 for u in updates),
      repr([u for u in updates if u.get("sessionUpdate") == "usage_update"]))
check("auto: usage was reported", kinds.count("usage_update") >= 1, repr(kinds))
import _path  # noqa: F401 - this checkout on sys.path, config isolated
import agent
state = agent.load_session(sid, ROOT)
first = (state["messages"][0].get("content") or "")
check("auto: the journal holds a summary", "compacted into the following summary" in first, repr(first)[:100])
check("auto: the history is bounded", len(state["messages"]) <= 6, "%d messages" % len(state["messages"]))
p.stdin.close()
p.wait(timeout=10)
check("process: exit 0", p.returncode == 0, repr(p.returncode))
print("--- stderr:"); stderr = p.stderr.read()
check("process: no traceback", "Traceback" not in stderr, stderr[-300:])
done("ACP AUTOCOMPACT")
