"""Offline tests for the self-reinforcing swarm: temporary repos, no real API calls."""
import json
import os
from pathlib import Path
import random
import re
import subprocess
import tempfile
import threading
import unittest
import uuid
from unittest.mock import Mock, patch

import flint
from swarm import learn, sandbox, swarmd
from swarm.learn import is_breakthrough, novelty, parse_scores, reward


class LearnTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.L = learn.Ledger(Path(self.tmp.name) / "learn.json")

    def test_bandit_shifts_toward_rewarded_model(self):
        for _ in range(15):
            self.L.update("implementer", "good", 0.9)
            self.L.update("implementer", "bad", 0.05)
        rng = random.Random(7)
        picks = [self.L.pick("implementer", ["good", "bad"], rng=rng) for _ in range(200)]
        self.assertGreater(picks.count("good"), 180)

    def test_resting_and_excluded_models_are_not_picked(self):
        self.L.cool("a", "overloaded")
        self.assertEqual(self.L.pick("implementer", ["a", "b"]), "b")
        self.assertIsNone(self.L.pick("implementer", ["a", "b"], exclude={"b"}))
        self.L.warm("a")
        self.assertEqual(self.L.pick("implementer", ["a", "b"], exclude={"b"}), "a")

    def test_reward_orders_outcomes_and_pays_for_creativity(self):
        s = {"impact": 5, "creativity": 2, "quality": 6}
        routine = reward("accepted", s)
        creative = reward("accepted", {**s, "creativity": 9})
        self.assertGreater(creative, routine)
        self.assertGreater(routine, reward("conflict"))
        for stage in learn.FAULTY:
            self.assertEqual(reward(stage), 0.0)
        self.assertGreater(reward("accepted", s, nov=1.0), reward("accepted", s, nov=0.0))
        self.assertLessEqual(reward("accepted", {"impact": 10, "creativity": 10, "quality": 10}, 1.0), 1.0)
        self.assertIsNone(reward("baseline"))

    def test_repairs_cost_reward_but_landing_still_beats_any_failure(self):
        s = {"impact": 6, "creativity": 6, "quality": 6}
        self.assertAlmostEqual(reward("accepted", s) - reward("accepted", s, repairs=2), 0.2)
        self.assertEqual(reward("accepted", {"impact": 0, "creativity": 0, "quality": 0}, repairs=3), 0.4)
        self.assertGreater(reward("accepted", s, repairs=3), reward("conflict"))

    def test_punishment_outweighs_praise(self):
        w = learn.weight
        self.assertGreater(w("weakened_tests"), w("tests_failed"))
        self.assertEqual(w("tests_failed"), w("rejected"))
        self.assertGreater(w("rejected"), 1.5 * w("accepted"))
        self.assertGreater(w("accepted"), w("conflict"))
        mean = lambda arm: (lambda s: s["a"] / (s["a"] + s["b"]))(self.L.snapshot()["arms"]["implementer"][arm])
        # Two models land the same two solid tasks; one then ships faulty code once.
        for arm in ("clean", "sloppy"):
            for _ in range(2):
                self.L.update("implementer", arm, 0.7, weight=w("accepted"))
        self.L.update("implementer", "sloppy", 0.0, weight=w("tests_failed"))
        self.assertLess(mean("sloppy"), 0.4)   # below even a model never tried (0.5)
        picks = [self.L.pick("implementer", ["sloppy", "clean"], rng=random.Random(i)) for i in range(300)]
        self.assertGreater(picks.count("clean"), 240)
        # Climbing back takes three more clean landings.
        for landings in (1, 2, 3):
            self.L.update("implementer", "sloppy", 0.7, weight=w("accepted"))
            self.assertEqual(mean("sloppy") >= 0.5, landings == 3)

    def test_exhibit_shows_the_offending_lines(self):
        diff = ("diff --git a/app/calc.py b/app/calc.py\n--- a/app/calc.py\n+++ b/app/calc.py\n"
                "@@ -1 +1,2 @@\n-def total(xs): return sum(xs)\n+def total(xs):\n+    return sum(xs[:-1])\n"
                "diff --git a/tests/test_calc.py b/tests/test_calc.py\n--- a/tests/test_calc.py\n"
                "+++ b/tests/test_calc.py\n@@ -3 +2,0 @@\n-    assert total([1, 2]) == 3\n")
        self.assertEqual(learn.exhibit(diff, "tests_failed"),
                         "# app/calc.py\n+def total(xs):\n+    return sum(xs[:-1])")
        self.assertEqual(learn.exhibit(diff, "weakened_tests"),
                         "# app/calc.py\n-def total(xs): return sum(xs)\n# tests/test_calc.py\n"
                         "-    assert total([1, 2]) == 3")
        self.assertTrue(learn.exhibit("+x\n" * 100, "rejected").endswith("# …"))

    def test_defect_is_read_from_supervisor_evidence(self):
        d = Path(self.tmp.name) / "attempt"
        d.mkdir()
        (d / "review-03.json").write_text(json.dumps({"verdict": "request_changes", "summary": "no", "findings": [
            {"severity": "minor", "path": "a.py", "line": 2, "issue": "style", "verification": "-"},
            {"severity": "blocker", "path": "app/calc.py", "line": 2, "issue": "drops the last item",
             "verification": "total([1, 2]) returns 1"}]}))
        self.assertEqual(learn.defect("rejected", d),
                         "app/calc.py:2 drops the last item (repro: total([1, 2]) returns 1)")
        (d / "gate-02.json").write_text(json.dumps([
            {"command": "pytest", "passed": False, "tail": "collected 3\nAssertionError: 1 != 3\n1 failed"}]))
        self.assertEqual(learn.defect("tests_failed", d), "`pytest` failed: 1 failed")
        self.assertIn("removed assertions", learn.defect("weakened_tests", d))
        self.assertEqual(learn.defect("tests_failed", None, "ok\nKeyError: 'x'\n"), "KeyError: 'x'")

    def test_the_wall_of_shame_reaches_every_prompt(self):
        for i, stage in enumerate(("rejected", "weakened_tests", "tests_failed", "rejected")):
            self.L.hang(f"model-{i}:free", {"id": f"t{i}", "title": f"task {i}"}, stage,
                        f"defect {i}", f"+bad line {i}")
        lessons, pitfalls = self.L.playbook()
        wall = [p for p in pitfalls if p["kind"] == "shame"]
        self.assertEqual(len(wall), 3)
        self.assertEqual(wall[0]["stage"], "weakened_tests")  # cheating hangs highest
        text = learn.format_playbook(lessons, pitfalls)
        self.assertIn("HUNG FROM THE RAFTERS", text)
        self.assertIn("`model-1:free` weakened existing tests to fake a pass on 'task 1': defect 1", text)
        self.assertIn("  +bad line 3", text)
        board = {r["arm"]: r for r in self.L.leaderboard()}
        self.assertEqual(board, {})  # faults are counted against implementer arms only once they exist
        self.L.update("implementer", "model-0:free", 0.0)
        self.assertEqual({r["arm"]: r["faults"] for r in self.L.leaderboard()}, {"model-0:free": 1})

    def test_breakthrough_needs_judged_excellence_or_an_outlier(self):
        self.assertTrue(is_breakthrough(0.9, {"breakthrough": True, "impact": 8, "creativity": 5}, []))
        self.assertFalse(is_breakthrough(0.9, {"breakthrough": True, "impact": 6, "creativity": 6}, []))
        history = [0.62, 0.65, 0.6, 0.66, 0.63, 0.64, 0.61, 0.65]
        self.assertTrue(is_breakthrough(0.95, {}, history))
        self.assertFalse(is_breakthrough(0.7, {}, history))
        self.assertFalse(is_breakthrough(0.95, {}, history[:3]))

    def test_lessons_that_help_rise_and_pinned_come_first(self):
        good = self.L.add_lesson("Keep pure scoring functions separate from IO so they are easy to test",
                                 "lesson", "t1", 0.6)
        bad = self.L.add_lesson("Rewrite the whole module whenever a small feature is needed",
                                "lesson", "t2", 0.6)
        for _ in range(5):
            self.L.credit([good], 0.9)
            self.L.credit([bad], 0.1)
        self.assertEqual(self.L.playbook()[0][0]["id"], good)
        pinned = self.L.add_lesson("Seeded RNG makes the level generator reproducible and testable",
                                   "lesson", "t3", 0.5, pinned=True)
        self.assertEqual(self.L.playbook()[0][0]["id"], pinned)
        duplicate = self.L.add_lesson("Keep pure scoring functions separate from IO so they are easy to test well",
                                      "lesson", "t4", 0.7)
        self.assertEqual(duplicate, good)

    def test_parse_scores_tolerates_prose_and_rejects_garbage(self):
        s = parse_scores('Here:\n{"impact": 7, "creativity": "8", "quality": 11, "breakthrough": true, '
                         '"lesson": "x", "follow_ups": [{"title": "Next"}, {"bad": 1}, {"title": "A"}, '
                         '{"title": "B"}]}\nthanks')
        self.assertEqual((s["impact"], s["creativity"], s["quality"]), (7.0, 8.0, 10.0))
        self.assertTrue(s["breakthrough"])
        self.assertEqual([f["title"] for f in s["follow_ups"]], ["Next", "A"])
        self.assertIsNone(parse_scores("APPROVE: fine"))
        self.assertIsNone(parse_scores('{"impact": "high", "creativity": 1, "quality": 1}'))

    def test_novelty_rewards_new_directions(self):
        prior = ["Deterministic level generator with seeded RNG"]
        self.assertLess(novelty("Seeded deterministic level generator", prior), 0.5)
        self.assertGreater(novelty("Replay recorder for multiplayer matches", prior), 0.9)


