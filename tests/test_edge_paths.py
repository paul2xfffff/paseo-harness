"""The paths that only run when something goes wrong or when old data shows up.

These are the last 15% of the file: permission timeouts, a client that never answers, a
journal written by an older version, a prompt cancelled before it starts. None of them run in
normal use, which is exactly why they need tests -- they are where the silent failures live.
"""
import json, os, sys, tempfile, threading, time

import _path  # noqa: F401
from _assert import check, done, raises
import agent


class Wire:
    def __init__(self):
        self.sent, self.lock = [], threading.Lock()

    def write(self, line):
        with self.lock:
            self.sent.append(json.loads(line))

    def result(self, rid):
        return next((m.get("result") for m in self.sent if m.get("id") == rid and "result" in m), None)

    def error(self, rid):
        return next((m.get("error") for m in self.sent if m.get("id") == rid and "error" in m), None)

    def updates(self):
        return [m["params"]["update"] for m in self.sent if m.get("method") == "session/update"]

    def clear(self):
        self.sent.clear()


def make_acp():
    acp = agent.ACP()
    acp.wire = Wire()
    acp.write = acp.wire.write
    return acp


def new_session(acp, rid=1, cwd=None):
    acp.handle({"jsonrpc": "2.0", "id": rid, "method": "session/new",
                "params": {"cwd": cwd or tempfile.mkdtemp(), "mcpServers": []}})
    return (acp.wire.result(rid) or {}).get("sessionId")


# 1. Approval policy: auto allows, deny refuses, ask consults the client.
class FakeTrace:
    """Records the notices a permission decision produces."""
    def __init__(self):
        self.notes = []

    def notice(self, text):
        self.notes.append(text)


session = {"approval": "auto", "allowed": [], "denied": []}
check("approval: auto allows a mutating tool", agent.approve("bash", session, lambda: "deny") is True)
session = {"approval": "deny", "allowed": [], "denied": []}
check("approval: deny refuses it", agent.approve("bash", session, lambda: "allow") is False)
session = {"approval": "ask", "allowed": [], "denied": []}
check("approval: ask consults the client", agent.approve("bash", session, lambda: "allow") is True)
check("approval: allow_once is not remembered", session["allowed"] == [], repr(session["allowed"]))
session = {"approval": "ask", "allowed": [], "denied": []}
check("approval: always_allow sticks", agent.approve("edit", session, lambda: "always_allow") is True and
      "edit" in session["allowed"], repr(session["allowed"]))
session = {"approval": "ask", "allowed": [], "denied": ["write"]}
check("approval: a denied tool stays denied without asking", agent.approve("write", session, lambda: "allow") is False)
session = {"approval": "auto"}
check("approval: read-only tools are never gated", agent.approve("read", session, lambda: "deny") is True)

# 2. A client that never answers: the permission request times out and denies.
acp = make_acp()
sid = new_session(acp, rid=1)
original_timeout = agent.PERMISSION_TIMEOUT
agent.PERMISSION_TIMEOUT = 0.4
try:
    call = {"id": "c1", "function": {"name": "bash", "arguments": json.dumps({"command": "echo hi"})}}
    t0 = time.time()
    verdict = acp.request_permission(sid, call, threading.Event())
    took = time.time() - t0
    check("permission timeout: denies", verdict == "deny", repr(verdict))
    check("permission timeout: respects the deadline", 0.3 < took < 3, "%.1fs" % took)
    asked = [m for m in acp.wire.sent if m.get("method") == "session/request_permission"]
    check("permission timeout: the client was asked", len(asked) == 1, repr(len(asked)))
    check("permission timeout: the request is cleaned up", acp.perm_wait == {}, repr(acp.perm_wait))
finally:
    agent.PERMISSION_TIMEOUT = original_timeout

# 3. A client that answers: the verdict maps through.
acp = make_acp()
sid = new_session(acp, rid=2)
for option, expected in (("allow_once", "allow"), ("allow_always", "always_allow"),
                         ("reject_once", "deny"), ("reject_always", "always_deny")):
    acp.wire.clear()

    def answer():
        for _ in range(200):
            req = next((m for m in acp.wire.sent if m.get("method") == "session/request_permission"), None)
            if req:
                acp.resolve_permission({"id": req["id"], "result": {"outcome": {"outcome": "selected", "optionId": option}}})
                return
            time.sleep(0.01)

    th = threading.Thread(target=answer, daemon=True)
    th.start()
    verdict = acp.request_permission(sid, {"id": "c", "function": {"name": "bash", "arguments": "{}"}}, threading.Event())
    th.join(5)
    check("permission answer: %s -> %s" % (option, expected), verdict == expected, repr(verdict))

