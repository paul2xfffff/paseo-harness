"""End-to-end tests against a real OpenAI-compatible endpoint.

Skipped unless LIVE_API_KEY is set:

    LIVE_BASE_URL=http://localhost:11434/v1 LIVE_MODEL=gpustack/qwen3-coder \
    LIVE_API_KEY=... .venv/bin/python tests/test_live.py
"""
import json, os, sys, tempfile, time

if not os.environ.get("LIVE_API_KEY"):
    print("skip: set LIVE_API_KEY (LIVE_BASE_URL, LIVE_MODEL) to run the live suite")
    sys.exit(0)

import _path  # noqa: F401 - repo root on sys.path

BASE = os.environ.get("LIVE_BASE_URL", "http://localhost:11434/v1").rstrip("/")
MODEL = os.environ.get("LIVE_MODEL", "gpustack/qwen3-coder")
TIMEOUT = float(os.environ.get("LIVE_TIMEOUT", "180"))

CONFIG = os.path.join(tempfile.mkdtemp(prefix="live-config-"), "config.json")
with open(CONFIG, "w", encoding="utf-8") as fh:
    json.dump({"default": MODEL, "settings": {"approval": "auto"},
               "providers": {"gpustack": {"name": "GPUStack", "base_url": BASE,
                                          "api_key": os.environ["LIVE_API_KEY"],
                                          "models": {"qwen3-coder": {"context": 262144, "max_output": 8192}}}}}, fh)
os.environ["AGENT_CONFIG"] = CONFIG
os.environ["AGENT_MODEL"] = MODEL
os.environ.pop("AGENT_MODELS", None)

from _assert import check, done
import agent


class Wire:
    """Records the agent's output and plays the client half of the permission handshake."""

    def __init__(self, acp=None):
        self.sent = []
        self.acp = acp

    def write(self, line):
        msg = json.loads(line)
        self.sent.append(msg)
        if self.acp is not None and msg.get("method") == "session/request_permission":
            self.acp.resolve_permission({"jsonrpc": "2.0", "id": msg.get("id"),
                                         "result": {"outcome": {"outcome": "selected", "optionId": "allow_once"}}})

    def result(self, rid):
        for m in self.sent:
            if m.get("id") == rid and "result" in m:
                return m["result"]
        return None

    def error(self, rid):
        for m in self.sent:
            if m.get("id") == rid and "error" in m:
                return m["error"]
        return None

    def updates(self):
        return [m["params"]["update"] for m in self.sent if m.get("method") == "session/update"]

    def text(self):
        return "".join(u["content"]["text"] for u in self.updates()
                       if u.get("sessionUpdate") == "agent_message_chunk"
                       and isinstance(u.get("content"), dict) and u["content"].get("text"))

    def tools(self):
        return [u.get("name") for u in self.updates() if u.get("sessionUpdate") == "tool_call"]

    def clear(self):
        self.sent.clear()


def make_acp():
    wire = Wire()
    acp = agent.ACP()
    wire.acp = acp
    acp.write = wire.write
    acp.wire = wire
    return acp


def new_session(acp, cwd):
    acp.handle({"jsonrpc": "2.0", "id": 1, "method": "session/new",
                "params": {"cwd": cwd, "mcpServers": []}})
    return (acp.wire.result(1) or {}).get("sessionId")


def ask(acp, sid, text):
    acp.wire.clear()
    acp.handle({"jsonrpc": "2.0", "id": 2, "method": "session/prompt",
                "params": {"sessionId": sid, "prompt": [{"type": "text", "text": text}]}})
    deadline = time.time() + TIMEOUT
    while time.time() < deadline:
        err = acp.wire.error(2)
        if err is not None:
            raise AssertionError("turn failed: %s" % err)
        res = acp.wire.result(2)
        if res is not None:
            return res
        time.sleep(0.1)
    raise AssertionError("turn timed out after %.0fs" % TIMEOUT)


acp = make_acp()
acp.handle({"jsonrpc": "2.0", "id": 3, "method": "initialize",
            "params": {"protocolVersion": 1, "clientCapabilities": {}}})
caps = (acp.wire.result(3) or {}).get("agentCapabilities") or {}
check("live: initialize returns capabilities", bool(caps), repr(caps)[:120])
check("live: loadSession advertised", caps.get("loadSession") is True, repr(caps)[:120])

cwd = tempfile.mkdtemp(prefix="live-cwd-")
sid = new_session(acp, cwd)
check("live: session created", isinstance(sid, str) and bool(sid), repr(sid))

res = ask(acp, sid, "Reply with exactly the word: pong")
check("live: prompt ends the turn", (res or {}).get("stopReason") == "end_turn", repr(res)[:160])
check("live: the model answered", "pong" in acp.wire.text().lower(), repr(acp.wire.text())[:200])

res = ask(acp, sid, "Use the bash tool to create a file named live.txt in the current directory containing the word hello. Then reply with the single word DONE.")
check("live: tool turn ends", (res or {}).get("stopReason") == "end_turn", repr(res)[:160])
check("live: bash tool called", "bash" in acp.wire.tools(), repr(acp.wire.tools()))
check("live: the file exists", os.path.exists(os.path.join(cwd, "live.txt")), repr(os.listdir(cwd)))

