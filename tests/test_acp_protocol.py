"""The ACP protocol layer, called in-process.

Unlike test_acp_*.py (which drives a subprocess over a pipe), these call the handlers with a
fake `send`, so a failure points at one method instead of at "the session hung".
"""
import json, os, sys, tempfile, threading, time

import _path  # noqa: F401
from _assert import check, done
import agent


class Wire:
    """Stands in for stdout: records every JSON-RPC message the agent emits."""

    def __init__(self):
        self.sent = []
        self.lock = threading.Lock()

    def write(self, line):
        with self.lock:
            self.sent.append(json.loads(line))

    def updates(self, sid=None):
        return [m["params"]["update"] for m in self.sent
                if m.get("method") == "session/update" and (sid is None or m["params"]["sessionId"] == sid)]

    def result(self, rid):
        return next((m.get("result") for m in self.sent if m.get("id") == rid and "result" in m), None)

    def error(self, rid):
        return next((m.get("error") for m in self.sent if m.get("id") == rid and "error" in m), None)

    def clear(self):
        self.sent.clear()


def make_acp():
    acp = agent.ACP()
    acp.write = Wire().write
    acp.wire = Wire()
    acp.write = acp.wire.write
    return acp


def new_session(acp, rid=1, cwd=None):
    acp.handle({"jsonrpc": "2.0", "id": rid, "method": "session/new",
                "params": {"cwd": cwd or tempfile.mkdtemp(), "mcpServers": []}})
    res = acp.wire.result(rid)
    return res["sessionId"] if res else None


def session_of(acp, rid):
    return (acp.wire.result(rid) or {}).get("sessionId")


# 1. initialize advertises what the implementation actually supports.
acp = make_acp()
acp.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": 1, "clientCapabilities": {}}})
init = acp.wire.result(1)
check("initialize: returns capabilities", bool(init and init.get("agentCapabilities")), repr(init)[:80])
check("initialize: loadSession declared", init["agentCapabilities"].get("loadSession") is True, repr(init.get("agentCapabilities")))
check("initialize: session list/close declared",
      set(init["agentCapabilities"].get("sessionCapabilities", {})) >= {"list", "close"},
      repr(init["agentCapabilities"].get("sessionCapabilities")))

# 2. session/new validates cwd and announces its commands.
acp.wire.clear()
acp.handle({"jsonrpc": "2.0", "id": 2, "method": "session/new", "params": {"cwd": "/definitely/not/a/dir"}})
check("session/new: bad cwd rejected", acp.wire.error(2) is not None, repr(acp.wire.result(2)))
sid = new_session(acp, rid=3)
check("session/new: returns a session", isinstance(sid, str) and sid, repr(sid))
cmds = [u for u in acp.wire.updates(sid) if u.get("sessionUpdate") == "available_commands_update"]
check("session/new: commands announced", bool(cmds), repr(acp.wire.updates(sid))[:100])
check("session/new: builtins present", cmds and any(c["name"] == "cost" for c in cmds[0]["availableCommands"]),
      repr(cmds[0]["availableCommands"]) if cmds else None)

# 3. Unknown methods and bad params produce protocol errors, not crashes.
acp.wire.clear()
acp.handle({"jsonrpc": "2.0", "id": 4, "method": "no/such/method", "params": {}})
check("unknown method: -32601", (acp.wire.error(4) or {}).get("code") == -32601, repr(acp.wire.error(4)))
acp.wire.clear()
acp.handle({"jsonrpc": "2.0", "id": 5, "method": "session/prompt", "params": {"sessionId": sid, "prompt": "not-an-array"}})
check("bad prompt type: -32602", (acp.wire.error(5) or {}).get("code") == -32602, repr(acp.wire.error(5)))
acp.wire.clear()
acp.handle({"jsonrpc": "2.0", "id": 6, "method": "session/prompt", "params": {"sessionId": "nope", "prompt": []}})
check("unknown session: rejected", acp.wire.error(6) is not None, repr(acp.wire.result(6)))
acp.wire.clear()
acp.handle({"jsonrpc": "2.0", "id": 7, "method": "session/set_mode", "params": {"sessionId": sid}})
check("session/set_mode: -32601", (acp.wire.error(7) or {}).get("code") == -32601, repr(acp.wire.error(7)))

