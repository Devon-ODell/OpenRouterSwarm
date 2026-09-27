"""The waiting that happens before and around a turn, rather than in it.

Every attempt ran the whole suite before the model started, and the studio's suite takes 105
seconds — 177 baseline runs, most of them proving the same unchanged trunk passes. The
implementer was then told to run that same suite itself, inside a bash tool that cut off at
120s. And 21 turns paused part-way through on the budget, throwing away what they had done.
"""
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import flint
from swarm import swarmd
from swarm.budget import Budget


class BaselineCacheTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.root = Path(d.name)
        p = patch.object(swarmd, "STATE", self.root)
        p.start()
        self.addCleanup(p.stop)
        self.c = {"test_cmd": "python3 -m unittest discover -s tests",
                  "validation_commands": ["node --test"]}

    def test_the_key_is_the_commit_and_the_commands(self):
        a = swarmd.baseline_key(self.c, "abc123")
        self.assertEqual(a, swarmd.baseline_key(dict(self.c), "abc123"))
        self.assertNotEqual(a, swarmd.baseline_key(self.c, "def456"))
        self.assertNotEqual(a, swarmd.baseline_key(dict(self.c, test_cmd="pytest -q"), "abc123"))
        self.assertNotEqual(a, swarmd.baseline_key(dict(self.c, validation_commands=[]), "abc123"))

    def test_a_pass_is_remembered_and_a_failure_is_not_reused(self):
        key = swarmd.baseline_key(self.c, "abc123")
        self.assertIsNone(swarmd.baseline_cache(key))
        swarmd.baseline_cache(key, passed=True)
        self.assertIsNotNone(swarmd.baseline_cache(key))
        swarmd.baseline_cache(key, passed=False)
        self.assertIsNone(swarmd.baseline_cache(key))

    def test_a_different_commit_is_not_a_hit(self):
        swarmd.baseline_cache(swarmd.baseline_key(self.c, "abc123"), passed=True)
        self.assertIsNone(swarmd.baseline_cache(swarmd.baseline_key(self.c, "def456")))

    def test_a_pass_goes_stale(self):
        key = swarmd.baseline_key(self.c, "abc123")
        swarmd.baseline_cache(key, passed=True)
        self.assertIsNotNone(swarmd.baseline_cache(key, ttl=86_400))
        self.assertIsNone(swarmd.baseline_cache(key, ttl=0))
        row = json.loads((self.root / "baseline.json").read_text())
        row["at"] = time.time() - 90_000
        (self.root / "baseline.json").write_text(json.dumps(row))
        self.assertIsNone(swarmd.baseline_cache(key))

    def test_a_missing_or_corrupt_record_just_means_run_the_gate(self):
        self.assertIsNone(swarmd.baseline_cache("nothing-here"))
        (self.root / "baseline.json").write_text("{not json")
        self.assertIsNone(swarmd.baseline_cache("nothing-here"))


class BaselineGateTests(unittest.TestCase):
    """The gate itself, skipped on a recent pass and run again when anything changed."""

    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.root = Path(d.name)
        for name in ("state", "logs"):
            (self.root / name).mkdir()
        for attr, value in (("STATE", self.root / "state"), ("LOGS", self.root / "logs")):
            p = patch.object(swarmd, attr, value)
            p.start()
            self.addCleanup(p.stop)
        self.repo = self.make_repo()

    def make_repo(self):
        import subprocess
        repo = self.root / "repo"
        repo.mkdir()

        def git(*a):
            subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)
        git("init", "-b", "main")
        git("config", "user.email", "t@example.invalid")
        git("config", "user.name", "T")
        (repo / "app.txt").write_text("baseline\n")
        git("add", "-A")
        git("commit", "-m", "baseline")
        return repo

    def run_attempt(self, gates):
        c = {"repo": str(self.repo), "base_branch": "main", "test_cmd": "true",
             "models": ["a:free"], "steps": {"architect": 0, "implementer": 2, "adversary": 2}}
        w = swarmd.Worker(0, c, swarmd.Queue(), Mock(), threading.Event())

        def fake(prompt, cwd, c_, role, *a):
            (cwd / "app.txt").write_text("changed\n")
            return "done"
        with patch.object(swarmd, "flint", side_effect=fake), \
             patch.object(w, "gate", side_effect=gates) as gate:
            w.do_task({"id": f"t{len(gates)}", "title": "x", "detail": "y"}, "goal")
        return [call.args[0] for call in gate.call_args_list]

    def test_the_first_attempt_runs_the_baseline_and_the_next_does_not(self):
        first = self.run_attempt([(True, "")] * 4)
        self.assertIn("baseline", first)
        second = self.run_attempt([(True, "")] * 4)
        self.assertNotIn("baseline", second, "the same trunk was gated twice")

    def test_the_skip_is_recorded_as_evidence(self):
        self.run_attempt([(True, "")] * 4)
        self.run_attempt([(True, "")] * 4)
        rows = [json.loads(p.read_text()) for p in
                (swarmd.STATE / "attempts").glob("*/attempt.json")]
        phases = [e["phase"] for r in rows for e in r["events"]]
        self.assertIn("baseline_cached", phases)

    def test_a_failed_baseline_is_not_cached(self):
        self.run_attempt([(False, "boom")])
        self.assertIn("baseline", self.run_attempt([(False, "boom")]))

    def test_a_changed_test_command_gates_again(self):
        self.run_attempt([(True, "")] * 4)
        c = {"repo": str(self.repo), "base_branch": "main", "test_cmd": "pytest -q",
             "models": ["a:free"], "steps": {"architect": 0, "implementer": 2, "adversary": 2}}
        w = swarmd.Worker(0, c, swarmd.Queue(), Mock(), threading.Event())
        with patch.object(swarmd, "flint", return_value="done"), \
             patch.object(w, "gate", side_effect=[(True, "")] * 4) as gate:
            w.do_task({"id": "t9", "title": "x", "detail": "y"}, "goal")
        self.assertIn("baseline", [call.args[0] for call in gate.call_args_list])


