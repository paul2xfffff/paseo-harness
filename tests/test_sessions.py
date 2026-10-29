"""Where session journals live.

A journal is the full conversation, which frequently contains API keys. It used to be written
into <cwd>/.harness/sessions, so every repository an agent touched could end up holding one,
protected only by whatever .gitignore happened to say. Sessions now live in one data directory
outside the workspace, and this file pins that: the default, the override, and the read-only
legacy fallback.

The environment is rebuilt per case because the answer depends on it -- a test that inherits
the developer's AGENT_SESSIONS is not testing the default at all.
"""
import os, json, subprocess, sys, tempfile

import _path  # noqa: F401 - repo root on sys.path
from _assert import check, done

AGENT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "agent.py")
KEYS = ("AGENT_SESSIONS", "AGENT_CONFIG")


def fresh_agent(**environment):
    """Import agent.py in a subprocess with exactly the environment the case needs."""
    script = ("import json, os, sys; sys.path.insert(0, %r); import agent; "
              "print(json.dumps({'dir': str(agent.sessions_dir()), "
              "'home': os.path.expanduser('~'), 'cwd': os.getcwd()}))" % os.path.dirname(AGENT))
    env = {k: v for k, v in os.environ.items() if k not in KEYS}
    env.update(environment)
    p = subprocess.run([sys.executable, "-B", "-c", script], capture_output=True, text=True, env=env)
    if p.returncode != 0:
        raise AssertionError("agent did not import: %s" % p.stderr[-400:])
    return json.loads(p.stdout)


# 1. The default must be outside any workspace. This is the whole point: a journal written
# into the repository is a journal that can be committed.
info = fresh_agent()
check("default: lives under the home directory",
      info["dir"].startswith(info["home"]), "%s not under %s" % (info["dir"], info["home"]))
check("default: never inside the current working directory",
      not info["dir"].startswith(info["cwd"]), "%s is inside %s" % (info["dir"], info["cwd"]))

# 2. AGENT_SESSIONS must win, because that is how a caller redirects state (the tests use it).
override = tempfile.mkdtemp(prefix="sessions-override-")
info = fresh_agent(AGENT_SESSIONS=override)
check("override: AGENT_SESSIONS is honoured", info["dir"] == override, "%s != %s" % (info["dir"], override))

# 3. A journal written before the move must still be findable, or an upgrade loses history.
work = tempfile.mkdtemp(prefix="legacy-work-")
legacy_dir = os.path.join(work, ".harness", "sessions")
os.makedirs(legacy_dir)
legacy_journal = os.path.join(legacy_dir, "sess_legacy.jsonl")
journal = legacy_journal
open(journal, "w").write('{"t": "session"}\n')
script = ("import sys; sys.path.insert(0, %r); import agent; print(agent.session_file('sess_legacy', %r))"
          % (os.path.dirname(AGENT), work))
p = subprocess.run([sys.executable, "-B", "-c", script], capture_output=True, text=True)
# compare resolved paths: on macOS /var is a symlink to /private/var and the function resolves
check("legacy: an old journal inside the workspace is still read",
      p.returncode == 0 and os.path.realpath(p.stdout.strip()) == os.path.realpath(journal),
      (p.stdout or p.stderr)[-200:])

# 4. A new session id never falls back: the fallback is for existing files only, or a fresh
# session would be written into the repository the moment a stale directory existed.
fresh = "sess_" + "0" * 32
script = ("import sys; sys.path.insert(0, %r); import agent; print(agent.session_file(%r, %r))"
          % (os.path.dirname(AGENT), fresh, work))
p = subprocess.run([sys.executable, "-B", "-c", script], capture_output=True, text=True)
out = (p.stdout or "").strip()
check("legacy: a new session is never placed in the workspace",
      p.returncode == 0 and not os.path.realpath(out).startswith(os.path.realpath(work)), out[-200:])

done("SESSIONS")