# 4. set_config_option: model and thought_level, with validation.
acp.wire.clear()
first_model = agent.DEFAULT_MODEL
acp.handle({"jsonrpc": "2.0", "id": 8, "method": "session/set_config_option",
            "params": {"sessionId": sid, "configId": "model", "value": "no/such-model"}})
check("config: unknown model rejected", (acp.wire.error(8) or {}).get("code") == -32602, repr(acp.wire.error(8)))
acp.wire.clear()
acp.handle({"jsonrpc": "2.0", "id": 9, "method": "session/set_config_option",
            "params": {"sessionId": sid, "configId": "model", "value": first_model}})
check("config: model set", acp.wire.result(9) is not None and
      acp.sessions[sid]["model"] == agent.model_info(first_model)["key"], repr(acp.wire.error(9)))
acp.wire.clear()
acp.handle({"jsonrpc": "2.0", "id": 10, "method": "session/set_config_option",
            "params": {"sessionId": sid, "configId": "thought_level", "value": "off"}})
check("config: reasoning set", acp.sessions[sid]["reasoning"] == "off", repr(acp.sessions[sid].get("reasoning")))
acp.wire.clear()
acp.handle({"jsonrpc": "2.0", "id": 11, "method": "session/set_config_option",
            "params": {"sessionId": sid, "configId": "thought_level", "value": "nonsense"}})
check("config: bad reasoning rejected", (acp.wire.error(11) or {}).get("code") == -32602, repr(acp.wire.error(11)))
acp.wire.clear()
acp.handle({"jsonrpc": "2.0", "id": 12, "method": "session/set_config_option",
            "params": {"sessionId": sid, "configId": "nope", "value": "x"}})
check("config: unknown option rejected", (acp.wire.error(12) or {}).get("code") == -32602, repr(acp.wire.error(12)))

# 5. session/list and session/close.
acp.wire.clear()
acp.handle({"jsonrpc": "2.0", "id": 13, "method": "session/list", "params": {"cwd": "/definitely/not/a/dir"}})
check("session/list: empty dir is not an error", acp.wire.result(13) is not None, repr(acp.wire.error(13)))
acp.wire.clear()
acp.handle({"jsonrpc": "2.0", "id": 14, "method": "session/close", "params": {"sessionId": sid}})
check("session/close: acknowledged", acp.wire.result(14) == {}, repr(acp.wire.error(14)))
check("session/close: session dropped", sid not in acp.sessions, repr(list(acp.sessions)))
acp.wire.clear()
acp.handle({"jsonrpc": "2.0", "id": 15, "method": "session/close", "params": {"sessionId": sid}})
check("session/close: idempotent", acp.wire.result(15) == {}, repr(acp.wire.error(15)))

# 6. session/load replays the journal and restores config.
# The trace is created lazily on the first turn, so the journal is empty until then: run one
# real turn rather than writing into the session dict, which the journal never sees.
acp.wire.clear()
cwd = tempfile.mkdtemp()
acp.handle({"jsonrpc": "2.0", "id": 16, "method": "session/new", "params": {"cwd": cwd, "mcpServers": []}})
sid2 = session_of(acp, 16)
orig_chat = agent.chat
agent.chat = lambda *a, **k: ({"role": "assistant", "content": None, "finish_reason": "tool_calls", "tool_calls": [
    {"id": "t1", "type": "function", "function": {"name": "write", "arguments": json.dumps({"path": "x.txt", "content": "hi"})}}]},
    {"prompt_tokens": 5, "completion_tokens": 2})
try:
    acp.handle({"jsonrpc": "2.0", "id": 160, "method": "session/prompt",
                "params": {"sessionId": sid2, "prompt": [{"type": "text", "text": "hello"}]}})
    deadline = time.time() + 20
    while time.time() < deadline and acp.wire.result(160) is None:
        time.sleep(0.05)
finally:
    agent.chat = orig_chat
