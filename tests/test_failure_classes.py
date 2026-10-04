"""What a failure costs the task and the model that attempted it.

Queue.release treated every failure alike: a note, an attempt spent, and after two attempts a
split or a park. So a step limit, a sandbox timeout or a daemon restart cost a task one of its
two lives and scored the model down at penalty weight. 90 of 121 finished tasks were split or
parked, and the four deep-game milestones GOAL.md puts first were all among them — not because
the work was too big, but because 54 attempts were killed by restarts and 48 turns hit the step
limit.
"""
import json
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from swarm import swarmd


class HarnessReleaseTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.root = Path(d.name)
        p = patch.object(swarmd, "STATE", self.root)
        p.start()
        self.addCleanup(p.stop)
        self.q = swarmd.Queue(max_depth=1)

    def fail(self, task, note="step limit", cls="harness"):
        return self.q.release(task["id"], False, note, failure_class=cls)

    def test_a_harness_failure_costs_the_task_nothing(self):
        task = self.q.add("Shard Stack M1", "the first milestone")
        self.q.claim()
        out = self.fail(task)
        self.assertEqual((out["status"], out["attempts"], out["harness_failures"]), ("retry", 0, 1))
        self.assertNotIn("notes", out)

    def test_it_comes_back_in_two_minutes(self):
        task = self.q.add("Shard Stack M1")
        self.q.claim()
        out = self.fail(task)
        self.assertAlmostEqual(out["not_before"] - swarmd.time.time(), 120, delta=5)

    def test_an_expensive_harness_failure_backs_off_and_records_the_model(self):
        task = self.q.add("Shard Stack M1")
        self.q.claim()
        out = self.q.release(task["id"], False, "agent timeout after 900s",
                             failure_class="harness", requests=16,
                             failed_model="dots:free", failed_role="implementer")
        self.assertAlmostEqual(out["not_before"] - swarmd.time.time(), 1800, delta=5)
        self.assertEqual(out["requests_spent"], 16)
        self.assertEqual(out["failed_models"][0]["model"], "dots:free")

    def test_three_of_them_park_it_for_a_person(self):
        task = self.q.add("Shard Stack M1")
        for i in range(2):
            self.q.claim()
            rows = swarmd._read(self.q.path)
            rows[0]["not_before"] = 0
            swarmd._write(self.q.path, rows)
            self.assertEqual(self.fail(task)["status"], "retry", i)
        self.q.claim()
        out = self.fail(task, "sandbox-exec timed out after 420 seconds")
        self.assertEqual(out["status"], "parked")
        self.assertIn("needs harness look", out["note"])
        self.assertIn("420 seconds", out["note"])
        self.assertEqual(out["attempts"], 0, "it never got an attempt that was about the task")

    def test_it_is_never_split(self):
        """Splitting says the task is too big. Nothing here is evidence of that."""
        task = self.q.add("Shard Stack M1")
        for _ in range(3):
            self.q.claim()
            rows = swarmd._read(self.q.path)
            if rows:
                rows[0]["not_before"] = 0
                swarmd._write(self.q.path, rows)
            out = self.fail(task)
        self.assertEqual(out["status"], "parked")
        self.assertNotEqual(out["status"], "split")

    def test_a_task_failure_still_spends_an_attempt_and_splits(self):
        task = self.q.add("Shard Stack M1")
        self.q.claim()
        out = self.fail(task, "2 tests failed", cls="task")
        self.assertEqual((out["status"], out["attempts"]), ("retry", 1))
        self.assertEqual(out["notes"], ["2 tests failed"])
        self.q.claim()
        out = self.fail(task, "2 tests failed", cls="task")
        self.assertEqual((out["status"], out["attempts"]), ("split", 2))

    def test_a_model_failure_counts_like_a_task_failure(self):
        """The model had its turn and produced nothing usable; the attempt is spent."""
        task = self.q.add("Shard Stack M1")
        self.q.claim()
        self.assertEqual(self.fail(task, "no usable JSON", cls="model")["attempts"], 1)

    def test_harness_failures_do_not_reset_the_attempts_already_spent(self):
        task = self.q.add("Shard Stack M1")
        self.q.claim()
        self.fail(task, "2 tests failed", cls="task")
        rows = swarmd._read(self.q.path)
        rows[0]["not_before"] = 0
        swarmd._write(self.q.path, rows)
        self.q.claim()
        out = self.fail(task, "step limit")
        self.assertEqual((out["attempts"], out["harness_failures"]), (1, 1))
        self.assertEqual(out["notes"], ["2 tests failed"], "the real failure note is kept")

    def test_a_success_is_unaffected(self):
        task = self.q.add("Shard Stack M1")
        self.q.claim()
        self.assertEqual(self.q.release(task["id"], True, "landed")["status"], "done")

    def test_the_default_is_the_old_behaviour(self):
        """Any caller that has not been taught the difference still spends an attempt."""
        task = self.q.add("Shard Stack M1")
        self.q.claim()
        self.assertEqual(self.q.release(task["id"], False, "something")["attempts"], 1)

    def test_a_single_exploration_exhausted_still_just_retries(self):
        task = self.q.add("Shard Stack M1")
        self.q.claim()
        out = self.q.release(task["id"], False, "stopped after 7 rounds without editing",
                             failure_class="harness", stage="exploration_exhausted",
                             failed_model="a:free")
        self.assertEqual((out["status"], out["exploration_failures"]), ("retry", 1))

    def test_two_exploration_exhausted_failures_from_different_models_split_the_task(self):
        """Two different models independently finding no entry point is decisive."""
        task = self.q.add("Shard Stack M1")
        self.q.claim()
        first = self.q.release(task["id"], False, "stopped after 7 rounds without editing",
                               failure_class="harness", stage="exploration_exhausted",
                               failed_model="a:free")
        self.assertEqual((first["status"], first["exploration_failures"]), ("retry", 1))
        rows = swarmd._read(self.q.path)
        rows[0]["not_before"] = 0
        swarmd._write(self.q.path, rows)
        self.q.claim()
        second = self.q.release(task["id"], False, "stopped after 8 rounds without editing",
                                failure_class="harness", stage="exploration_exhausted",
                                failed_model="b:free")
        self.assertEqual((second["status"], second["exploration_failures"],
                         second["harness_failures"]), ("split", 2, 2))

    def test_exploration_exhausted_does_not_split_past_max_depth(self):
        parent = self.q.add("Shard Stack M1")
        self.q.release(parent["id"], False, "timeout", split_now=True)
        child = self.q.add("Shard Stack M1 part 1", parent=parent["id"])
        self.q.claim()
        first = self.q.release(child["id"], False, "stopped after 7 rounds without editing",
                               failure_class="harness", stage="exploration_exhausted",
                               failed_model="a:free")
        rows = swarmd._read(self.q.path)
        rows[0]["not_before"] = 0
        swarmd._write(self.q.path, rows)
        self.q.claim()
        second = self.q.release(child["id"], False, "stopped after 8 rounds without editing",
                                failure_class="harness", stage="exploration_exhausted",
                                failed_model="b:free")
        self.assertEqual((first["status"], second["status"]), ("retry", "retry"),
                         "already at max_depth=1, so a second occurrence must not split it")

    def test_model_pool_exhausted_splits_within_depth_and_parks_at_the_ceiling(self):
        task = self.q.add("Shard Stack M1")
        self.q.claim()
        out = self.q.release(task["id"], False, "no model left to try", park=True,
                             stage="model_pool_exhausted")
        self.assertEqual(out["status"], "split")

        parent = self.q.add("Shard Stack M2")
        self.q.release(parent["id"], False, "timeout", split_now=True)
        child = self.q.add("Shard Stack M2 part 1", parent=parent["id"])
        self.q.claim()
        out = self.q.release(child["id"], False, "no model left to try", park=True,
                             stage="model_pool_exhausted")
        self.assertEqual(out["status"], "parked")


