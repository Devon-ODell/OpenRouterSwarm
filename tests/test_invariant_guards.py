"""Guards for the landing invariants that had no test failing when they were removed.

DECOMPOSE-SWARMD.md: "Each must keep a test that fails if its guard is removed." A mutation
pass after P13 deleted each guard in turn; for these the whole suite stayed green:

  2. a test command that changes the candidate gets it rejected (review sees the exact tree)
  3. the committed tree must equal the reviewed tree before trunk moves
  4. a rebase onto a newer trunk triggers new verification AND a new review
  and flint's exit code 11 (no-edit exhaustion) translating to ExplorationExhausted.
"""
import contextlib
import subprocess
import unittest
from unittest.mock import Mock, patch

from swarm import swarmd, workspace
from tests.test_attempt_stages import RepoCase, _git, review


def _task():
    return {"id": "t1", "title": "Add a settings screen", "detail": "show the volume slider",
            "kind": "feature", "origin": "human", "acceptance": ["the slider appears"]}


def _move_trunk_independently(repo):
    """Land an unrelated commit on swarm/trunk, as another worker would."""
    _git(repo, "checkout", "-q", "swarm/trunk")
    (repo / "other.txt").write_text("other\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "independent trunk change")
    _git(repo, "checkout", "-q", "main")


class CandidateIntegrityTests(RepoCase):
    def test_a_test_command_that_changes_the_candidate_gets_it_rejected(self):
        """Invariant 2: the reviewer must see the tree the tests ran on. A gate that edits a
        tracked file makes the tested tree differ from the snapshot, so it never reaches review."""
        c = self.cfg(test_cmd="if [ -f feature.txt ]; then echo extra >> feature.txt; fi")
        reviewed = []

        def fake(prompt, cwd, c_, role, *a):
            if role == "implementer":
                (cwd / "feature.txt").write_text("settings\n")
                return "done"
            if role == "adversary":
                reviewed.append(prompt)
                return review(prompt)
            return "done"

        w = self.worker(c)
        with patch.object(swarmd, "flint", side_effect=fake):
            ok, note = w.do_task(_task(), "goal")
        self.assertFalse(ok)
        self.assertEqual(w.stage, "rejected")
        self.assertIn("test command modified the candidate", note)
        self.assertEqual(reviewed, [])
        self.assertNotIn("feature.txt", _git(self.repo, "ls-tree", "--name-only", "swarm/trunk"))

    def test_trunk_does_not_move_when_the_commit_is_not_the_reviewed_tree(self):
        """Invariant 3: compare-and-swap refuses a candidate whose committed tree differs from
        the tree the adversary approved, and leaves trunk where it was."""
        c = self.cfg()
        swarmd.ensure_trunk(c)
        old = _git(self.repo, "rev-parse", "swarm/trunk")
        wt = self.root / "wt"
        _git(self.repo, "worktree", "add", "-q", "-b", "swarm/cand", str(wt), "swarm/trunk")
        self.addCleanup(lambda: subprocess.run(["git", "-C", str(self.repo), "worktree", "remove",
                                                "--force", str(wt)], capture_output=True))
        (wt / "feature.txt").write_text("reviewed\n")
        _git(wt, "add", "-A")
        _git(wt, "commit", "-qm", "reviewed candidate")
        reviewed_tree = _git(wt, "rev-parse", "HEAD^{tree}")
        (wt / "feature.txt").write_text("changed after review\n")
        _git(wt, "commit", "-qam", "slipped in after review")

        status, note = workspace.land_on_trunk(
            c, wt, old, reviewed_tree,
            git=swarmd.git, locked=swarmd.trunk_locked, checked_out_at=swarmd.checked_out_at)
        self.assertEqual(status, "mismatch", note)
        self.assertEqual(_git(self.repo, "rev-parse", "swarm/trunk"), old)

        # The same call with the tree that was actually committed does land: the refusal above
        # is the tree check, not something else about this setup.
        status, _ = workspace.land_on_trunk(
            c, wt, old, _git(wt, "rev-parse", "HEAD^{tree}"),
            git=swarmd.git, locked=swarmd.trunk_locked, checked_out_at=swarmd.checked_out_at)
        self.assertEqual(status, "landed")


class RebaseReverificationTests(RepoCase):
    """Invariant 4: a candidate rebased onto a newer trunk is a new tree, so it is tested and
    reviewed again before it lands."""

    def _run(self, second_verdict="approve"):
        c = self.cfg()
        verdicts = iter(["approve", second_verdict, second_verdict, second_verdict])
        reviews = []

        def fake(prompt, cwd, c_, role, *a):
            if role == "implementer":
                (cwd / "feature.txt").write_text("settings\n")
                return "done"
            if role == "adversary":
                if not reviews:
                    _move_trunk_independently(self.repo)
                reviews.append(prompt)
                return review(prompt, next(verdicts))
            return "done"

        labels = []
        gate = swarmd.Worker.gate

        def spy(worker, label, c=None):
            labels.append(label)
            return gate(worker, label, c)

        w = self.worker(c)
        with patch.object(swarmd, "flint", side_effect=fake), \
                patch.object(swarmd.Worker, "gate", spy):
            ok, note = w.do_task(_task(), "goal")
        return w, ok, note, labels, reviews

    def test_a_rebased_candidate_is_tested_and_reviewed_again(self):
        w, ok, note, labels, reviews = self._run()
        self.assertTrue(ok, note)
        self.assertIn("rebased", labels)
        self.assertEqual(len(reviews), 2)
        self.assertEqual(_git(self.repo, "show", "swarm/trunk:other.txt"), "other")
        self.assertEqual(_git(self.repo, "show", "swarm/trunk:feature.txt"), "settings")

    def test_a_rebased_candidate_the_second_review_rejects_does_not_land(self):
        w, ok, note, labels, reviews = self._run(second_verdict="request_changes")
        self.assertFalse(ok)
        self.assertGreaterEqual(len(reviews), 2)
        self.assertNotIn("feature.txt", _git(self.repo, "ls-tree", "--name-only", "swarm/trunk"))


class TurnExitCodeTests(RepoCase):
    """P8 acceptance: every exit code flint.py can return translates to the same typed outcome."""

    TABLE = {3: "CapReached", 6: "CapReached", 4: "NoCredits", 5: "StepLimit",
             11: "ExplorationExhausted", 7: "ProviderDown", 8: "ProviderBusy", 9: "ModelGone",
             1: "ModelError", 2: "ModelError"}

    def turn(self, rc):
        p = Mock(returncode=rc)
        p.communicate.return_value = ("the answer\n", None)

        @contextlib.contextmanager
        def fake_process(cmd, **kw):
            yield p

        budget = Mock(cap=900, reserve=300)
        budget.check.return_value = (True, 0, "")
        with patch.object(swarmd, "process", fake_process), \
                patch.object(swarmd, "sandboxed", lambda cmd, cwd, c: cmd):
            return swarmd.flint("prompt", self.root, {}, "implementer", "w0", budget, 4, "m:free")

    def test_success_returns_the_stripped_answer(self):
        self.assertEqual(self.turn(0), "the answer")

    def test_every_failure_code_raises_its_exact_type(self):
        for rc, name in self.TABLE.items():
            with self.subTest(rc=rc):
                with self.assertRaises(Exception) as caught:
                    self.turn(rc)
                self.assertIs(type(caught.exception), getattr(swarmd, name))


if __name__ == "__main__":
    unittest.main()
