"""Background jobs: reaping, per-owner isolation, and the concurrency cap.

Two mechanisms clean up a job, and neither is allowed to leak:
  - `start_job` reaps already-exited jobs from the owner's map before starting a new one
  - `reap_job` runs on its own thread, waits for the process, reports the result, and clears
    both `owner.jobs` and the process-wide `JOBS`
Because the second is asynchronous, asserting an exact `JOBS` size at an arbitrary moment is a
race, not a test. Everything here either waits for a condition or asserts about something that
cannot disappear on its own: the cap and the ownership rules.
"""
import threading
import time

import _path  # noqa: F401
import agent
from _assert import check, done, raises

WAIT = 10.0


def wait_for(predicate, timeout=WAIT):
    """Poll until true. Returns the elapsed seconds, or None if the deadline passed."""
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        if predicate():
            return time.monotonic() - start
        time.sleep(0.02)
    return None


# 1. A finished job leaves the process-wide map on its own. The old code sliced `[:-32]` off
# the finished list, so with fewer than 33 finished jobs nothing was ever reaped -- a
# long-lived session silently accumulated corpses until MAX_JOBS refused new work.
agent.kill_all_jobs()
ids = [agent.start_job("sleep 0.1", cwd="/tmp").split()[1] for _ in range(6)]
check("start: six jobs are registered", len(agent.JOBS) == 6, "%d in JOBS" % len(agent.JOBS))
elapsed = wait_for(lambda: not agent.JOBS)
check("reap: finished jobs leave the map without a second start_job", elapsed is not None,
      "still %d jobs after %.1fs: %s" % (len(agent.JOBS), WAIT, list(agent.JOBS)))
check("reap: every id was released", not (set(ids) & set(agent.JOBS)),
      "left behind: %s" % sorted(set(ids) & set(agent.JOBS)))
check("reap: it did not sit on the deadline", elapsed is not None and elapsed < 5.0,
      "took %.1fs for six 0.1s jobs" % (elapsed or WAIT))

# 2. start_job also reaps synchronously. Asserted without a race: the job is already dead and
# its reaper thread has finished (nothing else can remove it), then it is put back so that the
# next start_job call is the only thing that could possibly clear it.
agent.kill_all_jobs()
dead = agent.start_job("true", cwd="/tmp").split()[1]
job = agent.JOBS.get(dead)
if job is not None:
    wait_for(lambda: job["proc"].poll() is not None or dead not in agent.JOBS)
    snapshot = job
    agent.JOBS[dead] = snapshot          # put it back if the reaper already took it
check("start_job: a dead job can be present in the map", dead in agent.JOBS, "could not stage the job")
agent.start_job("sleep 5", cwd="/tmp")
check("start_job: reaps a dead job before starting another", dead not in agent.JOBS,
      "%s survived a start_job call" % dead)
agent.kill_all_jobs()

# 3. The cap is exact. A runaway agent must not be able to fork without bound, and the refusal
# has to be an error the model can read rather than a silent queue.
check("cap: MAX_JOBS is positive", agent.MAX_JOBS > 0, repr(agent.MAX_JOBS))
for _ in range(agent.MAX_JOBS):
    agent.start_job("sleep 20", cwd="/tmp")
check("cap: exactly MAX_JOBS are admitted", len(agent.JOBS) == agent.MAX_JOBS,
      "%d running, cap is %d" % (len(agent.JOBS), agent.MAX_JOBS))
raises("cap: one past MAX_JOBS is refused", agent.ToolError,
       lambda: agent.start_job("sleep 20", cwd="/tmp"), "too many background jobs")
check("cap: the refusal started nothing", len(agent.JOBS) == agent.MAX_JOBS,
      "%d running after a refusal" % len(agent.JOBS))
agent.kill_all_jobs()

# 4. Ownership: a job belongs to the run that started it. Another run must not see it, kill it,
# or have it counted against its own cap.
session = agent.make_session("/tmp", agent.DEFAULT_MODEL, system="P")
owner = agent.open_trace(session, emit=lambda u: None)
other_session = agent.make_session("/tmp", agent.DEFAULT_MODEL, system="P")
other = agent.open_trace(other_session, emit=lambda u: None)
jid = agent.start_job("sleep 20", cwd="/tmp", trace=owner).split()[1]
check("ownership: the owner sees its job", jid in owner.jobs, repr(list(owner.jobs)))
check("ownership: another run does not", jid not in other.jobs, repr(list(other.jobs)))
raises("ownership: another run cannot kill it", agent.ToolError,
       lambda: agent.kill_job(jid, scope=other.session_id), "unknown job")
check("ownership: the job is still running", jid in owner.jobs, "a foreign kill removed it")
check("ownership: the owner can kill it",
      agent.kill_job(jid, scope=owner.session_id) == "killed " + jid, "the owner could not kill its own job")
owner.close()
other.close()
agent.kill_all_jobs()

# 5. A finished job wakes an idle session. Nobody sends a prompt for a background result, so
# the agent starts the turn itself; while a turn is running the queue covers it and the wake
# must not race a second thread onto the same session.
class FakeServer:
    def __init__(self):
        self.lock, self.running, self.spawned = threading.Lock(), None, []
        self.sessions = {"sess_wake": {}, "sess_root": {}}

    def spawn(self, item): self.spawned.append(item)

real, fake = agent.SERVER, FakeServer()
agent.SERVER = fake
woken = agent.Trace(agent.DEFAULT_MODEL, session_id="sess_wake")
woken.pending.append("[background job_x finished]\noutput")
agent.wake(woken)
check("wake: an idle session gets a turn", len(fake.spawned) == 1, repr(fake.spawned))
check("wake: the turn is marked auto", fake.spawned and fake.spawned[0].get("auto") is True, repr(fake.spawned[:1]))
check("wake: it targets that session", fake.spawned and fake.spawned[0]["sid"] == "sess_wake", repr(fake.spawned[:1]))
check("wake: the report stays queued for the loop", woken.pending, "pending was consumed early")
fake.running, fake.spawned = {"sid": "sess_wake"}, []
agent.wake(woken)
check("wake: a running turn is left alone", not fake.spawned, repr(fake.spawned))
nested = agent.Sub(agent.Trace(agent.DEFAULT_MODEL, session_id="sess_root"), "s", agent.DEFAULT_MODEL)
nested.pending.append("from child")
fake.running, fake.spawned = None, []
agent.wake(nested)
check("wake: a nested run wakes its root", fake.spawned and fake.spawned[0]["sid"] == "sess_root", repr(fake.spawned[:1]))
agent.SERVER = real

done("JOBS")
