"""The 2026-09-27 shift review: honest worker status, scope gates, review retries, spare time.

Every test here is offline. No model is called that is not a local stand-in, and no test
starts a swarm.
"""
import json
from pathlib import Path
import re
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

from swarm import swarmd


def capture(fn, args):
    """Run a bridge command and return the JSON object it emitted."""
    import contextlib, io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn(args)
    return json.loads(buf.getvalue().strip().splitlines()[-1])


def allows_a_turn():
    """A budget with free capacity, for the paths that only ask whether a turn may start."""
    return NS(check=lambda need=1: (True, 0, ""), paid_would_help=lambda: False)


def review(prompt, verdict="approve"):
    """A well-formed structured review of the tree named in the reviewer prompt."""
    tree = re.search(r"GIT TREE TO REVIEW: (\w+)", prompt).group(1)
    ids = dict.fromkeys(re.findall(r'"id": "(C\d+)"', prompt))
    ok = verdict == "approve"
    return json.dumps({"verdict": verdict, "tree": tree, "summary": "reviewed",
                       "checks": [{"criterion": i, "passed": ok, "evidence": "tests"} for i in ids],
                       "findings": [] if ok else [{"severity": "blocker", "path": "app.txt",
                                                   "line": 1, "issue": "wrong",
                                                   "verification": "run it"}]})


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for name in ("state", "logs"):
            (self.root / name).mkdir()
        for name, value in (("STATE", self.root / "state"), ("LOGS", self.root / "logs"),
                            ("HERE", self.root)):
            p = patch.object(swarmd, name, value)
            p.start()
            self.addCleanup(p.stop)
        swarmd._stop.clear()
        for book in (swarmd._waiting, swarmd._ran_on, swarmd._active_turns, swarmd._turn_logs,
                     swarmd._held, swarmd._study_calls, swarmd._edit_calls, swarmd._charges):
            book.clear()

    def repo(self):
        repo = self.root / "repo"
        repo.mkdir()

        def git(*args):
            return subprocess.run(["git", "-C", str(repo), *args], check=True,
                                  capture_output=True, text=True).stdout.strip()
        git("init", "-b", "main")
        git("config", "user.email", "test@example.invalid")
        git("config", "user.name", "Test")
        (repo / "app.txt").write_text("baseline\n")
        git("add", ".")
        git("commit", "-m", "baseline")
        return repo, git

    def cfg(self, repo, **kw):
        return {"repo": str(repo), "base_branch": "main", "test_cmd": "true",
                "steps": {"architect": 0, "implementer": 2, "adversary": 2, "judge": 1,
                          "planner": 4, "decomposer": 1},
                **kw}


class WorkerStatusTests(Base):
    """`now.json` is the only account most people ever read of what the swarm is doing."""

    def test_forgetting_a_worker_drops_everything_its_last_task_left(self):
        swarmd._waiting["w0"] = "waiting for the allowance"
        swarmd._ran_on["w0"] = "paid/stand-in"
        swarmd._active_turns["w0"] = "implementer with a"
        swarmd._turn_logs["w0"] = "turn.log"
        swarmd._held["w0"] = time.time()
        swarmd.count_edits("w0", 3)
        swarmd.count_usd("w0", 1.5)
        swarmd.forget_worker("w0")
        for book in (swarmd._waiting, swarmd._ran_on, swarmd._active_turns, swarmd._turn_logs,
                     swarmd._held, swarmd._edit_calls, swarmd._charges):
            self.assertNotIn("w0", book)

    def test_a_worker_between_tasks_is_not_reported_as_working(self):
        idle = NS(name="w0", task=None, started=0, doing=None, is_alive=lambda: True)
        self.assertEqual(swarmd.write_now([idle])["workers"], [])

    def test_the_idle_line_does_not_quote_a_worker_that_is_gone(self):
        swarmd._waiting["w0"] = "waiting for the allowance: spent"
        gone = NS(name="w0", task=None, started=0, doing=None, is_alive=lambda: False)
        self.assertNotIn("idle", swarmd.write_now([gone], self.cfg(self.root)))
        live = NS(name="w0", task=None, started=0, doing=None, is_alive=lambda: True)
        self.assertIn("idle", swarmd.write_now([live], self.cfg(self.root)))

    def test_a_worker_clears_its_task_when_the_queue_runs_dry(self):
        repo, _ = self.repo()
        q = swarmd.Queue()
        q.add("Add a marker file", "Create marker.txt next to app.txt",
              acceptance=["marker.txt exists and holds the word ready"])

        def fake(prompt, cwd, c, role, *args):
            if role == "implementer":
                (cwd / "marker.txt").write_text("ready\n")
                return "done"
            return review(prompt) if role == "adversary" else "done"
        tally = swarmd.Tally(limit=1)
        w = swarmd.Worker(0, self.cfg(repo), q, Mock(), threading.Event(), tally=tally)
        with patch.object(swarmd, "flint", side_effect=fake):
            w.run()
        self.assertEqual(w.task, None)
        self.assertEqual(w.started, 0)
        self.assertNotIn("w0", swarmd._waiting)
        # And the status written after it says nobody is working, not that w0 still is.
        self.assertEqual(swarmd.write_now([w])["workers"], [])


