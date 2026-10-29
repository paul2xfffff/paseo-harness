"""The invariants that a mutation test proved were untested.

Each case here is a guard whose removal changed nothing in the suite. They are not edge cases:
they are the reasons the tools are safe to hand to a model. If any of them stops holding, the
agent will happily overwrite a file it never read, recurse without bound, or leak its MCP
children -- and nothing else would notice.
"""
import json, os, sys, tempfile, threading, time

import _path  # noqa: F401
from _assert import check, done
import agent


def fresh():
    s = agent.make_session(tempfile.mkdtemp(), agent.DEFAULT_MODEL, system="P")
    t = agent.open_trace(s, s["id"], emit=lambda u: None)
    t.context_window = 100_000
    return s, t


def call(tool, args, cwd, trace=None, scope=""):
    return agent.run_tool({"id": "c", "function": {"name": tool, "arguments": json.dumps(args)}},
                          agent.BUILTIN, cwd=cwd, scope=scope or getattr(trace, "session_id", ""), trace=trace)


def guard_hit(out):
    return out[2] is True


# 1. A subagent may not spawn a subagent. The tool map is the guard, so removing `subagent`
#    from CHILD must break here -- nothing else asserts it.
check("child set excludes subagent", "subagent" not in agent.CHILD.by_name,
      repr(sorted(agent.CHILD.by_name)))
check("builtin set includes subagent", "subagent" in agent.BUILTIN.by_name, repr(sorted(agent.BUILTIN.by_name)))
out = agent.run_tool({"id": "c", "function": {"name": "subagent", "arguments": json.dumps({"task": "x"})}},
                     agent.CHILD, cwd=tempfile.mkdtemp(), scope="")
check("child calling subagent is refused", guard_hit(out) and "unknown tool" in out[0], repr(out[0])[:80])
# and every other tool is still there, so the guard is narrow
for name in ("read", "bash", "edit", "write"):
    check("child keeps %s" % name, name in agent.CHILD.by_name, repr(sorted(agent.CHILD.by_name)))

# 2. write refuses a file this run has not read.
s, t = fresh()
target = os.path.join(s["cwd"], "existing.txt")
with open(target, "w") as f:
    f.write("original\n")
out = call("write", {"path": target, "content": "clobbered\n"}, s["cwd"], t)
check("write without reading is refused", guard_hit(out) and "read the file before" in out[0], repr(out[0])[:80])
check("write refusal did not touch the file", open(target).read() == "original\n", repr(open(target).read()))
call("read", {"path": target}, s["cwd"], t)
out = call("write", {"path": target, "content": "allowed\n"}, s["cwd"], t)
check("write after reading is allowed", not guard_hit(out), repr(out[0])[:80])
check("write after reading took effect", open(target).read() == "allowed\n", repr(open(target).read()))

# 3. edit refuses a file this run has not read, and refuses an ambiguous oldText.
target2 = os.path.join(s["cwd"], "dup.txt")
with open(target2, "w") as f:
    f.write("same\nsame\n")
out = call("edit", {"path": target2, "edits": [{"oldText": "same", "newText": "x"}]}, s["cwd"], t)
check("edit without reading is refused", guard_hit(out) and "read the file before" in out[0], repr(out[0])[:80])
call("read", {"path": target2}, s["cwd"], t)
out = call("edit", {"path": target2, "edits": [{"oldText": "same", "newText": "x"}]}, s["cwd"], t)
check("edit with an ambiguous oldText is refused", guard_hit(out) and "appears" in out[0], repr(out[0])[:80])
check("edit refusal did not touch the file", open(target2).read() == "same\nsame\n", repr(open(target2).read()))
out = call("edit", {"path": target2, "edits": [{"oldText": "same\nsame", "newText": "one"}]}, s["cwd"], t)
check("edit with a unique oldText is allowed", not guard_hit(out), repr(out[0])[:80])
# overlapping edits in one call are refused, not applied in a half-order
target3 = os.path.join(s["cwd"], "overlap.txt")
with open(target3, "w") as f:
    f.write("abcdef\n")
call("read", {"path": target3}, s["cwd"], t)
out = call("edit", {"path": target3, "edits": [{"oldText": "abcd", "newText": "x"},
                                               {"oldText": "cdef", "newText": "y"}]}, s["cwd"], t)
check("overlapping edits are refused", guard_hit(out) and "overlap" in out[0], repr(out[0])[:80])

# 4. A command that answers itself does not reach the model: `turn` must honour `""`.
calls = []
agent.chat = lambda *a, **k: (calls.append(1), ({"role": "assistant", "content": "rephrased",
                                                "finish_reason": "stop"}, {"prompt_tokens": 5, "completion_tokens": 2}))[1]