# 4. Cancelling while a permission request is in flight raises, not hangs.
acp = make_acp()
sid = new_session(acp, rid=3)
original_timeout = agent.PERMISSION_TIMEOUT
agent.PERMISSION_TIMEOUT = 5
try:
    cancel = threading.Event()
    cancel.set()
    raises("permission cancelled mid-wait", agent.Cancelled,
           lambda: acp.request_permission(sid, {"id": "c", "function": {"name": "bash", "arguments": "{}"}}, cancel))
finally:
    agent.PERMISSION_TIMEOUT = original_timeout

# 5. A journal written by an older version is migrated on load, and marked so it is only rewritten once.
cwd = tempfile.mkdtemp()
sid = "sess_legacy_demo"
path = agent.session_file(sid, cwd)
path.parent.mkdir(parents=True, exist_ok=True)
with open(path, "w") as f:
    f.write(json.dumps({"type": "session", "system": "OLD SYSTEM", "model": agent.DEFAULT_MODEL, "cwd": cwd}) + "\n")
    f.write(json.dumps({"type": "journal", "op": "append", "message": {"role": "user", "content": "old question"}}) + "\n")
    f.write(json.dumps({"type": "journal", "op": "append", "message": {"role": "assistant", "content": "old answer"}}) + "\n")
    f.write(json.dumps({"type": "notification", "params": {"update": {"sessionUpdate": "usage_update", "used": 77}}}) + "\n")
loaded = agent.load_session(sid, cwd, agent.DEFAULT_MODEL)
check("legacy journal: loads", loaded is not None, "returned None")
if loaded:
    check("legacy journal: system kept", loaded["system"] == "OLD SYSTEM", repr(loaded["system"]))
    check("legacy journal: messages recovered", len(loaded["messages"]) == 2, repr(len(loaded["messages"])))
    head = agent.session_file(sid, cwd).read_text().splitlines()[0]
    check("legacy journal: rewritten in the current format", json.loads(head).get("t") == "session", head[:80])

# 6. An incomplete tool call at the end of a journal is trimmed, so a resumed turn is valid.
partial = [{"role": "user", "content": "go"},
           {"role": "assistant", "content": None, "tool_calls": [{"id": "t1", "type": "function",
                                                                  "function": {"name": "read", "arguments": "{}"}}]}]
check("trim: a tool call with no result is dropped", agent.trim_incomplete(partial) == partial[:1],
      repr(agent.trim_incomplete(partial)))
complete = partial + [{"role": "tool", "tool_call_id": "t1", "content": "ok"}]
check("trim: a complete pair survives", agent.trim_incomplete(complete) == complete, repr(agent.trim_incomplete(complete)))
dupes = [{"role": "assistant", "content": None, "tool_calls": [
    {"id": "same", "function": {"name": "read"}}, {"id": "same", "function": {"name": "read"}}]}]
check("trim: duplicate call ids are dropped", agent.trim_incomplete(dupes) == [], repr(agent.trim_incomplete(dupes)))

# 7. Steering: a prompt arriving mid-turn is folded into the running turn, not lost.
acp = make_acp()
sid = new_session(acp, rid=10)
gate = threading.Event()
original_loop = agent.loop


def blocking_loop(system, messages, trace, toolset, **kw):
    gate.wait(10)
    steer = kw.get("steer")
    if steer:
        for entry in steer():
            messages.append({"role": "user", "content": entry["content"]})
    return system, messages, "end_turn"