class ScopeGateTests(Base):
    def test_an_open_ended_task_names_its_gap(self):
        gap = swarmd.scope_gap({"title": "Improve the codebase",
                                "detail": "Tidy the whole thing as you see fit",
                                "acceptance": ["Improve the codebase"]})
        self.assertIn("open-ended", gap)

    def test_a_task_that_names_an_observable_passes(self):
        self.assertEqual("", swarmd.scope_gap(
            {"title": "Add a pause key", "detail": "in game.js",
             "acceptance": ["pressing P halts the loop and draws PAUSED"]}))

    def test_a_task_that_declared_nothing_is_left_to_the_queues_own_checks(self):
        self.assertEqual("", swarmd.scope_gap({"title": "one", "detail": ""}))

    def test_an_unscoped_task_is_parked_without_calling_a_model(self):
        repo, _ = self.repo()
        q = swarmd.Queue()
        q.add("Improve everything", "Clean up the whole codebase as needed",
              acceptance=["Improve everything"])
        tid = q.pending()[0]["id"]
        w = swarmd.Worker(0, self.cfg(repo), q, Mock(), threading.Event())
        with patch.object(swarmd, "flint") as model:
            ok, note = w.do_task(q.claim(), "goal")
        self.assertFalse(ok)
        self.assertEqual(w.stage, "out_of_scope")
        model.assert_not_called()
        # Parked, not retried: a second attempt cannot supply what the first one lacked.
        q.release(tid, False, note, failure_class=w.failure_class, park=True)
        self.assertEqual(q.pending(), [])
        self.assertEqual(swarmd._read(q.done)[-1]["status"], "parked")

    def test_the_gate_can_be_switched_off(self):
        repo, _ = self.repo()
        task = {"id": "t1", "title": "Improve everything",
                "detail": "Clean up the whole codebase as needed",
                "acceptance": ["Improve everything"]}
        w = swarmd.Worker(0, self.cfg(repo, scope_gate=False), swarmd.Queue(), Mock(),
                          threading.Event())

        def fake(prompt, cwd, c, role, *args):
            if role == "implementer":
                (cwd / "app.txt").write_text("changed\n")
                return "done"
            return review(prompt) if role == "adversary" else "done"
        with patch.object(swarmd, "flint", side_effect=fake):
            ok, note = w.do_task(task, "goal")
        self.assertTrue(ok, note)


class ReviewFormatTests(Base):
    """A reviewer that cannot emit JSON says nothing about the code it was sent."""

    def implementation(self, replies):
        """A fake turn: the implementer always edits; the adversary answers from `replies`."""
        def fake(prompt, cwd, c, role, *args):
            if role == "implementer":
                (cwd / "app.txt").write_text("changed\n")
                return "done"
            if role == "adversary":
                answer = replies.pop(0)
                return review(prompt) if answer == "good" else answer
            return "done"
        return fake

    def task(self):
        return {"id": "t1", "title": "Change app.txt",
                "detail": "Write 'changed' into app.txt",
                "acceptance": ["app.txt reads 'changed'"]}

    def test_an_unreadable_review_is_put_to_another_reviewer_and_the_work_lands(self):
        repo, git = self.repo()
        c = self.cfg(repo, models=["a:free", "b:free", "c:free"], max_repairs=0)
        w = swarmd.Worker(0, c, swarmd.Queue(), Mock(), threading.Event())
        with patch.object(swarmd, "flint", side_effect=self.implementation(["I think it's fine!", "good"])):
            ok, note = w.do_task(self.task(), "goal")
        self.assertTrue(ok, note)
        self.assertEqual(git("show", "swarm/trunk:app.txt"), "changed")

    def test_the_attempt_ends_only_once_every_reviewer_has_failed(self):
        repo, _ = self.repo()
        # Four models: one implements, and the three that are left each get asked in turn.
        c = self.cfg(repo, models=["a:free", "b:free", "c:free", "d:free"], max_repairs=0,
                     max_review_formats=2)
        w = swarmd.Worker(0, c, swarmd.Queue(), Mock(), threading.Event())
        prose = ["looks good to me", "seems fine", "no complaints"]
        with patch.object(swarmd, "flint", side_effect=self.implementation(prose)):
            ok, note = w.do_task(self.task(), "goal")
        self.assertFalse(ok)
        self.assertEqual(w.stage, "review_error")
        self.assertEqual(prose, [])          # all three reviewers were asked

    def test_the_repair_budget_is_untouched_by_a_reviewer_that_cannot_answer(self):
        repo, _ = self.repo()
        c = self.cfg(repo, models=["a:free", "b:free", "c:free"], max_repairs=2)
        w = swarmd.Worker(0, c, swarmd.Queue(), Mock(), threading.Event())
        roles = []

        def fake(prompt, cwd, c_, role, *args):
            roles.append(role)
            if role == "implementer":
                (cwd / "app.txt").write_text("changed\n")
                return "done"
            if role == "adversary":
                return "not json" if roles.count("adversary") == 1 else review(prompt)
            return "done"
        with patch.object(swarmd, "flint", side_effect=fake):
            ok, note = w.do_task(self.task(), "goal")
        self.assertTrue(ok, note)
        self.assertNotIn("repair", roles)


