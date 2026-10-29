#!/usr/bin/env python3
"""Run every test in this directory. No framework: each file is a script that asserts
and prints. A non-zero exit is a failure.

    python tests/run.py            # all
    python tests/run.py tools loop # just those
"""
import os, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable


def discover():
    return sorted(f[:-3] for f in os.listdir(HERE)
                  if f.startswith("test_") and f.endswith(".py"))


def main(argv):
    wanted = argv[1:]
    names = [n for n in discover() if not wanted or any(w in n for w in wanted)]
    if not names:
        sys.exit("no tests matched %r" % (wanted,))
    failed = []
    for name in names:
        path = os.path.join(HERE, name + ".py")
        r = subprocess.run([PY, "-B", "-u", path], capture_output=True, text=True, timeout=600)
        ok = r.returncode == 0
        print("%-4s %s" % ("ok" if ok else "FAIL", name))
        if not ok:
            failed.append(name)
            tail = (r.stdout + r.stderr).strip().splitlines()[-15:]
            for line in tail:
                print("     " + line)
    print("\n%d passed, %d failed" % (len(names) - len(failed), len(failed)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
