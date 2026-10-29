"""The claims the README makes about the repository, checked instead of trusted.

Everything here is about the *project*, not the agent: it fails when a fact stated in
README.md or guaranteed by .gitignore stops being true. These are the invariants that no
behavioural test can see, and the ones most likely to rot silently -- a stray blank line, a
secret committed by accident, a fourth dependency nobody meant to add.
"""
import os, re, subprocess, sys

import _path  # noqa: F401 - also puts the repo root on sys.path
from _assert import check, done

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGENT = os.path.join(ROOT, "agent.py")

source = open(AGENT, encoding="utf-8").read()
lines = source.splitlines()

# 1. One file, no blank lines. The style is deliberate: it keeps a 2000-line file scannable,
# and a single stray blank line is the first symptom of someone pasting code in from elsewhere.
check("agent.py: exists and is readable", len(source) > 1000, "%d bytes" % len(source))
blanks = [i + 1 for i, line in enumerate(lines) if line.strip() == ""]
check("agent.py: zero blank lines", not blanks, "blank at lines %s" % blanks[:10])
check("agent.py: no trailing whitespace", not re.search(r"[ \t]+$", source, re.M),
      repr(re.search(r".*[ \t]+$", source, re.M).group(0) if re.search(r"[ \t]+$", source, re.M) else ""))
check("agent.py: ends with exactly one newline", source.endswith("\n") and not source.endswith("\n\n"),
      repr(source[-3:]))
check("agent.py: no tabs", "\t" not in source, "a tab appeared")
check("agent.py: no debug leftovers", not re.search(r"\b(breakpoint\(\)|pdb\.set_trace|print\(\"DEBUG)", source),
      "a debugger left behind")

# 2. The dependency list in requirements.txt is the real one. `mcp` is a hard import: without
# it the module does not load at all, so a missing pin is a broken install.
require = open(os.path.join(ROOT, "requirements.txt"), encoding="utf-8").read()
pinned = {m.group(1).lower() for m in re.finditer(r"^([A-Za-z0-9_.-]+)", require, re.M) if not m.group(1).startswith("#")}
imported = set()
for match in re.finditer(r"^(?:import|from)\s+([A-Za-z0-9_]+)", source, re.M):
    imported.add(match.group(1).lower())
stdlib = set(getattr(sys, "stdlib_module_names", ()))
third_party = {name for name in imported if name not in stdlib and name not in ("agent",)}
check("requirements: every third-party import is pinned", third_party <= pinned,
      "unpinned: %s" % sorted(third_party - pinned))
check("requirements: mcp is pinned", "mcp" in pinned, repr(sorted(pinned)))
check("requirements: anyio is pinned", "anyio" in pinned, repr(sorted(pinned)))
check("requirements: requests is pinned", "requests" in pinned, repr(sorted(pinned)))
check("requirements: exactly the three that are used", len(pinned) == 3,
      "pinned=%s imported=%s" % (sorted(pinned), sorted(third_party)))

# 3. Secrets stay out of the repository. This is the boundary .gitignore is supposed to hold:
# session journals contain whole conversations, and those contain API keys.
ignore = open(os.path.join(ROOT, ".gitignore"), encoding="utf-8").read()
for pattern in (".*/", "config.json"):
    check(".gitignore: covers %s" % pattern, pattern in ignore, "missing from .gitignore")
key_files = []
for base, dirs, files in os.walk(ROOT):
    if any(part in base for part in (".venv", "__pycache__", ".git")):
        continue
    for name in files:
        if not name.endswith((".json", ".jsonl", ".yaml", ".yml", ".txt", ".md", ".py")):
            continue
        path = os.path.join(base, name)
        rel = os.path.relpath(path, ROOT)
        if rel.startswith(".harness/sessions") or rel.startswith(".harness/jobs"):
            continue  # deliberately ignored, verified above
        try:
            text = open(path, encoding="utf-8", errors="ignore").read()
        except OSError:
            continue
        if re.search(r"gpustack_[A-Za-z0-9]{8,}|tvly-[a-z0-9]{8,}|sk-[A-Za-z0-9]{20,}", text):
            key_files.append(rel)
check("no secrets outside the ignored session directories", not key_files, "found in %s" % key_files[:5])

# 4. The README's own numbers. It claims one file, a command, and a tool count; each of those
# is checkable, and a README that drifts is worse than no README.
readme = open(os.path.join(ROOT, "README.md"), encoding="utf-8").read()
check("README: describes a single file", "one file" in readme.lower(), "the claim was removed")
check("README: shows the acp command", "agent.py acp" in readme, "the run command drifted")
# The stated test count is derived from the directory, so it cannot go stale on its own.
test_files = sorted(n for n in os.listdir(os.path.join(ROOT, "tests"))
                    if n.startswith("test_") and n.endswith(".py"))
stated = re.search(r"#\s*(\d+)\s*tests", readme)
check("README: the stated test count is real",
      stated is not None and int(stated.group(1)) == len(test_files),
      "README says %s, there are %d" % (stated.group(1) if stated else "nothing", len(test_files)))
for tool in ("read", "bash", "edit", "write", "subagent"):
    check("README: documents the %s tool" % tool, "`%s`" % tool in readme or " %s " % tool in readme,
          "tool missing from the README")
