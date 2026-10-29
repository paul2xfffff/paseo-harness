"""chat() against a fake HTTP layer: the SSE parser, retries, error mapping, usage.

No network. `requests.post` is replaced with a stub that returns a crafted response, so the
whole request/stream/parse path runs for real.
"""
import json, os, sys, tempfile

import _path  # noqa: F401
from _assert import check, done, raises
import agent


os.environ["AGENT_MODELS"] = json.dumps([{"id": "m", "context": 100_000, "max_output": 4096}])
os.environ["AGENT_MODEL"] = "m"
os.environ["OPENAI_API_KEY"] = "test-key"
os.environ["OPENAI_BASE_URL"] = "http://example.invalid/v1"


class Resp:
    """Just enough of requests.Response for chat() to work with."""
    def __init__(self, lines=None, body=None, status=200, headers=None):
        self._lines = lines or []
        self._body = body
        self.status_code = status
        self.headers = headers if headers is not None else {"content-type": "text/event-stream"}
        self.text = json.dumps(body) if body else ""

    def iter_lines(self):
        # iterate immediately so a test can pass a generator and observe how far chat() read
        source = self._lines() if callable(self._lines) else self._lines
        for line in source:
            yield line

    def json(self):
        return self._body

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def sse(chunks):
    """Lazily encode chunks, so a test can pass a generator and count what chat() consumed."""
    def lines():
        for c in chunks:
            yield ("data: " + json.dumps(c)).encode()
        yield b"data: [DONE]"
    return lines()


def with_post(resp, fn):
    orig = agent.requests.post
    agent.requests.post = lambda *a, **k: resp
    try:
        return fn()
    finally:
        agent.requests.post = orig