acp.wire.clear()
acp.handle({"jsonrpc": "2.0", "id": 17, "method": "session/load", "params": {"sessionId": sid2, "cwd": cwd}})
check("session/load: succeeds", acp.wire.result(17) is not None, repr(acp.wire.error(17)))
replay = acp.wire.updates(sid2)
kinds = [u.get("sessionUpdate") for u in replay]
check("session/load: replays the conversation", "user_message_chunk" in kinds, repr(kinds))
check("session/load: replays tool results", any(u.get("sessionUpdate") == "tool_call" for u in replay), repr(kinds))
acp.wire.clear()
acp.handle({"jsonrpc": "2.0", "id": 18, "method": "session/load", "params": {"sessionId": "sess_does_not_exist"}})
check("session/load: missing session rejected", acp.wire.error(18) is not None, repr(acp.wire.result(18)))

# 7. Prompt content conversion: text, images, resource links, resources.
blocks = [{"type": "text", "text": "look"},
          {"type": "image", "mimeType": "image/png", "data": "AAAA"},
          {"type": "resource_link", "uri": "file:///x"},
          {"type": "resource", "resource": {"text": "inline"}}]
text, content = agent.acp_prompt_content(blocks)
check("prompt content: text extracted", "look" in text and "file:///x" in text and "inline" in text, repr(text))
check("prompt content: image kept as parts", isinstance(content, list) and
      any(p.get("type") == "image_url" for p in content), repr(content)[:100])
text2, content2 = agent.acp_prompt_content([{"type": "text", "text": "plain"}])
check("prompt content: text-only stays a string", isinstance(content2, str) and content2 == "plain", repr(content2))
check("prompt content: empty stays empty", agent.acp_prompt_content([]) == ("", ""), repr(agent.acp_prompt_content([])))

# 8. JSON-RPC framing: a batch, a notification, and a parse error.
acp.wire.clear()
acp.handle({"jsonrpc": "2.0", "id": 19, "method": "initialized", "params": {}})
check("notification: no reply", not acp.wire.sent, repr(acp.wire.sent)[:80])

# 9. Steering: a second prompt while one runs is queued, not lost.
acp.wire.clear()
sid3 = new_session(acp, rid=20)
gate = threading.Event()
orig_loop = agent.loop
agent.loop = lambda *a, **k: (gate.wait(10), ("s", [], "end_turn"))[1]
try:
    acp.handle({"jsonrpc": "2.0", "id": 21, "method": "session/prompt",
                "params": {"sessionId": sid3, "prompt": [{"type": "text", "text": "first"}]}})
    time.sleep(0.2)
    acp.handle({"jsonrpc": "2.0", "id": 22, "method": "session/prompt",
                "params": {"sessionId": sid3, "prompt": [{"type": "text", "text": "second"}]}})
    steered = bool(acp.steering) or bool(acp.queued)
    gate.set()
    deadline = time.time() + 10
    while time.time() < deadline and len([m for m in acp.wire.sent if m.get("id") in (21, 22) and "result" in m]) < 2:
        time.sleep(0.1)
    answered = [m["id"] for m in acp.wire.sent if m.get("id") in (21, 22) and "result" in m]
    check("steering: second prompt accepted", steered or 22 in answered, "steering=%s queued=%s" % (bool(acp.steering), bool(acp.queued)))
    check("steering: both prompts answered", set(answered) >= {21}, repr(answered))
finally:
    agent.loop = orig_loop

# 10. A cancelled prompt answers with stopReason cancelled.
acp.wire.clear()
sid4 = new_session(acp, rid=30)
started = threading.Event()
def slow_loop(*a, **k):
    started.set()
    for _ in range(200):
        if k.get("cancelled") and k["cancelled"](): return "s", [], "cancelled"
        time.sleep(0.02)
    return "s", [], "end_turn"
agent.loop = slow_loop
try:
    acp.handle({"jsonrpc": "2.0", "id": 31, "method": "session/prompt",
                "params": {"sessionId": sid4, "prompt": [{"type": "text", "text": "go"}]}})
    started.wait(5)
    acp.handle({"jsonrpc": "2.0", "id": 32, "method": "session/cancel", "params": {"sessionId": sid4}})
    deadline = time.time() + 10
    while time.time() < deadline and acp.wire.result(31) is None:
        time.sleep(0.05)
    res = acp.wire.result(31) or {}
    check("cancel: prompt answers with cancelled", res.get("stopReason") == "cancelled", repr(res))
    check("cancel: request acknowledged", acp.wire.result(32) == {}, repr(acp.wire.error(32)))
finally:
    agent.loop = orig_loop

print()
done("ACP IN-PROCESS")
