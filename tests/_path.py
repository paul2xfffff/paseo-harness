"""Every test imports this first.

It puts the repo root on sys.path and, critically, points the agent at a config that does not
exist. Without that, a developer's ~/.harness/config.json leaks into the tests: real models,
real MCP servers, real network -- and a suite that passes or hangs depending on whose laptop
it runs on.
"""
import os, sys, tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# an empty config, a placeholder key and a scratch session dir: tests never touch the developer's
# ~/.harness, real models or real network
os.environ["AGENT_CONFIG"] = os.path.join(ROOT, "tests", "no-such-config.json"); os.environ.setdefault("OPENAI_API_KEY", "test")
os.environ.setdefault("AGENT_SESSIONS", os.path.join(tempfile.gettempdir(), "harness-test-sessions"))
