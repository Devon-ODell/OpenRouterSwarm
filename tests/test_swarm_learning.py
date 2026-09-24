"""Offline tests for the self-reinforcing swarm: temporary repos, no real API calls."""
import contextlib
import io
import json
import os
from pathlib import Path
import random
import re
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace as NS
import unittest
import uuid
from unittest.mock import Mock, patch

import flint
from swarm import learn, sandbox, swarmd, workflow
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

    def test_busy_rest_is_short_and_leaves_outage_strikes_alone(self):
        now = time.time()
        self.assertAlmostEqual(self.L.cool("a", "rate-limited upstream", busy=True) - now, learn.BUSY_COOLDOWN, delta=5)
        self.assertAlmostEqual(self.L.cool("a", "rate-limited upstream", busy=True) - now, 2 * learn.BUSY_COOLDOWN, delta=5)
        self.assertEqual(self.L.snapshot()["cooldown"]["a"]["strikes"], 0)
        self.assertIsNone(self.L.pick("implementer", ["a"]))
        self.assertEqual([m for m, _, _ in self.L.resting()], ["a"])

    def test_outage_strikes_from_an_earlier_run_are_forgotten(self):
        for _ in range(4):
            self.L.cool("a", "down")
        with self.L.txn() as d:   # a ledger from before `last` was recorded, rest long over
            d["cooldown"]["a"].pop("last")
            d["cooldown"]["a"]["until"] = time.time() - learn.COOLDOWN_MAX - 60
        self.assertAlmostEqual(self.L.cool("a", "down") - time.time(), learn.COOLDOWN_BASE, delta=5)

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