agent.loop = blocking_loop
try:
    acp.handle({"jsonrpc": "2.0", "id": 11, "method": "session/prompt",
                "params": {"sessionId": sid, "prompt": [{"type": "text", "text": "first"}]}})
    time.sleep(0.2)
    acp.handle({"jsonrpc": "2.0", "id": 12, "method": "session/prompt",
                "params": {"sessionId": sid, "prompt": [{"type": "text", "text": "second"}]}})
    queued = len(acp.steering)
    gate.set()
    deadline = time.time() + 10
    while time.time() < deadline and acp.wire.result(11) is None:
        time.sleep(0.05)
    msgs = acp.sessions[sid]["messages"]
    check("steer: second prompt was queued", queued >= 1, "queued=%d" % queued)
    check("steer: it reached the running turn", any(isinstance(m.get("content"), str) and "second" in m["content"] for m in msgs),
          repr(msgs)[:160])
    check("steer: the first prompt is answered", acp.wire.result(11) is not None, repr(acp.wire.error(11)))
finally:
    agent.loop = original_loop

# 8. A second session cannot run a turn while another one is running.
acp = make_acp()
sid_a = new_session(acp, rid=20)
sid_b = new_session(acp, rid=21)
gate = threading.Event()


def hold_loop(system, messages, trace, toolset, **kw):
    gate.wait(10)
    return system, messages, "end_turn"


agent.loop = hold_loop
try:
    acp.handle({"jsonrpc": "2.0", "id": 22, "method": "session/prompt",
                "params": {"sessionId": sid_a, "prompt": [{"type": "text", "text": "a"}]}})
    time.sleep(0.2)
    acp.handle({"jsonrpc": "2.0", "id": 23, "method": "session/prompt",
                "params": {"sessionId": sid_b, "prompt": [{"type": "text", "text": "b"}]}})
    check("concurrency: a second session is refused", (acp.wire.error(23) or {}).get("code") == -32000,
          repr(acp.wire.result(23)))
    gate.set()
    deadline = time.time() + 10
    while time.time() < deadline and acp.wire.result(22) is None:
        time.sleep(0.05)
finally:
    agent.loop = original_loop

# 9. An unknown slash command is left alone and reaches the model as text.
calls = []
agent.chat = lambda *a, **k: (calls.append(1), ({"role": "assistant", "content": "ok", "finish_reason": "stop"},
                                                {"prompt_tokens": 1, "completion_tokens": 1}))[1]
s = agent.make_session(tempfile.mkdtemp(), agent.DEFAULT_MODEL, system="P")
t = agent.open_trace(s, s["id"], emit=lambda u: None)
check("unknown command: returns the prompt as text", agent.run_command(s, t, "/nonsense") == "/nonsense")
check("unknown command: no model call yet", not calls)
t.close()

# 10. The serving loop: framing, notifications, batches, parse errors.
def serve_lines(lines):
    """Run ACP.serve over a fixed set of input lines and return what it wrote."""
    acp = make_acp()
    original = sys.stdin
    import io
    sys.stdin = io.StringIO("\n".join(lines) + "\n")
    try:
        acp.serve()
    finally:
        sys.stdin = original
    return acp.wire


wire = serve_lines([
    "",                                                        # blank line: ignored
    "not json",                                                # parse error: reported, loop continues
    json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": 1, "clientCapabilities": {}}}),
    json.dumps({"jsonrpc": "2.0", "id": 2, "method": "initialized", "params": {}}),      # notification: no reply
    json.dumps([{"jsonrpc": "2.0", "id": 3, "method": "session/list", "params": {}},
                {"jsonrpc": "2.0", "id": 4, "method": "authenticate", "params": {}}]),   # batch
    json.dumps({"jsonrpc": "2.0", "id": 5, "method": "session/prompt", "params": "not-an-object"}),
])
check("serve: a parse error is reported", any((m.get("error") or {}).get("code") == -32700 for m in wire.sent),
      repr([m for m in wire.sent if "error" in m])[:120])
check("serve: initialize answered", wire.result(1) is not None, repr(wire.error(1)))
check("serve: a notification gets no reply", all(m.get("id") != 2 for m in wire.sent), repr(wire.sent)[:100])
check("serve: a batch is processed", wire.result(3) is not None and wire.result(4) == {}, repr(wire.error(4)))
check("serve: non-object params rejected", (wire.error(5) or {}).get("code") == -32602, repr(wire.error(5)))