def review(prompt, verdict="approve", issue="off-by-one at the last index"):
    """A well-formed structured review of the tree named in the reviewer prompt."""
    tree = re.search(r"GIT TREE TO REVIEW: (\w+)", prompt).group(1)
    ids = dict.fromkeys(re.findall(r'"id": "(C\d+)"', prompt))
    approve = verdict == "approve"
    return json.dumps({"verdict": verdict, "tree": tree, "summary": "reviewed",
                       "checks": [{"criterion": i, "passed": approve, "evidence": "tests"} for i in ids],
                       "findings": [] if approve else [{"severity": "blocker", "path": "app.txt", "line": 1,
                                                        "issue": issue, "verification": "page 10 of 10"}]})


class SwarmBase(unittest.TestCase):
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
                "steps": {"architect": 0, "implementer": 2, "adversary": 2, "judge": 1, "decomposer": 1},
                **kw}

    def worker(self, c, q=None):
        return swarmd.Worker(0, c, q or swarmd.Queue(), Mock(), threading.Event())


class TrunkTests(SwarmBase):
    def test_second_task_builds_on_first_and_main_is_untouched(self):
        repo, git = self.repo()
        head = git("rev-parse", "HEAD")
        seen = []

        def fake(prompt, cwd, c, role, *args):
            if role == "implementer":
                p = cwd / "feature.txt"
                seen.append(p.read_text() if p.exists() else "")
                p.write_text(seen[-1] + "step\n")
                return "done"
            return review(prompt) if role == "adversary" else "done"
        c = self.cfg(repo)
        with patch.object(swarmd, "flint", side_effect=fake):
            for title in ("one", "two"):
                ok, note = self.worker(c).do_task({"id": f"t-{title}", "title": title, "detail": ""}, "goal")
                self.assertTrue(ok, note)
        self.assertEqual(seen, ["", "step\n"])
        self.assertEqual(git("show", "swarm/trunk:feature.txt"), "step\nstep")
        self.assertEqual(git("rev-parse", "main"), head)
        self.assertEqual(git("rev-list", "--count", "main..swarm/trunk"), "2")

    def _race(self, filename):
        """An implementer during whose turn another worker lands on trunk."""
        repo, git = self.repo()

        def fake(prompt, cwd, c, role, *args):
            if role == "implementer":
                (cwd / "app.txt").write_text("worker\n")
                other = self.root / "other"
                git("worktree", "add", "-b", "other", str(other), "swarm/trunk")
                (other / filename).write_text("other\n")
                subprocess.run(["git", "-C", str(other), "add", "-A"], check=True)
                subprocess.run(["git", "-C", str(other), "commit", "-qm", "other"], check=True)
                git("update-ref", "refs/heads/swarm/trunk", "other")
                return "done"
            return review(prompt) if role == "adversary" else "done"
        w = self.worker(self.cfg(repo))
        with patch.object(swarmd, "flint", side_effect=fake):
            ok, note = w.do_task({"id": "t1", "title": "race", "detail": ""}, "goal")
        return git, w, ok, note

    def test_conflicting_trunk_change_is_not_landed(self):
        git, w, ok, _ = self._race("app.txt")
        self.assertFalse(ok)
        self.assertEqual(w.stage, "conflict")
        self.assertEqual(git("show", "swarm/trunk:app.txt"), "other")
        self.assertEqual(git("show", f"{w.branch}:app.txt"), "worker")

    def test_independent_trunk_change_is_rebased_and_landed(self):
        git, w, ok, note = self._race("other.txt")
        self.assertTrue(ok, note)
        self.assertEqual(git("show", "swarm/trunk:app.txt"), "worker")
        self.assertEqual(git("show", "swarm/trunk:other.txt"), "other")

    def test_trunk_checked_out_by_the_user_is_not_moved(self):
        repo, git = self.repo()
        c = self.cfg(repo)
        swarmd.ensure_trunk(c)
        git("checkout", "-q", "swarm/trunk")
        before = git("rev-parse", "swarm/trunk")

        def fake(prompt, cwd, c, role, *args):
            if role == "implementer":
                (cwd / "app.txt").write_text("worker\n")
            return review(prompt) if role == "adversary" else "done"
        w = self.worker(c)
        with patch.object(swarmd, "flint", side_effect=fake):
            ok, note = w.do_task({"id": "t1", "title": "x", "detail": ""}, "goal")
        self.assertFalse(ok)
        self.assertIn("checked out", note)
        self.assertEqual(git("rev-parse", "swarm/trunk"), before)
        self.assertEqual((repo / "app.txt").read_text(), "baseline\n")

    def test_worktrees_are_removed_but_work_is_kept_on_branches(self):
        repo, git = self.repo()

        def fake(prompt, cwd, c, role, *args):
            if role == "implementer":
                (cwd / "app.txt").write_text("attempt\n")
                return "done"
            return review(prompt, "request_changes") if role == "adversary" else "tried"
        w = self.worker(self.cfg(repo, keep_worktrees=False))
        with patch.object(swarmd, "flint", side_effect=fake):
            ok, _ = w.do_task({"id": "t1", "title": "x", "detail": ""}, "goal")
        self.assertFalse(ok)
        self.assertFalse(w.wt.exists())
        self.assertEqual(git("show", f"{w.branch}:app.txt"), "attempt")


