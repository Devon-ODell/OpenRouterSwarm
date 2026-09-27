"""Two ways a restart used to cost the swarm real work.

54 attempts in one day were killed by a daemon restart — 5.6 hours of model turns — and every
one of them was implemented again from nothing, even though the work was sitting on its branch.
And a restart was the *only* way to pick up an edit to the supervisor, so the swarm ran half on
one version of the harness and half on another: it keeps swarmd in memory but spawns flint.py
afresh for every turn.
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from swarm import swarmd


class RepoCase(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.root = Path(d.name).resolve()
        for name in ("state", "logs"):
            (self.root / name).mkdir()
        for attr, value in (("STATE", self.root / "state"), ("LOGS", self.root / "logs")):
            p = patch.object(swarmd, attr, value)
            p.start()
            self.addCleanup(p.stop)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "t@example.invalid")
        self.git("config", "user.name", "T")
        (self.repo / "app.py").write_text("def f():\n    return 1\n")
        self.git("add", "-A")
        self.git("commit", "-qm", "baseline")
        self.base = self.git("rev-parse", "HEAD")

    def git(self, *a):
        return subprocess.run(["git", "-C", str(self.repo), *a], check=True,
                              capture_output=True, text=True).stdout.strip()

    def cfg(self, **kw):
        return {"repo": str(self.repo), "base_branch": "main", "test_cmd": "true",
                "models": ["a:free"], "workers": 1, **kw}


class RetainedWorkTests(RepoCase):
    """Finding the work an interrupted attempt left behind."""

    def attempt(self, name="t1-abcd1234", task_id="t1", finished=None, **kw):
        d = swarmd.STATE / "attempts" / name
        d.mkdir(parents=True, exist_ok=True)
        row = {"id": name, "task": {"id": task_id, "title": "Shard Stack M1"},
               "base_commit": self.base, "started": 100.0, "implementer": "qwen/qwen3-coder", **kw}
        if finished:
            row["finished"] = finished
        (d / "attempt.json").write_text(json.dumps(row))
        return d

    def work_on_branch(self, branch="swarm/t1-abcd1234"):
        """What _cleanup leaves: the unaccepted change committed to the attempt's branch."""
        self.git("branch", branch)
        wt = self.root / "wt"
        subprocess.run(["git", "-C", str(self.repo), "worktree", "add", "-q", str(wt), branch],
                       check=True, capture_output=True)
        (wt / "app.py").write_text("def f():\n    return 2\n")
        subprocess.run(["git", "-C", str(wt), "commit", "-aqm", "swarm (not accepted)"],
                       check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.repo), "worktree", "remove", "--force", str(wt)],
                       check=True, capture_output=True)
        return branch

    def test_work_committed_to_the_branch_is_found(self):
        branch = self.work_on_branch()
        self.attempt(branch=branch)
        diff = swarmd.retained_diff(self.cfg(), {"base_commit": self.base, "branch": branch})
        self.assertIn("return 2", diff)
        self.assertIn("app.py", diff)

    def test_work_still_loose_in_the_worktree_is_found(self):
        """A daemon killed before cleanup ran leaves it uncommitted."""
        wt = self.root / "live-wt"
        subprocess.run(["git", "-C", str(self.repo), "worktree", "add", "-q", "-b", "swarm/live",
                        str(wt), "main"], check=True, capture_output=True)
        (wt / "app.py").write_text("def f():\n    return 3\n")
        diff = swarmd.retained_diff(self.cfg(), {"base_commit": self.base, "branch": "swarm/live",
                                                 "worktree": str(wt)})
        self.assertIn("return 3", diff)

    def test_an_attempt_that_produced_nothing_has_nothing_to_resume(self):
        self.git("branch", "swarm/empty")
        self.assertEqual(swarmd.retained_diff(self.cfg(), {"base_commit": self.base,
                                                           "branch": "swarm/empty"}), "")

    def test_a_branch_that_is_gone_is_not_an_error(self):
        self.assertEqual(swarmd.retained_diff(self.cfg(), {"base_commit": self.base,
                                                           "branch": "swarm/never"}), "")
        self.assertEqual(swarmd.retained_diff(self.cfg(), {}), "")