class BashTimeoutTests(unittest.TestCase):
    """flint's bash tool cut off at 120s, 15 seconds short of the studio's own suite."""

    def test_the_default_comes_from_the_environment(self):
        self.assertEqual(flint.BASH_TIMEOUT, int(__import__("os").environ.get(
            "FLINT_BASH_TIMEOUT", "120")))

    def test_the_schema_tells_the_model_the_real_default(self):
        schema = next(t["function"] for t in flint.TOOL_SCHEMAS if t["function"]["name"] == "bash")
        self.assertIn(str(flint.BASH_TIMEOUT),
                      schema["parameters"]["properties"]["timeout"]["description"])

    def test_an_explicit_timeout_still_wins(self):
        self.assertIn("timed out after 1s", flint.bash("sleep 5", timeout=1))

    def test_the_swarm_hands_down_its_test_timeout(self):
        env = self.turn_env({"test_timeout": 600})
        self.assertEqual(env["FLINT_BASH_TIMEOUT"], "600")

    def test_the_default_test_timeout_is_handed_down_too(self):
        self.assertEqual(self.turn_env({})["FLINT_BASH_TIMEOUT"], "900")

    def turn_env(self, extra):
        """The environment swarmd builds for one flint turn."""
        seen = {}

        class FakeProc:
            returncode = 0

            def communicate(self, timeout=None):
                return "answer", ""
        import contextlib

        @contextlib.contextmanager
        def process(cmd, **kw):
            seen.update(kw.get("env") or {})
            yield FakeProc()
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        root = Path(d.name)
        (root / "logs").mkdir()
        c = {"repo": str(root), "test_cmd": "true", "models": ["a:free"], **extra}
        budget = Budget(cap=100, reserve=0, owner_window=["00:00", "00:00"])
        with patch.object(swarmd, "process", process), \
             patch.object(swarmd, "LOGS", root / "logs"), \
             patch.object(swarmd, "STATE", root), \
             patch.object(swarmd, "sandboxed", side_effect=lambda cmd, *a: cmd):
            swarmd.flint("prompt", root, c, "implementer", "w0", budget, 12, "a:free")
        return seen


class WholeTurnBudgetTests(unittest.TestCase):
    """A turn that can start but not finish used to pause part-way and lose everything."""

    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.root = Path(d.name)
        (self.root / "logs").mkdir()
        for attr, value in (("STATE", self.root), ("LOGS", self.root / "logs")):
            p = patch.object(swarmd, attr, value)
            p.start()
            self.addCleanup(p.stop)
        swarmd._ran_on.clear()

    def run_turn(self, fits_whole_turn, paid=("paid/x",), steps=12):
        c = {"repo": str(self.root), "test_cmd": "true", "models": ["a:free"],
             "paid_models": list(paid), "allow_paid": True, "monthly_usd": 20}
        budget = Mock(cap=50, reserve=10)   # real numbers: swarmd serialises these into the env
        # Allowed to start; whether the whole turn fits is what is under test.
        budget.check.side_effect = lambda need=1, state=None: (
            (True, 0, "ok") if need == 1 or fits_whole_turn else (False, 60, "not enough left"))
        budget.paid_would_help.return_value = False

        class FakeProc:
            returncode = 0

            def communicate(self, timeout=None):
                return "answer", ""
        import contextlib

        @contextlib.contextmanager
        def process(cmd, **kw):
            yield FakeProc()
        with patch.object(swarmd, "process", process), \
             patch.object(swarmd, "sandboxed", side_effect=lambda cmd, *a: cmd), \
             patch.object(swarmd, "spend_left", return_value=5.0), \
             patch.object(swarmd, "spend_used", return_value=1.0):
            swarmd.flint("prompt", self.root, c, "implementer", "w0", budget, steps, "a:free")
        return swarmd._ran_on.get("w0")

    def test_a_turn_that_fits_runs_free(self):
        self.assertIsNone(self.run_turn(fits_whole_turn=True))

    def test_a_turn_that_cannot_finish_goes_paid_before_it_starts(self):
        self.assertEqual(self.run_turn(fits_whole_turn=False), "paid/x")

    def test_the_reason_says_it_was_the_whole_turn_that_did_not_fit(self):
        self.run_turn(fits_whole_turn=False)
        rows = [json.loads(l) for l in
                (self.root / "journal.jsonl").read_text().splitlines() if l.strip()]
        paid = [r for r in rows if r["event"] == "paid_fallback"]
        self.assertEqual(len(paid), 1)
        self.assertIn("12-round turn", paid[0]["reason"])

    def test_with_no_paid_model_it_still_runs_rather_than_idling(self):
        """The free allowance only resets at midnight; a short turn may still finish."""
        self.assertIsNone(self.run_turn(fits_whole_turn=False, paid=()))


if __name__ == "__main__":
    unittest.main()
