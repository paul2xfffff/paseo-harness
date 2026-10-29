"""A model that answers plainly and summarises on demand.

Used by the /compact test: nothing here should overrun the window on its own, so the only
compaction that happens is the one the client asks for. Keeping it separate from
driver_autocompact (which deliberately floods the context) makes the two tests mean
different things -- a shared driver would let one test's fixture hide the other's bug.
"""
import os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _path  # noqa: F401  (repo root on sys.path, config isolated from the developer's machine)
import agent


def fake_chat(system, messages, **kw):
    if agent.SUMMARY_SYSTEM in system:
        return {"role": "assistant", "content": "SUMMARY", "finish_reason": "stop"}, {"total_tokens": 20}
    cb = kw.get("on_content")
    if cb:
        cb("reply")
    return {"role": "assistant", "content": "reply", "finish_reason": "stop"}, {"total_tokens": 30}


agent.chat = fake_chat
sys.exit(agent.acp_main())
