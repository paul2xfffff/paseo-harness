from _assert import check, done
import json, os, queue, shutil, subprocess, sys, threading
ROOT = "/tmp/acp_usage12"
shutil.rmtree(ROOT, ignore_errors=True); os.makedirs(ROOT)
p = subprocess.Popen([sys.executable, "%s/drivers/driver_usage12.py" % os.path.dirname(os.path.abspath(__file__))], cwd=ROOT, stdin=subprocess.PIPE,
                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
lines = queue.Queue()
threading.Thread(target=lambda: [lines.put(l) for l in p.stdout], daemon=True).start()
seq = [0]; seen = []
def send(o): p.stdin.write(json.dumps(o) + "\n"); p.stdin.flush()
def read(t=20): return json.loads(lines.get(timeout=t))
def req(method, params):
    seq[0] += 1; send({"jsonrpc": "2.0", "id": seq[0], "method": method, "params": params})
    while True:
        m = read()
        if m.get("method") == "session/update": seen.append(m["params"]["update"])
        if m.get("id") == seq[0] and "method" not in m: return m
req("initialize", {"protocolVersion": 1, "clientCapabilities": {}})
sid = req("session/new", {"cwd": ROOT, "mcpServers": []})["result"]["sessionId"]
res = req("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "hi"}]})["result"]
check("prompt: end_turn", res.get("stopReason") == "end_turn", repr(res))
usage = res.get("usage") or {}
check("usage: input is reported", usage.get("inputTokens") == 1000, repr(usage))
check("usage: output is reported", usage.get("outputTokens") == 50, repr(usage))
check("usage: cache reads are reported separately", usage.get("cachedReadTokens") == 800, repr(usage))
check("usage: total is input + output", usage.get("totalTokens") == 1050, repr(usage))
check("usage: total does not double-count cache", usage.get("totalTokens") == usage.get("inputTokens", 0) + usage.get("outputTokens", 0),
      repr(usage))
updates = [u for u in seen if u.get("sessionUpdate") == "usage_update"]
check("usage_update: was emitted", len(updates) == 1, "%d updates" % len(updates))
check("usage_update: reports used", updates and updates[0].get("used") == 1050, repr(updates))
check("usage_update: reports the window size", updates and updates[0].get("size") == 524288, repr(updates))
p.stdin.close()
p.wait(timeout=10)
check("process: exit 0", p.returncode == 0, repr(p.returncode))
done("ACP USAGE")
