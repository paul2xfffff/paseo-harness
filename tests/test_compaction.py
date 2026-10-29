import json, sys
import _path  # noqa: F401  (puts the repo root on sys.path)
import agent
# a summary far longer than the old 60_000 char cap must survive intact
LONG = "## Goal\n" + ("- detail line with file path src/module.py and error 'boom'\n" * 2000)
assert len(LONG) > 60_000, len(LONG)
print("fake summary length: %d chars (old cap was 60000)" % len(LONG))
def fake_chat(system, messages, **kw):
    assert kw.get("tools") is None, "summarization must not send tools"
    return {"role": "assistant", "content": LONG, "finish_reason": "stop"}, {"prompt_tokens": 100, "completion_tokens": 900}
agent.chat = fake_chat
class T:
    used = 0; context_window = 0
    def usage(self, *a, **k): pass
    def compacted(self):
        T.saved = "ok"
    def compaction_failed(self, error):
        raise error
    def reset(self, messages, usage=None): pass
msgs = []
for i in range(40):
    msgs += [{"role": "user", "content": "task %d padding " % i + "x" * 4000},
             {"role": "assistant", "content": "result %d " % i + "y" * 4000}]
out = agent.compact_messages(msgs, T(), agent.model_info("gpustack/deepseek-v4.1-flash"),
                             "gpustack/deepseek-v4.1-flash", 20000, None)
assert out is not None, "compaction produced nothing"
body = agent.summary_text(out[0])
print("persisted summary length: %d chars" % len(body))
assert LONG.rstrip() in body or body.startswith("## Goal"), "summary was truncated"
assert len(body) > 60_000, "summary still capped at %d" % len(body)
print("NO SUMMARY CAP: OK")
