from _assert import check, done, read_lines
import json, os, shutil, subprocess, sys
ROOT = "/tmp/acp_compact"
shutil.rmtree(ROOT, ignore_errors=True); os.makedirs(ROOT)
env = dict(os.environ, AGENT_MODEL="small", AGENT_MODELS='[{"id":"small","context":1200}]', AGENT_KEEP_TOKENS="1")
DRIVER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "drivers", "driver_plain.py")
p = subprocess.Popen([sys.executable, DRIVER], cwd=ROOT, env=env,
                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                     text=True, encoding="utf-8", bufsize=1)
seq = [0]; updates = []
def send(obj): p.stdin.write(json.dumps(obj) + "\n"); p.stdin.flush()
read = read_lines(p)
def request(method, params):
    seq[0] += 1; rid = seq[0]; send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
    while True:
        m = read()
        if m.get("id") == rid and "method" not in m: return m
        if m.get("method") == "session/update": updates.append(m["params"]["update"])
def prompt(text):
    before = len(updates)
    r = request("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": text}]})
    return r, updates[before:]
request("initialize", {"protocolVersion": 1, "clientCapabilities": {"session": {"compaction": {}}}})
sid = request("session/new", {"cwd": ROOT, "mcpServers": []})["result"]["sessionId"]
for text in ("one " + "x" * 500, "two " + "y" * 500):
    r, _ = prompt(text)
    check("turn %s: completes" % text.split()[0], r["result"]["stopReason"] == "end_turn", repr(r["result"]))
r, new = prompt("/compact")
check("/compact: ends the turn", r["result"]["stopReason"] == "end_turn", repr(r["result"]))
said = [u["content"]["text"] for u in new if u.get("sessionUpdate") == "agent_message_chunk" and u.get("content")]
check("/compact: reports what it did", any("Compacted" in t for t in said), repr(said)[:150])
check("/compact: announces the cleared window", any("window is clear" in t for t in said), repr(said)[:150])
import _path  # noqa: F401 - this checkout on sys.path, config isolated
import agent
state = agent.load_session(sid, ROOT)
check("/compact: the history shrank", len(state["messages"]) < 5, "%d messages" % len(state["messages"]))
check("/compact: a summary leads the history",
      "compacted into the following summary" in (state["messages"][0].get("content") or ""),
      repr(state["messages"][:1])[:120])
p.stdin.close()
p.wait(timeout=10)
check("process: exit 0", p.returncode == 0, repr(p.returncode))
done("ACP COMPACT")