class NotScoredAgainstTheModelTests(unittest.TestCase):
    """The bandit learns which models do the work. A step limit is not about the model."""

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

    def worker(self, **kw):
        cfg = {"repo": str(self.root), "base_branch": "main", "test_cmd": "true",
               "models": ["a:free"], "steps": {"implementer": 2, "adversary": 2}, **kw}
        w = swarmd.Worker(0, cfg, swarmd.Queue(), Mock(), threading.Event())
        w.name = "w0"
        return w

    def reinforce(self, stage, info):
        w = self.worker()
        w._reinforce({"id": "t1", "title": "Shard Stack M1", "detail": ""}, stage, info)
        return w.ledger.snapshot()["arms"].get("implementer", {})

    def test_a_turn_that_never_reached_an_edit_is_not_the_models_fault(self):
        arms = self.reinforce("no_change", {"implementer": "qwen/qwen3-coder", "edit_calls": 0})
        self.assertEqual(arms, {}, "the model was scored for our step limit")

    def test_a_model_that_edited_and_changed_nothing_is(self):
        arms = self.reinforce("no_change", {"implementer": "qwen/qwen3-coder", "edit_calls": 2})
        self.assertIn("qwen/qwen3-coder", arms)

    def test_a_timeout_is_not_scored(self):
        self.assertEqual(self.reinforce("agent_timeout", {"implementer": "m:free"}), {})

    def test_a_failed_test_run_still_is(self):
        self.assertIn("m:free", self.reinforce("tests_failed", {"implementer": "m:free"}))

    def test_a_harness_failure_is_journalled_so_it_can_be_counted(self):
        self.reinforce("no_change", {"implementer": "m:free", "edit_calls": 0})
        rows = [json.loads(l) for l in
                (swarmd.STATE / "journal.jsonl").read_text().splitlines() if l.strip()]
        self.assertEqual([r["event"] for r in rows], ["harness_failure"])
        self.assertEqual(rows[0]["stage"], "no_change")