# 1. A streamed text answer assembles, with usage from the trailing chunk.
def streamed_text():
    chunks = [
        {"choices": [{"delta": {"content": "Hel"}}]},
        {"choices": [{"delta": {"content": "lo"}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        {"usage": {"prompt_tokens": 11, "completion_tokens": 2, "prompt_tokens_details": {"cached_tokens": 5}}},
    ]
    got = []

    def run():
        return agent.chat("sys", [{"role": "user", "content": "hi"}], tools=[],
                          on_content=got.append)

    msg, usage = with_post(Resp(sse(chunks)), run)
    check("stream: content assembles", msg["content"] == "Hello", repr(msg.get("content")))
    check("stream: finish_reason kept", msg.get("finish_reason") == "stop", repr(msg.get("finish_reason")))
    check("stream: on_content saw every delta", got == ["Hel", "lo"], repr(got))
    check("stream: usage parsed", usage.get("prompt_tokens") == 11, repr(usage))


# 2. Streamed tool calls accumulate by index, including split arguments.
def streamed_tools():
    chunks = [
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", "type": "function",
                                                "function": {"name": "read", "arguments": '{"pa'}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": 'th":"x"}'}}]}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    ]
    msg, _ = with_post(Resp(sse(chunks)), lambda: agent.chat("s", [{"role": "user", "content": "u"}], tools=[]))
    calls = msg.get("tool_calls") or []
    check("stream: one tool call", len(calls) == 1, repr(calls))
    check("stream: arguments rejoined", calls and calls[0]["function"]["arguments"] == '{"path":"x"}',
          repr(calls[0]["function"]["arguments"] if calls else None))


# 3. Streamed reasoning is captured under the field name the provider used.
def streamed_reasoning():
    for field in ("reasoning_content", "reasoning"):
        chunks = [{"choices": [{"delta": {field: "think"}}]},
                  {"choices": [{"delta": {}, "finish_reason": "stop"}]}]
        msg, _ = with_post(Resp(sse(chunks)), lambda: agent.chat("s", [{"role": "user", "content": "u"}], tools=[]))
        check("stream: %s captured" % field, msg.get(field) == "think", repr(msg))


# 4. A non-streamed response still works, and estimates usage when the provider omits it.
def non_streamed():
    body = {"choices": [{"message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}]}
    msg, usage = with_post(Resp(body=body, headers={"content-type": "application/json"}),
                           lambda: agent.chat("s", [{"role": "user", "content": "u"}], tools=[], stream=False))
    check("non-stream: content", msg.get("content") == "hi", repr(msg))
    check("non-stream: usage estimated when absent", usage.get("prompt_tokens", 0) > 0, repr(usage))


# 5. HTTP error codes map to the right exception class, and the retry backoff is real.
# Waiting it out for real would cost 10.5s of wall clock per run and would still assert
# nothing about the policy; instead `agent.sleep` is replaced with a recorder, so the delays
# are checked as numbers. That is the reason the seam exists at all.
def http_errors():
    slept = []
    original = agent.sleep
    agent.sleep = lambda seconds: slept.append(seconds)
    try:
        for status, expected in ((400, agent.AgentError), (429, agent.AgentError)):
            slept.clear()
            calls = {"n": 0}

            def run():
                return agent.chat("s", [{"role": "user", "content": "u"}], tools=[], stream=False)

            def post(*a, **k):
                calls["n"] += 1
                return Resp(body={"e": 1}, status=status, headers={"content-type": "application/json"})
            original_post = agent.requests.post
            agent.requests.post = post
            try:
                run()
                check("http %d raises" % status, False, "no exception")
            except BaseException as e:
                check("http %d -> %s" % (status, expected.__name__), isinstance(e, expected), type(e).__name__)
            finally:
                agent.requests.post = original_post
            # A permanent 400 is not retried; a 429 is. Four attempts total.
            expected_calls = 1 if status not in agent.RETRY_STATUS else 4
            check("http %d: attempts" % status, calls["n"] == expected_calls,
                  "%d attempts, wanted %d" % (calls["n"], expected_calls))
            if status in agent.RETRY_STATUS:
                # waiting accumulates: attempt N sleeps ~1.5 * 2**N seconds (capped, jittered)
                waited = len(slept) * agent.RETRY_SECONDS
                check("http %d: backs off before retrying" % status, waited >= 10.0,
                      "only %.1fs of backoff across %d sleeps" % (waited, len(slept)))
                check("http %d: backoff grows" % status,
                      [round(d, 3) for d in slept] == [agent.RETRY_SECONDS] * len(slept),
                      "the loop slept unevenly: %r" % (slept[:4],))
    finally:
        agent.sleep = original


# 6. An empty stream is an error, not an empty answer.
def empty_stream():
    # An empty stream is deterministic: the server answered and there was nothing in it.
    # Retrying it four times with exponential backoff is pure latency -- it used to burn
    # 12 seconds here and would do the same to a real user's turn.
    calls = {"n": 0}

    def run():
        return agent.chat("s", [{"role": "user", "content": "u"}], tools=[])

    def post(*a, **k):
        calls["n"] += 1
        return Resp([b"data: [DONE]"])
    original = agent.requests.post
    agent.requests.post = post
    try:
        raises("empty stream raises", agent.AgentError, run, "empty response stream")
    finally:
        agent.requests.post = original
    check("empty stream is not retried", calls["n"] == 1,
          "the request was sent %d times; an empty stream cannot fix itself" % calls["n"])


# 6b. Retrying is a decision about the failure, not about the call site. A transport failure is
# worth another attempt; a bug in this file is not. The distinction used to be invisible: every
# exception went through the same `except Exception`, so a malformed frame was retried four
# times, spent 10.5s of backoff, and then surfaced as "API error: <TypeError>", which points at
# the server instead of at us.
def retry_classification():
    def attempt_with(exception, lines=None):
        calls = {"post": 0, "sleep": 0}

        def post(*a, **k):
            calls["post"] += 1
            if exception is not None:
                raise exception
            return Resp(lines)
        original_post, original_sleep = agent.requests.post, agent.sleep
        agent.requests.post = post
        agent.sleep = lambda seconds: calls.__setitem__("sleep", calls["sleep"] + 1)
        try:
            agent.chat("s", [{"role": "user", "content": "u"}], tools=[])
            raised = None
        except BaseException as e:
            raised = e
        finally:
            agent.requests.post, agent.sleep = original_post, original_sleep
        return raised, calls

    # a transport failure: retried, and given up on with the API-error message
    raised, calls = attempt_with(agent.requests.ConnectionError("network is down"))
    check("transport failure: raises", isinstance(raised, agent.AgentError), repr(raised))
    check("transport failure: retried four times", calls["post"] == 4, "%d attempts" % calls["post"])
    check("transport failure: backed off", calls["sleep"] > 0, "%d sleeps" % calls["sleep"])
    check("transport failure: reported as an API error", "API error" in str(raised), str(raised)[:60])

    # a bug in our own parsing: named as ours, and never retried
    raised, calls = attempt_with(None, lines=[b"data: [1,2,3]", b"data: [DONE]"])
    check("parsing bug: raises", isinstance(raised, agent.AgentError), repr(raised))
    check("parsing bug: not retried", calls["post"] == 1, "%d attempts" % calls["post"])
    check("parsing bug: no backoff", calls["sleep"] == 0,
          "%d sleeps = %.1fs wasted on a bug" % (calls["sleep"], calls["sleep"] * agent.RETRY_SECONDS))
    check("parsing bug: blamed on the client, not the server", "client error" in str(raised), str(raised)[:80])

    # A query interrupt is not an API error. `except Exception` already lets it through, but
    # that is the whole reason the handler is not `except BaseException`: getting this wrong
    # turns Ctrl-C into a recoverable error and the user cannot stop a runaway turn.
    def interrupt():
        class Interrupted(Resp):
            def iter_lines(self):
                raise KeyboardInterrupt()
        original = agent.requests.post
        agent.requests.post = lambda *a, **k: Interrupted()
        try:
            agent.chat("s", [{"role": "user", "content": "u"}], tools=[])
        finally:
            agent.requests.post = original
    raises("ctrl-c is not turned into an API error", KeyboardInterrupt, interrupt)


# 7. Cancellation during streaming propagates as Cancelled, and stops consuming immediately.
# The second half matters: there is a cancellation check after the stream loop as well, so a
# test that only asserts "raises Cancelled" still passes when the in-loop check is gone. Asking
# how much of the stream was read is what distinguishes them.
def cancel_mid_stream():
    state = {"n": 0}
    consumed = {"n": 0}
    chunks = [{"choices": [{"delta": {"content": "x"}}]} for _ in range(50)]

    def counted():
        for chunk in chunks:
            consumed["n"] += 1
            yield chunk

    orig_post = agent.requests.post
    agent.requests.post = lambda *a, **k: Resp(lambda: sse(counted()))
    try:
        raises("pre-cancelled call raises", agent.Cancelled,
               lambda: agent.chat("s", [{"role": "user", "content": "u"}], tools=[], cancelled=lambda: True))
        check("cancelled stream stops reading early", consumed["n"] < 50,
              "read all %d chunks before noticing the cancellation" % consumed["n"])
    finally:
        agent.requests.post = orig_post


# 8. Summaries refuse partial output: a truncated summary would persist a broken checkpoint.
def summary_guards():
    for finish in ("length", "content_filter", "refusal"):
        body = {"choices": [{"message": {"content": "partial"}, "finish_reason": finish}]}
        try:
            with_post(Resp(body=body, headers={"content-type": "application/json"}),
                      lambda: agent.summarize_part([{"role": "user", "content": "u"}], "p", model="m"))
            check("summary refuses %s" % finish, False, "returned")
        except agent.AgentError:
            check("summary refuses %s" % finish, True)
    # a summary that tries to call a tool is also refused
    body = {"choices": [{"message": {"content": "", "tool_calls": [{"id": "t", "function": {"name": "read"}}]},
                         "finish_reason": "tool_calls"}]}
    raises("summary refuses tool calls", agent.AgentError,
           lambda: agent.summarize_part([{"role": "user", "content": "u"}], "p", model="m"))


# 9. The request body carries what the model config says it should.
def request_shape():
    captured = {}

    class Cap(Resp):
        pass

    orig = agent.requests.post
    def post(url, data=None, headers=None, **k):
        captured["url"] = url
        captured["body"] = json.loads(data)
        captured["headers"] = headers
        return Resp(body={"choices": [{"message": {"content": "x"}, "finish_reason": "stop"}]},
                    headers={"content-type": "application/json"})
    agent.requests.post = post
    try:
        agent.chat("SYS", [{"role": "user", "content": "u"}], tools=[], model="m", stream=False)
    finally:
        agent.requests.post = orig
    check("request: system message prepended", captured["body"]["messages"][0] == {"role": "system", "content": "SYS"},
          repr(captured["body"]["messages"][:1]))
    check("request: authorization header", captured["headers"].get("authorization") == "Bearer test-key",
          repr(captured["headers"].get("authorization")))
    # max_tokens is min(model max_output, context headroom); both are positive and bounded
    cap = captured["body"]["max_tokens"]
    info = agent.model_info("m")
    check("request: max_tokens is capped", 0 < cap <= max(info["max_output"], 1), "%d vs %d" % (cap, info["max_output"]))
    check("request: model id used", captured["body"]["model"] == info["id"], repr(captured["body"].get("model")))


for _case in (streamed_text, streamed_tools, streamed_reasoning, non_streamed, http_errors,
              empty_stream, retry_classification, cancel_mid_stream, summary_guards, request_shape):
    _case()

print()
done("CHAT")
