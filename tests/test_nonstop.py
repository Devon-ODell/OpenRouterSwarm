"""Timed-loop tests with simulated workers; never spend real model quota."""
import contextlib
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import flint
import nonstop


class Commands(unittest.TestCase):
    def test_explicit_triggers_only(self):
        self.assertEqual(nonstop.parse_trigger("run this nonstop"), (None, ""))
        self.assertEqual(nonstop.parse_trigger("run this nonstop for 10 hours: fix physics"), (10, "fix physics"))
        self.assertEqual(nonstop.parse_trigger("/nonstop for 8h: fix tests"), (8, "fix tests"))
        self.assertIsNone(nonstop.parse_trigger('Explain the phrase "run this nonstop"'))
        self.assertIsNone(nonstop.parse_trigger("do not run this nonstop"))

    def test_invalid_durations(self):
        for hours in (0, -1, float("nan"), float("inf")):
            with self.subTest(hours=hours), self.assertRaises(ValueError):
                nonstop.validate_hours(hours)

    def test_goal_fallbacks(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(ValueError):
                nonstop.resolve_goal("", td)
            Path(td, "GOAL.md").write_text("Goal from file")
            self.assertEqual(nonstop.resolve_goal("", td), "Goal from file")
            self.assertEqual(nonstop.resolve_goal("", td, "Previous request"), "Previous request")
            self.assertEqual(nonstop.resolve_goal("Explicit", td, "Previous"), "Explicit")

    def test_cli_enables_nonstop_without_yolo(self):
        with patch.object(sys, "argv", ["flint", "--nonstop", "--hours", "10", "-p", "Improve physics"]), patch.object(flint, "run_unattended", return_value=0) as run:
            with self.assertRaises(SystemExit) as exc:
                flint.main()
        self.assertEqual(exc.exception.code, 0)
        run.assert_called_once_with("Improve physics", flint.DEFAULT_MODEL, 10, None)

    def test_prompt_alias_and_default_hours(self):
        with patch.object(sys, "argv", ["flint", "-p", "run this nonstop: Fix collisions"]), patch.object(flint, "run_unattended", return_value=0) as run:
            with self.assertRaises(SystemExit):
                flint.main()
        run.assert_called_once_with("Fix collisions", flint.DEFAULT_MODEL, 8, None)

    def test_read_only_cannot_be_overridden_by_trigger(self):
        with patch.object(sys, "argv", ["flint", "--read-only", "--nonstop", "-p", "task"]), contextlib.redirect_stderr(io.StringIO()), patch.object(flint, "run_unattended") as run:
            with self.assertRaises(SystemExit) as exc:
                flint.main()
        self.assertEqual(exc.exception.code, 2)
        run.assert_not_called()

    def test_interactive_trigger_uses_previous_request(self):
        from types import SimpleNamespace
        agent = SimpleNamespace(read_only=False, model="test", messages=[{"role": "user", "content": "Fix collisions"}])
        agent.reset = lambda: None
        with patch.object(flint, "banner"), patch.object(flint.console, "input", side_effect=["run this nonstop for 10 hours", EOFError]), patch.object(flint, "run_unattended", return_value=0) as run, patch.object(flint.console, "print"):
            flint.repl(agent)
        run.assert_called_once_with("Fix collisions", "test", 10)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class Loop(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.clock = Clock()
        for target, value in (("monotonic", self.clock.monotonic), ("sleep", self.clock.sleep)):
            p = patch.object(nonstop.time, target, value)
            p.start()
            self.addCleanup(p.stop)

    def run_loop(self, worker, seconds=40, test_command=None):
        with patch.object(nonstop, "run_process", side_effect=worker), contextlib.redirect_stderr(io.StringIO()):
            rc = nonstop.run_nonstop("Fix the physics", self.root, "test:free", self.root,
                                     hours=seconds / 3600, test_command=test_command)
        status_path = next((self.root / "nonstop").glob("*/status.json"))
        return rc, json.loads(status_path.read_text())

    def test_round_limit_continues_with_evidence_and_verified_failure(self):
        prompts = []
        def worker(cmd, cwd, out, err, timeout, prompt=None):
            self.clock.now += 1
            if prompt is None:
                Path(out).write_text("collision test failed")
                return 1
            prompts.append(prompt)
            self.assertIn("--yolo", cmd)
            self.assertIn("-", cmd)
            if len(prompts) == 1:
                Path(cmd[-1]).write_text('recent evidence: updated collision.py')
                return 5
            Path(out).write_text("Corrected collision boundaries")
            return 0
        rc, status = self.run_loop(worker, test_command="run-tests")
        self.assertEqual(rc, 0)
        self.assertEqual(status["status"], "time_limit")
        self.assertGreaterEqual(len(prompts), 2)
        self.assertIn("updated collision.py", prompts[1])
        self.assertIn("collision test failed", prompts[1])
        self.assertIn("Fix the physics", prompts[1])
        self.assertEqual(status["test_exit"], 1)

    def test_daily_cap_wait_is_bounded_by_deadline(self):
        (self.root / "requests.json").write_text(json.dumps({"blocked_until": time.time() + 3600}))
        calls = []
        def worker(*args, **kw):
            calls.append(1)
            return 3
        rc, status = self.run_loop(worker)
        self.assertEqual(rc, 0)
        self.assertEqual(calls, [1])
        self.assertEqual(self.clock.now, 1040)
        self.assertEqual(status["status"], "time_limit")

    def test_transient_provider_recovers(self):
        calls = []
        def worker(cmd, cwd, out, err, timeout, prompt=None):
            calls.append(prompt)
            if len(calls) == 1:
                return 7
            Path(out).write_text("Verified small fix")
            return 0
        rc, status = self.run_loop(worker)
        self.assertEqual(rc, 0)
        self.assertEqual(len(calls), 2)
        self.assertEqual(status["worker_exit"], 0)

    def test_empty_failure_checkpoint_keeps_last_useful_progress(self):
        prompts = []
        def worker(cmd, cwd, out, err, timeout, prompt=None):
            prompts.append(prompt)
            if len(prompts) == 1:
                Path(out).write_text("Fixed collision bug; next add edge case test.")
                return 0
            if len(prompts) == 2:
                Path(cmd[-1]).write_text("[]")
                return 7
            return 4
        rc, _ = self.run_loop(worker, seconds=120)
        self.assertEqual(rc, 4)
        self.assertIn("Fixed collision bug", prompts[2])

    def test_three_unknown_errors_stop(self):
        rc, status = self.run_loop(lambda *args, **kw: 1, seconds=600)
        self.assertEqual(rc, 1)
        self.assertEqual(status["status"], "failed")
        self.assertEqual(status["cycle"], 3)

    def test_repeated_summaries_pause_instead_of_spinning(self):
        def worker(cmd, cwd, out, err, timeout, prompt=None):
            Path(out).write_text("No remaining work found")
            return 0
        rc, status = self.run_loop(worker, seconds=120)
        self.assertEqual(rc, 0)
        self.assertEqual(status["cycle"], 3)

    def test_credit_failure_stops_instead_of_retrying(self):
        rc, status = self.run_loop(lambda *a, **k: 4)
        self.assertEqual(rc, 4)
        self.assertEqual(status["status"], "blocked")
        self.assertEqual(status["cycle"], 1)

    def test_interrupt_is_recorded(self):
        def worker(*args, **kw):
            raise KeyboardInterrupt
        rc, status = self.run_loop(worker)
        self.assertEqual(rc, 130)
        self.assertEqual(status["status"], "interrupted")

    def test_duplicate_run_cannot_edit_same_checkout(self):
        def worker(*args, **kw):
            with self.assertRaisesRegex(ValueError, "already owns"):
                nonstop.run_nonstop("Other goal", self.root, "test", self.root, hours=1)
            return 4
        self.run_loop(worker)


class Processes(unittest.TestCase):
    def test_deadline_stops_real_worker_and_child(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            script = root / "worker.py"
            script.write_text('''import subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "from pathlib import Path; import time\\nwhile True:\\n Path('heartbeat').write_text(str(time.time_ns())); time.sleep(0.02)"])
time.sleep(30)
''')
            start = time.monotonic()
            rc = nonstop.run_process([sys.executable, str(script)], root, root / "out", root / "err", 0.5, prompt="test")
            self.assertEqual(rc, 124)
            self.assertLess(time.monotonic() - start, 3)
            heartbeat = (root / "heartbeat").read_text()
            time.sleep(0.1)
            self.assertEqual((root / "heartbeat").read_text(), heartbeat)

    def test_checkpoint_is_bounded_and_excludes_original_prompt(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "checkpoint.json"
            messages = [{"role": "user", "content": "original goal"}] + [
                {"role": "tool", "content": "x" * 20000} for _ in range(30)]
            nonstop.save_checkpoint(path, messages)
            rows = json.loads(path.read_text())
            self.assertLessEqual(len(rows), 8)
            self.assertTrue(all(len(r["content"]) <= 1600 for r in rows))


if __name__ == "__main__":
    unittest.main()