class EditCallCountTests(unittest.TestCase):
    """Whether a turn even tried to edit, read from the turn's own log."""

    def test_edit_and_write_calls_are_counted_per_worker(self):
        swarmd.count_edits("w9", reset=True)
        self.assertEqual(swarmd.count_edits("w9"), 0)
        swarmd.count_edits("w9", 2)
        swarmd.count_edits("w9", 1)
        self.assertEqual(swarmd.count_edits("w9"), 3)
        self.assertEqual(swarmd.count_edits("w9", reset=True), 3)
        self.assertEqual(swarmd.count_edits("w9"), 0)

    def test_a_log_of_reads_only_counts_none(self):
        progress = "tool: list_files\ntool: read_file\ntool: bash\ntool: read_file\n"
        import re
        self.assertEqual(len(re.findall(r"^tool: (?:edit_file|write_file)$", progress, re.M)), 0)

    def test_a_log_with_edits_counts_them(self):
        progress = "tool: read_file\ntool: edit_file\ntool: write_file\ntool: edit_file\n"
        import re
        self.assertEqual(len(re.findall(r"^tool: (?:edit_file|write_file)$", progress, re.M)), 3)

    def test_request_counts_are_attempt_scoped_and_update_the_run(self):
        swarmd._run.clear()
        swarmd.count_requests("w9", reset=True)
        self.assertEqual(swarmd.count_requests("w9", 7), 7)
        self.assertEqual(swarmd._run["requests_started"], 7)
        self.assertEqual(swarmd.count_requests("w9", reset=True), 7)
        self.assertEqual(swarmd.count_requests("w9"), 0)


class RequestBudgetTests(unittest.TestCase):
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
        swarmd.count_requests("w0", reset=True)

    def worker(self, **extra):
        cfg = {"repo": str(self.root), "base_branch": "main", "test_cmd": "true",
               "models": ["a:free"], "steps": {"implementer": 10, "adversary": 2}, **extra}
        worker = swarmd.Worker(0, cfg, swarmd.Queue(), Mock(), threading.Event())
        worker.name = "w0"
        worker.task = {"id": "task"}
        worker.evidence = None
        return worker

    def test_remaining_task_budget_clamps_the_role_rounds(self):
        worker = self.worker(task_request_cap=5)
        swarmd.count_requests("w0", 4)
        seen = []

        def fake(_prompt, _cwd, _cfg, _role, _worker, _budget, steps, _model):
            seen.append(steps)
            return "done"

        with patch.object(swarmd, "flint", side_effect=fake):
            worker.call("implementer", "prompt", self.root, 10, "a:free")
        self.assertEqual(seen, [1])

    def test_spent_task_budget_stops_before_calling_a_model(self):
        worker = self.worker(task_request_cap=5)
        swarmd.count_requests("w0", 5)
        with patch.object(swarmd, "flint") as model, self.assertRaises(swarmd.RequestBudget):
            worker.call("implementer", "prompt", self.root, 10, "a:free")
        model.assert_not_called()

    def test_task_budget_includes_requests_spent_on_prior_attempts(self):
        worker = self.worker(task_request_cap=5)
        worker.task["requests_spent"] = 4
        seen = []

        def fake(_prompt, _cwd, _cfg, _role, _worker, _budget, steps, _model):
            seen.append(steps)
            return "done"

        with patch.object(swarmd, "flint", side_effect=fake):
            worker.call("implementer", "prompt", self.root, 10, "a:free")
        self.assertEqual(seen, [1])


