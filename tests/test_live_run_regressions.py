"""Regressions found by watching the first live SkyRing shift (2026-10-11).

1. An attempt stopped by a provider outage or the allowance re-raised before its requests were
   banked, so the task's requests_spent stayed 0 after 16+ requests: task_request_cap could never
   stop it and the scorecard could not see the attempts.
"""
import threading
from unittest.mock import patch

from swarm import swarmd
from tests.test_attempt_stages import RepoCase


def _task():
    return {"id": "t1", "title": "Add a stdlib validator", "detail": "write tools/x.py",
            "kind": "feature", "origin": "human", "acceptance": ["tools/x.py exists"]}


class DeferredRequestsTests(RepoCase):
    def outage(self, prompt, cwd, c_, role, worker, *a):
        swarmd.count_requests(worker, 8)            # what the real turn's sidecar would record
        raise swarmd.ProviderDown(a[-1] if a else "a:free", "provider failed mid-reply")

    def test_a_deferred_attempt_banks_its_requests_and_journals_itself(self):
        w = self.worker()
        with patch.object(swarmd, "flint", side_effect=self.outage), \
                self.assertRaises(swarmd.ProviderDown):
            w.do_task(_task(), "goal")
        self.assertEqual(w.attempt_requests, 8)
        self.assertEqual(swarmd.count_requests("w0"), 0)          # banked, not left behind
        rows = [r for r in swarmd._read(swarmd.STATE / "journal.jsonl")
                if r.get("event") == "attempt_deferred"]
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["id"], rows[0]["requests"], rows[0]["reason"]),
                         ("t1", 8, "ProviderDown"))

    def test_the_worker_loop_charges_them_to_the_task(self):
        q = swarmd.Queue()
        task = q.add(**{k: v for k, v in _task().items() if k != "id"})
        stop = threading.Event()

        def outage(*args):
            stop.set()                              # let the loop exit after this release
            return self.outage(*args)

        w = swarmd.Worker(0, self.cfg(), q, budget=None, stop=stop)
        w.name = "w0"
        with patch.object(swarmd, "flint", side_effect=outage), \
                patch.object(swarmd, "read_goal", return_value="goal"):
            w.run()
        row = next(r for r in q.pending() if r["id"] == task["id"])
        self.assertEqual(row["requests_spent"], 8)
        self.assertEqual(row.get("attempts", 0), 0)               # an outage costs no attempt
        self.assertEqual(row.get("role_turns"), 1)


class BackoffIsNotModelTimeTests(RepoCase):
    """2. While a deferred task waits for a model, the worker said it was still on the last
    model's turn, and the wait was banked as model time."""

    def test_the_wait_after_an_outage_is_reported_and_banked_as_backoff(self):
        q = swarmd.Queue()
        q.add(**{k: v for k, v in _task().items() if k != "id"})
        seen = []
        holder = {}

        class RecordingStop(threading.Event):
            def wait(self, timeout=None):
                w = holder.get("w")
                if w is not None and self.is_set():
                    seen.append((w.doing, w.state))
                return super().wait(0)

        stop = RecordingStop()

        def outage(prompt, cwd, c_, role, worker, *a):
            stop.set()
            raise swarmd.ProviderDown("a:free", "provider failed mid-reply")

        w = swarmd.Worker(0, self.cfg(), q, budget=None, stop=stop)
        w.name = "w0"
        holder["w"] = w
        with patch.object(swarmd, "flint", side_effect=outage), \
                patch.object(swarmd, "read_goal", return_value="goal"):
            w.run()
        self.assertTrue(seen, "the worker never waited after the outage")
        doing, state = seen[0]
        self.assertEqual(state, "provider_backoff")
        self.assertIn("waiting for a model", doing)
        self.assertNotIn("implementer with", doing)


class DeferredWorkIsResumedTests(RepoCase):
    """4. A turn killed by a provider error left its edits on the attempt's branch, but the
    retry in the same run started from nothing: only a daemon restart looked for that work."""

    def test_the_next_attempt_in_the_same_run_starts_from_the_deferred_work(self):
        from tests.test_attempt_stages import review
        calls = []

        def first(prompt, cwd, c_, role, worker, *a):
            if role == "implementer":
                (cwd / "feature.txt").write_text("half written\n")
                raise swarmd.ProviderDown(a[-1], "provider failed mid-reply")
            return "done"

        # Two models: the one that failed rests, and the retry needs another to take it.
        w = self.worker(self.cfg(models=["a:free", "b:free"]))
        with patch.object(swarmd, "flint", side_effect=first), \
                self.assertRaises(swarmd.ProviderDown):
            w.do_task(_task(), "goal")
        self.assertIn("t1", swarmd.load_resume())

        def second(prompt, cwd, c_, role, worker, *a):
            calls.append((role, (cwd / "feature.txt").exists() and (cwd / "feature.txt").read_text()))
            if role == "implementer":
                (cwd / "feature.txt").write_text("finished\n")
                return "done"
            return review(prompt) if role == "adversary" else "done"

        with patch.object(swarmd, "flint", side_effect=second):
            ok, note = w.do_task(_task(), "goal")
        self.assertTrue(ok, note)
        # The retry re-applied the deferred edits and verified them instead of re-implementing:
        # the first turn it made already saw them, and no implementer turn started from nothing.
        self.assertEqual(calls[0][1], "half written\n")
        self.assertNotIn(("implementer", False), calls)
        self.assertNotIn("t1", swarmd.load_resume())             # handed over once


class FollowUpTests(RepoCase):
    """11. A judge's follow-up was queued with no acceptance criteria and no file limits: the
    judge's criteria were dropped on parse, and the scope gate admits criteria-less tasks."""

    def follow(self, ideas, **cfg):
        from swarm import attempt_runner
        from tests.test_attempt_runner import build_deps
        q = swarmd.Queue()
        parent = q.add("Add a stdlib validator", "x", "feature", acceptance=["tools/x.py exists"])
        w = swarmd.Worker(0, self.cfg(**cfg), q, budget=None, stop=threading.Event())
        deps = build_deps()
        runner = attempt_runner.AttemptRunner(w, deps)
        runner.follow_up(parent, {"follow_ups": ideas}, 0.9, False, self.cfg(**cfg))
        return [t for t in q.pending() if t.get("origin") == "follow_up"], deps

    def test_criteria_and_paths_from_the_judge_are_kept(self):
        from swarm import learn
        scores = learn.parse_scores('{"impact": 5, "creativity": 4, "quality": 6, "follow_ups": ['
                                    '{"title": "Wire the validator into verify", "detail": "d", '
                                    '"acceptance": ["python3 tools/verify.py --profile local passes"], '
                                    '"paths": ["tools/verify.py", "tools/tests"]}]}')
        queued, _ = self.follow(scores["follow_ups"])
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0]["acceptance"], ["python3 tools/verify.py --profile local passes"])
        self.assertEqual(queued[0]["allowed_paths"], ["tools/verify.py", "tools/tests/"])

    def test_a_follow_up_without_criteria_is_not_queued(self):
        queued, deps = self.follow([{"title": "Integrate it everywhere", "detail": "d",
                                     "acceptance": [], "paths": []}])
        self.assertEqual(queued, [])
        self.assertIn("follow_up_skipped", [c.args[0] for c in deps.journal.call_args_list])

    def test_a_fixed_backlog_can_turn_follow_ups_off(self):
        queued, _ = self.follow([{"title": "Next", "detail": "d", "acceptance": ["a check"],
                                  "paths": []}], follow_ups=False)
        self.assertEqual(queued, [])