class IdleImprovementTests(Base):
    def setUp(self):
        super().setUp()
        self.repo_path, self.git = self.repo()
        self.c = self.cfg(self.repo_path, models=["a:free"], goal_file="GOAL.md")
        (self.repo_path / "GOAL.md").write_text("Make the games deeper.\n")
        self.git("add", "."), self.git("commit", "-m", "goal")
        self.q = swarmd.Queue()

    def propose(self, obj):
        return patch.object(swarmd, "flint", return_value=json.dumps(obj))

    def test_it_stands_down_while_there_is_assigned_work(self):
        self.q.add("Real work", "in app.txt", acceptance=["app.txt changed"])
        ok, why = swarmd.idle_ready(self.c, self.q)
        self.assertFalse(ok)
        self.assertIn("assigned work", why)

    def test_it_stands_down_while_the_roadmap_is_held(self):
        (self.root / "state" / "roadmap-execution.json").write_text(json.dumps(
            {"blocked": {"F02": {"reason": "attempt_failed"}}, "roots": ["F02"]}))
        ok, why = swarmd.idle_ready(self.c, self.q)
        self.assertFalse(ok)
        self.assertIn("F02", why)
        self.assertIn("needs a person", why)

    def test_it_stands_down_at_the_end_of_the_shift(self):
        with patch.dict("os.environ", {"FLINT_RUN_DEADLINE": str(time.time() + 120)}):
            ok, why = swarmd.idle_ready(self.c, self.q)
        self.assertFalse(ok)
        self.assertIn("shift", why)

    def test_it_queues_one_improvement_and_ranks_it_below_everything_else(self):
        with self.propose({"title": "Test the pause key", "kind": "test",
                           "detail": "game.js has no test for the pause key",
                           "acceptance": ["a test asserts the loop halts when P is pressed"],
                           "why_now": "the behaviour is untested"}):
            task = swarmd.idle_improvement(self.c, self.q, allows_a_turn())
        self.assertEqual(task["title"], "Test the pause key")
        self.assertEqual(task["origin"], "idle")
        self.assertEqual(task["priority"], -1)
        state = swarmd.idle_state()
        self.assertEqual(state["active"], task["id"])
        self.assertEqual(state["cycles"][-1]["status"], "queued")

    def test_only_one_improvement_is_open_at_a_time(self):
        with self.propose({"title": "First", "detail": "in app.txt",
                           "acceptance": ["app.txt gains a header"]}):
            first = swarmd.idle_improvement(self.c, self.q, allows_a_turn())
        self.assertIsNotNone(first)
        with patch.object(swarmd, "flint") as model:
            self.assertIsNone(swarmd.idle_improvement(self.c, self.q, allows_a_turn()))
        model.assert_not_called()

    def test_a_finished_improvement_is_settled_and_recorded(self):
        with self.propose({"title": "First", "detail": "in app.txt",
                           "acceptance": ["app.txt gains a header"]}):
            first = swarmd.idle_improvement(self.c, self.q, allows_a_turn())
        self.q.release(first["id"], True, "integrated abc on swarm/trunk")
        with patch.object(swarmd, "flint") as model:
            swarmd.idle_improvement(self.c, self.q, allows_a_turn())   # cooldown blocks a new cycle
        model.assert_not_called()
        state = swarmd.idle_state()
        self.assertIsNone(state["active"])
        self.assertEqual(state["cycles"][-1]["status"], "done")

    def test_nothing_worth_doing_is_a_complete_answer(self):
        with self.propose({"title": None, "why_not": "the suite already covers this"}):
            self.assertIsNone(swarmd.idle_improvement(self.c, self.q, allows_a_turn()))
        cycle = swarmd.idle_state()["cycles"][-1]
        self.assertEqual(cycle["status"], "nothing_found")
        self.assertIn("already covers", cycle["why"])

    def test_a_vague_proposal_is_dropped_before_it_reaches_the_queue(self):
        with self.propose({"title": "Improve everything",
                           "detail": "clean up the whole codebase as needed",
                           "acceptance": ["Improve everything"]}):
            self.assertIsNone(swarmd.idle_improvement(self.c, self.q, allows_a_turn()))
        self.assertEqual(self.q.pending(), [])
        self.assertEqual(swarmd.idle_state()["cycles"][-1]["status"], "rejected")