with open(os.path.join(cwd, "note.txt"), "w", encoding="utf-8") as fh:
    fh.write("颜色=蓝色\n")
res = ask(acp, sid, "Read note.txt with the read tool and reply with its exact contents.")
check("live: read turn ends", (res or {}).get("stopReason") == "end_turn", repr(res)[:160])
check("live: read tool called", "read" in acp.wire.tools(), repr(acp.wire.tools()))
body = acp.wire.text()
check("live: multibyte survived", "颜色" in body or "蓝色" in body, repr(body)[-200:])

res = ask(acp, sid, "Use the subagent tool once: ask the child to reply with exactly the word child42, then report what it said.")
check("live: subagent turn ends", (res or {}).get("stopReason") == "end_turn", repr(res)[:160])
check("live: subagent called", "subagent" in acp.wire.tools(), repr(acp.wire.tools()))
body = acp.wire.text()
check("live: child answer reached the parent", "child42" in body, repr(body)[-300:])

usage = agent.usage_snapshot(acp.sessions.get(sid) or {})
check("live: usage recorded", int(usage.get("input") or 0) > 0, repr(usage))
ask(acp, sid, "/cost")
check("live: /cost answers", "usage:" in acp.wire.text().lower(), repr(acp.wire.text()[-160:]))

acp.handle({"jsonrpc": "2.0", "id": 8, "method": "session/list", "params": {"cwd": cwd}})
listed = (acp.wire.result(8) or {}).get("sessions") or []
check("live: session/list includes the session", any(s.get("sessionId") == sid for s in listed), repr(listed)[:200])
acp.wire.clear()
acp.handle({"jsonrpc": "2.0", "id": 9, "method": "session/load",
            "params": {"sessionId": sid, "cwd": cwd}})
check("live: session/load succeeds", acp.wire.result(9) is not None, "session/load did not answer")
check("live: session/load replays the conversation",
      "user_message_chunk" in [u.get("sessionUpdate") for u in acp.wire.updates()],
      repr([u.get("sessionUpdate") for u in acp.wire.updates()])[:200])

# 8. Memory: a later prompt sees the earlier exchange.
res = ask(acp, sid, "Remember the number 4711. Reply with just OK.")
check("live: memory turn ends", (res or {}).get("stopReason") == "end_turn", repr(res)[:160])
res = ask(acp, sid, "What number did I ask you to remember? Reply with the number only.")
check("live: the model recalled the number", "4711" in acp.wire.text(), repr(acp.wire.text())[-200:])

# 9. A fresh session does not inherit the old conversation.
sid2 = new_session(acp, cwd)
check("live: second session created", isinstance(sid2, str) and sid2 != sid, repr(sid2))
res = ask(acp, sid2, "What number did I ask you to remember earlier? Reply with only the number, or 'none'.")
check("live: sessions are isolated", "4711" not in acp.wire.text(), repr(acp.wire.text())[:200])

# 10. Permissions: with approval "ask" the tool waits for the client to decide.
sid3 = new_session(acp, cwd)
acp.sessions[sid3]["approval"] = "ask"
res = ask(acp, sid3, "Use the bash tool to run: echo perm-ok")
check("live: permission turn ends", (res or {}).get("stopReason") == "end_turn", repr(res)[:160])
check("live: permission requested", any(m.get("method") == "session/request_permission" for m in acp.wire.sent),
      repr([m.get("method") for m in acp.wire.sent])[:200])
check("live: the approved tool ran", any("bash" in n for n in acp.wire.tools()), repr(acp.wire.tools()))

# 11. Cancel: a cancelled turn answers with stopReason cancelled.
acp.wire.clear()
acp.handle({"jsonrpc": "2.0", "id": 11, "method": "session/prompt",
            "params": {"sessionId": sid3, "prompt": [{"type": "text", "text": "Count slowly from 1 to 100, one number per line."}]}})
time.sleep(0.8)
acp.wire.clear()
acp.handle({"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": sid3}})
deadline = time.time() + TIMEOUT
res = None
while time.time() < deadline:
    res = acp.wire.result(11)
    if res is not None:
        break
    time.sleep(0.1)
check("live: cancel answers", res is not None, "no result after cancel")
check("live: cancel reason", (res or {}).get("stopReason") in ("cancelled", "end_turn"), repr(res)[:160])

# 12. A finished background job wakes the idle session by itself, with no new prompt.
res = ask(acp, sid, "Use the bash tool with background=true to run: sleep 3; echo woke-up-123. "
                    "Reply immediately with the single word STARTED.")
check("live: background turn ends", (res or {}).get("stopReason") == "end_turn", repr(res)[:160])
before = len(acp.wire.updates())
deadline = time.time() + 90
grew = False
while time.time() < deadline:
    if len(acp.wire.updates()) > before:
        grew = True
        break
    time.sleep(0.2)
check("live: background job wakes the idle session", grew, "no update arrived after the job finished")

done("LIVE")
