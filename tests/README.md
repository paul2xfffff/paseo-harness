# Tests

No framework. Each `test_*.py` is a script that asserts and prints; a non-zero exit is a
failure. Run them all:

```bash
.venv/bin/python tests/run.py           # 22 tests, ~18s
.venv/bin/python tests/run.py tools     # just the ones matching "tools"
```

`_assert.py` provides `check(name, cond, detail)`, `raises(name, exc, fn, contains)` and
`done(label)`. A test that only prints can never fail; a test that uses `check` cannot pass
without asserting. `raises` exists because the hand-written
`try: fn() / except X: check(True)` pattern records nothing at all when `fn` raises the *wrong*
exception — it just leaks the traceback and the assertion is never reached.

## What is covered

| | |
|---|---|
| `test_tools` | read/edit/write, the read-before-write guard, line numbers, diff output |
| `test_loop` | the turn loop: tool calls, step/error/budget guards, compaction |
| `test_context` | context-window math, automatic compaction, summary carry-forward |
| `test_compaction` | summarization boundaries, stub fallback, no summary cap |
| `test_p0p1` | model routing, provider selection |
| `test_round12` | truncated output: a cut tool call never executes, a cut summary is refused |
| `test_round13` | the journal: incremental snapshots, shrinking, crash recovery |
| `test_chat` | the HTTP layer: SSE parsing, retries, usage, cancellation mid-stream |
| `test_mcp` | the MCP client, against hand-written JSON-RPC stdio servers |
| `test_acp_protocol` | every ACP method, called in-process |
| `test_acp_*` | a real ACP session over a pipe, driven by a fake model in `drivers/` |
| `test_invariants` | the guards a mutation test proved nothing else covered |
| `test_edge_paths` | permission timeouts, legacy journals, cancellation, empty results |
| `test_jobs` | background job reaping and the concurrency cap |
| **`test_regressions`** | **the failures this codebase actually had** |
| **`test_project`** | **the README's claims and the repo's own rules** |

`test_regressions.py` is the important one. Every bug fixed in the last rounds was found by
exercising a failure path, not by reading the code: a command that burned a model turn, a
subagent whose background output vanished, a session that leaked its jobs when saving failed,
a skill description that could break out of the prompt's XML. If any of those come back, that
file says so.

`test_project.py` is the other kind: it checks the repository rather than the agent. Zero blank
lines in `agent.py`, exactly the three pinned dependencies and no fourth, `.gitignore` still
covering `.harness/sessions/`, no API key anywhere outside those ignored directories, every
`import agent` preceded by `_path` so a test cannot read the developer's config. Those are the
invariants that rot silently, because nothing else looks at them.

## The ACP tests are not mocked

`test_acp_*` start `drivers/driver_*.py` as a subprocess. The driver replaces `agent.chat`
with a scripted response and runs the real `acp_main()` — real JSON-RPC over stdin/stdout,
real session files, real permission prompts. Only the model is fake.

That is deliberate: several bugs lived in the protocol layer and were invisible to unit tests
that called the handler functions directly.

Every driver and every test that imports `agent` imports `_path` first. That module puts *this*
checkout on `sys.path` and points `AGENT_CONFIG` at a file that does not exist. Without it a
test reads `~/.harness/config.json` — real models, real MCP servers, real network — and the
suite passes or hangs depending on whose laptop it runs on. A hard-coded
`sys.path.insert(0, "/Users/someone/harness")` defeats this silently: the test then exercises
that path's `agent.py` rather than the one next to it, so a broken checkout still passes.

## Verifying a test actually tests something

A green suite proves nothing about the suite. The way to check a test is to break the code it
claims to cover and confirm it fails:

```bash
cp agent.py /tmp/agent_good.py
python - <<'PY'
src = open("/tmp/agent_good.py").read()
open("agent.py", "w").write(src.replace("read the file before overwriting", "OVERRIDE OK", 1))
PY
.venv/bin/python tests/test_tools.py; echo "exit=$?"   # must be non-zero
cp /tmp/agent_good.py agent.py
```

Every `test_*` file here has been through that. Three tests failed it the first time and were
rewritten: `test_acp_steer` asserted that the steered text appeared in the session, which is
also true when the steer is *dropped* and retried as a second turn; `test_jobs` printed the
concurrency cap instead of asserting it; `test_edge_paths` had a `tool_end` block with no
assertion in it at all.
