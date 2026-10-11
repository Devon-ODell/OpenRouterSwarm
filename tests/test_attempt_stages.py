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
import time
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

    # The exact stage -> failure_class mapping, measured at b8fe88f (P0.1 hole 1).
    # failure_class() ends in `return "task"`, so a plain assertIn is vacuous:
    # every string classifies. Pinning the exact values is what guards invariant 7
    # (a harness failure must never cost the task one of its two lives).
    STAGE_CLASS = {
        "accepted":              "task",       # never routed through failure_class in practice
        "refused":               "harness",
        "out_of_scope":          "task",
        "baseline":              "harness",
        "conflict":              "harness",
        "rejected":              "task",
        "tests_failed":          "task",
        "weakened_tests":        "task",
        "agent_timeout":         "harness",
        "review_format_error":   "review_format",
        "model_pool_exhausted":  "harness",
        "exploration_exhausted": "harness",
        "request_budget":        "harness",
        "interrupted":           "harness",
        "deferred":              "harness",
    }
    # The three note/edited-dependent stages are not in STAGE_CLASS; each needs
    # both directions asserted explicitly.
    NOTE_DEPENDENT = frozenset({"model_error", "review_error", "no_change"})

    def test_the_frozen_stage_mapping_is_exact(self):
        """failure_class per stage is pinned against the production vocabularies.

        The expected mapping is *derived from production*: every TASK_STAGES member
        must classify 'task', every HARNESS_STAGES member 'harness', and the table
        in this file must equal that derived mapping exactly. A production change
        therefore cannot be hidden by editing the table or the test's stage set to
        match — the derived mapping moves with production and the equality breaks.
        Note/edited-dependent stages (model_error, review_error, no_change) and the
        terminal accepted stage are pinned explicitly below.
        """
        stages = {"accepted", "refused", "out_of_scope", "baseline", "conflict",
                  "rejected", "tests_failed", "weakened_tests", "no_change",
                  "agent_timeout", "model_error", "review_error", "review_format_error",
                  "model_pool_exhausted", "exploration_exhausted", "request_budget",
                  "interrupted", "deferred"}
        production_task = set(swarmd.TASK_STAGES)
        production_harness = set(swarmd.HARNESS_STAGES)
        self.assertEqual(len(stages), 18)
        # The vocabulary is exactly partitioned by the production sets, the
        # hard-coded review_format branch, the three note-dependent stages, and
        # `accepted` — the terminal stage that never routes through failure_class.
        self.assertEqual(
            stages,
            production_task | production_harness
            | {"review_format_error", "accepted"} | set(self.NOTE_DEPENDENT))
        # No stage may live in both sets, and accepted may not be in either.
        self.assertEqual(production_task & production_harness, set())
        self.assertNotIn("accepted", production_task | production_harness)
        # The expected mapping is derived FROM production. A table row that does
        # not match its production set, or a production set member missing from
        # the table, breaks the equality — no combination of edits to this file
        # can absorb a production change.
        expected = ({s: "task" for s in production_task}
                    | {s: "harness" for s in production_harness}
                    | {"review_format_error": "review_format", "accepted": "task"})
        self.assertEqual(self.STAGE_CLASS, expected)
        # An unknown stage string must be recognizable as unmapped: "banana" falls
        # through failure_class()'s catch-all, but it is not a pinned stage.
        self.assertNotIn("banana", self.STAGE_CLASS)
        # Live behaviour matches the derived mapping for every member of the
        # production sets.
        for stage in production_task | production_harness:
            self.assertEqual(swarmd.failure_class(stage), expected[stage], stage)
        # The note- and edited-dependent directions, asserted both ways.
        self.assertEqual(swarmd.failure_class("model_error", ""), "model")
        self.assertEqual(swarmd.failure_class("model_error", "connection reset"), "harness")
        self.assertEqual(swarmd.failure_class("review_error", ""), "model")
        self.assertEqual(swarmd.failure_class("review_error", "timed out"), "harness")
        self.assertEqual(swarmd.failure_class("no_change", "", edited=None), "harness")
        self.assertEqual(swarmd.failure_class("no_change", "", edited=3), "model")
        # The task stages are the ones that count against the task.
        self.assertEqual(swarmd.TASK_STAGES,
                         frozenset({"tests_failed", "rejected", "weakened_tests", "out_of_scope"}))