class FindInterruptedTests(RetainedWorkTests):
    def queue_with(self, *ids):
        q = swarmd.Queue()
        for i in ids:
            row = q.add(f"task {i}")
            rows = swarmd._read(q.path)
            for r in rows:
                if r["id"] == row["id"]:
                    r["id"] = i
            swarmd._write(q.path, rows)
        return q

    def test_an_interrupted_attempt_on_a_queued_task_is_offered(self):
        branch = self.work_on_branch()
        self.attempt(branch=branch)
        found = swarmd.find_interrupted(self.cfg(), self.queue_with("t1"))
        self.assertEqual(list(found), ["t1"])
        self.assertEqual(found["t1"]["implementer"], "qwen/qwen3-coder")
        self.assertTrue(Path(found["t1"]["patch"]).is_file())

    def test_a_finished_attempt_is_not_offered(self):
        branch = self.work_on_branch()
        self.attempt(branch=branch, finished=123.0)
        self.assertEqual(swarmd.find_interrupted(self.cfg(), self.queue_with("t1")), {})

    def test_an_attempt_whose_task_is_no_longer_queued_is_not_offered(self):
        branch = self.work_on_branch()
        self.attempt(branch=branch)
        self.assertEqual(swarmd.find_interrupted(self.cfg(), self.queue_with("other")), {})

    def test_the_newest_interrupted_attempt_wins(self):
        branch = self.work_on_branch()
        self.attempt(name="t1-old", branch=branch, started=100.0)
        self.attempt(name="t1-new", branch=branch, started=900.0)
        found = swarmd.find_interrupted(self.cfg(), self.queue_with("t1"))
        self.assertEqual(found["t1"]["attempt"], "t1-new")

    def test_an_unreadable_attempt_record_is_skipped(self):
        d = swarmd.STATE / "attempts" / "broken"
        d.mkdir(parents=True)
        (d / "attempt.json").write_text("{not json")
        self.assertEqual(swarmd.find_interrupted(self.cfg(), self.queue_with("t1")), {})

    def test_it_is_recorded_once_and_taken_once(self):
        branch = self.work_on_branch()
        self.attempt(branch=branch)
        swarmd.note_interrupted(self.cfg(), self.queue_with("t1"))
        self.assertIn("t1", swarmd.load_resume())
        self.assertIsNotNone(swarmd.take_resume("t1"))
        self.assertIsNone(swarmd.take_resume("t1"), "a patch must not be retried for ever")