class ReinforcementTests(SwarmBase):
    JUDGE = json.dumps({"impact": 9, "creativity": 9, "quality": 8, "breakthrough": True,
                        "why": "new capability", "lesson": "Model levels as data so new mechanics need no code",
                        "follow_ups": [{"title": "Level editor", "detail": "round-trips a level"},
                                       {"title": "Level sharing", "detail": "export and import"}]})

    def test_breakthrough_is_reinforced_recorded_and_followed_up(self):
        repo, _ = self.repo()
        c = self.cfg(repo)
        q = swarmd.Queue()
        task = q.add("Data-driven levels", "levels load from JSON", persona="inventor", planner_model="b:free")
        w = self.worker(c, q)
        scores = parse_scores(self.JUDGE)
        w._reinforce(task, "accepted", {"implementer": "a:free", "scores": scores, "repairs": 0})
        snap = w.ledger.snapshot()
        impl = snap["arms"]["implementer"]["a:free"]
        r = snap["accepted"][0]["reward"]
        # Landing counts double; a breakthrough adds three more pulls at full reward.
        self.assertAlmostEqual(impl["a"], 1 + learn.DISCOUNT * (2 * r) + learn.BREAKTHROUGH_WEIGHT)
        self.assertEqual((impl["n"], snap["arms"]["persona"]["inventor"]["n"]), (2, 2))
        self.assertEqual(snap["arms"]["planner"]["b:free"]["n"], 1)
        self.assertTrue(snap["accepted"][0]["breakthrough"])
        self.assertTrue(w.ledger.playbook()[0][0]["pinned"])
        self.assertIn("Data-driven levels", (swarmd.STATE / "BREAKTHROUGHS.md").read_text())
        follow = [t for t in q.pending() if t.get("origin") == "follow_up"]
        self.assertEqual(sorted(t["title"] for t in follow), ["Level editor", "Level sharing"])
        self.assertTrue(all(t["priority"] == 2 and t["persona"] == "inventor" for t in follow))
        swarmd.ensure_trunk(c)
        report = swarmd.build_report(c)
        self.assertIn("★ **Data-driven levels**", report)
        self.assertIn("main..swarm/trunk", report)

    def test_faulty_code_is_hung_from_the_rafters_for_every_later_agent(self):
        repo, git = self.repo()
        c = self.cfg(repo, models=["sloppy:free"])
        prompts = []

        def fake(prompt, cwd, c, role, worker, budget, steps, model):
            if role == "implementer":
                prompts.append(prompt)
                (cwd / "app.txt").write_text("page = items[offset:offset + size - 1]\n")
                return "done"
            return review(prompt, "request_changes") if role == "adversary" else "tried again"
        q = swarmd.Queue()
        task = q.add("Paginate results", "pages of 10", persona="builder", planner_model="p:free")
        q.claim()
        w = self.worker(c, q)
        with patch.object(swarmd, "flint", side_effect=fake):
            ok, note = w.do_task(task, "goal")
            self.assertFalse(ok)
            self.assertEqual(w.stage, "rejected")
            retry = q.release(task["id"], ok, note)
            self.worker(c, q).do_task(retry, "goal")
        snap = w.ledger.snapshot()
        arm = snap["arms"]["implementer"]["sloppy:free"]
        self.assertEqual((arm["n"], arm["total"], arm["a"]), (2, 0.0, 1.0))
        self.assertAlmostEqual(arm["b"], 1 + learn.DISCOUNT * 4 + 4)  # hung twice, 4 pulls each
        persona = snap["arms"]["persona"]["builder"]
        self.assertLess(persona["b"], arm["b"])  # the planner is not punished for the author's code
        exhibit = snap["shame"][0]
        self.assertEqual((exhibit["model"], exhibit["stage"]), ("sloppy:free", "rejected"))
        self.assertEqual(exhibit["text"], "app.txt:1 off-by-one at the last index (repro: page 10 of 10)")
        self.assertIn("+page = items[offset:offset + size - 1]", exhibit["exhibit"])
        rafters = (swarmd.STATE / "RAFTERS.md").read_text()
        self.assertIn("`sloppy:free` shipped a defect the reviewer proved", rafters)
        self.assertIn(w.branch, rafters)
        # The next implementer sees the exhibit, named, with the offending code.
        self.assertIn("HUNG FROM THE RAFTERS", prompts[1])
        self.assertIn("`sloppy:free` shipped a defect the reviewer proved on 'Paginate results'", prompts[1])
        self.assertIn("+page = items[offset:offset + size - 1]", prompts[1])
        self.assertNotIn("HUNG FROM THE RAFTERS", prompts[0])

    def test_a_reviewer_that_cannot_deliver_a_verdict_is_penalised(self):
        repo, _ = self.repo()
        used = {}

        def fake(prompt, cwd, c, role, worker, budget, steps, model):
            used[role] = model
            if role == "implementer":
                (cwd / "app.txt").write_text("change\n")
                return "done"
            return "Looks fine to me!"
        w = self.worker(self.cfg(repo, models=["a:free", "b:free"]))
        with patch.object(swarmd, "flint", side_effect=fake):
            ok, _ = w.do_task({"id": "t1", "title": "x", "detail": ""}, "goal")
        self.assertFalse(ok)
        self.assertEqual(w.stage, "review_error")
        arms = w.ledger.snapshot()["arms"]["implementer"]
        self.assertEqual(arms[used["adversary"]]["b"], 1 + learn.PENALTY_WEIGHT["model_error"])
        self.assertNotIn(used["implementer"], arms)  # the implementer is not blamed

    def test_provider_outage_rests_the_model_without_penalty(self):
        repo, _ = self.repo()

        def fake(prompt, cwd, c, role, worker, budget, steps, model):
            raise swarmd.ProviderDown(model, "upstream overloaded")
        w = self.worker(self.cfg(repo, models=["a:free"]))
        with patch.object(swarmd, "flint", side_effect=fake), self.assertRaises(swarmd.ProviderDown):
            w.do_task({"id": "t1", "title": "x", "detail": ""}, "goal")
        snap = w.ledger.snapshot()
        self.assertIn("a:free", snap["cooldown"])
        self.assertEqual(snap["arms"], {})
        self.assertIsNone(w.pick())