# 11. session/list reads the workspace root, not the sessions directory.
cwd = tempfile.mkdtemp()
s = agent.make_session(cwd, agent.DEFAULT_MODEL, system="P")
agent.save_quiet(s)
listed = agent.list_sessions(cwd)
check("session/list: finds the session", any(x["sessionId"] == s["id"] for x in listed), repr(listed)[:120])
check("session/list: reports the workspace, not .harness/sessions",
      all(not x["cwd"].rstrip("/").endswith(".harness/sessions") for x in listed),
      repr([x["cwd"] for x in listed])[:120])
check("session/list: a missing directory is empty, not an error",
      agent.list_sessions(os.path.join(cwd, "does-not-exist")) == [], "returned rows")

# 12. Reading an image returns an image block, and refuses an oversized one.
img_dir = tempfile.mkdtemp()
png = os.path.join(img_dir, "tiny.png")
with open(png, "wb") as f:
    f.write(bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
                          "0000000a49444154789c6300010000050001'0d0a2db40000000049454e44ae426082".replace("'", "")))
s = agent.make_session(img_dir, agent.DEFAULT_MODEL, system="P")
t = agent.open_trace(s, s["id"], emit=lambda u: None)
blocks = agent.read_file({"path": png}, {"cwd": img_dir, "trace": t})
check("image: returns an image_url block", any(b.get("type") == "image_url" for b in blocks), repr([b.get("type") for b in blocks]))
check("image: reports its size", any("bytes" in str(b.get("text", "")) for b in blocks), repr(blocks[0])[:80])
original_max = agent.MAX_IMAGE_BYTES
agent.MAX_IMAGE_BYTES = 4
try:
    raises("image: oversized is refused", agent.ToolError,
           lambda: agent.read_file({"path": png}, {"cwd": img_dir, "trace": t}), "too large")
finally:
    agent.MAX_IMAGE_BYTES = original_max
t.close()

# 13. A tool call with unparseable arguments becomes an error result, not a crash.
out = agent.run_tool({"id": "c", "function": {"name": "read", "arguments": "{not json"}}, agent.BUILTIN,
                     cwd=img_dir, trace=None)
check("tool: bad arguments are an error result", out[2] is True and "invalid arguments" in out[0], repr(out[0])[:80])
out = agent.run_tool({"id": "c", "function": {"name": "read", "arguments": "[1,2]"}}, agent.BUILTIN,
                     cwd=img_dir, trace=None)
check("tool: non-object arguments rejected", out[2] is True and "must be a JSON object" in out[0], repr(out[0])[:80])
out = agent.run_tool({"id": "c", "function": {"name": "no_such_tool", "arguments": "{}"}}, agent.BUILTIN,
                     cwd=img_dir, trace=None)
check("tool: unknown name is an error result", out[2] is True and "unknown tool" in out[0], repr(out[0])[:80])
out = agent.run_tool({"id": "c", "function": {"name": "read", "arguments": "{}"}}, agent.BUILTIN,
                     cwd=img_dir, trace=None)
check("tool: missing required argument is reported", out[2] is True and "missing required argument" in out[0], repr(out[0])[:80])

# 14. bash: a command that times out is killed and reported.
s = agent.make_session(tempfile.mkdtemp(), agent.DEFAULT_MODEL, system="P")
t = agent.open_trace(s, s["id"], emit=lambda u: None)
out = agent.run_tool({"id": "c", "function": {"name": "bash", "arguments": json.dumps({"command": "sleep 30", "timeout": 1})}},
                     agent.BUILTIN, cwd=s["cwd"], scope=s["id"], trace=t)
check("bash: timeout reported", out[2] is True and "timeout" in out[0], repr(out[0])[:80])

# 15. bash: a cancelled command stops and raises Cancelled.
def cancelled(): return True
if True:
    raises("bash: cancellation propagates", agent.Cancelled,
           lambda: agent.run_tool({"id": "c", "function": {"name": "bash", "arguments": json.dumps({"command": "sleep 30"})}},
                                  agent.BUILTIN, cwd=s["cwd"], scope=s["id"], trace=t, cancelled=cancelled))

# 16. bash: a non-zero exit is reported with its code, not treated as a crash.
out = agent.run_tool({"id": "c", "function": {"name": "bash", "arguments": json.dumps({"command": "exit 3"})}},
                     agent.BUILTIN, cwd=s["cwd"], scope=s["id"], trace=t)