class AdoptRetainedTests(RetainedWorkTests):
    """Re-applying the work, onto a worktree freshly branched from trunk."""

    def worker(self):
        w = swarmd.Worker(0, self.cfg(), swarmd.Queue(), Mock(), threading.Event())
        w.name = "w0"
        return w

    def fresh_worktree(self):
        wt = self.root / "fresh"
        subprocess.run(["git", "-C", str(self.repo), "worktree", "add", "-q", "-b",
                        "swarm/fresh", str(wt), "main"], check=True, capture_output=True)
        return wt

    def offer(self, diff):
        patch = self.root / "retained.patch"
        patch.write_text(diff)
        swarmd.save_resume({"t1": {"attempt": "t1-abcd", "patch": str(patch),
                                   "implementer": "qwen/qwen3-coder", "lines": 5}})

    def test_the_work_is_re_applied_and_the_model_credited(self):
        branch = self.work_on_branch()
        self.offer(swarmd.retained_diff(self.cfg(), {"base_commit": self.base, "branch": branch}))
        wt = self.fresh_worktree()
        with patch.object(swarmd, "log"):
            row = self.worker().adopt_retained({"id": "t1"}, wt)
        self.assertIsNotNone(row)
        self.assertEqual(row["implementer"], "qwen/qwen3-coder")
        self.assertIn("return 2", (wt / "app.py").read_text())

    def test_a_patch_that_no_longer_applies_leaves_the_worktree_alone(self):
        self.offer("--- a/app.py\n+++ b/app.py\n@@ -1,2 +1,2 @@\n-def gone():\n+def other():\n")
        wt = self.fresh_worktree()
        before = (wt / "app.py").read_text()
        with patch.object(swarmd, "log"):
            self.assertIsNone(self.worker().adopt_retained({"id": "t1"}, wt))
        self.assertEqual((wt / "app.py").read_text(), before)
        self.assertEqual(subprocess.run(["git", "-C", str(wt), "status", "--porcelain"],
                                        capture_output=True, text=True).stdout, "")

    def test_a_failure_is_journalled_so_it_can_be_counted(self):
        self.offer("not even a patch\n")
        with patch.object(swarmd, "log"):
            self.worker().adopt_retained({"id": "t1"}, self.fresh_worktree())
        rows = [json.loads(l) for l in
                (swarmd.STATE / "journal.jsonl").read_text().splitlines() if l.strip()]
        self.assertEqual([r["event"] for r in rows], ["resume_failed"])

    def test_nothing_offered_means_a_normal_attempt(self):
        self.assertIsNone(self.worker().adopt_retained({"id": "t1"}, self.fresh_worktree()))

    def test_a_missing_patch_file_means_a_normal_attempt(self):
        swarmd.save_resume({"t1": {"attempt": "x", "patch": str(self.root / "gone.patch")}})
        self.assertIsNone(self.worker().adopt_retained({"id": "t1"}, self.fresh_worktree()))