class RecursionTests(SwarmBase):
    def test_task_failed_twice_splits_into_prioritised_subtasks(self):
        repo, _ = self.repo()
        q = swarmd.Queue(max_depth=1)
        t = q.add("Big feature", "everything at once", persona="builder", planner_model="p:free")
        q.claim()
        self.assertEqual(q.release(t["id"], False, "tests failed: KeyError 'x'")["status"], "retry")
        split = q.release(t["id"], False, "REJECT: breaks on empty input")
        self.assertEqual(split["status"], "split")
        seen = {}

        def fake(prompt, cwd, c, role, worker, budget, steps, model):
            seen["prompt"], seen["role"] = prompt, role
            return json.dumps([{"title": "Parse input", "detail": "a", "kind": "feature"},
                               {"title": "Handle empty input", "detail": "b", "kind": "bugfix"},
                               {"detail": "no title"}])
        w = self.worker(self.cfg(repo), q)
        w.evidence = None  # decomposition happens between attempts, not inside one
        with patch.object(swarmd, "flint", side_effect=fake):
            self.assertEqual(w.decompose(split, "goal"), 2)
        self.assertEqual(seen["role"], "decomposer")
        self.assertIn("breaks on empty input", seen["prompt"])
        first = q.claim()
        self.assertEqual((first["title"], first["priority"], first["depth"], first["parent"],
                          first["persona"], first["origin"]),
                         ("Parse input", 1, 1, t["id"], "builder", "split"))

    def test_planner_tasks_carry_credit_and_a_botched_plan_is_penalised(self):
        repo, _ = self.repo()
        c = self.cfg(repo, models=["p:free"])
        c["steps"]["planner"] = 3
        q = swarmd.Queue()
        with patch.object(swarmd, "flint", return_value="You should add streaks."):
            with self.assertRaises(swarmd.ModelError):
                swarmd.plan(c, q, Mock(), 3)
        arm = learn.Ledger(swarmd.STATE / "learn.json").snapshot()["arms"]["planner"]["p:free"]
        self.assertEqual((arm["n"], arm["total"]), (1, 0.0))
        reply = 'Plan:\n[{"title": "Streaks", "detail": "current and longest", "kind": "feature"}]'
        with patch.object(swarmd, "flint", return_value=reply):
            self.assertEqual(swarmd.plan(c, q, Mock(), 3), 1)
        task = q.claim()
        self.assertIn(task["persona"], swarmd.PERSONAS)
        self.assertEqual((task["planner_model"], task["origin"]), ("p:free", "plan"))

    def test_subtasks_park_instead_of_splitting_forever(self):
        q = swarmd.Queue(max_depth=1)
        parent = q.add("Big piece")
        t = q.add("Small piece", parent=parent["id"])
        self.assertEqual(t["depth"], 1)
        q.release(t["id"], False, "x")
        self.assertEqual(q.release(t["id"], False, "y")["status"], "parked")

    def test_priority_then_age_decides_what_runs_next(self):
        q = swarmd.Queue()
        q.add("old normal")
        q.add("split child", priority=1)
        q.add("breakthrough follow-up", priority=2)
        self.assertEqual([q.claim()["title"] for _ in range(3)],
                         ["breakthrough follow-up", "split child", "old normal"])