acp = agent.ACP()


class Wire:
    def __init__(self):
        self.sent, self.lock = [], threading.Lock()

    def write(self, line):
        with self.lock:
            self.sent.append(json.loads(line))

    def updates(self, sid):
        return [m["params"]["update"] for m in self.sent
                if m.get("method") == "session/update" and m["params"]["sessionId"] == sid]

    def result(self, rid):
        return next((m.get("result") for m in self.sent if m.get("id") == rid and "result" in m), None)


acp.wire = Wire()
acp.write = acp.wire.write
acp.handle({"jsonrpc": "2.0", "id": 1, "method": "session/new",
            "params": {"cwd": tempfile.mkdtemp(), "mcpServers": []}})
sid = acp.wire.result(1)["sessionId"]
calls.clear()
acp.handle({"jsonrpc": "2.0", "id": 2, "method": "session/prompt",
            "params": {"sessionId": sid, "prompt": [{"type": "text", "text": "/cost"}]}})
deadline = time.time() + 15
while time.time() < deadline and acp.wire.result(2) is None:
    time.sleep(0.02)
said = "".join((u.get("content") or {}).get("text", "") for u in acp.wire.updates(sid)
               if u.get("sessionUpdate") == "agent_message_chunk")
check("a self-answering command does not reach the model", not calls, "model was called %d times" % len(calls))
check("its answer still reaches the client", "usage:" in said, repr(said)[:80])
check("the turn still completes", (acp.wire.result(2) or {}).get("stopReason") == "end_turn",
      repr(acp.wire.result(2)))
check("the session recorded no messages for it", acp.sessions[sid]["messages"] == [],
      repr(acp.sessions[sid]["messages"])[:100])
t.close()

# 5. A skill command does reach the model, with its body, and the prompt is replaced.
skill_root = tempfile.mkdtemp()
skill_dir = os.path.join(skill_root, ".agents", "skills", "demo")
os.makedirs(skill_dir)
with open(os.path.join(skill_dir, "SKILL.md"), "w") as f:
    f.write("---\nname: demo\ndescription: a demo skill\n---\nBODY-MARKER\n")
calls.clear()
acp.wire = Wire()
acp.write = acp.wire.write
acp.handle({"jsonrpc": "2.0", "id": 10, "method": "session/new",
            "params": {"cwd": skill_root, "mcpServers": []}})
sid2 = acp.wire.result(10)["sessionId"]
acp.handle({"jsonrpc": "2.0", "id": 11, "method": "session/prompt",
            "params": {"sessionId": sid2, "prompt": [{"type": "text", "text": "/skill:demo"}]}})
deadline = time.time() + 15
while time.time() < deadline and acp.wire.result(11) is None:
    time.sleep(0.02)
msgs = acp.sessions[sid2]["messages"]
joined = json.dumps(msgs)
check("a skill command reaches the model", bool(calls), "model was not called")
check("the skill body is in the prompt", "BODY-MARKER" in joined, joined[:120])
check("the raw command text is not", "/skill:demo" not in joined, joined[:120])

# 6. Closing a run releases the jobs it owns. Without this the children outlive the session.
s, t = fresh()
agent.start_job("sleep 30", cwd=s["cwd"], scope=s["id"], trace=t)
jid = next(iter(t.jobs), None)
check("job is owned by the run", jid is not None and jid in agent.JOBS, repr(list(t.jobs)))
proc = t.jobs[jid]["proc"] if jid else None
t.close()
check("close empties the run's jobs", t.jobs == {}, repr(t.jobs))
check("close removes them from the process index", jid not in agent.JOBS, repr(jid in agent.JOBS))
if proc is not None:
    deadline = time.time() + 10
    while time.time() < deadline and proc.poll() is None:
        time.sleep(0.1)
    check("close kills the child process", proc.poll() is not None, "still running")

# 7. Closing an MCP hub makes it unusable, and a second close is harmless.
try:
    import mcp  # noqa: F401
    have_mcp = True
except ImportError:
    have_mcp = False
