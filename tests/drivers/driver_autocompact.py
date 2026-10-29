import json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _path  # noqa: F401  (repo root on sys.path, config isolated from the developer's machine)
import agent
n = {"i": 0}
def fake_chat(system, messages, **kw):
    if agent.SUMMARY_SYSTEM in system:
        return {"role": "assistant", "content": "## Goal\n- compacted work", "finish_reason": "stop"}, {"prompt_tokens": 10, "completion_tokens": 5}
    n["i"] += 1
    if n["i"] == 1:
        return {"role": "assistant", "content": None, "finish_reason": "tool_calls", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "bash", "arguments": json.dumps({"command": "seq 1 4000"})}}]}, \
            {"prompt_tokens": 3800, "completion_tokens": 10}
    return {"role": "assistant", "content": "all done", "finish_reason": "stop"}, {"prompt_tokens": 3800, "completion_tokens": 10}
agent.chat = fake_chat
sys.exit(agent.acp_main())
