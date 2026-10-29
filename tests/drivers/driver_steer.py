import json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _path  # noqa: F401  (repo root on sys.path, config isolated from the developer's machine)
import agent

calls = {"n": 0}
def fake_chat(system, messages, **kw):
    if agent.SUMMARY_SYSTEM in system:
        return {"role": "assistant", "content": "SUMMARY"}, {"total_tokens": 10}
    calls["n"] += 1
    if calls["n"] == 1:
        time.sleep(0.35)
        return {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "read", "arguments": json.dumps({"path": "note.txt"})}}]}, {"total_tokens": 10}
    seen = [m.get("content") for m in messages if m.get("role") == "user"]
    with open("/tmp/acp_steer_seen.json", "w") as f: json.dump(seen, f)
    with open("/tmp/acp_steer_calls.json", "w") as f: json.dump(calls["n"], f)
    cb = kw.get("on_content")
    if cb: cb("done")
    return {"role": "assistant", "content": "done"}, {"total_tokens": 10}
agent.chat = fake_chat
sys.exit(agent.acp_main())