class EmptySplitTests(unittest.TestCase):
    """A split that returns nothing strands the task and everything waiting on it."""

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
        self.q = swarmd.Queue(max_depth=2)

    def worker(self, models=("a:free", "b:free")):
        cfg = {"repo": str(self.root), "base_branch": "main", "test_cmd": "true",
               "models": list(models), "steps": {"decomposer": 4}}
        w = swarmd.Worker(0, cfg, self.q, Mock(), threading.Event())
        w.name = "w0"
        return w

    def task(self):
        """A task that has already been through the queue, as a real split's parent has."""
        t = self.q.add("Rune Garden M1", "the first milestone", priority=1)
        self.q.claim()
        return dict(t, notes=["it failed"])

    def test_an_empty_answer_is_retried_on_another_model(self):
        w = self.worker()
        used = []

        def call(role, prompt, wd, steps, model, avoid=()):
            used.append(model)
            return "[]" if len(used) == 1 else json.dumps(
                [{"title": "Rune Garden M1a", "detail": "half of it", "kind": "feature"}])
        with patch.object(w, "call", side_effect=call), patch.object(swarmd, "refresh_view",
                                                                    return_value=self.root):
            self.assertEqual(w.decompose(self.task(), "goal"), 1)
        self.assertEqual(len(used), 2)
        self.assertNotEqual(used[0], used[1], "the same model was asked twice")

    def test_it_is_retried_only_once(self):
        w = self.worker()
        used = []

        def call(role, prompt, wd, steps, model, avoid=()):
            used.append(model)
            return "[]"
        with patch.object(w, "call", side_effect=call), patch.object(swarmd, "refresh_view",
                                                                    return_value=self.root):
            self.assertEqual(w.decompose(self.task(), "goal"), 0)
        self.assertEqual(len(used), 2)
        rows = [json.loads(l) for l in
                (swarmd.STATE / "journal.jsonl").read_text().splitlines() if l.strip()]
        failed = [r for r in rows if r["event"] == "split_failed"]
        self.assertEqual(len(failed), 1)
        self.assertEqual(sorted(failed[0]["models"]), ["a:free", "b:free"])

    def test_a_model_that_fails_outright_is_retried_too(self):
        w = self.worker()
        used = []

        def call(role, prompt, wd, steps, model, avoid=()):
            used.append(model)
            if len(used) == 1:
                raise swarmd.ModelError("no answer")
            return json.dumps([{"title": "Rune Garden M1a", "detail": "half", "kind": "feature"}])
        with patch.object(w, "call", side_effect=call), patch.object(swarmd, "refresh_view",
                                                                    return_value=self.root):
            self.assertEqual(w.decompose(self.task(), "goal"), 1)
        self.assertEqual(len(used), 2)

    def test_one_model_in_the_pool_is_not_asked_twice(self):
        w = self.worker(models=["a:free"])
        used = []

        def call(role, prompt, wd, steps, model, avoid=()):
            used.append(model)
            return "[]"
        with patch.object(w, "call", side_effect=call), patch.object(swarmd, "refresh_view",
                                                                    return_value=self.root):
            self.assertEqual(w.decompose(self.task(), "goal"), 0)
        self.assertEqual(used, ["a:free"])


if __name__ == "__main__":
    unittest.main()
