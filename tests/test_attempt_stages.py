"""P0: the `_attempt` stage sequence and fault behavior.

Records the exact stage strings a Worker._attempt returns for its main exits,
so P9 (AttemptRunner) has a byte-identical reference. Stages are not open for
invention — failure_class(), TASK_STAGES, HARNESS_STAGES and every consumer
depend on them.

The full attempt path runs against a real temp git repo with a real worktree
and a fake flint, exactly as test_swarm_learning does: `do_task` exercises the
whole validate → worktree → baseline → implement → review → integrate cycle.
"""
import json
import re
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from swarm import swarmd


def review(prompt, verdict="approve"):
    tree = re.search(r"GIT TREE TO REVIEW: (\w+)", prompt).group(1)
    ids = dict.fromkeys(re.findall(r'"id": "(C\d+)"', prompt))
    approve = verdict == "approve"
    return json.dumps({"verdict": verdict, "tree": tree, "summary": "reviewed",
                       "checks": [{"criterion": i, "passed": approve, "evidence": "tests"}
                                  for i in ids],
                       "findings": [] if approve else [{"severity": "blocker", "path": "app.txt",
                                                        "line": 1, "issue": "defect",
                                                        "verification": "repro"}],
                       "lesson": "keep it simple"})


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


class RepoCase(unittest.TestCase):
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
        self.repo = self.root / "repo"
        self.repo.mkdir()
        _git(self.repo, "init", "-q", "-b", "main")
        _git(self.repo, "config", "user.email", "t@example.invalid")
        _git(self.repo, "config", "user.name", "T")
        (self.repo / "app.txt").write_text("baseline\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", "baseline")
        swarmd._run.clear()
        swarmd._stop.clear()

    def cfg(self, **kw):
        return {"repo": str(self.repo), "base_branch": "main", "test_cmd": "true",
                "models": ["a:free"],
                "steps": {"architect": 0, "implementer": 2, "adversary": 2,
                          "judge": 1, "decomposer": 1},
                **kw}

    def worker(self, c=None, q=None):
        w = swarmd.Worker(0, c or self.cfg(), q or swarmd.Queue(), Mock(),
                          threading.Event())
        w.name = "w0"
        return w


class AttemptStageTests(RepoCase):
    def task(self, **kw):
        return {"id": "t1", "title": "Add a settings screen",
                "detail": "show the volume slider", "kind": "feature",
                "origin": "human", "acceptance": ["the slider appears"], **kw}

    def test_a_harness_kind_task_is_refused(self):
        w = self.worker()
        task = self.task(kind="harness")
        ok, note = w.do_task(task, "goal")
        self.assertFalse(ok)
        self.assertEqual(w.stage, "refused")
        self.assertIn("separate harness checkout", note)

    def test_an_out_of_scope_task_is_refused(self):
        w = self.worker()
        task = {"id": "t2", "title": "clean up the whole codebase as needed",
                "detail": "", "kind": "feature", "origin": "human",
                "acceptance": ["clean up the whole codebase as needed"]}
        ok, note = w.do_task(task, "goal")
        self.assertFalse(ok)
        self.assertEqual(w.stage, "out_of_scope")
        self.assertIn("open-ended", note)

    def test_an_accepted_task_lands_on_trunk(self):
        """The happy path: implement → gate → review → integrate → accepted."""
        c = self.cfg()
        q = swarmd.Queue()
        task = self.task()

        def fake(prompt, cwd, c_, role, *a):
            if role == "implementer":
                (cwd / "feature.txt").write_text("settings\n")
                return "done"
            if role == "adversary":
                return review(prompt)
            return "done"

        w = self.worker(c, q)
        with patch.object(swarmd, "flint", side_effect=fake):
            ok, note = w.do_task(task, "goal")
        self.assertTrue(ok, note)
        self.assertEqual(w.stage, "accepted")
        self.assertIn("integrated", note)
        # The work is on swarm/trunk and main is untouched.
        self.assertEqual(_git(self.repo, "show", "swarm/trunk:feature.txt"), "settings")
        self.assertEqual(_git(self.repo, "rev-parse", "main"), self.base_commit())

    def base_commit(self):
        return _git(self.repo, "rev-parse", "HEAD")

    def test_scope_gate_can_be_disabled(self):
        c = self.cfg(scope_gate=False)
        task = {"id": "t3", "title": "clean up the whole codebase as needed",
                "detail": "", "kind": "feature", "origin": "human",
                "acceptance": ["clean up the whole codebase as needed"]}

        def fake(prompt, cwd, c_, role, *a):
            if role == "implementer":
                (cwd / "app.txt").write_text("cleaned\n")
                return "done"
            if role == "adversary":
                return review(prompt)
            return "done"

        w = self.worker(c)
        with patch.object(swarmd, "flint", side_effect=fake):
            ok, note = w.do_task(task, "goal")
        self.assertTrue(ok, note)
        self.assertEqual(w.stage, "accepted")

    def test_the_frozen_stage_vocabulary_is_closed(self):
        """The stages an attempt can exit with are a frozen, non-inventable set.

        P0 pins the vocabulary from the work order's Contracts section; P9 turns it
        into AttemptOutcome.stage and every consumer depends on the exact strings.
        """
        stages = {"accepted", "refused", "out_of_scope", "baseline", "conflict",
                  "rejected", "tests_failed", "weakened_tests", "no_change",
                  "agent_timeout", "model_error", "review_error", "review_format_error",
                  "model_pool_exhausted", "exploration_exhausted", "request_budget",
                  "interrupted", "deferred"}
        # Every stage maps to a failure_class via the pure function.
        for stage in stages:
            self.assertIn(swarmd.failure_class(stage),
                          ("task", "model", "harness", "review_format"))
        # The task stages are the ones that count against the task.
        self.assertEqual(swarmd.TASK_STAGES,
                         frozenset({"tests_failed", "rejected", "weakened_tests", "out_of_scope"}))


if __name__ == "__main__":
    unittest.main()