if have_mcp:
    import subprocess, textwrap
    d = tempfile.mkdtemp()
    server = os.path.join(d, "s.py")
    with open(server, "w") as f:
        f.write(textwrap.dedent('''
            import json, sys
            TOOLS = [{"name": "add", "description": "add",
                      "inputSchema": {"type": "object", "properties": {}}}]
            for line in sys.stdin:
                if not line.strip(): continue
                m = json.loads(line); i, method = m.get("id"), m.get("method")
                if method == "initialize":
                    out = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}, "serverInfo": {"name": "t", "version": "1"}}
                elif method and method.startswith("notifications/"): continue
                elif method == "tools/list": out = {"tools": TOOLS}
                elif method == "tools/call": out = {"content": [{"type": "text", "text": "ok"}], "isError": False}
                else: out = None
                if i is not None:
                    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": i, "result": out}) + "\\n"); sys.stdout.flush()
        '''))
    hub = agent.MCP([{"name": "t", "command": sys.executable, "args": [server]}], d)
    ts = hub.toolset()
    check("mcp: hub loaded before close", ts is not None and "mcp__t__add" in ts.by_name, repr(hub.errors))
    hub.close()
    check("mcp: close marks it closed", hub.closed is True, repr(hub.closed))
    # A second close must be a no-op, not an exception and not a state change: close() is
    # called from Trace.close(), from close_session, and by the user, so it can arrive twice.
    try:
        hub.close()
        twice_ok, twice_detail = True, ""
    except BaseException as e:  # noqa: BLE001 - any exception is the failure being tested
        twice_ok, twice_detail = False, "%s: %s" % (type(e).__name__, str(e)[:80])
    check("mcp: a second close does not raise", twice_ok, twice_detail)
    check("mcp: a second close leaves it closed", hub.closed is True, repr(hub.closed))
    if ts is not None:
        after = agent.run_tool({"id": "c", "function": {"name": "mcp__t__add", "arguments": "{}"}}, ts, cwd=d)
        check("mcp: calls fail after close", after[2] is True, repr(after[0])[:60])

# 8. clip really truncates -- nothing else asserts it.
long_text = "\n".join("line %d" % i for i in range(500))
clipped = agent.clip(long_text, max_lines=10)
check("clip: honours max_lines", clipped.count("\n") <= 11 and "truncated" in clipped, repr(clipped[-60:]))
check("clip: keeps the head by default", "line 0" in clipped, repr(clipped[:40]))
tail = agent.clip(long_text, max_lines=10, tail=True)
check("clip: tail=True keeps the end", "line 499" in tail, repr(tail[-40:]))
big = "x" * 100_000
check("clip: honours max_bytes", len(agent.clip(big, max_lines=10_000, max_bytes=100).encode()) <= 200,
      repr(len(agent.clip(big, max_lines=10_000, max_bytes=100))))

print()
done("INVARIANTS")

# 9. A subagent told to isolate must not touch the parent's directory. Measured the hard way:
# a child asked to edit its own copy edited /Users/paul/harness instead, because `subagent`
# documented "works in the same directory" and nothing enforced otherwise.
def isolation():
    import shutil, subprocess, tempfile
    root = tempfile.mkdtemp()
    open(os.path.join(root, "target.py"), "w").write("VALUE = 1\n")
    for cmd in (["git", "init", "-q"], ["git", "add", "-A"],
                ["git", "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qm", "init"]):
        subprocess.run(cmd, cwd=root, capture_output=True)
    original = agent.chat
    state = {"n": 0}

    seen = {}

    def fake(system, messages, **kw):
        state["n"] += 1
        if state["n"] == 1:
            return ({"role": "assistant", "content": None, "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "read", "arguments": '{"path": "target.py"}'}}]},
                {"total_tokens": 10})
        # the tool result the child would have produced; the path proves which tree it saw
        for m in reversed(messages):
            if m.get("role") == "tool":
                seen["path"] = m.get("content")
                break
        # a real write, so isolation is proven by the filesystem and not by a report
        return ({"role": "assistant", "content": None, "tool_calls": [
            {"id": "c2", "type": "function", "function": {"name": "edit",
             "arguments": '{"path": "target.py", "edits": [{"oldText": "VALUE = 1", "newText": "VALUE = 2"}]}'}}]},
            {"total_tokens": 10})
    def fake_run(call, toolset, **kw):
        return agent.run_tool(call, toolset, **kw)
    agent.chat = fake
    parent = agent.Trace("test", cwd=root, emit=lambda u: None)
    try:
        out = agent.run_child("edit target.py", parent, root, None, True)
    finally:
        agent.chat = original
        parent.close()
    # the read tool resolves against the child's cwd: if it resolved to the parent, isolation failed
    check("isolate: the parent's file is untouched", open(os.path.join(root, "target.py")).read() == "VALUE = 1\n",
          repr(open(os.path.join(root, "target.py")).read()))
    resolved = (seen.get("path") or "").split("\n")[0].strip()
    check("isolate: the child read from its own tree, not the parent's",
          root not in resolved and resolved != "" and os.path.abspath(resolved).startswith(os.path.abspath(root)) is False,
          "child read %r from root %r" % (resolved, root))
    check("isolate: the result names a separate workspace", "[workspace " in out, out[-200:])
    clone = next((l.split(" ", 1)[1].rstrip("]") for l in out.splitlines() if l.startswith("[workspace ")), "")
    check("isolate: the clone is a different directory", os.path.abspath(clone) != os.path.abspath(root), clone)
    # the child really edited target.py, so the clone must be kept and the diff named
    check("isolate: a changed clone is kept and named",
          "[changed] target.py" in out and os.path.isdir(clone), out[-160:])
    check("isolate: the clone holds the child's edit",
          "VALUE = 2" in open(os.path.join(clone, "target.py")).read(), clone)

    # a child that only reads leaves nothing behind: the clone is removed
    agent.chat = lambda system, messages, **kw: (
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "r1", "type": "function", "function": {"name": "read", "arguments": '{"path": "target.py"}'}}]},
        {"total_tokens": 10}) if not any(m.get("role") == "tool" for m in messages) else (
        {"role": "assistant", "content": "nothing to do", "finish_reason": "stop"}, {"total_tokens": 10})
    parent = agent.Trace("test", cwd=root, emit=lambda u: None)
    try:
        quiet = agent.run_child("just look", parent, root, None, True)
    finally:
        parent.close()
    quiet_clone = next((l.split(" ", 1)[1].rstrip("]") for l in quiet.splitlines() if l.startswith("[workspace ")), "")
    check("isolate: an unchanged clone is reported and removed",
          "[changed] nothing" in quiet and not os.path.isdir(quiet_clone), quiet[-160:])