if __name__ == "__main__":
    unittest.main()


class EvidenceTests(Base):
    """A task's evidence has to survive the next task, and the link to it has to resolve."""

    def attempt_for(self, task, note="parked after two tries"):
        from swarm import workflow
        name = f"{task['id']}-w0-20260927-120000"
        bundle = workflow.Attempt(self.root / "state", name, task, self.root / "wt", "swarm/w0")
        bundle.finish("rejected", note)
        return bundle

    def test_an_attempt_is_filed_under_its_task_not_its_worker(self):
        repo, _ = self.repo()
        q = swarmd.Queue()
        q.add("Change app.txt", "Write 'changed' into app.txt",
              acceptance=["app.txt reads 'changed'"])
        task = q.claim()

        def fake(prompt, cwd, c, role, *args):
            if role == "implementer":
                (cwd / "app.txt").write_text("changed\n")
                return "done"
            return review(prompt) if role == "adversary" else "done"
        w = swarmd.Worker(0, self.cfg(repo), q, Mock(), threading.Event())
        with patch.object(swarmd, "flint", side_effect=fake):
            w.do_task(task, "goal")
        found = swarmd.attempts_for(task["id"])
        self.assertEqual(len(found), 1)
        self.assertTrue((found[0][0] / "HANDOFF.md").is_file())
        # The old naming put every task in attempts/w0 and overwrote the last one.
        self.assertNotEqual(found[0][0].name, "w0")

    def test_the_bridge_finds_a_tasks_evidence(self):
        from swarm import bridge
        repo, _ = self.repo()
        task = {"id": "t1a2b3c4", "title": "Change app.txt", "detail": "",
                "acceptance": ["app.txt reads 'changed'"]}
        self.attempt_for(task)
        with patch.object(bridge.swarmd, "STATE", self.root / "state"), \
             patch.object(bridge, "config_for", return_value={"repo": str(repo)}), \
             patch.object(bridge.swarmd, "use_repo"):
            out = capture(bridge.cmd_evidence, NS(repo=str(repo), id="t1a2b3c4"))
        self.assertEqual(len(out["attempts"]), 1)
        self.assertEqual(out["attempts"][0]["phase"], "rejected")
        self.assertTrue(out["attempts"][0]["handoff"].endswith("HANDOFF.md"))
        self.assertIn("attempt.json", [f["name"] for f in out["attempts"][0]["files"]])

    def test_pruning_keeps_the_newest_bundles(self):
        for i in range(5):
            self.attempt_for({"id": f"t{i}", "title": f"task {i}", "detail": "",
                              "acceptance": ["something observable"]})
        self.assertEqual(swarmd.prune_attempts(keep=2), 3)
        self.assertEqual(len(list((self.root / "state" / "attempts").iterdir())), 2)


class TaskPreviewTests(Base):
    """What the editor shows before it queues anything."""

    def preview(self, repo, title, detail):
        from swarm import bridge
        c = {"repo": str(repo), "base_branch": "main", "test_cmd": "true", "max_queue": 20}
        with patch.object(bridge.swarmd, "STATE", self.root / "state"), \
             patch.object(bridge, "config_for", return_value=c), \
             patch.object(bridge.swarmd, "use_repo"), \
             patch.object(bridge, "is_target", return_value=True), \
             patch.object(bridge, "daemon_running", return_value=False):
            return capture(bridge.cmd_task, NS(
                repo=str(repo), title=title, detail=detail, kind="feature", priority=1,
                file=None, start=None, end=None, selection_file=None, preview=True))

    def test_a_preview_queues_nothing(self):
        repo, _ = self.repo()
        out = self.preview(repo, "Add a pause key", "pressing P halts the loop in game.js")
        self.assertTrue(out["preview"])
        self.assertEqual(out["scope_gap"], "")
        self.assertEqual(out["task"]["acceptance"], ["pressing P halts the loop in game.js"])
        self.assertEqual(swarmd.Queue().pending(), [])

    def test_a_preview_names_the_scope_gap_before_anything_is_spent(self):
        repo, _ = self.repo()
        out = self.preview(repo, "Improve everything", "clean up the whole codebase as needed")
        self.assertIn("open-ended", out["scope_gap"])
        self.assertEqual(swarmd.Queue().pending(), [])