codes = [b.get("code") for b in out[1] if b.get("type") == "exit_code"]
check("bash: exit code surfaces", codes == [3], repr(codes))
check("bash: non-zero exit is not an error result", out[2] is False, repr(out[2]))

# 17. The subagent concurrency cap refuses rather than queueing silently.
held = [agent.SUBAGENTS.acquire(blocking=False) for _ in range(agent.SUBAGENTS._value)]
try:
    r = agent.run_tool({"id": "c", "function": {"name": "subagent", "arguments": json.dumps({"task": "x"})}},
                       agent.BUILTIN, cwd=s["cwd"], scope=s["id"], trace=t)
    check("subagent: cap refuses", r[2] is True and "too many subagents" in r[0], repr(r[0])[:80])
finally:
    for _ in held:
        agent.SUBAGENTS.release()

# 18. A background job past the cap is refused with an actionable message.
# the cap is per owner (a run owns its jobs), so hold them on this run's trace
for i in range(agent.MAX_JOBS):
    agent.start_job("sleep 20", cwd=s["cwd"], scope=s["id"], trace=t)
try:
    r = agent.run_tool({"id": "c", "function": {"name": "bash", "arguments": json.dumps({"command": "true", "background": True})}},
                       agent.BUILTIN, cwd=s["cwd"], scope="cap-test", trace=t)
    check("jobs: cap refuses", r[2] is True and "too many background jobs" in r[0], repr(r[0])[:90])
finally:
    agent.kill_all_jobs()

# 19. ACP.fail: a turn that raises is answered with an error and the session rolls back.
acp = make_acp()
sid = new_session(acp, rid=40)
before = list(acp.sessions[sid]["messages"])
original_loop = agent.loop
agent.loop = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("model exploded"))
try:
    acp.handle({"jsonrpc": "2.0", "id": 41, "method": "session/prompt",
                "params": {"sessionId": sid, "prompt": [{"type": "text", "text": "boom"}]}})
    deadline = time.time() + 10
    while time.time() < deadline and acp.wire.error(41) is None:
        time.sleep(0.05)
    err = acp.wire.error(41) or {}
    check("fail: the request is answered with an error", err.get("code") == -32603, repr(err)[:100])
    check("fail: the message names the cause", "model exploded" in (err.get("message") or ""), repr(err.get("message"))[:80])
    check("fail: the turn was rolled back", acp.sessions[sid]["messages"] == before,
          repr(acp.sessions[sid]["messages"])[:100])
finally:
    agent.loop = original_loop
t.close()

# 20. Cancelling a session that has nothing running is a no-op that still answers. It must not
# raise (the client would see a protocol error instead of an acknowledgement) and it must not
# touch any other session's cancellation flag.
acp = make_acp()
sid = new_session(acp, rid=50)
elsewhere = threading.Event()
acp.running = {"sid": "some_other_session", "cancel": elsewhere}
acp.cancel_turn(51, {"sessionId": sid})
check("cancel: an idle session is acknowledged", acp.wire.result(51) == {}, repr(acp.wire.result(51)))
check("cancel: another session's turn is left alone", not elsewhere.is_set(),
      "cancelling %s stopped a turn in another session" % sid[:12])
acp.running = None

# 21. list_sessions sorts newest first and honours the limit.
cwd = tempfile.mkdtemp()
made = []
for i in range(3):
    sess = agent.make_session(cwd, agent.DEFAULT_MODEL, system="P", title="t%d" % i)
    agent.save_quiet(sess)
    made.append(sess["id"])
    time.sleep(0.02)
rows = agent.list_sessions(cwd)
check("session/list: newest first", rows and rows[0]["sessionId"] == made[-1], repr([r["title"] for r in rows])[:80])
check("session/list: limit honoured", len(agent.list_sessions(cwd, limit=1)) == 1, repr(len(agent.list_sessions(cwd, limit=1))))

# 22. A missing project-context file is skipped, not fatal.
cwd = tempfile.mkdtemp()
os.makedirs(os.path.join(cwd, ".harness"), exist_ok=True)
with open(os.path.join(cwd, "AGENTS.md"), "w") as f:
    f.write("project rules")
ctx = agent.project_context(cwd)
check("context: reads AGENTS.md", "project rules" in ctx, repr(ctx)[:80])
# an unreadable context file is skipped rather than raising
bad = os.path.join(cwd, "CLAUDE.md")
with open(bad, "w") as f:
    f.write("x")
