import json, os, shutil, tempfile
os.environ["AGENT_MODELS"] = json.dumps([{"id": "small", "context": 4000, "reserve": 1000, "keep_recent": 300, "max_output": 500,
                                          "input_cost": 2.0, "output_cost": 4.0, "cache_read_cost": 0.2, "cache_write_cost": 2.5}])
import sys
import _path  # noqa: F401  (puts the repo root on sys.path)
import agent

root = tempfile.mkdtemp()
# exact prompts + cached usage math
assert "### Done" in agent.SUMMARY_PROMPT and "PRESERVE all existing information" in agent.UPDATE_PROMPT
assert "Create a concise checkpoint" in agent.TURN_PROMPT and "ONLY output the structured summary" in agent.SUMMARY_SYSTEM
parts = agent.usage_parts({"prompt_tokens": 1000, "completion_tokens": 10, "prompt_tokens_details": {"cached_tokens": 800}})
assert parts == (1000, 10, 800, 0, 200), parts
trace = agent.Trace("small", cwd=root)
trace.usage({"prompt_tokens": 1000, "completion_tokens": 10, "prompt_tokens_details": {"cached_tokens": 800}}, agent.model_info("small"))
cost = (200 * 2.0 + 800 * 0.2 + 10 * 4.0) / 1e6
assert abs(trace.cost - cost) < 1e-12, (trace.cost, cost)
assert (trace.cache_read, trace.cache_write) == (800, 0)
assert "(cache 800 read" in agent.cost_text({"model": "small", "trace": trace, "usage": {}}), agent.cost_text({"model": "small", "trace": trace, "usage": {}})
trace.close()
# archive + focus + update prompt
session = agent.make_session(root, "small", system="SYS", messages=[agent.summary_message("OLD")] +
    [{"role": "user" if i % 2 == 0 else "assistant", "content": ("m%d " % i) * 80} for i in range(80)])
seen = []
def summary_chat(system, messages, **kw):
    seen.append(messages[-1]["content"])
    return {"role": "assistant", "content": "UPDATED"}, {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
agent.chat = summary_chat
tr = agent.Trace("small", session_id=session["id"], cwd=root)
out = agent.compact_session(session, tr, focus="focus X")
assert "kept" in out and "Additional focus: focus X" in seen[0]
assert "NEW conversation messages" in seen[0] and "<previous-summary>\nOLD" in seen[0]
assert agent.is_summary_message(session["messages"][0]) and "UPDATED" in agent.summary_text(session["messages"])
# journal crash recovery
session2 = agent.make_session(root, "small", system="SYS", messages=[{"role": "user", "content": "base"}])
agent.save_quiet(session2)
t2 = agent.Trace("small", session_id=session2["id"], cwd=root); t2.record({"role": "assistant", "content": "recovered"}); t2.close()
state = agent.load_session(session2["id"], root)
assert [m.get("content") for m in state["messages"]] == ["base", "recovered"], state["messages"]
# tool-output stubs are a last-resort projection, not a mutation
tools = [{"role": "tool", "tool_call_id": "c%d" % i, "content": "x" * 5000} for i in range(10)]
projected, saved = agent.stub_tools(tools)
assert saved > 0 and projected[0]["content"].startswith("[compacted tool output:") and projected[-1] == tools[-1]
assert tools[0]["content"] == "x" * 5000 and projected[1] is not None
# parallel reads
with open(os.path.join(root, "a.txt"), "w") as f: f.write("AAA\n")
with open(os.path.join(root, "b.txt"), "w") as f: f.write("BBB\n")
calls = [{"id": "r1", "type": "function", "function": {"name": "read", "arguments": json.dumps({"path": "a.txt"})}},
         {"id": "r2", "type": "function", "function": {"name": "read", "arguments": json.dumps({"path": "b.txt"})}}]
calls = [{"id": c["id"], "type": "function", "function": {"name": c["function"]["name"], "arguments": c["function"]["arguments"]}} for c in calls]
state = {"n": 0}
def read_chat(system, messages, **kw):
    state["n"] += 1
    if state["n"] == 1: return {"role": "assistant", "content": None, "tool_calls": calls}, {"total_tokens": 10}
    assert [m.get("tool_call_id") for m in messages if m.get("role") == "tool"] == ["r1", "r2"], messages
    assert "AAA" in messages[-2]["content"] and "BBB" in messages[-1]["content"], messages[-2:]
    return {"role": "assistant", "content": "done"}, {"total_tokens": 10}
agent.chat = read_chat
msgs = [{"role": "user", "content": "go"}]
tr = agent.Trace("small", cwd=root)
sysmsg, msgs, stop = agent.loop("SYS", msgs, tr, agent.BUILTIN, model="small", cwd=root)
assert stop == "end_turn" and state["n"] == 2, (stop, state)
# steering is injected after a tool result
state = {"n": 0}; steered = {"done": False}
def steer_chat(system, messages, **kw):
    state["n"] += 1
    if state["n"] == 1: return {"role": "assistant", "content": None, "tool_calls": calls[:1]}, {"total_tokens": 10}
    assert any(m.get("content") == "steer now" for m in messages), messages
    return {"role": "assistant", "content": "done"}, {"total_tokens": 10}
def steer():
    if steered["done"]: return []
    steered["done"] = True; return ["steer now"]
agent.chat = steer_chat
msgs = [{"role": "user", "content": "go"}]; tr = agent.Trace("small", cwd=root)
sysmsg, msgs, stop = agent.loop("SYS", msgs, tr, agent.BUILTIN, model="small", cwd=root, steer=steer)
assert stop == "end_turn" and any(m.get("content") == "steer now" for m in msgs)
# threshold compaction failure must not kill the turn
def failing_summary_chat(system, messages, **kw):
    if agent.SUMMARY_SYSTEM in system: raise agent.AgentError("summary down")
    return {"role": "assistant", "content": "still running"}, {"total_tokens": 50}
agent.chat = failing_summary_chat
msgs = [{"role": "user" if i % 2 == 0 else "assistant", "content": ("m%d " % i) * 80} for i in range(80)]
tr = agent.Trace("small", cwd=root); tr.used = 3500
sysmsg, msgs, stop = agent.loop("SYS", msgs, tr, agent.BUILTIN, model="small", cwd=root)
assert stop == "end_turn" and msgs[-1]["content"] == "still running", (stop, msgs[-1])
s3 = agent.make_session(root, "small", system="SYS", messages=[])
t3 = agent.Trace("small", session_id=s3["id"], cwd=root); t3.record({"role": "user", "content": "x"})
agent.save_quiet(s3)
s4 = agent.make_session(root, "small", system="SYS", messages=[{"role": "user", "content": "base"}])
agent.save_quiet(s4)
t4 = agent.Trace("small", session_id=s4["id"], cwd=root)
t4.record({"role": "assistant", "content": "new"}); t4.close()
state4 = agent.load_session(s4["id"], root)
assert [m.get("content") for m in state4["messages"]] == ["base", "new"], state4["messages"]
print("reload:", [m.get("content") for m in state4["messages"]])
print("P0P1 OK")