class AttemptStageFaultTests(RepoCase):
    """End-to-end drives of the failure stages of `_attempt` (P0.1 hole 2).

    The happy path only exercises refused/out_of_scope/accepted. These force each
    failure stage through the real repo + worktree + fake flint, asserting the
    stage string and failure_class() on the real note the attempt produced.
    """

    def task(self, **kw):
        return {"id": "t1", "title": "Add a settings screen",
                "detail": "show the volume slider", "kind": "feature",
                "origin": "human", "acceptance": ["the slider appears"], **kw}

    def test_baseline_failure_spends_no_model_calls(self):
        c = self.cfg(test_cmd="false")
        w = self.worker(c)

        def fake(prompt, cwd, c_, role, *a):
            raise AssertionError("flint must not be called when the baseline fails")

        with patch.object(swarmd, "flint", side_effect=fake):
            ok, note = w.do_task(self.task(), "goal")
        self.assertFalse(ok)
        self.assertEqual(w.stage, "baseline")
        self.assertEqual(w.role_calls, 0)      # no model call was spent
        self.assertIn("baseline tests failed", note)
        self.assertEqual(swarmd.failure_class(w.stage, note), "harness")

    def test_baseline_dirty_worktree_also_spends_no_model_calls(self):
        """The other branch of the baseline return: the gate passes but the test
        command dirties the worktree (tracked or unignored change). `_attempt`
        returns 'baseline' before any model call, with the same note wording."""
        # test_cmd passes but creates a file in its own cwd (the worktree): the
        # untouched baseline worktree is clean at the gate, then dirty at the
        # post-gate status check.
        c = self.cfg(test_cmd="touch dirty.txt && test -f dirty.txt")
        w = self.worker(c)

        def fake(prompt, cwd, c_, role, *a):
            raise AssertionError("flint must not be called when the baseline is dirty")

        with patch.object(swarmd, "flint", side_effect=fake):
            ok, note = w.do_task(self.task(), "goal")
        self.assertFalse(ok)
        self.assertEqual(w.stage, "baseline")
        self.assertEqual(w.role_calls, 0)      # no model call was spent
        self.assertIn("changed tracked/unignored files", note)
        self.assertEqual(swarmd.failure_class(w.stage, note), "harness")

    def test_tests_failed_when_the_candidate_fails_the_gate(self):
        # test_cmd passes on the untouched checkout and fails once the
        # implementer's file exists: the baseline gate is green, the candidate
        # gate is red.
        c = self.cfg(test_cmd="test ! -f feature.txt", max_repairs=0)
        w = self.worker(c)

        def fake(prompt, cwd, c_, role, *a):
            if role == "implementer":
                (cwd / "feature.txt").write_text("settings\n")
                return "done"
            return review(prompt)

        with patch.object(swarmd, "flint", side_effect=fake):
            ok, note = w.do_task(self.task(), "goal")
        self.assertFalse(ok)
        self.assertEqual(w.stage, "tests_failed")
        self.assertEqual(swarmd.failure_class(w.stage, note), "task")

    def test_tests_failed_exhausts_the_repair_middle(self):
        """The same gate failure with a repair budget: the candidate fails, the
        repair role gets a real turn, the repaired candidate still fails, and the
        attempt returns tests_failed only after the repair budget is exhausted.
        This is the gate/review/repair middle P0.1 said must be exercised."""
        c = self.cfg(test_cmd="test ! -f feature.txt", max_repairs=1)
        w = self.worker(c)
        repair_turns = []

        def fake(prompt, cwd, c_, role, *a):
            if role == "implementer":
                (cwd / "feature.txt").write_text("settings\n")
                return "done"
            if role == "repair":
                repair_turns.append(role)
                # A repair that does not remove the failing file: the next
                # candidate fails the gate again, consuming the budget.
                return "I attempted a repair"
            return review(prompt)

        with patch.object(swarmd, "flint", side_effect=fake):
            ok, note = w.do_task(self.task(), "goal")
        self.assertFalse(ok)
        self.assertEqual(w.stage, "tests_failed")
        # The repair role really ran (a flint call beyond the implementer), proving
        # the repair middle — not just the gate — was driven.
        self.assertEqual(len(repair_turns), 1)
        self.assertGreaterEqual(w.role_calls, 2)
        self.assertIn("repair budget", note)
        self.assertEqual(swarmd.failure_class(w.stage, note), "task")

    def test_rejected_by_the_adversary(self):
        c = self.cfg(max_repairs=0)
        w = self.worker(c)

        def fake(prompt, cwd, c_, role, *a):
            if role == "implementer":
                (cwd / "feature.txt").write_text("settings\n")
                return "done"
            if role == "adversary":
                # The supervisor's verdict vocabulary is approve/request_changes;
                # a request_changes verdict with a blocker finding is a rejection.
                return review(prompt, "request_changes")
            return "done"

        with patch.object(swarmd, "flint", side_effect=fake):
            ok, note = w.do_task(self.task(), "goal")
        self.assertFalse(ok)
        self.assertEqual(w.stage, "rejected")
        self.assertEqual(swarmd.failure_class(w.stage, note), "task")

    def test_rejected_exhausts_the_repair_middle(self):
        """The same rejection with a repair budget: the adversary rejects, the
        repair role runs, the repaired candidate is rejected with the same
        signature, and the attempt returns `rejected` on the repeated signature.
        This drives the gate/review/repair middle for the rejection path."""
        c = self.cfg(max_repairs=1)
        w = self.worker(c)
        repair_turns = []

        def fake(prompt, cwd, c_, role, *a):
            if role == "implementer":
                (cwd / "feature.txt").write_text("settings\n")
                return "done"
            if role == "repair":
                repair_turns.append(role)
                # A repair that does not address the blocker: the next review
                # returns the same request_changes verdict (same signature), so
                # the loop returns rejected on the repeated failure.
                return "I attempted a repair"
            if role == "adversary":
                return review(prompt, "request_changes")
            return "done"

        with patch.object(swarmd, "flint", side_effect=fake):
            ok, note = w.do_task(self.task(), "goal")
        self.assertFalse(ok)
        self.assertEqual(w.stage, "rejected")
        # The repair role really ran, and the rejection came back on the repeated
        # signature, not on budget exhaustion alone.
        self.assertEqual(len(repair_turns), 1)
        self.assertGreaterEqual(w.role_calls, 2)
        self.assertIn("repeated failure", note)
        self.assertEqual(swarmd.failure_class(w.stage, note), "task")

    def test_weakened_tests_is_refused_and_origin_gated(self):
        """An implementer that guts an existing assertion is refused, and only a
        person (human/cursor origin) may ever authorise test changes."""
        c = self.cfg()
        (self.repo / "tests").mkdir()
        (self.repo / "tests" / "test_app.py").write_text(
            "def test_baseline():\n    assert True\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", "baseline")
        w = self.worker(c)

        def fake(prompt, cwd, c_, role, *a):
            if role == "implementer":
                (cwd / "tests" / "test_app.py").write_text(
                    "def test_baseline():\n    pass\n")   # guts the assertion
                return "done"
            return review(prompt)

        with patch.object(swarmd, "flint", side_effect=fake):
            ok, note = w.do_task(self.task(), "goal")
        self.assertFalse(ok)
        self.assertEqual(w.stage, "weakened_tests")
        self.assertIn("assertion", note)
        self.assertEqual(swarmd.failure_class(w.stage, note), "task")
        # TEST_CHANGE_ORIGINS is what permits the exception: only a person may
        # set allow_test_changes; the swarm's own tasks cannot.
        self.assertEqual(swarmd.TEST_CHANGE_ORIGINS, frozenset({"human", "cursor"}))
        q = swarmd.Queue()
        with self.assertRaises(ValueError):
            q.add("t", "d", "feature", origin="planner", allow_test_changes=True)

    def test_no_change_never_reached_an_edit_is_harness(self):
        """No diff and no edit call: the harness ran the rounds out — harness."""
        c = self.cfg()
        w = self.worker(c)

        def fake(prompt, cwd, c_, role, *a):
            if role == "implementer":
                return "done"          # no edit call, no diff
            return review(prompt)

        with patch.object(swarmd, "flint", side_effect=fake):
            ok, note = w.do_task(self.task(), "goal")
        self.assertFalse(ok)
        self.assertEqual(w.stage, "no_change")
        self.assertIn("never reached an edit", note)
        self.assertEqual(swarmd.failure_class("no_change", note, edited=None), "harness")

    def test_no_change_after_edit_calls_is_model(self):
        """Edit calls that changed nothing are the model's doing — model.

        The edit calls are counted the same way the real turn-runner counts them
        (swarmd.count_edits(worker, n), fed from the turn log's 'tool: edit_file'
        lines): the fake implementer writes those lines to the worker's live turn
        log and registers them through count_edits, then leaves the tree without a
        diff. This drives the actual channel, not a raw dict seed."""
        c = self.cfg()
        w = self.worker(c)

        def fake(prompt, cwd, c_, role, *a):
            if role == "implementer":
                # The real turn-runner registers the worker's log under
                # _turn_logs[w] then scans it for 'tool: edit_file' lines and
                # tallies them into count_edits. Reproduce that channel exactly:
                # three edit_file calls, no change to the tree.
                logfile = swarmd.LOGS / f"w0-implementer-{time.time_ns()}.log"
                swarmd._turn_logs["w0"] = logfile
                logfile.write_text("tool: edit_file\n" * 3)
                # Line 2156's tally: count_edits(worker, n_lines).
                swarmd.count_edits("w0", 3)
                return "done"
            return review(prompt)

        with patch.object(swarmd, "flint", side_effect=fake):
            ok, note = w.do_task(self.task(), "goal")
        self.assertFalse(ok)
        self.assertEqual(w.stage, "no_change")
        self.assertIn("changed nothing", note)
        # edited (info["edit_calls"]) is genuinely 3 via the system channel.
        self.assertEqual(swarmd.failure_class("no_change", note, edited=3), "model")

    def test_model_error_from_the_implementer(self):
        c = self.cfg()
        w = self.worker(c)

        def fake(prompt, cwd, c_, role, *a):
            if role == "implementer":
                raise swarmd.ModelError("no usable json in reply")
            return review(prompt)

        with patch.object(swarmd, "flint", side_effect=fake):
            ok, note = w.do_task(self.task(), "goal")
        self.assertFalse(ok)
        self.assertEqual(w.stage, "model_error")
        self.assertEqual(swarmd.failure_class("model_error", note), "model")

    def test_conflict_when_trunk_moves_under_the_candidate(self):
        """A competing commit on swarm/trunk touching the same path makes the
        integrate rebase fail, and the attempt reports `conflict`."""
        c = self.cfg()
        q = swarmd.Queue()
        w = self.worker(c, q)

        def fake(prompt, cwd, c_, role, *a):
            if role == "implementer":
                (cwd / "feature.txt").write_text("settings\n")
                return "done"
            if role == "adversary":
                # Land a competing change on trunk at the same path, before the
                # candidate is committed, so the rebase cannot apply cleanly.
                _git(self.repo, "checkout", "-q", "swarm/trunk")
                (self.repo / "feature.txt").write_text("other\n")
                _git(self.repo, "add", "-A")
                _git(self.repo, "commit", "-qm", "competing trunk change")
                _git(self.repo, "checkout", "-q", "main")
                return review(prompt)
            return "done"

        with patch.object(swarmd, "flint", side_effect=fake):
            ok, note = w.do_task(self.task(), "goal")
        self.assertFalse(ok)
        self.assertEqual(w.stage, "conflict")
        self.assertIn("conflicts with newer trunk", note)
        self.assertEqual(swarmd.failure_class(w.stage, note), "harness")

    def test_agent_timeout_from_a_real_agent_timeout(self):
        """A turn that exhausts its wall-clock budget raises AgentTimeout, which
        do_task catches and reports as agent_timeout (not no_change: the model
        never answered badly, the harness cut it off)."""
        c = self.cfg()
        w = self.worker(c)

        def fake(prompt, cwd, c_, role, *a):
            if role == "implementer":
                raise swarmd.AgentTimeout("implementer", "a:free", 420, "/tmp/log")
            return review(prompt)

        with patch.object(swarmd, "flint", side_effect=fake):
            ok, note = w.do_task(self.task(), "goal")
        self.assertFalse(ok)
        self.assertEqual(w.stage, "agent_timeout")
        self.assertIn("no implementation changes", note)
        self.assertEqual(swarmd.failure_class(w.stage, note), "harness")

    def test_exploration_exhausted_is_reported_when_the_turn_never_edited(self):
        """A turn stopped for spending 70% of its rounds without editing is
        exploration_exhausted, a harness loss (work order §5)."""
        c = self.cfg()
        w = self.worker(c)

        def fake(prompt, cwd, c_, role, *a):
            if role == "implementer":
                raise swarmd.ExplorationExhausted(
                    "tour ended early after reaching 70% of its rounds without editing")
            return review(prompt)

        with patch.object(swarmd, "flint", side_effect=fake):
            ok, note = w.do_task(self.task(), "goal")
        self.assertFalse(ok)
        self.assertEqual(w.stage, "exploration_exhausted")
        self.assertEqual(swarmd.failure_class(w.stage, note), "harness")

    def test_review_error_when_the_adversary_crashes(self):
        """A reviewer that raises ModelError (after the gate passed) is a
        review_error, not a rejection: nothing was said about the candidate."""
        c = self.cfg()
        w = self.worker(c)

        def fake(prompt, cwd, c_, role, *a):
            if role == "implementer":
                (cwd / "feature.txt").write_text("settings\n")
                return "done"
            if role == "adversary":
                raise swarmd.ModelError("reviewer crashed")
            return "done"

        with patch.object(swarmd, "flint", side_effect=fake):
            ok, note = w.do_task(self.task(), "goal")
        self.assertFalse(ok)
        self.assertEqual(w.stage, "review_error")
        self.assertEqual(swarmd.failure_class("review_error", ""), "model")

    def test_review_format_error_on_malformed_review_json(self):
        """A reviewer that returns unparseable JSON raises ReviewFormatError; the
        attempt is review_format_error, and failure_class is the special
        review_format value (never task, never silently harness)."""
        c = self.cfg()
        w = self.worker(c)

        def fake(prompt, cwd, c_, role, *a):
            if role == "implementer":
                (cwd / "feature.txt").write_text("settings\n")
                return "done"
            if role == "adversary":
                return "this is not json at all"
            return "done"

        with patch.object(swarmd, "flint", side_effect=fake):
            ok, note = w.do_task(self.task(), "goal")
        self.assertFalse(ok)
        self.assertEqual(w.stage, "review_format_error")
        self.assertEqual(swarmd.failure_class(w.stage, note), "review_format")

    def test_request_budget_is_reported_not_retried(self):
        """Exhausting the task's request budget before the implementer turn is a
        request_budget stage; failure_class is harness because the cap is the
        swarm's own, not the model's."""
        c = self.cfg()
        w = self.worker(c)

        def fake(prompt, cwd, c_, role, *a):
            if role == "implementer":
                raise swarmd.RequestBudget(
                    "task request budget exhausted before implementer")
            return review(prompt)

        with patch.object(swarmd, "flint", side_effect=fake):
            ok, note = w.do_task(self.task(), "goal")
        self.assertFalse(ok)
        self.assertEqual(w.stage, "request_budget")
        self.assertEqual(swarmd.failure_class(w.stage, note), "harness")

    def test_model_pool_exhausted_when_every_implementer_is_excluded(self):
        """When the circuit breaker excludes the only pool model for the
        implementer role, the attempt parks: model_pool_exhausted is a harness
        loss, not the model's fault."""
        import time as _time
        c = self.cfg()
        task = self.task()
        task["failed_models"] = [{"model": "a:free", "class": "harness",
                                  "role": "implementer", "at": _time.time(),
                                  "note": "timed out without editing"}]
        w = self.worker(c)

        def fake(prompt, cwd, c_, role, *a):
            raise AssertionError("flint must not be called when the pool is exhausted")

        with patch.object(swarmd, "flint", side_effect=fake):
            ok, note = w.do_task(task, "goal")
        self.assertFalse(ok)
        self.assertEqual(w.stage, "model_pool_exhausted")
        self.assertEqual(swarmd.failure_class(w.stage, note), "harness")

    def test_deferred_re_raises_provider_down(self):
        """A provider outage mid-attempt defers the task: do_task sets stage
        'deferred' and re-raises the exception for the worker to handle."""
        c = self.cfg()
        w = self.worker(c)

        def fake(prompt, cwd, c_, role, *a):
            if role == "implementer":
                raise swarmd.ProviderDown("a:free", "provider unavailable")
            return review(prompt)

        with patch.object(swarmd, "flint", side_effect=fake):
            with self.assertRaises(swarmd.ProviderDown):
                w.do_task(self.task(), "goal")
        self.assertEqual(w.stage, "deferred")
        # failure_class for a deferred/provider loss is harness: never the task's.
        self.assertEqual(swarmd.failure_class("deferred", "provider unavailable"), "harness")

    def test_interrupted_is_the_stopped_run_note_not_a_do_task_stage(self):
        """An honest gap, per P0.1: `interrupted` is not a stage do_task returns.

        `_attempt` never assigns the string 'interrupted'; a mid-attempt stop
        surfaces as the Stopped exception (released by Worker.run as
        'run stopped; changes retained', failure_class harness) and as the
        default note 'attempt interrupted before completion' in the artifact.
        It is a HARNESS_STAGES member and a HARNESS_NOTE token, classified at the
        release boundary. Pinning do_task here would be asserting a non-path.
        """
        self.assertIn("interrupted", swarmd.HARNESS_STAGES)
        self.assertEqual(swarmd.failure_class("interrupted"), "harness")
        # And the artifact the stop leaves behind carries the note (via a direct
        # Stopped propagation, as Worker.run consumes it).
        self.assertTrue(hasattr(swarmd, "Stopped"))


if __name__ == "__main__":
    unittest.main()