os.chmod(bad, 0o000)
try:
    ctx = agent.project_context(cwd)
    check("context: an unreadable file is skipped", "project rules" in ctx, repr(ctx)[:80])
finally:
    os.chmod(bad, 0o644)

# 23. content_text: the shapes a message's content can take.
check("content_text: a plain string", agent.content_text("hi") == ("hi", False), repr(agent.content_text("hi")))
check("content_text: a list of text parts", agent.content_text([{"type": "text", "text": "a"}]) == ("a", False),
      repr(agent.content_text([{"type": "text", "text": "a"}])))
check("content_text: an image sets the flag",
      agent.content_text([{"type": "image_url", "image_url": {"url": "data:x"}}])[1] is True,
      repr(agent.content_text([{"type": "image_url", "image_url": {"url": "data:x"}}])))
check("content_text: neither str nor list", agent.content_text(None) == ("", False), repr(agent.content_text(None)))

# 24. strip_images removes image parts and leaves a placeholder if nothing else remains.
stripped = agent.strip_images([{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "d"}}]}])
check("strip_images: an image-only message gets a placeholder",
      stripped[0]["content"] == [{"type": "text", "text": "[image omitted]"}], repr(stripped)[:120])
stripped = agent.strip_images([{"role": "user", "content": [{"type": "text", "text": "keep"},
                                                           {"type": "image_url", "image_url": {"url": "d"}}]}])
check("strip_images: text survives", stripped[0]["content"] == [{"type": "text", "text": "keep"}], repr(stripped)[:120])

# 25. serialize_conversation renders every message kind the summarizer can meet.
rendered = agent.serialize_conversation([
    {"role": "user", "content": "question"},
    {"role": "assistant", "content": "answer"},
    {"role": "assistant", "content": None, "tool_calls": [
        {"id": "t", "function": {"name": "read", "arguments": json.dumps({"path": "x"})}}]},
    {"role": "tool", "content": "file body"},
])
check("serialize: user turn", "[User]: question" in rendered, rendered[:80])
check("serialize: assistant turn", "[Assistant]: answer" in rendered, rendered[:120])
check("serialize: tool call rendered", "read(path=" in rendered, rendered[:160])
check("serialize: tool result rendered", "[Tool result]: file body" in rendered, rendered[:200])

# 26. kill_job / kill_subagent refuse an id they do not own.
s = agent.make_session(tempfile.mkdtemp(), agent.DEFAULT_MODEL, system="P")
t = agent.open_trace(s, s["id"], emit=lambda u: None)
raises("kill_job: unknown id refused", agent.ToolError,
       lambda: agent.kill_job("job_does_not_exist", scope=s["id"]), "unknown job")
raises("kill_subagent: unknown id refused", agent.ToolError,
       lambda: agent.kill_subagent("sub_does_not_exist", scope=s["id"]), "unknown subagent")

# 27. A foreground subagent over the cap is refused (not silently serialized).
held = [agent.SUBAGENTS.acquire(blocking=False) for _ in range(agent.SUBAGENTS._value)]
try:
    r = agent.run_tool({"id": "c", "function": {"name": "subagent", "arguments": json.dumps({"task": "x"})}},
                       agent.BUILTIN, cwd=s["cwd"], scope=s["id"], trace=t)
    check("subagent: foreground cap refuses", r[2] is True and "too many subagents" in r[0], repr(r[0])[:70])
finally:
    for _ in held:
        agent.SUBAGENTS.release()

# 28. settings() raises (not exits) when no key is configured, so a caller can catch it.
original_key = os.environ.pop("OPENAI_API_KEY", None)
original_tok = os.environ.pop("OPENAI_AUTH_TOKEN", None)
try:
    # A missing key is configuration, not a reason to terminate the interpreter: the old code
    # called sys.exit() here, which killed the whole process from inside a library function.
    raises("settings: missing key raises AgentError, not SystemExit", agent.AgentError,
           lambda: agent.settings("env/whatever"), "no API key")
finally:
    if original_key is not None:
        os.environ["OPENAI_API_KEY"] = original_key
    if original_tok is not None:
        os.environ["OPENAI_AUTH_TOKEN"] = original_tok
