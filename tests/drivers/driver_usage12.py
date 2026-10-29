import json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _path  # noqa: F401  (repo root on sys.path, config isolated from the developer's machine)
import agent
def fake_chat(system, messages, **kw):
    cb = kw.get("on_content")
    if cb: cb("ok")
    return {"role": "assistant", "content": "ok"}, {"prompt_tokens": 1000, "completion_tokens": 50, "total_tokens": 1050,
                                                     "prompt_tokens_details": {"cached_tokens": 800}}
agent.chat = fake_chat
sys.exit(agent.acp_main())
