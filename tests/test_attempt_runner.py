"""AttemptRunner.run is the single application operation: one attempt, one typed outcome.

The runner is exercised without a real model, git repo or filesystem state: a fake worker
provides exactly what `run` touches, and every AttemptDeps Callable is a MagicMock.
"""
import types
import unittest
from dataclasses import fields
from unittest.mock import MagicMock

# swarmd is imported first: its top-level `from learn import ...`/`from metrics import ...`
# imports resolve only because importing it puts swarm/ on sys.path (no swarm/__init__.py).
from swarm import swarmd  # noqa: F401  (import ordering matters)
from swarm import attempt_runner


def build_deps():
    kwargs = {}
    for f in fields(attempt_runner.AttemptDeps):
        if f.name == "state":
            kwargs[f.name] = "/tmp/nonexistent-state"
        elif f.name == "ran_on":
            kwargs[f.name] = {}
        elif f.name == "run":
            kwargs[f.name] = {}
        elif f.name == "view_lock":
            kwargs[f.name] = object()
        elif f.name == "run_lock":
            kwargs[f.name] = object()
        elif f.name == "read_only_roles":
            kwargs[f.name] = ()
        elif f.name == "kinds":
            kwargs[f.name] = {}
        else:
            kwargs[f.name] = MagicMock()
    kwargs["count_requests"].return_value = 0
    kwargs["count_usd"].return_value = 0.0
    kwargs["count_study"].return_value = 0
    return attempt_runner.AttemptDeps(**kwargs)


def build_worker(mode="accept"):
    """A fake Worker exposing just what `run` touches."""
    calls = {"reinforce": 0}

    def _attempt_inputs(c, b):
        return c, b

    def _attempt(task, goal, info, c, b):
        if mode == "timeout":
            raise attempt_runner.AgentTimeout("reviewer", "m", 300, "log")
        if mode == "explored":
            raise attempt_runner.ExplorationExhausted("no edits")
        if mode == "failed":
            return "tests_failed", "boom", {"implementer": "m"}
        if mode == "cap":
            raise attempt_runner.CapReached("cap")
        return "accepted", "ok", {"implementer": "m", "commit": "abc"}

    def _cleanup(c):
        return None

    def _reinforce(*args, **kwargs):
        calls["reinforce"] += 1

    return types.SimpleNamespace(
        name="w", branch="swarm/w", evidence=None, stage=None, failure_class="task",
        role_calls=0, attempt_requests=0, task=None, wt=None, doing=None, started=None,
        worktree_provisioned=False, last_model=None, last_role=None, mit=None,
        agent_timed_out=False, execution_class="standard",
        _attempt_inputs=_attempt_inputs, _attempt=_attempt, _cleanup=_cleanup,
        _reinforce=_reinforce, _calls=calls,
    )


class AttemptRunnerRunTests(unittest.TestCase):
    def run_one(self, mode="accept"):
        worker = build_worker(mode)
        runner = attempt_runner.AttemptRunner(worker, build_deps())
        outcome = runner.run({"id": "t1", "title": "T"}, "goal", {"k": "v"})
        return worker, outcome

    def test_accepted_outcome(self):
        worker, outcome = self.run_one("accept")
        self.assertTrue(outcome.accepted)
        self.assertIsNone(outcome.failure_class)
        self.assertEqual(outcome.commit, "abc")
        self.assertEqual(outcome.implementer, "m")
        self.assertEqual(outcome.requests, 0)
        self.assertIsNone(outcome.evidence_path)
        self.assertEqual(worker._calls["reinforce"], 1)

    def test_agent_timeout_is_harness(self):
        worker, outcome = self.run_one("timeout")
        self.assertEqual(outcome.stage, "agent_timeout")
        self.assertEqual(outcome.failure_class, "harness")

    def test_exploration_exhausted_is_harness(self):
        worker, outcome = self.run_one("explored")
        self.assertEqual(outcome.stage, "exploration_exhausted")
        self.assertEqual(outcome.failure_class, "harness")

    def test_failed_attempt_is_task_and_reinforces(self):
        worker, outcome = self.run_one("failed")
        self.assertEqual(outcome.stage, "tests_failed")
        self.assertEqual(outcome.failure_class, "task")
        self.assertEqual(worker._calls["reinforce"], 1)

    def test_cap_reached_propagates_and_defers(self):
        worker = build_worker("cap")
        runner = attempt_runner.AttemptRunner(worker, build_deps())
        with self.assertRaises(attempt_runner.CapReached):
            runner.run({"id": "t1", "title": "T"}, "goal", {"k": "v"})
        self.assertEqual(worker.stage, "deferred")

    def test_produced_stages_are_in_stages(self):
        for mode in ("accept", "timeout", "explored", "failed"):
            _, outcome = self.run_one(mode)
            self.assertIn(outcome.stage, attempt_runner.STAGES)


class ReinforceTests(unittest.TestCase):
    """_reinforce ends by journaling the reward and logging one summary line.

    A P9c slice once split that last log line off onto the end of the next method; the
    suite stayed green because nothing pinned it."""

    def test_scored_attempt_journals_and_logs_its_reward(self):
        ledger = MagicMock()
        ledger.accepted_texts.return_value = []
        ledger.accepted_rewards.return_value = []
        worker = types.SimpleNamespace(
            name="w", ledger=ledger, _attempt_inputs=lambda c, b: (c, b),
            celebrate=MagicMock(), follow_up=MagicMock(), hang=MagicMock())
        deps = build_deps()
        deps.read_goal.return_value = ""
        runner = attempt_runner.AttemptRunner(worker, deps)
        info = {"implementer": "m", "scores": {"impact": 3, "quality": 3, "creativity": 3}}
        runner._reinforce({"id": "t1", "title": "T"}, "accepted", info, {"k": "v"})
        events = [call.args[0] for call in deps.journal.call_args_list]
        self.assertIn("reward", events)
        last = deps.log.call_args_list[-1]
        self.assertRegex(last.args[0], r"^t1: accepted, reward \d\.\d\d")
        self.assertEqual(last.args[1], "w")


if __name__ == "__main__":
    unittest.main()