"""Compaction end to end: when it fires, what it preserves, and how a summary is updated."""
import json, os, sys, tempfile

os.environ["AGENT_MODEL"] = "small"
os.environ["AGENT_MODELS"] = json.dumps([{"id": "small", "context": 4000, "reserve": 1000, "keep_recent": 300,
                                          "max_output": 1000, "reasoning": False}])
import _path  # noqa: F401
import agent
from _assert import check, done

check("windows: derived from context and reserve",
      agent.context_windows(agent.model_info("small")) == (3000, 300),
      repr(agent.context_windows(agent.model_info("small"))))

calls = []


def fake_chat(system, messages, **kw):
    calls.append((system, messages))
    if agent.SUMMARY_SYSTEM in system:
        prompt = messages[-1]["content"]
        text = "TURN SUMMARY" if "earlier part of the current turn" in prompt else (
            "UPDATED" if "<previous-summary>" in prompt else "SUMMARY")
        return {"role": "assistant", "content": text, "finish_reason": "stop"}, {"prompt_tokens": 100, "completion_tokens": 20}
    if len([c for c in calls if c[0] != agent.SUMMARY_SYSTEM]) == 1:
        return ({"role": "assistant", "content": None, "finish_reason": "tool_calls", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "write",
                                                          "arguments": json.dumps({"path": "out.txt", "content": "x" * 3000})}},
            {"id": "c2", "type": "function", "function": {"name": "read",
                                                          "arguments": json.dumps({"path": "in.txt"})}}]},
                {"prompt_tokens": 3000, "completion_tokens": 500})
    return {"role": "assistant", "content": "done", "finish_reason": "stop"}, {"prompt_tokens": 400, "completion_tokens": 100}


agent.chat = fake_chat
root = tempfile.mkdtemp(prefix="pi-ctx-")
trace = agent.Trace("small", cwd=root, emit=lambda x: None)
system, messages, stop = agent.loop("SYS", [{"role": "user", "content": "build it"}], trace, agent.BUILTIN,
                                    model="small", cwd=root)
summary = agent.summary_text(messages)
check("auto-compact: the turn completes", stop == "end_turn", repr(stop))
check("auto-compact: a summary replaces the head", agent.is_summary_message(messages[0]), repr(messages[0])[:60])
check("auto-compact: the split turn is marked", "Turn Context" in summary, repr(summary[-120:]))
check("auto-compact: file tags are stripped from the summary body", "<modified-files>" not in summary, repr(summary[-120:]))
check("auto-compact: the history is shorter than the raw turn", len(messages) < 10, "%d messages" % len(messages))
check("auto-compact: the summarizer was called", any(c[0] == agent.SUMMARY_SYSTEM for c in calls), "%d calls" % len(calls))
trace.close()

# An update must carry the previous summary forward, and keep the file lists.
calls.clear()
session = agent.make_session(root, "small", system="SYS",
                            messages=[agent.summary_message("OLD <read-files>\nold.txt\n</read-files>")] +
                                     [{"role": "user" if i % 2 == 0 else "assistant", "content": ("m%d " % i) * 100}
                                      for i in range(100)])
trace = agent.Trace("small", cwd=root)
out = agent.compact_session(session, trace)
update = [c[1][-1]["content"] for c in calls if c[0] == agent.SUMMARY_SYSTEM and "<previous-summary>" in c[1][-1]["content"]]
check("update: reports a compaction", out.startswith("Compacted"), repr(out))
check("update: the previous summary was passed in", bool(update) and "old.txt" in update[-1], "%d updates" % len(update))
check("update: the new text is used", "UPDATED" in agent.summary_text(session["messages"]),
      repr(agent.summary_text(session["messages"]))[:80])
check("update: the file list is carried forward",
      "<read-files>\nold.txt\n</read-files>" in agent.summary_text(session["messages"]),
      repr(agent.summary_text(session["messages"]))[-120:])
check("update: the history shrank", len(session["messages"]) < 101, "%d messages" % len(session["messages"]))
trace.close()

# file_ops: read minus modified, sorted; previous lists survive.
read, modified = agent.file_ops([
    {"role": "assistant", "tool_calls": [{"function": {"name": "write", "arguments": json.dumps({"path": "a"})}}]},
    {"role": "assistant", "tool_calls": [{"function": {"name": "read", "arguments": json.dumps({"path": "b"})}}]},
    {"role": "assistant", "tool_calls": [{"function": {"name": "edit", "arguments": json.dumps({"path": "c"})}}]}],
    "<read-files>\nold.txt\n</read-files>")
check("file_ops: modified list is separate", modified == ["a", "c"], repr(modified))
check("file_ops: read list excludes modified", read == ["b", "old.txt"], repr(read))

# A legacy "# Previous progress" system prompt becomes a summary on load.
old = agent.make_session(root, "small", system="SYS\n\n# Previous progress\n\nOLD SUMMARY")
agent.save_quiet(old)
loaded = agent.load_session(old["id"], root)
check("migrate: the marker is split out of the system prompt", loaded["system"] == "SYS", repr(loaded["system"]))
check("migrate: the prior progress became a summary", agent.summary_text(loaded["messages"]) == "OLD SUMMARY",
      repr(agent.summary_text(loaded["messages"])))

# /reload rebuilds the system prompt but does not lose the summary.
reloaded = agent.make_session(root, "small", system="SYS", messages=[agent.summary_message("S")])
agent.reload_system(reloaded)
check("reload: the summary survives", agent.summary_text(reloaded["messages"]) == "S", repr(reloaded["messages"])[:60])

done("CONTEXT")
