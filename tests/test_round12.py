"""Truncated output, and what the loop does about it.

A truncated tool call has possibly-cut arguments. Executing it would run half a command, so
the call is failed instead -- and two truncations in a row end the turn rather than looping.
"""
import json, os, sys, tempfile

os.environ["AGENT_MODEL"] = "small"
os.environ["AGENT_MODELS"] = json.dumps([{"id": "small", "context": 200000, "reserve": 1000, "keep_recent": 300,
                                          "max_output": 1000, "reasoning": False}])
import _path  # noqa: F401
import agent
from _assert import check, done

root = tempfile.mkdtemp(prefix="r12-")
usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}

# 1. A truncated tool call must never execute.
ran = []
original = agent.BUILTIN.by_name["read"]
patched = dict(original, run=lambda *a, **k: ran.append(1) or "SHOULD NOT RUN")
agent.BUILTIN.by_name["read"] = patched
agent.BUILTIN.tools[0] = patched
sequence = [
    {"role": "assistant", "content": None, "finish_reason": "length",
     "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "read", "arguments": '{"path": "half'}}]},
    {"role": "assistant", "content": "recovered", "finish_reason": "stop"}]
calls = []


def fake_chat(system, messages, **kw):
    calls.append(messages)
    return sequence[len(calls) - 1], usage


agent.chat = fake_chat
trace = agent.Trace("small", cwd=root, emit=lambda x: None)
_, messages, stop = agent.loop("SYS", [{"role": "user", "content": "go"}], trace, agent.BUILTIN,
                               model="small", cwd=root, reasoning="off")
check("truncated call: did not execute", not ran, "%d executions" % len(ran))
check("truncated call: a tool result explains it",
      any(m.get("role") == "tool" and "truncated" in str(m.get("content")) for m in messages),
      repr([str(m.get("content"))[:40] for m in messages if m.get("role") == "tool"]))
check("truncated call: the assistant message is kept", any(m.get("role") == "assistant" and m.get("tool_calls") for m in messages))
check("truncated call: the turn recovers", stop == "end_turn", repr(stop))
trace.close()

# 2. Two truncations in a row end the turn with max_tokens.
ran.clear()
calls.clear()
sequence = [{"role": "assistant", "content": None, "finish_reason": "length",
             "tool_calls": [{"id": "c%d" % i, "type": "function",
                             "function": {"name": "read", "arguments": '{"path": "x"}'}}]} for i in range(2)]


def fake2(system, messages, **kw):
    calls.append(messages)
    return sequence[len(calls) - 1], usage


agent.chat = fake2
trace = agent.Trace("small", cwd=root)
_, _, stop = agent.loop("SYS", [{"role": "user", "content": "go"}], trace, agent.BUILTIN, model="small", cwd=root)
check("two truncations: stop with max_tokens", stop == "max_tokens", repr(stop))
check("two truncations: no tool ran", not ran, "%d executions" % len(ran))
check("two truncations: bounded calls", len(calls) <= 3, "%d calls" % len(calls))
trace.close()

# 3. A truncated summary must not be persisted as a checkpoint.
bad = {"role": "assistant", "content": "PARTIAL SUMMARY", "finish_reason": "length"}
good = {"role": "assistant", "content": "SUMMARY", "finish_reason": "stop"}


def boom(system, messages, **kw):
    return (bad if agent.SUMMARY_SYSTEM in system else good), usage


agent.chat = boom
session = agent.make_session(root, "small", system="SYS",
                            messages=[{"role": "user" if i % 2 == 0 else "assistant", "content": ("m%d " % i) * 200}
                                      for i in range(40)])
trace = agent.Trace("small", cwd=root)
try:
    agent.compact_session(session, trace)
    check("truncated summary: refused", False, "it persisted")
except agent.AgentError as e:
    check("truncated summary: refused", "refusing to persist" in str(e), str(e)[:80])
check("truncated summary: nothing was written", not agent.summary_text(session["messages"]),
      repr(agent.summary_text(session["messages"]))[:60])
trace.close()

# 4. finish_reason maps to the ACP stop reason.
for reason, expected in (("length", "max_tokens"), ("content_filter", "refusal"), ("stop", "end_turn")):
    agent.chat = lambda system, messages, r=reason, **kw: ({"role": "assistant", "content": "x", "finish_reason": r}, usage)
    trace = agent.Trace("small", cwd=root)
    _, _, stop = agent.loop("SYS", [{"role": "user", "content": "go"}], trace, agent.BUILTIN, model="small", cwd=root)
    check("stop reason: %s -> %s" % (reason, expected), stop == expected, repr(stop))
    trace.close()

# 5. CJK costs more than ASCII per character, and both scale.
ascii_tokens = agent.text_tokens("a" * 350)
cjk_tokens = agent.text_tokens("中" * 350)
check("tokens: ascii is ~2 chars per token", 90 < ascii_tokens < 110, repr(ascii_tokens))
check("tokens: CJK is ~1 char per token", 290 < cjk_tokens < 310, repr(cjk_tokens))
check("tokens: CJK costs more than ascii", cjk_tokens > ascii_tokens, "%d vs %d" % (cjk_tokens, ascii_tokens))
check("tokens: empty is zero", agent.text_tokens("") == 0, repr(agent.text_tokens("")))

done("ROUND12")
