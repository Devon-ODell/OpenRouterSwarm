"""P0 table-driven characterization of every `Queue.release()` branch.

release() (swarmd.py:1043) is the daemon's whole retry/split/park policy in one
method. P0 freezes every branch — status, attempts, harness_failures,
exploration_failures, backoff delay, truncation, and the run-level circuit
breaker — so P5 can split policy from persistence with a byte-identical
reference.

`time` is pinned so `not_before` delays are asserted exactly.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from swarm import swarmd
from swarm.learn import Ledger

NOW = 1_700_000_000.0


class ReleaseCase(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.root = Path(d.name)
        (self.root / "state").mkdir()
        (self.root / "logs").mkdir()
        for attr, value in (("STATE", self.root / "state"), ("LOGS", self.root / "logs")):
            p = patch.object(swarmd, attr, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(swarmd.time, "time", return_value=NOW)
        p.start()
        self.addCleanup(p.stop)
        swarmd._run.clear()

    def queue(self, max_depth=2):
        return swarmd.Queue(max_depth=max_depth)

    def add(self, q, title="T", **meta):
        return q.add(title, "detail", "feature", origin="human", **meta)

    def write_row(self, q, **fields):
        """Inject a task row directly into the queue file for full field control."""
        row = {"id": "t-raw-000001", "title": "Raw", "detail": "d", "kind": "feature",
               "attempts": 0, "created": NOW, "priority": 0, "depth": 0, **fields}
        with open(q.path, "a") as f:
            f.write(json.dumps(row) + "\n")
        return row["id"]


class ReleaseBranchTests(ReleaseCase):
    def test_done(self):
        q = self.queue()
        t = self.add(q)
        q.claim()
        out = q.release(t["id"], True, "landed", stage="accepted", requests=3,
                        turn_seconds=120, role_turns=4)
        self.assertEqual(out["status"], "done")
        self.assertEqual(out["attempts"], 1)
        self.assertEqual(out["requests_spent"], 3)
        self.assertEqual(out["turn_seconds"], 120)
        self.assertEqual(out["role_turns"], 4)
        self.assertEqual(q.pending(), [])
        done = swarmd._read(q.done)
        self.assertEqual([r["id"] for r in done], [t["id"]])

    def test_retry_plain_backoff_squares(self):
        q = self.queue()
        t = self.add(q)
        q.claim()
        out = q.release(t["id"], False, "tests failed", stage="tests_failed")
        self.assertEqual(out["status"], "retry")
        self.assertEqual(out["attempts"], 1)
        # backoff = 300 * attempts^2 (attempts is 1 here).
        self.assertEqual(out["not_before"], NOW + 300)
        self.assertEqual(out["notes"], ["tests failed"])
        # A retry stays in the live queue and is not written to done.jsonl.
        self.assertEqual([r["id"] for r in q.pending()], [t["id"]])
        self.assertEqual(swarmd._read(q.done), [])

    def test_retry_deferred_does_not_spend_an_attempt(self):
        q = self.queue()
        t = self.add(q)
        q.claim()
        out = q.release(t["id"], False, "held", defer=600, failure_class="harness")
        self.assertEqual(out["status"], "retry")
        self.assertEqual(out["not_before"], NOW + 600)
        self.assertEqual(out["attempts"], 0, "a deferred retry must not spend a life")

    # ------------------------------------------------------------- harness backoff tiers
    def harness(self, q, t, note, requests=0):
        q.claim()
        return q.release(t["id"], False, note, failure_class="harness", requests=requests)

    def test_harness_provider_busy_backs_off_zero(self):
        q = self.queue()
        t = self.add(q)
        out = self.harness(q, t, "provider busy (rate-limited upstream)")
        self.assertEqual(out["status"], "retry")
        self.assertEqual(out["not_before"], NOW, "provider-busy is a cheap handoff")
        self.assertEqual(out["harness_failures"], 1)
        self.assertEqual(out["attempts"], 0)

    def test_harness_timeout_backs_off_30m(self):
        q = self.queue()
        t = self.add(q)
        out = self.harness(q, t, "agent timeout after 900s")
        self.assertEqual(out["status"], "retry")
        self.assertEqual(out["not_before"], NOW + 30 * 60)
        self.assertEqual(out["harness_failures"], 1)

    def test_harness_incomplete_backs_off_15m(self):
        q = self.queue()
        t = self.add(q)
        out = self.harness(q, t, "no usable json", requests=10)
        self.assertEqual(out["status"], "retry")
        self.assertEqual(out["not_before"], NOW + 15 * 60)
        self.assertEqual(out["harness_failures"], 1)

    def test_harness_transport_backs_off_2m(self):
        q = self.queue()
        t = self.add(q)
        out = self.harness(q, t, "connection reset by peer", requests=2)
        self.assertEqual(out["status"], "retry")
        self.assertEqual(out["not_before"], NOW + 2 * 60)
        self.assertEqual(out["harness_failures"], 1)

    # ----------------------------------------------------------------- splits and parks
    def test_split_when_attempts_exhausted(self):
        q = self.queue(max_depth=1)
        t = self.add(q)
        q.claim()
        q.release(t["id"], False, "tests failed", stage="tests_failed")
        q.claim()
        out = q.release(t["id"], False, "tests failed again", stage="tests_failed")
        self.assertEqual(out["status"], "split")
        self.assertEqual(out["attempts"], 2)
        # Split rows are finished, so they land in done.jsonl.
        self.assertEqual([r["status"] for r in swarmd._read(q.done)], ["split"])

    def test_park_when_depth_exhausted(self):
        q = self.queue(max_depth=1)
        parent = self.add(q, "Parent")
        # A child inherits depth = parent.depth + 1; at max_depth=1 that is exhausted.
        t = self.add(q, "Child", parent=parent["id"])
        self.assertEqual(t["depth"], 1)
        q.claim()
        out = q.release(t["id"], False, "tests failed", stage="tests_failed", split_now=True)
        self.assertEqual(out["status"], "parked")
        self.assertEqual(out["attempts"], 1)
        self.assertEqual([r["status"] for r in swarmd._read(q.done)], ["parked"])

    def test_split_on_exploration_exhausted_below_max_depth(self):
        q = self.queue(max_depth=1)
        t = self.add(q)
        q.claim()
        q.release(t["id"], False, "no entry point", failure_class="harness",
                  stage="exploration_exhausted")
        q.claim()
        out = q.release(t["id"], False, "still no entry point", failure_class="harness",
                        stage="exploration_exhausted")
        self.assertEqual(out["status"], "split")
        self.assertEqual(out["exploration_failures"], 2)
        self.assertEqual(out["attempts"], 0, "harness failures never spend a life")

    def test_split_on_model_pool_exhausted_below_max_depth(self):
        q = self.queue(max_depth=1)
        t = self.add(q)
        q.claim()
        out = q.release(t["id"], False, "every implementer is excluded", park=True,
                        failure_class="harness", stage="model_pool_exhausted")
        self.assertEqual(out["status"], "split")
        self.assertEqual([r["status"] for r in swarmd._read(q.done)], ["split"])

    def test_park_on_review_format_error(self):
        q = self.queue(max_depth=2)
        t = self.add(q)
        q.claim()
        out = q.release(t["id"], False, "not a review json", failure_class="review_format")
        self.assertEqual(out["status"], "parked")
        self.assertEqual(out["review_format_failures"], 1)
        self.assertEqual(out["attempts"], 0)
        self.assertEqual([r["status"] for r in swarmd._read(q.done)], ["parked"])

    def test_park_after_max_harness_failures(self):
        q = self.queue()
        t = self.add(q)
        for n in (1, 2):
            self.harness(q, t, f"harness noise {n}")
        out = self.harness(q, t, "harness noise 3")
        self.assertEqual(out["status"], "parked")
        self.assertEqual(out["harness_failures"], 3)
        self.assertEqual(out["attempts"], 0)
        self.assertEqual([r["status"] for r in swarmd._read(q.done)], ["parked"])

    # ------------------------------------------------------------- run-level circuit breaker
    def test_second_non_harness_failure_cools_the_model_for_the_run(self):
        q = self.queue()
        ledger = Ledger(self.root / "state" / "learn.json")
        for n in range(2):
            t = self.add(q, f"Task {n}")
            q.claim()
            q.release(t["id"], False, "no usable output", failure_class="model",
                      failed_model="m:free", failed_role="implementer", ledger=ledger)
        resting = [m for m, _, _ in ledger.resting()]
        self.assertIn("m:free", resting)
        # The run state counted two failures for that model|role.
        self.assertEqual(swarmd._run["model_failures"].get("m:free|implementer"), 2)

    def test_harness_failures_never_fire_the_run_circuit_breaker(self):
        q = self.queue()
        ledger = Ledger(self.root / "state" / "learn.json")
        for n in range(2):
            t = self.add(q, f"Task {n}")
            q.claim()
            q.release(t["id"], False, "provider busy", failure_class="harness",
                      failed_model="m:free", failed_role="implementer", ledger=ledger)
        self.assertEqual([m for m, _, _ in ledger.resting()], [])

    # ------------------------------------------------------------------------- truncations
    def test_failed_models_truncate_to_8(self):
        q = self.queue()
        tid = self.write_row(q)
        for n in range(10):
            q.claim()
            q.release(tid, False, f"fail {n}", failure_class="task",
                      failed_model="m:free", failed_role="implementer")
            # Keep it in the retry branch so the truncation is observable.
            row = q.get(tid)
            row["attempts"] = 0
            swarmd._write(q.path, [r for r in swarmd._read(q.path) if r["id"] != tid] + [row])
        row = q.get(tid)
        self.assertEqual(len(row["failed_models"]), 8)
        self.assertEqual(row["failed_models"][-1]["note"], "fail 9")

    def test_notes_truncate_to_2(self):
        q = self.queue()
        tid = self.write_row(q)
        for n in range(3):
            q.claim()
            q.release(tid, False, f"note {n}", failure_class="task")
            row = q.get(tid)
            row["attempts"] = 0
            swarmd._write(q.path, [r for r in swarmd._read(q.path) if r["id"] != tid] + [row])
        row = q.get(tid)
        self.assertEqual(row["notes"], ["note 1", "note 2"])

    def test_cumulative_fields_survive_retries(self):
        q = self.queue()
        t = self.add(q)
        q.claim()
        q.release(t["id"], False, "timeout", failure_class="harness", requests=5,
                  turn_seconds=300, role_turns=2)
        q.claim()
        out = q.release(t["id"], False, "timeout again", failure_class="harness",
                        requests=7, turn_seconds=400, role_turns=3)
        self.assertEqual((out["requests_spent"], out["turn_seconds"], out["role_turns"]),
                         (12, 700, 5))


if __name__ == "__main__":
    unittest.main()
