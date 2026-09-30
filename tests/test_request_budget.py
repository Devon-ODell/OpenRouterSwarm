"""Work order §1 + §12: requests are a first-class, enforceable budget with exact
accounting that survives a SIGKILLed turn, and provider/local numbers reconcile
explicitly rather than being silently attributed.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from swarm import budget_requests, metrics


class RequestSidecarTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.path = Path(d.name) / "requests.jsonl"
        self.log = metrics.RequestLog(self.path)

    def test_every_attempt_is_a_row_including_retries(self):
        self.log.append(model="m:free", role="implementer", attempt=1)
        self.log.append(model="m:free", role="implementer", attempt=2, retry_of=1,
                        error_class="APIError")
        self.log.append(model="m:free", role="repair", attempt=1, retry_of=None)
        rows, total = self.log.read()
        self.assertEqual(total, 3)
        self.assertEqual([r["attempt"] for r in rows], [1, 2, 1])
        self.assertEqual(rows[1]["retry_of"], 1)
        self.assertEqual(rows[1]["error_class"], "APIError")

    def test_corrupt_lines_are_skipped_not_fatal(self):
        self.path.write_text('{"t": 1}\nnot json\n{"t": 2}\n')
        rows, total = self.log.read()
        self.assertEqual(total, 2)
        self.assertEqual(len(rows), 2)

    def test_a_killed_turn_still_leaves_its_counts(self):
        self.log.append(model="m", attempt=1)
        # A SIGKILL between writes still leaves the first row durable.
        rows, total = self.log.read()
        self.assertEqual(total, 1)

    def test_read_total_counts_every_line_once(self):
        for _ in range(5):
            self.log.append(model="m")
        _, total = self.log.read()
        self.assertEqual(total, 5)
        self.log.clear()
        self.assertEqual(self.log.read()[1], 0)


class RequestBudgetTests(unittest.TestCase):
    def test_task_budget_left(self):
        self.assertIsNone(budget_requests.task_budget(0, 10))
        self.assertEqual(budget_requests.task_budget(45, 12), 33)
        self.assertEqual(budget_requests.task_budget(45, 99), 0)

    def test_run_budget_left(self):
        self.assertIsNone(budget_requests.run_budget(0, 99))
        self.assertEqual(budget_requests.run_budget(300, 120), 180)

    def test_can_afford_scopes(self):
        ok, scope = budget_requests.can_afford(None, None, need=2)
        self.assertTrue(ok)
        self.assertIsNone(scope)
        ok, scope = budget_requests.can_afford(1, None, need=2)
        self.assertFalse(ok)
        self.assertEqual(scope, "task")
        ok, scope = budget_requests.can_afford(None, 1, need=2)
        self.assertFalse(ok)
        self.assertEqual(scope, "run")

    def test_an_implementation_gets_no_more_than_70_percent(self):
        self.assertEqual(budget_requests.implementation_budget(45), 31)
        self.assertIsNone(budget_requests.implementation_budget(0))

    def test_hold_reason_names_the_scope(self):
        self.assertIn("task", budget_requests.hold_reason("task"))
        self.assertIn("run request budget", budget_requests.hold_reason("run"))


class ReconcileTests(unittest.TestCase):
    def test_provider_delta_vs_local_attribution(self):
        rows = [{"paid": False}, {"paid": False}, {"paid": True}]
        s = metrics.Reconcile.summary(rows, provider_before=218, provider_after=355)
        self.assertEqual(s["provider_delta"], 137)
        self.assertEqual(s["locally_observed"], 2)
        self.assertEqual(s["paid_requests"], 1)
        self.assertEqual(s["other_unattributed"], 135)

    def test_difference_is_shown_not_assigned(self):
        s = metrics.Reconcile.summary([], provider_before=100, provider_after=130)
        self.assertEqual(s["other_unattributed"], 30)
        self.assertEqual(s["locally_observed"], 0)

    def test_no_provider_numbers_leave_unknowns(self):
        s = metrics.Reconcile.summary([{"paid": False}])
        self.assertIsNone(s["provider_delta"])
        self.assertIsNone(s["other_unattributed"])


class BucketClockTests(unittest.TestCase):
    def test_transitions_bank_into_disjoint_buckets(self):
        clock = metrics.BucketClock()
        t0 = 1_000_000.0
        with mock.patch.object(metrics, "_now", side_effect=[t0, t0 + 10, t0 + 25]):
            clock.enter(metrics.ACTIVE_MODEL)
            clock.enter(metrics.TESTING)
            snap = clock.snapshot()
        self.assertAlmostEqual(snap[metrics.ACTIVE_MODEL], 10.0)
        self.assertAlmostEqual(snap[metrics.TESTING], 15.0)

    def test_total_approximates_wall_time(self):
        clock = metrics.BucketClock()
        with mock.patch.object(metrics, "_now", side_effect=[0, 30, 95, 95]):
            clock.enter(metrics.ACTIVE_MODEL)
            clock.enter(metrics.ALLOWANCE_BLOCKED)
            snap = clock.snapshot()
            total = clock.total()
        self.assertAlmostEqual(snap[metrics.ALLOWANCE_BLOCKED], 65.0)
        self.assertAlmostEqual(total, 95.0)

    def test_unknown_state_is_refused(self):
        with self.assertRaises(ValueError):
            metrics.BucketClock().enter("flying")

    def test_poke_freezes_without_changing_state(self):
        clock = metrics.BucketClock()
        with mock.patch.object(metrics, "_now", side_effect=[0, 10, 10, 20]):
            clock.enter(metrics.ACTIVE_MODEL)   # now=0
            clock.poke()                        # now=10 -> +10
            clock.poke()                        # now=10 -> +0
            snap = clock.snapshot()             # now=20 -> +10
        self.assertAlmostEqual(snap[metrics.ACTIVE_MODEL], 20.0)


class AppreciateTests(unittest.TestCase):
    def test_human_durations(self):
        self.assertEqual(metrics.appreciate_seconds(4 * 3600), "4h00m")
        self.assertEqual(metrics.appreciate_seconds(3600 + 48 * 60), "1h48m")
        self.assertEqual(metrics.appreciate_seconds(23 * 60), "23m")
        self.assertEqual(metrics.appreciate_seconds(45), "45s")


if __name__ == "__main__":
    unittest.main()