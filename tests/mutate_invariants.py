#!/usr/bin/env python3
"""Mutation check for the landing invariants: delete each guard, the suite must fail.

DECOMPOSE-SWARMD.md: "Each must keep a test that fails if its guard is removed." A green suite
does not show that: on 2026-10-11 five of these guards could be deleted with every test still
passing. This script deletes each guard in turn, in a throwaway worktree of HEAD, runs the
suite, and reports any mutant that survives. tests/test_invariant_anchors.py keeps every anchor
below matching the source, so a refactor that rewords a guard fails the normal suite instead of
quietly retiring its mutant.

    .venv/bin/python tests/mutate_invariants.py            # all mutants, ~10-15 minutes
    .venv/bin/python tests/mutate_invariants.py 4a 4b      # only mutants whose id starts so

Exit status: 0 when every mutant is killed, 1 when any survives or an anchor is missing.
Runs against committed HEAD, never the working tree, and never touches the checkout itself.
"""
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# (id, invariant, file, exact text of the guard, the guard deleted)
MUTANTS = [
    ("1", "tests pass before review is requested", "swarm/attempt_runner.py",
     "            review = None\n            if ok:\n                review, adv, exc = w.reviewed(",
     "            review = None\n            if True:\n                review, adv, exc = w.reviewed("),
    ("2", "review sees the exact candidate: tests may not change it", "swarm/attempt_runner.py",
     "            if after != tree:\n                return \"rejected\", \"test command modified",
     "            if False:\n                return \"rejected\", \"test command modified"),
    ("3", "the committed tree equals the reviewed tree", "swarm/workspace.py",
     "        if committed_tree != reviewed_tree:",
     "        if False:"),
    ("4a", "a rebase triggers new verification", "swarm/attempt_runner.py",
     "                ok, output = w.gate(\"rebased\", c)",
     "                ok, output = True, \"\""),
    ("4b", "a rebase triggers a new review", "swarm/attempt_runner.py",
     "                if review[\"verdict\"] != \"approve\":\n                    return False, \"rebased candidate needs changes",
     "                if False:\n                    return False, \"rebased candidate needs changes"),
    ("5a", "existing tests cannot be weakened", "swarm/attempt_runner.py",
     "            lost = weakened_tests(diff, allow_test_changes=allowed)",
     "            lost = []"),
    ("5b", "only a human may authorize weakening tests", "swarm/task_queue.py",
     "                if task.get(\"origin\") not in TEST_CHANGE_ORIGINS:",
     "                if False:"),
    ("7", "a harness failure costs the task no attempt", "swarm/task_queue.py",
     "(0 if defer or harness or parked else 1)",
     "(0 if defer or parked else 1)"),
    ("8", "a requested stop is never converted into a restart", "swarm/supervisor.py",
     "                if restart_for and d.drain_reason[\"stop_requested\"]:",
     "                if False:"),
    ("9", "the daemon never restarts into code that does not compile", "swarm/swarmd.py",
     "    ok, why = source_compiles()\n    if not ok:",
     "    ok, why = source_compiles()\n    if False:"),
    ("10a", "queue writes hold the fcntl lock", "swarm/task_queue.py",
     "            fcntl.flock(lock, fcntl.LOCK_EX)",
     "            pass"),
    ("10b", "queue writes replace the file atomically", "swarm/task_queue.py",
     "        tmp.replace(path)",
     "        path.write_text(tmp.read_text())"),
    ("rc11", "flint exit 11 is ExplorationExhausted", "swarm/agent_turn.py",
     "        if p.returncode == 11:",
     "        if p.returncode == 111:"),
    ("rc9", "flint exit 9 is ModelGone", "swarm/agent_turn.py",
     "        if p.returncode == 9:",
     "        if p.returncode == 99:"),
]


def anchor_counts(root=ROOT):
    """{mutant id: how many times its guard text occurs in its file}. Each must be exactly 1."""
    return {mid: (root / rel).read_text().count(old) for mid, _, rel, old, _ in MUTANTS}


def run(selected=(), python=sys.executable):
    chosen = [m for m in MUTANTS if not selected or any(m[0].startswith(s) for s in selected)]
    with tempfile.TemporaryDirectory(prefix="mutate-invariants-") as td:
        wt = Path(td) / "wt"
        subprocess.run(["git", "-C", str(ROOT), "worktree", "add", "-q", "--detach", str(wt), "HEAD"],
                       check=True)
        try:
            return _run_in(wt, chosen, python)
        finally:
            subprocess.run(["git", "-C", str(ROOT), "worktree", "remove", "--force", str(wt)],
                           capture_output=True)


def _run_in(wt, chosen, python):
    rows = []
    for mid, what, rel, old, new in chosen:
        path = wt / rel
        src = path.read_text()
        if src.count(old) != 1:
            rows.append((mid, what, "ANCHOR MISSING", f"{rel}: guard text found {src.count(old)}x"))
            continue
        path.write_text(src.replace(old, new, 1))
        t0 = time.time()
        try:
            r = subprocess.run([python, "-m", "pytest", "tests/", "-x", "-q", "-p", "no:cacheprovider"],
                               cwd=wt, capture_output=True, text=True, timeout=1200)
            lines = r.stdout.strip().splitlines()
            failed = [l for l in lines if l.startswith(("FAILED", "ERROR"))]
            verdict = "killed" if r.returncode != 0 else "SURVIVED"
            detail = (failed[0] if failed else lines[-1] if lines else "")[:150]
        except subprocess.TimeoutExpired:
            verdict, detail = "TIMEOUT", "suite exceeded 20 minutes"
        finally:
            path.write_text(src)
        rows.append((mid, what, verdict, f"{detail} ({time.time() - t0:.0f}s)"))
        print(f"{mid:5} {verdict:14} {what}", flush=True)
    return rows


def main(argv):
    rows = run(argv)
    bad = [r for r in rows if r[2] != "killed"]
    print("\n" + "\n".join(f"{mid:5} {verdict:14} {what}\n      {detail}"
                           for mid, what, verdict, detail in rows))
    print(f"\n{len(rows) - len(bad)}/{len(rows)} mutants killed")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