class BusyModelTests(SwarmBase):
    """A model rate-limited upstream ("busy") hands its turn over instead of sinking the attempt."""
    MODELS = ["a:free", "b:free", "c:free"]

    def test_busy_reviewer_hands_the_review_to_another_model(self):
        repo, _ = self.repo()
        calls = []

        def fake(prompt, cwd, c, role, worker, budget, steps, model):
            calls.append((role, model))
            if role == "implementer":
                (cwd / "app.txt").write_text("change\n")
                return "done"
            if role == "adversary" and [r for r, _ in calls].count("adversary") == 1:
                raise swarmd.ProviderBusy(model, "flint: model busy: rate-limited upstream")
            return review(prompt) if role == "adversary" else "{}"
        w = self.worker(self.cfg(repo, models=self.MODELS))
        with patch.object(swarmd, "flint", side_effect=fake):
            ok, note = w.do_task({"id": "t1", "title": "x", "detail": ""}, "goal")
        self.assertTrue(ok, note)
        author = calls[0][1]
        busy, reviewer = [m for r, m in calls if r == "adversary"]
        self.assertEqual(len({author, busy, reviewer}), 3)   # nobody reviews their own work
        rest = w.ledger.snapshot()["cooldown"][busy]
        self.assertEqual((rest["busy"], rest["strikes"]), (1, 0))
        self.assertLess(rest["until"] - time.time(), learn.BUSY_COOLDOWN + 5)
        handoff = [j for j in swarmd._read(swarmd.STATE / "journal.jsonl") if j["event"] == "handoff"]
        self.assertEqual([(j["role"], j["model"], j["to"], j["busy"]) for j in handoff],
                         [("adversary", busy, reviewer, True)])

    def test_busy_implementer_hands_over_before_editing_and_the_author_is_credited(self):
        repo, _ = self.repo()
        calls = []

        def fake(prompt, cwd, c, role, worker, budget, steps, model):
            calls.append((role, model))
            if role == "implementer":
                if len(calls) == 1:
                    raise swarmd.ProviderBusy(model, "rate-limited upstream")
                (cwd / "app.txt").write_text("change\n")
                return "done"
            return review(prompt) if role == "adversary" else "{}"
        w = self.worker(self.cfg(repo, models=self.MODELS))
        with patch.object(swarmd, "flint", side_effect=fake):
            ok, note = w.do_task({"id": "t1", "title": "x", "detail": ""}, "goal")
        self.assertTrue(ok, note)
        (_, busy), (_, author) = calls[:2]
        arms = w.ledger.snapshot()["arms"]["implementer"]
        self.assertIn(author, arms)
        self.assertNotIn(busy, arms)
        self.assertNotIn(author, [m for r, m in calls if r == "adversary"])

    def test_partial_work_is_never_finished_by_another_model(self):
        repo, _ = self.repo()
        calls = []

        def fake(prompt, cwd, c, role, worker, budget, steps, model):
            calls.append((role, model))
            (cwd / "app.txt").write_text("half done\n")
            raise swarmd.ProviderBusy(model, "rate-limited upstream")
        w = self.worker(self.cfg(repo, models=self.MODELS))
        with patch.object(swarmd, "flint", side_effect=fake), self.assertRaises(swarmd.ProviderBusy):
            w.do_task({"id": "t1", "title": "x", "detail": ""}, "goal")
        self.assertEqual(len(calls), 1)
        self.assertEqual(w.stage, "deferred")

    def test_when_every_model_rests_the_log_says_who_is_back_first(self):
        L = learn.Ledger(swarmd.STATE / "learn.json")
        L.cool("a:free", "down")
        L.cool("b:free", "busy", busy=True)
        L.cool("gone:free", "busy", busy=True)   # no longer in the pool: ignored
        c = {"models": ["a:free", "b:free"]}
        self.assertEqual(swarmd.next_wake(L, c)[0], "b:free")
        self.assertRegex(swarmd.all_resting(L, c), r"b:free is back at \d\d:\d\d:\d\d .*swarm wake")


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
        for rc, exc in ((7, swarmd.ProviderDown), (8, swarmd.ProviderBusy), (9, swarmd.ModelGone),
                        (5, swarmd.StepLimit),
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

    def test_a_folder_of_projects_is_tested_one_level_down(self):
        """The layout that stopped a real run: an umbrella folder holding several projects."""
        repo = self.root / "hedge-fund"
        (repo / "kraken-bot-trainingGrounds").mkdir(parents=True)
        (repo / "kraken-bot-trainingGrounds" / "go.mod").write_text("module x\n")
        (repo / "condor").mkdir()                               # empty: not a project
        (repo / "kraken-agent-files" / "signalman").mkdir(parents=True)
        (repo / "kraken-agent-files" / "signalman" / "go.mod").write_text("module y\n")   # too deep
        self.assertEqual(swarmd.detect_test_cmd(repo), "cd kraken-bot-trainingGrounds && go test ./...")
        # A second project one level down is ambiguous: the owner picks, and is shown the options.
        (repo / "condor" / "go.mod").write_text("module z\n")
        self.assertIsNone(swarmd.detect_test_cmd(repo))
        hint = swarmd.test_cmd_hint(repo)
        self.assertIn("--test-cmd 'cd condor && go test ./...'", hint)
        self.assertIn("--test-cmd 'cd kraken-bot-trainingGrounds && go test ./...'", hint)
        # A directory whose name needs quoting stays a single shell word.
        plain = self.root / "plain"
        (plain / "my app").mkdir(parents=True)
        (plain / "my app" / "go.mod").write_text("module w\n")
        self.assertEqual(swarmd.detect_test_cmd(plain), "cd 'my app' && go test ./...")
        self.assertIn("Nothing recognisable to test", swarmd.test_cmd_hint(self.root / "nope"))

    def test_a_project_in_its_own_git_repo_is_never_silently_tested(self):
        """An embedded repository's files are absent from the parent's worktrees."""
        repo, git = self.repo()
        inner = repo / "kraken-bot-trainingGrounds"
        inner.mkdir()
        (inner / "go.mod").write_text("module x\n")
        subprocess.run(["git", "-C", str(inner), "init", "-q", "-b", "main"], check=True)
        self.assertEqual(swarmd.embedded_repos(repo), ["kraken-bot-trainingGrounds"])
        self.assertIsNone(swarmd.detect_test_cmd(repo))          # not offered as the one project
        hint = swarmd.test_cmd_hint(repo)
        self.assertIn("its own Git repository", hint)
        self.assertIn(f"swarm grind {inner}", hint)
        c = self.cfg(repo, models=["a:free"], test_cmd="cd kraken-bot-trainingGrounds && go test ./...")
        with patch.object(swarmd, "account", return_value=None), \
                patch.object(swarmd, "free_tool_models", return_value={"a:free": {}}):
            with self.assertRaises(SystemExit) as exc:
                swarmd.preflight(c)
        self.assertIn("separate Git repository", str(exc.exception))
        # A test command that stays out of it runs as usual.
        with patch.object(swarmd, "account", return_value=None), \
                patch.object(swarmd, "free_tool_models", return_value={"a:free": {}}):
            swarmd.preflight(self.cfg(repo, models=["a:free"], test_cmd="true"))

    def test_added_tasks_carry_each_acceptance_criterion_separately(self):
        """The reviewer checks criteria one by one, so one blob is worth less than a list."""
        repo, _ = self.repo()
        c = self.cfg(repo)
        out = io.StringIO()
        args = NS(title="Enforce TRAIL in the pair backtest", detail="TRAIL is defined but unused.",
                  kind="bugfix", priority=2,
                  acceptance=["a position closes at the trailing stop when price retraces past it",
                              "a table-driven test covers a retrace that does and does not trigger"])
        with patch.object(swarmd, "_setup", return_value=c), contextlib.redirect_stdout(out):
            swarmd.cmd_add(args)
        task = swarmd.Queue().pending()[0]
        self.assertEqual([r["id"] for r in workflow.criteria(task)], ["C1", "C2"])
        self.assertIn("closes at the trailing stop", workflow.criteria(task)[0]["text"])
        self.assertEqual(task["priority"], 2)
        self.assertIn("C2", out.getvalue())
        # Without any, the detail stands as the single criterion, as before.
        with patch.object(swarmd, "_setup", return_value=c), contextlib.redirect_stdout(io.StringIO()):
            swarmd.cmd_add(NS(**{**vars(args), "title": "Another", "acceptance": None}))
        other = next(t for t in swarmd.Queue().pending() if t["title"] == "Another")
        self.assertEqual(workflow.criteria(other)[0]["text"], "TRAIL is defined but unused.")
        # A duplicate title is refused loudly rather than silently dropped.
        with patch.object(swarmd, "_setup", return_value=c), self.assertRaises(SystemExit) as exc:
            swarmd.cmd_add(args)
        self.assertIn("already tried", str(exc.exception))

    def test_a_long_turn_reports_which_model_it_is_waiting_on(self):
        """Minutes of silence during a turn read as a freeze, so each turn announces itself."""
        lines = []
        budget = Mock(cap=10, reserve=1)
        budget.check.return_value = (True, 0, "ok")
        p = Mock(returncode=0)
        p.communicate.return_value = ("answer", None)

        class Fake:
            def __init__(self, *a, **kw):
                pass

            def __enter__(self):
                return p

            def __exit__(self, *a):
                return False
        with patch.object(swarmd, "process", Fake), \
                patch.object(swarmd, "log", side_effect=lambda m, *a: lines.append(m)):
            swarmd.flint("x", self.root, {"python": "python3"}, "implementer", "w0", budget, 25, "a:free")
        self.assertIn("implementer: asking a:free (up to 25 rounds", lines[0])

    def test_a_working_worker_is_reported_alive_with_what_it_is_doing(self):
        w = self.worker(self.cfg(self.root))
        w.task, w.started, w.doing = {"title": "Ingest Binance order books"}, time.time() - 600, "implementer with a:free"
        idle = self.worker(self.cfg(self.root))          # claimed nothing: nothing to report
        idle.name, idle.task, idle.started = "w1", None, 0
        said, lines = {}, []
        with patch.object(swarmd, "log", side_effect=lambda m, *a: lines.append(m)), \
                patch.object(type(w), "is_alive", lambda self: True):
            swarmd.report_progress([w, idle], said)
            swarmd.report_progress([w, idle], said)       # again inside the interval: stays quiet
        self.assertEqual(len(lines), 1)
        self.assertIn("still on 'Ingest Binance order books' (10m): implementer with a:free", lines[0])
        with patch.object(swarmd, "log", side_effect=lambda m, *a: lines.append(m)), \
                patch.object(type(w), "is_alive", lambda self: True):
            swarmd.report_progress([w], said, every=0)     # once the interval passes, again
        self.assertEqual(len(lines), 2)

    def test_the_runner_up_test_command_is_offered_when_the_first_fails(self):
        """A Go service with Python beside it: the wrong guess must name the right one."""
        repo, _ = self.repo()
        (repo / "pyproject.toml").write_text("[project]\nname = 'x'\n")
        (repo / "engine").mkdir()
        (repo / "engine" / "go.mod").write_text("module x\n")
        chosen = swarmd.detect_test_cmd(repo)
        self.assertIn("python3 -m", chosen)                       # what it picked at the top
        other = swarmd.other_test_cmds(repo, chosen)
        self.assertIn("--test-cmd 'cd engine && go test ./...'", other)
        self.assertIn("(found engine/)", other)
        (repo / "go.mod").write_text("module x\n")                # now Go at the top level too
        self.assertIn("--test-cmd 'go test ./...'", swarmd.other_test_cmds(repo, chosen))
        self.assertEqual(swarmd.other_test_cmds(self.root / "empty", "true"), "")

    def test_config_is_seeded_from_the_template_and_not_tracked(self):
        """The swarm rewrites config.json every run, so git must not own it."""
        root = self.root / "checkout" / "swarm"
        root.mkdir(parents=True)
        (root / "config.example.json").write_text(json.dumps({"models": ["a:free"], "workers": 1}))
        with patch.object(swarmd, "CONFIG", root / "config.json"), \
                patch.object(swarmd, "EXAMPLE", root / "config.example.json"):
            self.assertEqual(swarmd.load_cfg()["models"], ["a:free"])
            self.assertTrue((root / "config.json").exists())
            (root / "config.json").write_text(json.dumps({"models": ["b:free"]}))
            self.assertEqual(swarmd.load_cfg()["models"], ["b:free"])   # never re-seeded over
        tracked = subprocess.run(["git", "ls-files", "--error-unmatch", "swarm/config.json"],
                                 cwd=Path(__file__).resolve().parent.parent, capture_output=True)
        self.assertNotEqual(tracked.returncode, 0, "swarm/config.json must not be tracked")

    def test_an_unrunnable_test_command_is_diagnosed_from_the_command(self):
        """Output only ever says "not found": the command itself says which problem it is."""
        installed = {"python3", "go", "true", "echo"}          # a Mac: python3, no python
        with patch.object(swarmd.shutil, "which", lambda c: c if c in installed else None):
            self.assertIn("macOS ships `python3`", swarmd.why_unrunnable("python -m pytest -q"))
            self.assertIn("not a description of the work",
                          swarmd.why_unrunnable("hello! I want you guys to update this extension"))
            self.assertIn("not installed or not on PATH", swarmd.why_unrunnable("nosuchtool --run"))
            self.assertEqual(swarmd.why_unrunnable("python3 -m pytest -q"), "")   # runnable
            self.assertEqual(swarmd.why_unrunnable("cd x && go test ./..."), "")  # compound: no guess
            self.assertEqual(swarmd.why_unrunnable(""), "")
            self.assertEqual(swarmd.why_unrunnable("echo 'unbalanced"), "")

    def test_goal_naming_the_goal_file_reads_the_file(self):
        repo, _ = self.repo()
        (repo / "GOAL.md").write_text("# Goal\n\nA triangular-arbitrage scanner.\n")
        (swarmd.STATE / "GOAL.md").write_text("a goal from an earlier run\n")
        c = self.cfg(repo)
        args = NS(repo=str(repo), goal="read GOAL.md", new=None, test_cmd="true",
                  hours=None, workers=None, max_tasks=0)
        with patch.object(swarmd, "configure"), patch.object(swarmd, "_setup", return_value=c), \
                patch.object(swarmd, "start"):
            swarmd.cmd_grind(args)
        self.assertFalse((swarmd.STATE / "GOAL.md").exists())   # the stale goal cannot shadow it
        self.assertIn("triangular-arbitrage scanner", swarmd.read_goal(c))
        # An actual goal is still stored and still wins.
        with patch.object(swarmd, "configure"), patch.object(swarmd, "_setup", return_value=c), \
                patch.object(swarmd, "start"):
            swarmd.cmd_grind(NS(**{**vars(args), "goal": "Build the ingest layer"}))
        self.assertEqual(swarmd.read_goal(c), "Build the ingest layer")

    def test_an_embedded_repo_note_says_how_to_work_on_it(self):
        repo, _ = self.repo()
        inner = repo / "kraken-bot-trainingGrounds"
        inner.mkdir()
        subprocess.run(["git", "-C", str(inner), "init", "-q", "-b", "main"], check=True)
        lines = []
        with patch.object(swarmd, "log", side_effect=lambda m, *a: lines.append(m)), \
                patch.object(swarmd, "account", return_value=None), \
                patch.object(swarmd, "free_tool_models", return_value={"a:free": {}}):
            swarmd.preflight(self.cfg(repo, models=["a:free"], test_cmd="true"))
        note = next(l for l in lines if l.startswith("NOTE:"))
        self.assertIn(f"swarm grind {inner}", note)          # the remedy, not just the diagnosis
        self.assertIn("is a Git repository of its own", note)

    def test_a_model_this_key_cannot_use_is_dropped_not_rested_and_retried(self):
        """403 "only available on agentic harnesses" is permanent: no penalty, no redraw."""
        repo, _ = self.repo()
        calls = []

        def fake(prompt, cwd, c, role, worker, budget, steps, model):
            calls.append(model)
            if model == "gated:free":
                raise swarmd.ModelGone(model, "flint: model unavailable: gated:free is not "
                                              "available to this API key (403): agentic harnesses only")
            if role == "implementer":
                (cwd / "app.txt").write_text("change\n")
                return "done"
            return review(prompt) if role == "adversary" else "{}"
        c = self.cfg(repo, models=["gated:free", "good:free", "other:free"])
        w = self.worker(c)
        with patch.object(swarmd, "flint", side_effect=fake):
            ok, note = w.do_task({"id": "t1", "title": "x", "detail": ""}, "goal")
        self.assertTrue(ok, note)
        snap = w.ledger.snapshot()
        self.assertTrue(snap["cooldown"]["gated:free"]["permanent"])
        self.assertNotIn("gated:free", snap["arms"].get("implementer", {}))   # never scored
        self.assertLessEqual(calls.count("gated:free"), 1)   # drawn once, then never again
        self.assertIsNone(w.ledger.pick("implementer", ["gated:free"]))
        # The log tells the owner what to do rather than promising it will come back.
        told = swarmd.rest(w.ledger, swarmd.ModelGone("gated:free", "403"))
        self.assertIn(str(swarmd.CONFIG), told)          # names the file holding the pool
        self.assertNotIn("resting it until", told)       # never promises it will come back

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
