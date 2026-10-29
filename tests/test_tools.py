"""read / edit / write: the file tools and the guards in front of them."""
import json, os, sys, tempfile

import _path  # noqa: F401
import agent
from _assert import check, done

root = tempfile.mkdtemp(prefix="tools-")


class Run:                       # a tool needs an owning run: that is where the read ledger lives
    def __init__(self):
        self.reads, self.read_cache, self.pending, self.jobs = set(), {}, [], {}


run = Run()
ctx = {"cwd": root, "trace": run}

path = os.path.join(root, "sample.py")
with open(path, "w") as f:
    f.write("\n".join("def f%d():\n    pass" % i for i in range(60)) + "\n")

first = agent.read_file({"path": path}, ctx)
second = agent.read_file({"path": path}, ctx)
forced = agent.read_file({"path": path, "force": True}, ctx)
check("read: numbers lines", first.startswith("     1\tdef f0():"), repr(first[:24]))
check("read: a repeat is answered from the cache", "already read" in second, repr(second[:60]))
check("read: force re-reads", forced == first, "forced read differed")

outline = agent.outline_file(agent.Path(path))
check("outline: lists definitions", "def f0():" in outline and "def f1():" in outline, repr(outline[:80]))

blocks = agent.edit_file({"path": path, "edits": [{"oldText": "def f0():\n    pass",
                                                  "newText": "def f0():\n    return 1"}]}, ctx)
kinds = [b["type"] for b in blocks]
check("edit: reports text and a diff", kinds == ["text", "diff"], repr(kinds))
check("edit: the diff carries the new text", "return 1" in blocks[-1]["newText"], repr(kinds))
after = agent.read_file({"path": path}, ctx)
check("edit: took effect on disk", "return 1" in after, repr(after[:60]))

guarded = os.path.join(root, "q.txt")
with open(guarded, "w") as f:
    f.write("old\n")
try:
    agent.write_file({"path": guarded, "content": "new\n"}, ctx)
    check("write: refuses an unread file", False, "it overwrote")
except agent.ToolError as e:
    check("write: refuses an unread file", "read the file before" in str(e), str(e)[:60])
check("write: the refusal left the file alone", open(guarded).read() == "old\n", repr(open(guarded).read()))
agent.read_file({"path": guarded}, ctx)
written = agent.write_file({"path": guarded, "content": "new\n"}, ctx)
check("write: succeeds after a read", written[0]["text"].startswith("overwrote"), repr(written[0]["text"])[:40])
check("write: took effect", open(guarded).read() == "new\n", repr(open(guarded).read()))

small = agent.context_windows({"context": 1200, "reserve": 16384, "keep_recent": 20000, "max_output": 1024})
large = agent.context_windows({"context": 8192, "reserve": 16384, "keep_recent": 20000, "max_output": 1024})
check("windows: a tiny context still leaves room", small[0] > 0 and small[0] < 1200, repr(small))
check("windows: the keep tail is bounded", large[1] <= large[0], repr(large))

started = agent.run_bash({"command": "sleep 0.2; echo ready", "background": True}, ctx)
jid = started.split()[1]
check("bash: background returns an id", jid.startswith("job_"), repr(started)[:40])
check("bash: the job is owned by the run", jid in run.jobs, repr(list(run.jobs)))
code = agent.JOBS[jid]["proc"].wait(5)
check("bash: the job finishes", code == 0, repr(code))

check("read ledger tracks what was read", len(run.reads) >= 2, repr(run.reads))

done("TOOLS")