check("README: names a licence", "MIT" in open(os.path.join(ROOT, "LICENSE"), encoding="utf-8").read(),
      "LICENSE is not MIT")
check("README: has no unresolved placeholders", not re.search(r"TODO|FIXME|<your-|example\.com|<repo>", readme),
      "placeholder text shipped")
# Every key the settings table advertises must be one env() actually reads: a documented knob
# that no code path consults is worse than documenting nothing.
section = readme.split("## Configure", 1)[-1].split("## Skills", 1)[0]
env_keys = {m.group(1).lower() for m in re.finditer(r'env\("AGENT_([A-Z0-9_]+)"', source)}
table_keys = {m.group(1) for m in re.finditer(r"^\| `([a-z_]+)` \| `", section, re.M)}
check("README: settings table names keys the code reads", table_keys <= env_keys,
      "not read by the code: %s" % sorted(table_keys - env_keys))

# 5. Every module-level name the tests reach for is a public one. A test that pokes a private
# helper pins an implementation detail, which is how a harmless rename becomes a test rewrite.
private_reach = set()
for name in sorted(os.listdir(os.path.join(ROOT, "tests"))):
    if not name.startswith("test_") or not name.endswith(".py"):
        continue
    text = open(os.path.join(ROOT, "tests", name), encoding="utf-8").read()
    for match in re.finditer(r"agent\.(_[a-zA-Z0-9]+)", text):
        private_reach.add(match.group(1))
# a handful are legitimately white-box (the journal internals); pin the list so it cannot grow
allowed_private = {"_path", "_assert", "_mtime", "_shrink", "_event", "_fp", "_lock", "_ts", "_settle"}
unexpected = private_reach - allowed_private
check("tests: reach into few private names", not unexpected, "new private coupling: %s" % sorted(unexpected))

# 6. The suite does not depend on the developer's machine. `_path` is the one place that
# guarantees it, and every test and driver must import it or the guarantee does not hold.
tests_dir = os.path.join(ROOT, "tests")
missing_isolation = []
for sub in ("", "drivers"):
    directory = os.path.join(tests_dir, sub)
    for name in sorted(os.listdir(directory)):
        if not name.endswith(".py") or name in ("_path.py", "_assert.py", "run.py"):
            continue
        if not (name.startswith("test_") or name.startswith("driver_")):
            continue
        text = open(os.path.join(directory, name), encoding="utf-8").read()
        # Only a file that imports the agent needs isolation itself. A driver-driven test that
        # never imports it is isolated by the driver, and demanding a decorative import would
        # be ceremony -- but a file that imports the agent without _path reads the developer's
        # config, and that is the failure this catches.
        imports_agent = re.search(r"^\s*import agent\b", text, re.M) is not None
        if imports_agent and "import _path" not in text and "from _path import" not in text:
            missing_isolation.append(os.path.join(sub, name) if sub else name)
check("every test and driver isolates its config", not missing_isolation,
      "no _path import: %s" % missing_isolation)

# 7. run.py discovers by filename, so a test that is not named test_*.py never runs. That is a
# silent hole: the file looks like a test and is dead weight.
run_src = open(os.path.join(tests_dir, "run.py"), encoding="utf-8").read()
check("run.py: discovers by the test_ prefix", 'startswith("test_")' in run_src, "discovery rule changed")
orphans = [n for n in sorted(os.listdir(tests_dir))
           if n.endswith(".py") and not n.startswith(("test_", "_"))
           and n not in ("run.py", "conftest.py")]
check("run.py: no test file is left undiscovered", not orphans, "never runs: %s" % orphans)

# 8. No dead code. A top-level name that is defined and never used again is either a leftover
# from a refactor or a helper nobody wired up; both are noise in a file this size. The check is
# deliberately syntactic: it cannot see a name used only by an ACP dispatch table, so the known
# protocol entry points are listed rather than guessed.
import ast

tree = ast.parse(source)
redefined = {}
for node in tree.body:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        redefined[node.name] = node.lineno
    elif isinstance(node, ast.Assign):
        for target in node.targets:
            if isinstance(target, ast.Name):
                redefined[target.id] = node.lineno
# ACP dispatches these by name from a message, so nothing in this file references them twice
PROTOCOL_ENTRY = {"initialize", "authenticate"}
dead = sorted((line, name) for name, line in redefined.items()
              if len(re.findall(r"\b%s\b" % re.escape(name), source)) <= 1 and name not in PROTOCOL_ENTRY)
check("agent.py: no dead top-level definitions", not dead,
      "defined and never used: %s" % ["%s:%d" % (n, l) for l, n in dead])

# 9. No unused function parameters, except the protocol methods whose signature is fixed.
unused_params = []
for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
    names = [a.arg for a in fn.args.args + fn.args.kwonlyargs if a.arg not in ("self", "cls")]
    if not names or fn.name in PROTOCOL_ENTRY:
        continue
    body = ast.dump(ast.Module(body=fn.body, type_ignores=[]))
    for name in names:
        if ("id=%r" % name) not in body and ("attr=%r" % name) not in body:
            unused_params.append("%s(%s) line %d" % (fn.name, name, fn.lineno))
check("agent.py: no unused parameters", not unused_params, "; ".join(unused_params[:6]))

done("PROJECT")