isolation()

# 10. An isolated child cannot be talked out of its workspace. The failure this prevents was
# measured: three children were given the parent's absolute path in their task and all three
# edited the parent repo, because nothing refused a path outside their clone.
def sandbox():
    import tempfile
    root = tempfile.mkdtemp()
    open(os.path.join(root, "inside.txt"), "w").write("a\n")
    # resolve both sides: on macOS /var is a symlink to /private/var, so a literal prefix
    # comparison would fail on a path that is genuinely inside the workspace
    real_root = agent.Path(root).resolve()
    inside_rel = agent.resolve_in("inside.txt", root, root)
    check("sandbox: a relative path inside is allowed", str(inside_rel).startswith(str(real_root)), str(inside_rel))
    check("sandbox: an absolute path inside is allowed",
          agent.resolve_in(os.path.join(root, "inside.txt"), root, root) == inside_rel, "refused its own file")
    for label, path in (("outside absolute", "/etc/hosts"), ("outside via ..", "../x.txt")):
        try:
            agent.resolve_in(path, root, root)
            check("sandbox: %s is refused" % label, False, "allowed %r" % path)
        except agent.ToolError as e:
            check("sandbox: %s is refused" % label, "escapes the isolated workspace" in str(e), str(e)[:80])
    check("sandbox: the parent agent is unrestricted", agent.resolve_in("/etc/hosts", root, None) == agent.Path("/etc/hosts"),
          "a run without a root must still read the machine")

    # an isolated child must actually HAVE a root. Checking resolve_in alone would pass even if
    # run_child never set it, which is the wiring bug this catches.
    original_chat = agent.chat
    seen = {}

    def grab_root(system, messages, **kw):
        # one real tool call, otherwise loop() finishes without ever reaching run_tool
        if not any(m.get("role") == "tool" for m in messages):
            return ({"role": "assistant", "content": None, "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "read", "arguments": '{"path": "inside.txt"}'}}]},
                {"total_tokens": 10})
        return ({"role": "assistant", "content": "ok", "finish_reason": "stop"}, {"total_tokens": 10})
    agent.chat = grab_root
    parent = agent.Trace("test", cwd=root, emit=lambda u: None)
    real_run_tool = agent.run_tool

    def spy(call, toolset, **kw):
        seen["trace_root"] = getattr(kw.get("trace"), "root", "MISSING")
        return real_run_tool(call, toolset, **kw)
    agent.run_tool = spy
    try:
        agent.run_child("look around", parent, root, None, True)
    finally:
        agent.chat = original_chat
        agent.run_tool = real_run_tool
        parent.close()
    check("sandbox: an isolated child is actually given a root", seen.get("trace_root") not in (None, "MISSING"),
          "the child ran with trace.root=%r" % (seen.get("trace_root"),))


sandbox()

print()
