"""A tiny assertion helper for the script-style tests.

These tests were written to print, which meant they could never fail: a regression showed up
as a different line of output, not as a non-zero exit. `check` makes the expectation explicit
and remembers failures, so `run.py` sees them.
"""
import json, queue, sys, threading

FAILS = []


def check(name, cond, detail=""):
    print("%-4s %s%s" % ("ok" if cond else "FAIL", name, (" — " + detail) if detail and not cond else ""))
    if not cond:
        FAILS.append(name)
    return cond


def raises(name, exc, fn, contains=""):
    """Assert that `fn` raises exactly `exc` (subclasses pass), optionally containing `contains`.

    Hand-rolled `try: fn() / except exc: check(True)` is worse than useless: if `fn` raises a
    *different* exception, or returns normally, the `check` line is never reached and the test
    passes without asserting anything. This always records a verdict.
    """
    try:
        fn()
    except exc as e:
        text = str(e)
        if contains and contains not in text:
            return check(name, False, "raised %s but the message lacks %r: %s" % (type(e).__name__, contains, text[:120]))
        return check(name, True)
    except BaseException as e:  # noqa: BLE001 - the wrong exception type is a failure, not an error
        return check(name, False, "raised %s, wanted %s: %s" % (type(e).__name__, exc.__name__, str(e)[:120]))
    return check(name, False, "returned normally, wanted %s" % exc.__name__)


def done(label):
    print()
    if FAILS:
        print("FAILED: %s" % ", ".join(FAILS))
        sys.exit(1)
    print("%s OK" % label)


def read_lines(proc, default_timeout=8):
    """Read JSON lines from a child process without the select()/readline race.

    select() on the raw fd reports nothing once the TextIOWrapper has buffered the next line,
    so a burst of notifications makes the test time out and look like a server hang. A reader
    thread owns the pipe instead; `read` waits at most `timeout` seconds for one line.
    """
    lines = queue.Queue()

    def pump():
        try:
            for line in proc.stdout:
                lines.put(line)
        finally:
            lines.put(None)
    threading.Thread(target=pump, daemon=True).start()

    def read(timeout=None):
        limit = default_timeout if timeout is None else timeout
        try:
            line = lines.get(timeout=limit)
        except queue.Empty:
            raise TimeoutError("no message within %.0fs" % limit)
        if line is None:
            raise EOFError("child closed stdout")
        return json.loads(line)
    return read
