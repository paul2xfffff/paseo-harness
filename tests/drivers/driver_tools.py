import json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _path  # noqa: F401  (repo root on sys.path, config isolated from the developer's machine)
import agent
state = {"i": 0}
def fake_chat(system, messages, **kw):
    if agent.SUMMARY_SYSTEM in system:
        return {"role": "assistant", "content": "SUMMARY"}, {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    i = state["i"]; state["i"] += 1
    if i % 2 == 0:
        name = "out%d.txt" % (i // 2 + 1)
        return {"role": "assistant", "content": None, "tool_calls": [{"id": "c%d" % i, "type": "function",
                "function": {"name": "write", "arguments": json.dumps({"path": name, "content": "made\n"})}}]}, {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25}
    cb = kw.get("on_content")
    if cb: cb("done")
    return {"role": "assistant", "content": "done"}, {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25}
agent.chat = fake_chat
sys.exit(agent.acp_main())
