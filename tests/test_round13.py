"""The journal: how it grows, how it shrinks, and what survives a crash.

A session file is append-only, but `save_quiet` must not rewrite it per turn (that would be
O(messages) per turn), and `_shrink` must keep the config and the newest checkpoint.
"""
import json, os, sys, tempfile

os.environ["AGENT_MODEL"] = "small"
os.environ["AGENT_MODELS"] = json.dumps([{"id": "small", "context": 4000, "reserve": 800,
                                          "keep_recent": 300, "max_output": 400}])
import _path  # noqa: F401
import agent
from _assert import check, done

root = tempfile.mkdtemp(prefix="r13-")
summary = "## Goal\n- keep going"


def fake(system, messages, **kw):
    if agent.SUMMARY_SYSTEM in system:
        return {"role": "assistant", "content": summary, "finish_reason": "stop"}, {"prompt_tokens": 50, "completion_tokens": 10}
    return {"role": "assistant", "content": "ok", "finish_reason": "stop"}, {"prompt_tokens": 3800, "completion_tokens": 10}


agent.chat = fake
windows = agent.context_windows(agent.model_info("small"))
check("windows: small model", windows == (3200, 300), repr(windows))
# AGENT_MODELS replaces the catalog, so every key resolves to this one model
check("windows: an unknown key falls back to the one model",
      agent.context_windows(agent.model_info("deepseek/deepseek-flash")) == windows,
      repr(agent.context_windows(agent.model_info("deepseek/deepseek-flash"))))

# 1. Automatic compaction over the threshold.
s = agent.make_session(root, "small", system="SYS")
msgs = [{"role": "user", "content": "start"}]
for i in range(6):
    msgs += [{"role": "assistant", "content": "x" * 900}, {"role": "user", "content": "y" * 900}]
t = agent.open_trace(s, emit=lambda u: None)
s["messages"] = msgs
_, out, stop = agent.loop("SYS", msgs, t, agent.BUILTIN, model="small", cwd=root)
check("auto-compact: completes", stop == "end_turn", repr(stop))
check("auto-compact: produced a summary", bool(agent.summary_text(out)), repr(out[:1])[:80])
check("auto-compact: the history is bounded", len(out) < len(msgs), "%d -> %d" % (len(msgs), len(out)))
t.close()
agent.save_quiet(s)

# 2. Reload keeps config, summary and usage.
state = agent.load_session(s["id"], root)
check("reload: messages survived", len(state["messages"]) > 0, repr(len(state["messages"])))
# a session made without a system prompt gets the built-in one, and that is what reloads
check("reload: system survived", state["system"].startswith("You are a coding agent"), repr(state["system"])[:60])
check("reload: summary survived", bool(agent.summary_text(state["messages"])), repr(state["messages"][:1])[:80])
check("reload: usage shape is present", set(state["usage"]) == {"input", "output", "cost", "cache_read", "cache_write"},
      repr(state["usage"]))

# 3. A snapshot is incremental: the file must not be rewritten with the whole history each turn.
p = agent.session_file(s["id"], root)
big = [{"role": "user", "content": "z" * 2000} for _ in range(200)]
s["messages"] = big
s["trace"] = agent.open_trace(s, s["id"])
before = os.path.getsize(p)
for _ in range(5):
    agent.save_quiet(s)
grew = os.path.getsize(p) - before
full_rewrite = len(json.dumps(big))
check("snapshot: cost per turn is small, not the whole history", grew // 5 < full_rewrite // 10,
      "%d bytes/turn vs %d for a full rewrite" % (grew // 5, full_rewrite))

# 4. Shrinking rewrites the journal down to its newest checkpoint. It deliberately refuses to
# cut inside a checkpoint that is itself over the cap (that would lose the state), so a fresh
# oversize journal with several checkpoints is the only fair input.
fresh = agent.make_session(root, "small", system="SYS")
fresh_path = agent.session_file(fresh["id"], root)
ft = agent.open_trace(fresh, emit=lambda u: None)
for i in range(20):
    ft.reset([{"role": "user", "content": ("m%d " % i) * 3000}])
    ft._event({"t": "msg", "message": {"role": "assistant", "content": ("a%d " % i) * 3000}})
ft._fp.flush()
agent.TRACE_MAX_BYTES = 50_000
oversize = os.path.getsize(fresh_path)
shrunk = ft._shrink()
size = os.path.getsize(fresh_path)
check("shrink: reported a shrink", shrunk is True, "%r from %d bytes" % (shrunk, oversize))
check("shrink: the file got smaller", size < oversize, "%d -> %d" % (oversize, size))
check("shrink: it kept a checkpoint rather than emptying the file", size > 0, "%d bytes" % size)
ft.close()
state = agent.load_session(fresh["id"], root)
check("shrink: still loads", bool(state), repr(state)[:60])
check("shrink: model survived", state["model"] == "env/small", repr(state["model"]))
check("shrink: the history survived", len(state["messages"]) > 0, "%d messages" % len(state["messages"]))
s = fresh

# 5. When no summary boundary exists, old tool output is stubbed instead.
tool_msgs = [{"role": "user", "content": "go"}]
for i in range(3):
    tool_msgs += [{"role": "assistant", "content": None, "tool_calls": [
        {"id": "c%d" % i, "type": "function", "function": {"name": "bash", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c%d" % i, "content": "z" * 40000}]
stubbed, saved = agent.stub_tools(tool_msgs)
check("stub: reports what it saved", saved > 0, repr(saved))
check("stub: older output is replaced", "[compacted tool output" in stubbed[2]["content"], repr(stubbed[2]["content"])[:60])
check("stub: the newest output is kept whole", stubbed[-1]["content"] == tool_msgs[-1]["content"], "newest was stubbed")

# 6. A crash mid-tool-call leaves a dangling call, which must be trimmed on load.
s["trace"].record({"role": "assistant", "content": None, "tool_calls": [
    {"id": "c9", "type": "function", "function": {"name": "read", "arguments": "{}"}}]})
s["trace"].close()
state = agent.load_session(s["id"], root)
check("crash: reloads", state is not None)
check("crash: the dangling tool call is dropped",
      not any(m.get("tool_calls") for m in state["messages"]),
      repr([m for m in state["messages"] if m.get("tool_calls")])[:120])

done("ROUND13")
