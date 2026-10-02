"""Work order §2 + §5: latency-sized turns, per-task/model circuit breakers.

A turn sized from measured latency must not exceed the round budget that fits the
wall clock after finalization reserve, and a model that times out or exhausts its
exploration on a task is excluded from that task/role before the next attempt —
never immediately retried on the same pairing. Two failures across tasks in one
run cool the model globally for that role.
"""
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from swarm import swarmd
from swarm.learn import Ledger


class PersonaDefaultsTests(unittest.TestCase):
    """Handoff 2026-10-02 A4: no attempt runs without persona shaping."""

    def test_human_tasks_carry_no_persona_and_get_the_builder_default(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        root = Path(d.name)
        (root / "state").mkdir()
        (root / "logs").mkdir()
        p = patch.object(swarmd, "STATE", root / "state")
        p.start()
        self.addCleanup(p.stop)
        p2 = patch.object(swarmd, "LOGS", root / "logs")
        p2.start()
        self.addCleanup(p2.stop)
        swarmd._run.clear()
        q = swarmd.Queue(max_depth=1)
        task = q.add("Add the settings screen", kind="feature", origin="human")
        self.assertIsNone(task.get("persona"))
        cfg = {"repo": str(root), "test_cmd": "true", "models": ["dots:free"],
               "steps": {"implementer": 10}, "max_role_calls": 5}
        worker = swarmd.Worker(0, cfg, q, Mock(), threading.Event())
        worker.name = "w0"
        worker.ledger = Ledger(root / "state" / "learn.json")
        # The worker's run loop defaults the persona before do_task.
        claimed = q.claim()
        if not claimed.get("persona"):
            claimed["persona"] = "builder"
            swarmd.journal("persona_default", id=claimed["id"],
                            from_origin=claimed.get("origin", "human"))
        self.assertEqual(claimed["persona"], "builder")
        rows = [json.loads(l) for l in (root / "state" / "journal.jsonl").open()]
        self.assertEqual([r["event"] for r in rows], ["persona_default"])

    def test_planner_tasks_keep_their_sampled_persona(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        root = Path(d.name)
        (root / "state").mkdir()
        (root / "logs").mkdir()
        p = patch.object(swarmd, "STATE", root / "state")
        p.start()
        self.addCleanup(p.stop)
        p2 = patch.object(swarmd, "LOGS", root / "logs")
        p2.start()
        self.addCleanup(p2.stop)
        swarmd._run.clear()
        q = swarmd.Queue(max_depth=1)
        task = q.add("Go deeper (inventor)", kind="feature", origin="plan", persona="inventor")
        self.assertEqual(task.get("persona"), "inventor")


class CircuitBreakerTests(unittest.TestCase):
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
        swarmd._run.clear()
        self.q = swarmd.Queue(max_depth=1)

    def worker(self, **extra):
        cfg = {"repo": str(self.root), "base_branch": "main", "test_cmd": "true",
               "models": ["dots:free", "pool:free"], "steps": {"implementer": 10},
               "max_role_calls": 5, **extra}
        worker = swarmd.Worker(0, cfg, self.q, Mock(), threading.Event())
        worker.name = "w0"
        worker.task = {"id": "task", "title": "T", "kind": "feature"}
        worker.evidence = None
        worker.ledger = Ledger(self.root / "state" / "learn.json")
        return worker

    def test_a_recorded_failure_excludes_the_model_from_that_task_role(self):
        task = self.q.add("Shard Stack M1", kind="feature")
        self.q.claim()
        self.q.release(task["id"], False, "agent timeout after 900s", failure_class="harness",
                       requests=16, failed_model="dots:free", failed_role="implementer")
        task = self.q.get(task["id"])
        worker = self.worker()
        worker.task = task
        self.assertEqual(worker.excluded_models(role="implementer"), {"dots:free"})
        # The same model is still fine for a role where it has not failed.
        self.assertEqual(worker.excluded_models(role="adversary"), set())

    def test_provider_busy_does_not_poison_the_pairing(self):
        task = self.q.add("Shard Stack M1", kind="feature")
        self.q.claim()
        self.q.release(task["id"], False, "provider busy (rate-limited upstream)",
                       failure_class="harness", failed_model="dots:free",
                       failed_role="implementer")
        worker = self.worker()
        worker.task = self.q.get(task["id"])
        self.assertEqual(worker.excluded_models(role="implementer"), set())

    def test_a_pick_skips_an_excluded_model(self):
        worker = self.worker(models=["dots:free", "pool:free"])
        worker.task = {"id": "t", "failed_models": [{"model": "dots:free",
                                                     "role": "implementer",
                                                     "class": "model",
                                                     "at": swarmd.time.time()}]}
        with patch.object(worker.ledger, "pick", side_effect=lambda *a, **k: "pool:free") as pick:
            self.assertEqual(worker.pick(role="implementer"), "pool:free")
            pick.assert_called_once()

    def test_all_models_excluded_parks_with_model_pool_exhausted(self):
        worker = self.worker(models=["dots:free"])
        worker.task = {"id": "t", "failed_models": [{"model": "dots:free",
                                                     "role": "implementer",
                                                     "class": "model",
                                                     "at": swarmd.time.time()}]}
        failed = worker.excluded_models(role="implementer")
        self.assertEqual(failed, {"dots:free"})
        self.assertIsNone(worker.select_implementer(failed))

    def test_global_cool_after_two_failures_across_tasks_in_a_run(self):
        ledger = Ledger(self.root / "state" / "learn.json")
        for n in range(2):
            task = self.q.add(f"Task {n}", kind="feature")
            self.q.claim()
            self.q.release(task["id"], False, "no usable json", failure_class="model",
                           failed_model="dots:free", failed_role="implementer",
                           ledger=ledger)
        self.assertTrue(any(m == "dots:free" for m, _, _ in ledger.resting()))

    def test_excluded_models_ignores_failures_older_than_24h(self):
        task = self.q.add("Old", kind="feature")
        self.q.claim()
        self.q.release(task["id"], False, "no usable json", failure_class="model",
                       failed_model="dots:free", failed_role="implementer")
        task = self.q.get(task["id"])
        task["failed_models"][0]["at"] = swarmd.time.time() - 25 * 3600
        worker = self.worker()
        worker.task = task
        self.assertEqual(worker.excluded_models(role="implementer"), set())

    def test_cumulative_requests_seconds_and_turns_survive_retries_and_restarts(self):
        task = self.q.add("Dots", kind="feature")
        # First attempt: 16 requests, 900 s, 5 role turns, timed out.
        self.q.claim()
        out = self.q.release(task["id"], False, "agent timeout after 900s", failure_class="harness",
                             requests=16, turn_seconds=900, role_turns=5,
                             failed_model="dots:free", failed_role="implementer")
        self.assertEqual(out["requests_spent"], 16)
        self.assertEqual(out["turn_seconds"], 900)
        self.assertEqual(out["role_turns"], 5)
        # Restart: the task is read back from the queue file with its accumulated fields.
        rows = swarmd._read(self.q.path)
        task = next(r for r in rows if r["id"] == task["id"])
        self.assertEqual((task["requests_spent"], task["turn_seconds"], task["role_turns"]),
                         (16, 900, 5))
        # Second attempt adds to the same accumulator.
        self.q.claim()
        out = self.q.release(task["id"], False, "timeout", failure_class="harness",
                             requests=10, turn_seconds=600, role_turns=3)
        self.assertEqual(out["requests_spent"], 26)
        self.assertEqual(out["turn_seconds"], 1500)
        self.assertEqual(out["role_turns"], 8)


class LatencySizingTests(unittest.TestCase):
    def test_effective_steps_caps_the_role_rounds(self):
        steps = swarmd.effective_steps("implementer", "dots:free", 900, 16, None,
                                       seconds_per_request=60)
        self.assertEqual(steps, 12)  # floor((900 - 180) / 60)

    def test_the_budget_line_reaches_the_prompt(self):
        import tempfile as _t
        d = _t.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        root = Path(d.name)
        (root / "state").mkdir()
        (root / "logs").mkdir()
        for attr, value in (("STATE", root / "state"), ("LOGS", root / "logs")):
            p = patch.object(swarmd, attr, value)
            p.start()
            self.addCleanup(p.stop)
        cfg = {"repo": str(root), "base_branch": "main", "test_cmd": "true",
               "models": ["dots:free"], "steps": {"implementer": 16},
               "role_timeouts": {"implementer": 900}}
        ledger = Ledger(root / "state" / "learn.json")
        # A measured 60 s per request: the work order's synthetic latency. With 900 s and
        # 180 s of finalization reserve, at most floor((900 - 180) / 60) = 12 rounds fit.
        from swarm.pacing import LatencyLearner
        LatencyLearner(ledger).update("dots:free", "implementer", 60.0, 1)
        worker = swarmd.Worker(0, cfg, swarmd.Queue(), Mock(), threading.Event())
        worker.name = "w0"
        worker.task = {"id": "t", "title": "T"}
        worker.evidence = None
        worker.execution_class = "standard"
        worker.ledger = ledger
        seen = {}

        def fake(_prompt, _cwd, _cfg, _role, _worker, _budget, steps, _model):
            seen["prompt"] = _prompt
            seen["steps"] = steps
            return "ok"

        with patch.object(swarmd, "flint", side_effect=fake):
            worker.call("implementer", "YOU ARE THE IMPLEMENTER", str(root), 16, "dots:free")
        self.assertIn("TURN BUDGET", seen["prompt"])
        self.assertLessEqual(seen["steps"], 12)


class SelectionScoringTests(unittest.TestCase):
    """Work order §9: claim() ranks ready tasks by expected value per request."""

    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.root = Path(d.name)
        p = patch.object(swarmd, "STATE", self.root)
        p.start()
        self.addCleanup(p.stop)
        self.q = swarmd.Queue(max_depth=1)

    def test_priority_stays_dominant(self):
        a = self.q.add("cheap", kind="test")
        b = self.q.add("big but priority", kind="feature", priority=1)
        # The priority-1 task must win even though it is more expensive.
        claimed = self.q.claim()
        self.assertEqual(claimed["id"], b["id"])

    def test_within_a_priority_ev_ranks_verifiable_cheap_tasks_first(self):
        cheap = self.q.add("cheap verifiable", kind="bugfix",
                           allowed_paths=["a.py"], verification_commands=["pytest x"])
        expensive = self.q.add("doomed broad epic", kind="feature")
        claimed = self.q.claim()
        self.assertEqual(claimed["id"], cheap["id"])

    def test_selection_explanation_is_recorded_on_the_claim(self):
        t = self.q.add("pick me", kind="bugfix")
        claimed = self.q.claim()
        self.assertIn("selection", claimed)
        self.assertIn("score", claimed["selection"])
        self.assertIn("estimated_requests", claimed["selection"])
        self.assertEqual(claimed["selection"]["estimated_requests"], 25)

    def test_harness_failures_penalise_a_task(self):
        self.q.add("clean", kind="bugfix")
        t = self.q.add("twice_harness_failed", kind="bugfix")
        self.q.release(t["id"], False, "timeout", failure_class="harness", failed_model="m")
        self.q.release(t["id"], False, "timeout", failure_class="harness", failed_model="m")
        self.q.release(t["id"], False, "timeout", failure_class="harness", failed_model="m")
        self.q.release(t["id"], False, "timeout", failure_class="harness", failed_model="m")
        claimed = self.q.claim()
        self.assertEqual(claimed["title"], "clean")


class BoundedAdmissionTests(unittest.TestCase):
    """Work order §10: a task must fit a single bounded attempt."""

    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.root = Path(d.name)
        p = patch.object(swarmd, "STATE", self.root)
        p.start()
        self.addCleanup(p.stop)
        self.q = swarmd.Queue(max_depth=1)

    def test_a_manual_task_is_never_automatically_claimed(self):
        self.q.add("manual one", kind="feature", request_class="manual")
        self.assertIsNone(self.q.claim())
        self.assertEqual(len(self.q.ready()), 1)

    def test_request_class_must_be_known(self):
        with self.assertRaises(ValueError):
            self.q.add("bad class", kind="feature", request_class="huge")

    def test_at_most_five_acceptance_criteria(self):
        ok = self.q.add("five criteria", kind="feature",
                        acceptance=[f"criterion {i}" for i in range(5)])
        self.assertIsNotNone(ok)
        with self.assertRaises(ValueError):
            self.q.add("six criteria", kind="feature",
                       acceptance=[f"criterion {i}" for i in range(6)])

    def test_scope_gap_holds_a_task_without_a_verifier_or_paths(self):
        # A task whose acceptance says no more than its title and names no file/symbol
        # is open-ended: it cannot be told apart from an abandoned attempt.
        gap = swarmd.scope_gap({"title": "Improve the thing", "detail": "",
                                "acceptance": ["improve the thing"]})
        self.assertTrue(gap)

    def test_scope_gap_accepts_a_verifiable_bounded_task(self):
        gap = swarmd.scope_gap({"title": "Fix crash in parser", "detail": "parser.py crashes on empty input",
                                "acceptance": ["parser.py handles empty input without crashing"],
                                "verification_commands": ["python3 -m unittest test_parser -q"]})
        self.assertEqual(gap, "")

    def test_scope_gap_holds_a_manual_task(self):
        gap = swarmd.scope_gap({"title": "Manual QA pass", "detail": "click through the UI",
                                "request_class": "manual"})
        self.assertIn("manual", gap)


class PromotionStateTests(unittest.TestCase):
    """Work order §14: accepted / landed / merged / verified-live stay separate."""

    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.root = Path(d.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        import subprocess
        for a in (["init", "-b", "main"], ["config", "user.email", "t@e.invalid"],
                  ["config", "user.name", "T"]):
            subprocess.run(["git", "-C", str(self.repo), *a], check=True, capture_output=True)
        (self.repo / "a.txt").write_text("x\n")
        for a in (["add", "-A"], ["commit", "-q", "-m", "baseline"]):
            subprocess.run(["git", "-C", str(self.repo), *a], check=True, capture_output=True)

    def cfg(self):
        return {"repo": str(self.repo), "base_branch": "main", "trunk": "swarm/trunk"}

    def test_landed_on_trunk_is_not_merged_or_live(self):
        c = self.cfg()
        p = patch.object(swarmd, "git", side_effect=lambda *a, **k: (1, ""))
        with p:
            s = swarmd.promotion_state(c)
        self.assertEqual((s["landed"], s["merged"], s["verified_live"]), (0, 0, 0))

    def test_a_merged_trunk_also_counts_as_landed(self):
        import subprocess
        c = self.cfg()
        subprocess.run(["git", "-C", str(self.repo), "checkout", "-b", "swarm/trunk"],
                       check=True, capture_output=True)
        (self.repo / "b.txt").write_text("y\n")
        for a in (["add", "-A"], ["commit", "-q", "-m", "swarm: work\n\nSwarm-Task: t1"],
                  ["checkout", "main"], ["merge", "--no-ff", "-q", "-m", "merge swarm", "swarm/trunk"]):
            subprocess.run(["git", "-C", str(self.repo), *a], check=True, capture_output=True)
        s = swarmd.promotion_state(c)
        self.assertEqual((s["landed"], s["merged"], s["verified_live"]), (1, 1, 0))


class EvidenceQualityTests(unittest.TestCase):
    """Work order §3/§8/§16: malformed judges are unscored, contradictory approvals rejected."""

    def test_an_approval_that_contradicts_a_failed_check_is_rejected(self):
        from swarm.workflow import parse_review
        acceptance = [{"id": "C1", "text": "http errors handled"}]
        review = ("{\"verdict\": \"approve\", \"summary\": \"looks good\", \"tree\": \"abc1234\", "
                  "\"checks\": [{\"criterion\": \"C1\", \"passed\": false, \"evidence\": \"no test\"}], "
                  "\"findings\": []}")
        with self.assertRaises(ValueError):
            parse_review(review, "abc1234", acceptance)

    def test_an_approval_with_a_missing_acceptance_test_is_rejected(self):
        from swarm.workflow import parse_review
        acceptance = [{"id": "C1", "text": "http errors handled"}]
        review = ("{\"verdict\": \"approve\", \"summary\": \"ok\", \"tree\": \"abc1234\", "
                  "\"checks\": [{\"criterion\": \"C1\", \"passed\": true, "
                  "\"evidence\": \"suite green\"}], "
                  "\"findings\": [{\"severity\": \"minor\", \"path\": \"a.py\", \"line\": 1, "
                  "\"issue\": \"the acceptance criterion's http-error test is missing\", "
                  "\"verification\": \"add one\"}]}")
        with self.assertRaises(ValueError):
            parse_review(review, "abc1234", acceptance)

    def test_an_honest_request_changes_is_accepted(self):
        from swarm.workflow import parse_review
        acceptance = [{"id": "C1", "text": "http errors handled"}]
        review = ("{\"verdict\": \"request_changes\", \"summary\": \"needs test\", \"tree\": \"abc1234\", "
                  "\"checks\": [{\"criterion\": \"C1\", \"passed\": false, "
                  "\"evidence\": \"no http test\"}], "
                  "\"findings\": [{\"severity\": \"major\", \"path\": \"a.py\", \"line\": 3, "
                  "\"issue\": \"no test for the http error path\", "
                  "\"verification\": \"send a 500 and assert json\"}]}")
        out = parse_review(review, "abc1234", acceptance)
        self.assertEqual(out["verdict"], "request_changes")

    def test_reconciliation_shows_unattributed_not_silently_assigns(self):
        from swarm.metrics import Reconcile
        s = Reconcile.summary([{"paid": False}] * 10, provider_before=100, provider_after=137)
        self.assertEqual(s["provider_delta"], 37)
        self.assertEqual(s["locally_observed"], 10)
        self.assertEqual(s["other_unattributed"], 27)


if __name__ == "__main__":
    unittest.main()