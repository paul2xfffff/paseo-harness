from _assert import check, done, read_lines
import json, os, shutil, subprocess, sys, time
ROOT = "/tmp/acp_steer"
shutil.rmtree(ROOT, ignore_errors=True); os.makedirs(ROOT)
open(os.path.join(ROOT, "note.txt"), "w").write("hello\n")
p = subprocess.Popen([sys.executable, "%s/drivers/driver_steer.py" % os.path.dirname(os.path.abspath(__file__))], cwd=ROOT,
                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                     text=True, encoding="utf-8", bufsize=1)
seq = [0]
def send(o): p.stdin.write(json.dumps(o) + "\n"); p.stdin.flush()
read = read_lines(p)
def request(method, params):
    seq[0] += 1; rid = seq[0]; send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
    while True:
        m = read()
        if m.get("id") == rid and "method" not in m: return m
request("initialize", {"protocolVersion": 1, "clientCapabilities": {}})
sid = request("session/new", {"cwd": ROOT, "mcpServers": []})["result"]["sessionId"]
def prompt(text):
    seq[0] += 1; rid = seq[0]
    send({"jsonrpc": "2.0", "id": rid, "method": "session/prompt", "params": {"sessionId": sid, "prompt": [{"type": "text", "text": text}]}})
    return rid
a = prompt("first"); time.sleep(0.1); b = prompt("steer now")
results = {}
while len(results) < 2:
    m = read(10)
    if m.get("id") in (a, b) and "method" not in m: results[m["id"]] = m
stops = {k: v.get("result", {}).get("stopReason") for k, v in results.items()}
check("both prompts answer", len(results) == 2, repr(len(results)))
check("both turns end cleanly", all(v == "end_turn" for v in stops.values()), repr(stops))
time.sleep(0.3)
seen_texts = json.load(open("/tmp/acp_steer_seen.json"))
check("the model saw the steered text", "steer now" in seen_texts, repr(seen_texts))
# If the steer were not folded into the running turn it would arrive as a second turn
# instead, which is a different (and worse) UX: the user would get two answers.
calls_n = json.load(open("/tmp/acp_steer_calls.json"))
check("the steer joined the running turn, not a new one", calls_n == 2, "%d model calls" % calls_n)
import _path  # noqa: F401 - this checkout on sys.path, config isolated
import agent
state = agent.load_session(sid, ROOT)
users = [m.get("content") for m in state["messages"] if m.get("role") == "user"]
check("no duplicate user messages", len(users) == len(set(users)), repr(users))
check("the steer is persisted", "steer now" in users, repr(users))
check("the original prompt is persisted", "first" in users, repr(users))
p.stdin.close()
p.wait(timeout=10)
check("process: exit 0", p.returncode == 0, repr(p.returncode))
done("ACP STEER")