class SourceChangeTests(unittest.TestCase):
    """The daemon noticing that the harness under it has been edited."""

    def test_the_fingerprint_covers_the_supervisor_and_the_agent(self):
        names = {Path(p).name for p in swarmd.source_fingerprint()}
        self.assertIn("swarmd.py", names)
        self.assertIn("flint.py", names)
        self.assertIn("learn.py", names)

    def test_an_untouched_tree_reports_nothing(self):
        self.assertEqual(swarmd.source_changed(swarmd.source_fingerprint()), [])

    def test_an_edited_file_is_named(self):
        before = dict(swarmd.source_fingerprint())
        key = next(k for k in before if k.endswith("wallet.py"))
        before[key] = (0.0, 0)
        self.assertEqual(swarmd.source_changed(before), ["wallet.py"])

    def test_the_real_tree_compiles(self):
        self.assertEqual(swarmd.source_compiles(), (True, ""))

    def test_a_half_saved_edit_is_refused_rather_than_restarted_into(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        broken = Path(d.name) / "half.py"
        broken.write_text("def f(:\n")
        with patch.object(swarmd, "source_files", return_value=[broken]):
            ok, why = swarmd.source_compiles()
            self.assertFalse(ok)
            self.assertIn("half.py", why)
            with patch.object(swarmd, "log"), patch.object(swarmd, "journal"), \
                 patch.object(swarmd.os, "execve") as execve:
                self.assertFalse(swarmd.restart_into_new_code(["half.py"]))
            execve.assert_not_called()

    def restart(self):
        with patch.object(swarmd, "source_compiles", return_value=(True, "")), \
             patch.object(swarmd, "shutdown"), patch.object(swarmd, "log"), \
             patch.object(swarmd, "journal"), patch.object(swarmd, "STATE", Path(tempfile.mkdtemp())), \
             patch.object(swarmd.os, "execve") as execve:
            swarmd.restart_into_new_code(["swarmd.py"])
        return execve.call_args.args

    def test_a_restart_replaces_this_process_with_the_same_command(self):
        _, argv, _ = self.restart()
        self.assertEqual(argv[0], sys.executable)
        self.assertEqual(argv[1:], sys.argv)

    def test_the_end_of_the_shift_is_inherited_exactly_not_recomputed(self):
        """A restart used to set the deadline to `hours` from *itself*, so each one pushed the
        end of the shift forward by however long the run had already lasted. Seen live: an
        8-hour shift started at 09:03 restarted at 09:14 and moved its end from 17:03 to 17:14.
        """
        ends_at = str(time.time() + 3600)
        with patch.dict(os.environ, {"FLINT_RUN_DEADLINE": ends_at}):
            _, _, env = self.restart()
        self.assertEqual(env["FLINT_RUN_DEADLINE"], ends_at)

    def test_a_shift_with_no_deadline_gains_none_from_restarting(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FLINT_RUN_DEADLINE", None)
            _, _, env = self.restart()
        self.assertNotIn("FLINT_RUN_DEADLINE", env)


class ShiftDeadlineTests(unittest.TestCase):
    """A restart must not extend the shift it was restarted into."""

    def test_without_a_deadline_the_hours_stand(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FLINT_RUN_DEADLINE", None)
            self.assertEqual(swarmd.hours_left(8), 8)
            self.assertIsNone(swarmd.hours_left(None))

    def test_a_deadline_shortens_the_run_to_what_is_left(self):
        with patch.dict(os.environ, {"FLINT_RUN_DEADLINE": str(time.time() + 3600)}):
            self.assertAlmostEqual(swarmd.hours_left(8), 1.0, places=1)

    def test_a_deadline_never_lengthens_a_shorter_run(self):
        with patch.dict(os.environ, {"FLINT_RUN_DEADLINE": str(time.time() + 36000)}):
            self.assertEqual(swarmd.hours_left(2), 2)

    def test_a_shift_that_is_already_over_says_so(self):
        with patch.dict(os.environ, {"FLINT_RUN_DEADLINE": str(time.time() - 60)}):
            self.assertIsNone(swarmd.hours_left(8))

    def test_the_first_start_fixes_when_the_shift_ends(self):
        with patch.dict(os.environ, {}, clear=False), \
             patch.object(swarmd, "run_daemon"), patch.object(swarmd, "preflight"), \
             patch.object(swarmd, "read_goal", return_value="g"), patch.object(swarmd, "log"), \
             tempfile.TemporaryDirectory() as d, patch.object(swarmd, "STATE", Path(d)):
            os.environ.pop("FLINT_RUN_DEADLINE", None)
            self.addCleanup(swarmd._stop.clear)
            self.addCleanup(os.environ.pop, "FLINT_RUN_DEADLINE", None)
            swarmd.start({"repo": ".", "workers": 1}, hours=8)
            self.assertAlmostEqual(float(os.environ["FLINT_RUN_DEADLINE"]),
                                   time.time() + 8 * 3600, delta=30)

    def test_preflight_time_counts_toward_the_deadline(self):
        clock = [1000.0]
        def preflight(c):
            clock[0] += 120
        with patch.dict(os.environ, {"FLINT_RUN_DEADLINE": "3880"}), \
             patch.object(swarmd.time, "time", side_effect=lambda: clock[0]), \
             patch.object(swarmd, "preflight", side_effect=preflight), \
             patch.object(swarmd, "run_daemon"), patch.object(swarmd, "read_goal", return_value="g"), \
             patch.object(swarmd, "log"), patch.object(swarmd.threading, "Timer") as timer, \
             tempfile.TemporaryDirectory() as d, patch.object(swarmd, "STATE", Path(d)):
            self.addCleanup(swarmd._stop.clear)
            swarmd.start({"repo": ".", "workers": 1}, hours=0.8)
            self.assertEqual(timer.call_args.args[0], 2760)

    def test_a_restarted_daemon_past_its_deadline_does_not_start(self):
        with patch.dict(os.environ, {"FLINT_RUN_DEADLINE": str(time.time() - 60)}), \
             patch.object(swarmd, "log") as log, patch.object(swarmd, "run_daemon") as run:
            swarmd.start({"repo": ".", "workers": 1}, hours=8)
        run.assert_not_called()
        self.assertIn("already over", log.call_args.args[0])

    def test_rubbish_in_the_deadline_is_ignored(self):
        with patch.dict(os.environ, {"FLINT_RUN_DEADLINE": "soon"}):
            self.assertEqual(swarmd.hours_left(8), 8)


if __name__ == "__main__":
    unittest.main()