class GuardTests(SwarmBase):
    def test_paid_models_are_dropped_or_refused(self):
        self.assertEqual(swarmd.pool({"models": ["a:free", "openai/gpt-x"]}), ["a:free"])
        self.assertEqual(len(swarmd.pool({"models": ["a:free", "openai/gpt-x"], "allow_paid": True})), 2)
        with self.assertRaisesRegex(SystemExit, "spend credits"):
            swarmd.preflight({"models": ["openai/gpt-x"]})

    def test_role_exit_codes_map_to_swarm_exceptions(self):
        budget = Mock(cap=10, reserve=1)
        budget.check.return_value = (True, 0, "ok")
        for rc, exc in ((7, swarmd.ProviderDown), (5, swarmd.StepLimit),
                        (1, swarmd.ModelError), (3, swarmd.CapReached), (4, swarmd.NoCredits)):
            p = Mock(returncode=rc)
            p.communicate.return_value = ("", None)

            class Fake:
                def __init__(self, *a, **kw):
                    pass

                def __enter__(self):
                    return p

                def __exit__(self, *a):
                    return False
            with self.subTest(rc=rc), patch.object(swarmd, "process", Fake), self.assertRaises(exc):
                swarmd.flint("x", self.root, {"python": "python3"}, "implementer", "w0", budget, 1, "a:free")

    def test_detect_test_cmd(self):
        cases = [({"go.mod": ""}, "go test ./..."),
                 ({"package.json": '{"scripts": {"test": "vitest run"}}', "package-lock.json": "{}"},
                  "npm ci --silent --no-audit --no-fund && npm test --silent"),
                 ({"package.json": '{"scripts": {"test": "echo \\"Error: no test specified\\" && exit 1"}}'}, None)]
        for files, expected in cases:
            d = self.root / uuid.uuid4().hex
            d.mkdir()
            for name, text in files.items():
                (d / name).write_text(text)
            with self.subTest(files=list(files)):
                self.assertEqual(swarmd.detect_test_cmd(d), expected)

    def test_gate_and_headless_shell_do_not_see_secrets(self):
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-or-secret", "FLINT_HEADLESS": "1"}):
            _, out = swarmd.run_gate(self.root, {"test_cmd": "env"})
            shell = flint.bash("env")
        self.assertNotIn("sk-or-secret", out)
        self.assertNotIn("sk-or-secret", shell)

    def test_study_tool_searches_the_corpus(self):
        from swarm import corpus_index
        src = self.root / "notes"
        src.mkdir()
        (src / "dp.md").write_text("# Dynamic programming\n\n" + "Memoize overlapping subproblems "
                                   "and define the recurrence before coding it. " * 5)
        db = self.root / "corpus.db"
        corpus_index.build(src, db, verbose=False)
        with patch.object(flint, "CORPUS_DB", db):
            self.assertTrue(flint._corpus_ready())
            self.assertIn("overlapping subproblems", flint.study("memoize subproblems"))
        a = flint.Agent.__new__(flint.Agent)
        a.read_only, a.model, a.messages = True, "m", []
        for ready in (True, False):
            a.corpus = ready
            names = [t["function"]["name"] for t in a._request_kwargs()["tools"]]
            self.assertEqual("study" in names, ready)

    def test_corpus_chunks_never_cross_a_heading(self):
        from swarm import corpus_index
        text = ("## Lecture 1: Hashing\n\n" + "universal hash families " * 60 + "\n\n"
                + "## Lecture 2: Heaps\n\n" + "binary heap sift down " * 60)
        chunks = list(corpus_index.chunk(text))
        self.assertEqual([h for h, _ in chunks], ["Lecture 1: Hashing", "Lecture 2: Heaps"])
        self.assertNotIn("heap", chunks[0][1])
        self.assertNotIn("hash", chunks[1][1])

    def test_corpus_prune_drops_deleted_files(self):
        from swarm import corpus_index
        src = self.root / "prune-notes"
        src.mkdir()
        for n in ("a", "b"):
            (src / f"{n}.md").write_text(f"# {n}\n\n" + f"distinctive{n} content paragraph " * 10)
        db = self.root / "prune.db"
        corpus_index.build(src, db, verbose=False)
        (src / "b.md").unlink()
        self.assertEqual(corpus_index.prune(src, db, verbose=False), 1)
        self.assertEqual(corpus_index.search("distinctiveb", db=db), [])
        self.assertTrue(corpus_index.search("distinctivea", db=db))


class SandboxTests(unittest.TestCase):
    @unittest.skipUnless(sandbox.available(), "sandbox-exec cannot run here (unavailable or nested)")
    def test_gate_cannot_write_outside_its_worktree_or_read_env_files(self):
        with tempfile.TemporaryDirectory() as td:
            wt = Path(td)
            outside = Path(__file__).resolve().parent / f".sandbox-probe-{uuid.uuid4().hex}"
            env_file = Path(__file__).resolve().parent.parent / ".env"
            try:
                ok, out = swarmd.run_gate(wt, {"sandbox": True, "test_cmd":
                                               f"touch '{outside}'; echo ok > inside.txt; cat '{env_file}'"})
                self.assertFalse(outside.exists())
                self.assertTrue((wt / "inside.txt").exists())
                self.assertIn("Operation not permitted", out)
                self.assertNotIn("OPENROUTER", out)
            finally:
                outside.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