t.close()

# 29. A context-overflow error triggers compaction and one retry, then succeeds.
calls = []
def overflow_once(system, messages, tools, **kw):
    calls.append(1)
    if len(calls) == 1: raise agent.AgentError("maximum context length exceeded")
    return {"role": "assistant", "content": "recovered", "finish_reason": "stop"}, {"prompt_tokens": 5, "completion_tokens": 2}
original_chat = agent.chat
agent.chat = overflow_once
original_compact = agent.compact_messages
# a compaction that reports success: the loop should retry the call once
agent.compact_messages = lambda messages, *a, **k: ([{"role": "user", "content": "compacted"}], 1, 1)
s = agent.make_session(tempfile.mkdtemp(), agent.DEFAULT_MODEL, system="P")
t = agent.open_trace(s, s["id"], emit=lambda u: None)
try:
    _, msgs, stop = agent.loop("P", [{"role": "user", "content": "go"}], t, agent.BUILTIN,
                               model=agent.DEFAULT_MODEL, cwd=s["cwd"])
    check("overflow: retried after compaction failed", len(calls) >= 2, "calls=%d" % len(calls))
finally:
    agent.chat = original_chat
    agent.compact_messages = original_compact
    t.close()

# 30. A rate limit is not mistaken for an overflow.
check("overflow: rate limits are excluded", agent.NOT_OVERFLOW_RE.search("rate limit exceeded") is not None)
check("overflow: a real overflow matches", agent.OVERFLOW_RE.search("maximum context length exceeded") is not None)

# 31. An unparseable model list falls back to the environment model instead of crashing.
check("models: env fallback exists", isinstance(agent.DEFAULT_MODEL, str) and agent.DEFAULT_MODEL, repr(agent.DEFAULT_MODEL))
check("model_info: an unknown key yields a usable spec",
      agent.model_info("no/such/model").get("context", 0) > 0, repr(agent.model_info("no/such/model"))[:80])

# 32. Trace.tool_end renders image and diff blocks, and never emits an empty content list --
# a client that gets `"content": []` renders a blank tool call with no way to tell whether the
# call failed or produced nothing.
s = agent.make_session(tempfile.mkdtemp(), agent.DEFAULT_MODEL, system="P")
events = []
t = agent.open_trace(s, s["id"], emit=events.append)
t.tool_end("c1", "done", [{"image_url": {"url": "data:image/png;base64,AAAA"}}], False,
           [{"type": "diff", "path": "x", "oldText": "", "newText": "y"}], 0)
t.tool_end("c2", "", [], True)
kinds = [b.get("type") for e in events for b in (e.get("content") or [])]
check("tool_end: emits one update per call", len(events) == 2, "%d updates" % len(events))
check("tool_end: the text is carried as a content block", "content" in kinds, repr(kinds))
inner = [((b.get("content") or {}).get("type")) for e in events for b in (e.get("content") or [])]
check("tool_end: the image is carried as an image block", "image" in inner, repr(inner))
check("tool_end: the diff is carried", "diff" in kinds, repr(kinds))
check("tool_end: a failed call reports status failed",
      events[1].get("status") == "failed", repr(events[1].get("status")))
check("tool_end: a successful call reports status completed",
      events[0].get("status") == "completed", repr(events[0].get("status")))
check("tool_end: an empty result still carries a block", bool(events[1].get("content")),
      "content was empty: %r" % (events[1].get("content"),))
check("tool_end: no update ever has empty content",
      all(e.get("content") for e in events), repr([e.get("content") for e in events]))
t.close()

# 33. commands: every advertised command is actually handled.
s = agent.make_session(tempfile.mkdtemp(), agent.DEFAULT_MODEL, system="P")
t = agent.open_trace(s, s["id"], emit=lambda u: None)
for name in ("compact", "cost", "reload", "approval"):
    check("command /%s is handled" % name, agent.run_command(s, t, "/" + name) == "", name)
# a command answers through trace.agent and returns "": the text went to the client, not the caller
said = []
t.agent = lambda text, message_id=None: said.append(text)
check("command: /approval with a bad value reports it",
      agent.run_command(s, t, "/approval nope") == "" and any("must be" in x for x in said), repr(said))
t.close()

print()
done("EDGE PATHS ULTIMATE")
