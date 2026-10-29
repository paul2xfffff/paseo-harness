"""The turn loop's four stopping conditions, plus compaction.

Each guard is a different way a turn can end, and every one of them was previously only
printed. A regression here means a runaway agent spends real money.
"""
import json, os, sys, tempfile

os.environ["AGENT_MODEL"] = "small"
os.environ["AGENT_MODELS"] = json.dumps([{"id": "small", "context": 1200, "max_output": 200,
                                          "reasoning": False, "input_cost": 500000, "output_cost": 0}])
os.environ["AGENT_MAX_STEPS"] = "2"
os.environ["AGENT_MAX_ERRORS"] = "2"
os.environ["AGENT_MAX_BUDGET"] = "0"
import _path  # noqa: F401
import agent
from _assert import check, done

root = tempfile.mkdtemp(prefix="loop-")


def usage(prompt=10, completion=5):
    return {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion}


# 1. Compaction: a tiny window, a long history, and a summary at the front afterwards.
events = []
trace = agent.Trace("small", cwd=root, emit=events.append)
session = agent.make_session(root, "small", system=agent.build_system(root), messages=[
    {"role": "user" if i % 2 == 0 else "assistant", "content": ("m%d " % i) * 100} for i in range(100)])


def fake_chat(system, messages, **kw):
    if agent.SUMMARY_SYSTEM in system:
        return {"role": "assistant", "content": "SUMMARY"}, usage()
    return {"role": "assistant", "content": "done"}, usage()


agent.chat = fake_chat
before = len(session["messages"])
text = agent.compact_session(session, trace)
check("compact: reports what it did", "Compacted" in text, repr(text))
check("compact: actually shrank the history", len(session["messages"]) < before,
      "%d -> %d" % (before, len(session["messages"])))
check("compact: the summary is first", "SUMMARY" in agent.summary_text(session["messages"]), repr(session["messages"][:1])[:80])
check("compact: the client was told", any("compacted" in str(e) for e in events), repr(events)[-1:])
trace.close()

# 2. Step guard: a model that always calls a tool stops at MAX_STEPS.
calls = {"n": 0}


def tool_chat(system, messages, **kw):
    calls["n"] += 1
    return ({"role": "assistant", "content": None, "tool_calls": [
        {"id": "c%d" % calls["n"], "type": "function", "function": {"name": "bash", "arguments": json.dumps({"command": "echo %d" % calls["n"]})}}]},
        usage())


agent.chat = tool_chat
trace = agent.Trace("small", cwd=root)
_, _, stop = agent.loop("SYS", [{"role": "user", "content": "go"}], trace, agent.BUILTIN, model="small", cwd=root)
check("step guard: stops", stop == "max_turn_requests", repr(stop))
check("step guard: honours the limit", calls["n"] == agent.MAX_STEPS, "%d vs %d" % (calls["n"], agent.MAX_STEPS))
trace.close()

# 3. Error guard: consecutive failing tools stop the turn.
calls["n"] = 0


def error_chat(system, messages, **kw):
    calls["n"] += 1
    # a non-zero exit is a result, not an error: use a tool call that actually fails
    return ({"role": "assistant", "content": None, "tool_calls": [
        {"id": "e%d" % calls["n"], "type": "function",
         "function": {"name": "read", "arguments": json.dumps({"path": "/definitely/not/a/file"})}}]},
        usage())


agent.MAX_STEPS = 0          # let the error guard be the one that fires
agent.chat = error_chat
emitted = []
trace = agent.Trace("small", cwd=root, emit=emitted.append)
_, msgs, stop = agent.loop("SYS", [{"role": "user", "content": "go"}], trace, agent.BUILTIN, model="small", cwd=root)
check("error guard: stops the turn", stop == "end_turn", repr(stop))
# the explanation goes to the client as an agent message, not into the history
check("error guard: the client is told why",
      any("too many consecutive" in str(e) for e in emitted),
      repr([str(e)[:60] for e in emitted])[-160:])
check("error guard: stops before the model is called forever", calls["n"] <= agent.MAX_ERRORS + 1,
      "%d calls" % calls["n"])
trace.close()

# 4. Budget guard: spend per call over the cap ends the turn.
agent.MAX_BUDGET = 0.5
agent.MAX_STEPS = 0
calls["n"] = 0
trace = agent.Trace("small", cwd=root)
_, _, stop = agent.loop("SYS", [{"role": "user", "content": "go"}], trace, agent.BUILTIN, model="small", cwd=root)
check("budget guard: stops", stop in ("end_turn",), repr(stop))
check("budget guard: did not run away", calls["n"] <= 3, "%d calls" % calls["n"])
check("budget guard: cost was tracked", trace.cost > 0, repr(trace.cost))
trace.close()
agent.MAX_BUDGET = 0

# 5. A plain answer ends the turn with end_turn and one model call.
calls["n"] = 0
agent.chat = fake_chat
trace = agent.Trace("small", cwd=root)
system, msgs, stop = agent.loop("SYS", [{"role": "user", "content": "hi"}], trace, agent.BUILTIN, model="small", cwd=root)
check("plain turn: end_turn", stop == "end_turn", repr(stop))
check("plain turn: the answer is in the history", msgs[-1].get("content") == "done", repr(msgs[-1])[:60])
trace.close()

done("LOOP")
