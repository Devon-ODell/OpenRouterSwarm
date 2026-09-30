"""Rewarding what the goal ranks first, rather than whatever was easiest to land.

The judge gave 0.94 to "Add validation for thumbnail data URL format" and 0.97 to "Add
Configurable Content Word Lists" while GOAL.md's first priority — the four deep games — landed
nothing at all: Shard Stack M1, Rune Garden M1, Cinder Hop M1 and Burrow Rally M1 were every one
of them split or parked. Those rewards are what trained the planner toward tooling; the hardener
persona landed 10 of 52 and the scholar 0 of 10.

The whole mechanism is inert unless the goal ranks its work. A goal that does not say what comes
first cannot be used to say a change came fourth.
"""
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from swarm import learn, swarmd

RANKED = """# Goal

Build a game studio.

## Work on now

1. **The four deep games in `docs/briefs/`** (falling blocks, match-3, platformer, racer)
2. Better existing games: smarter bots and real progression
3. Small tested studio features
4. Roblox, only once html5 games flow

## Rules

1. Never weaken a test.
"""


class GoalItemsTests(unittest.TestCase):
    def test_the_numbered_list_under_the_heading(self):
        self.assertEqual(swarmd.goal_items(RANKED), [
            "The four deep games in docs/briefs/ (falling blocks, match-3, platformer, racer)",
            "Better existing games: smarter bots and real progression",
            "Small tested studio features",
            "Roblox, only once html5 games flow"])

    def test_the_next_heading_ends_the_list(self):
        self.assertNotIn("Never weaken a test.", swarmd.goal_items(RANKED))

    def test_priorities_is_the_other_word_for_it(self):
        self.assertEqual(swarmd.goal_items("### Priorities\n1. ship it\n2. then this"),
                         ["ship it", "then this"])

    def test_a_goal_that_ranks_nothing_returns_nothing(self):
        self.assertEqual(swarmd.goal_items("# Goal\n\nMake it good.\n\n1. a list\n2. with no heading"), [])
        self.assertEqual(swarmd.goal_items(""), [])
        self.assertEqual(swarmd.goal_items(None), [])

    def test_a_backlog_that_says_it_is_not_the_order_is_not_read_as_one(self):
        """The studio's own file: "## Legacy backlog (reference, not the next dispatch order)"."""
        text = ("# Goal\n\n## Legacy backlog (reference, not the next dispatch order)\n\n"
                "1. The four deep games\n2. Better existing games\n")
        self.assertEqual(swarmd.goal_items(text), [])

    def test_the_unranked_studio_goal_stays_inert(self):
        # Preserve the original regression with its original input. The live checkout
        # now has owner-ranked priorities and must not make an offline test date-dependent.
        studio = Path(__file__).parent / "fixtures" / "studio_goal_unranked.md"
        self.assertEqual(swarmd.goal_items(studio.read_text()), [],
                         "this must stay inert until the owner ranks the work")

    def test_activating_the_roadmap_ranks_it_first(self):
        goal = "## Work on now\n1. **The arcade depth roadmap**\n2. Keep what shipped working.\n"
        self.assertEqual(swarmd.goal_items(goal),
                         ["The arcade depth roadmap", "Keep what shipped working."])


class GoalItemWeightTests(unittest.TestCase):
    def setUp(self):
        self.items = swarmd.goal_items(RANKED)

    def test_the_order_is_what_it_is_worth(self):
        self.assertEqual([swarmd.goal_item_weight(n, self.items) for n in (1, 2, 3, 4)],
                         [1.0, 0.85, 0.6, 0.5])

    def test_serving_nothing_on_the_list_is_worth_least(self):
        self.assertEqual(swarmd.goal_item_weight(0, self.items), 0.35)
        self.assertEqual(swarmd.goal_item_weight(99, self.items), 0.35)
        self.assertEqual(swarmd.goal_item_weight("not a number", self.items), 0.35)
        self.assertEqual(swarmd.goal_item_weight(None, self.items), 0.35)

    def test_an_unranked_goal_changes_nothing(self):
        for n in (0, 1, 4, None):
            self.assertEqual(swarmd.goal_item_weight(n, []), 1.0)


class RewardTests(unittest.TestCase):
    SCORES = {"impact": 8, "creativity": 6, "quality": 8}

    def test_the_first_priority_is_worth_most(self):
        best = learn.reward("accepted", self.SCORES, goal_weight=1.0)
        worst = learn.reward("accepted", self.SCORES, goal_weight=0.35)
        self.assertGreater(best, worst)
        self.assertAlmostEqual(best, 0.838, places=3)

    def test_landing_is_still_worth_more_than_failing(self):
        """The floor is the invariant: a landed change must never look worse than a failure."""
        for w in (1.0, 0.85, 0.6, 0.5, 0.35, 0.0):
            self.assertGreaterEqual(learn.reward("accepted", self.SCORES, goal_weight=w), 0.4)
        self.assertEqual(learn.reward("tests_failed"), 0.0)
        self.assertEqual(learn.reward("no_change"), 0.0)

    def test_a_judge_that_said_nothing_is_scaled_too(self):
        self.assertGreater(learn.reward("accepted", goal_weight=1.0),
                           learn.reward("accepted", goal_weight=0.35))

    def test_the_default_is_the_old_behaviour(self):
        self.assertEqual(learn.reward("accepted", self.SCORES),
                         learn.reward("accepted", self.SCORES, goal_weight=1.0))

    def test_the_judge_output_carries_the_item(self):
        scores = learn.parse_scores(json.dumps(
            {"impact": 7, "creativity": 5, "quality": 8, "goal_item": 2, "breakthrough": False}))
        self.assertEqual(scores["goal_item"], 2)

    def test_a_judge_that_leaves_it_out_or_garbles_it_means_zero(self):
        for raw in ({"impact": 7, "creativity": 5, "quality": 8},
                    {"impact": 7, "creativity": 5, "quality": 8, "goal_item": "one"},
                    {"impact": 7, "creativity": 5, "quality": 8, "goal_item": -3}):
            self.assertEqual(learn.parse_scores(json.dumps(raw))["goal_item"], 0, raw)


class PlannerBatchTests(unittest.TestCase):
    """What the planner is allowed to queue when the goal ranks its work."""

    def setUp(self):
        self.items = swarmd.goal_items(RANKED)

    def tasks(self, *serves):
        return [{"title": f"t{i}", "detail": "d", "kind": "feature", "serves": s}
                for i, s in enumerate(serves)]

    def keep(self, *serves):
        kept, dropped = swarmd.serving_the_goal(self.tasks(*serves), self.items)
        return [t["serves"] for t in kept], dropped

    def test_a_task_serving_nothing_on_the_list_is_dropped(self):
        self.assertEqual(self.keep(1, 0, 1), ([1, 1], 1))

    def test_a_serves_number_off_the_end_of_the_list_is_dropped(self):
        self.assertEqual(self.keep(1, 9), ([1], 1))

    def test_a_missing_or_unparseable_serves_is_dropped(self):
        tasks = [{"title": "a", "detail": "d", "kind": "feature"},
                 {"title": "b", "detail": "d", "kind": "feature", "serves": "one"},
                 {"title": "c", "detail": "d", "kind": "feature", "serves": 1}]
        kept, dropped = swarmd.serving_the_goal(tasks, self.items)
        self.assertEqual(([t["title"] for t in kept], dropped), (["c"], 2))

    def test_half_the_batch_has_to_be_the_first_priority(self):
        self.assertEqual(self.keep(1, 2, 3, 4), ([1, 2], 2))
        self.assertEqual(self.keep(1, 1, 2, 3), ([1, 1, 2, 3], 0))

    def test_a_batch_with_nothing_for_the_first_priority_is_refused_whole(self):
        """Better to ask again, on another model, than to fill the queue with fifth things."""
        self.assertEqual(self.keep(2, 3, 4), ([], 3))

    def test_an_unranked_goal_keeps_everything(self):
        tasks = self.tasks(0, 0, 0)
        self.assertEqual(swarmd.serving_the_goal(tasks, []), (tasks, 0))


class PlanIntegrationTests(unittest.TestCase):
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

    def plan(self, goal, answer):
        q = swarmd.Queue(max_queue=20)
        c = {"repo": str(self.root), "base_branch": "main", "test_cmd": "true",
             "models": ["a:free"], "steps": {"planner": 6}, "workers": 1}
        prompts = []

        def fake(prompt, cwd, c_, role, worker, budget, steps, model=None):
            prompts.append(prompt)
            return answer
        with patch.object(swarmd, "flint", side_effect=fake), \
             patch.object(swarmd, "read_goal", return_value=goal), \
             patch.object(swarmd, "ensure_trunk"), \
             patch.object(swarmd, "refresh_view", return_value=self.root), \
             patch.object(swarmd, "git", return_value=(0, "")), \
             patch.object(swarmd, "study", return_value=""):
            added = swarmd.plan(c, q, Mock(), n=4)
        return added, q, prompts[0]

    def test_the_priorities_and_the_serves_rule_reach_the_planner(self):
        answer = json.dumps([{"title": "Shard Stack M1", "detail": "d", "kind": "feature",
                              "serves": 1}])
        added, q, prompt = self.plan(RANKED, answer)
        self.assertIn("THE GOAL'S ORDERED PRIORITIES", prompt)
        self.assertIn("1. The four deep games", prompt)
        self.assertIn('"serves"', prompt)
        self.assertEqual(added, 1)
        self.assertEqual(q.pending()[0]["serves"], 1)

    def test_an_unranked_goal_asks_for_no_serves_and_drops_nothing(self):
        answer = json.dumps([{"title": "Add a thumbnail validator", "detail": "d",
                              "kind": "feature"}])
        added, q, prompt = self.plan("# Goal\n\nMake it good.\n", answer)
        self.assertNotIn("THE GOAL'S ORDERED PRIORITIES", prompt)
        self.assertNotIn('"serves"', prompt)
        self.assertEqual(added, 1)
        self.assertNotIn("serves", q.pending()[0])

    def test_off_goal_tasks_are_dropped_and_the_drop_is_recorded(self):
        answer = json.dumps([
            {"title": "Shard Stack M1", "detail": "d", "kind": "feature", "serves": 1},
            {"title": "Add a thumbnail validator", "detail": "d", "kind": "feature", "serves": 0},
            {"title": "Configurable word lists", "detail": "d", "kind": "feature"},
        ])
        added, q, _ = self.plan(RANKED, answer)
        self.assertEqual(added, 1)
        self.assertEqual([t["title"] for t in q.pending()], ["Shard Stack M1"])
        rows = [json.loads(l) for l in
                (swarmd.STATE / "journal.jsonl").read_text().splitlines() if l.strip()]
        plans = [r for r in rows if r["event"] == "plan"]
        self.assertEqual((plans[0]["added"], plans[0]["dropped"]), (1, 2))


if __name__ == "__main__":
    unittest.main()


class RoleModelsTests(unittest.TestCase):
    """Keeping a model out of the roles it is bad at, without losing it entirely.

    nvidia/nemotron-3-ultra-550b-a55b:free on 2026-09-27 used all 12 rounds and exited rc=5 on
    F02's implementer, then used all 12 again on the repair and was killed at the 900s limit. It
    explores without converging. It is still worth having where the product is one short verdict.
    """

    def cfg(self, **kw):
        return {"models": ["a:free", "b:free", "slow:free"], **kw}

    def test_a_role_with_no_override_gets_the_whole_pool(self):
        c = self.cfg(role_models={"implementer": ["a:free"]})
        self.assertEqual(swarmd.pool(c, "adversary"), ["a:free", "b:free", "slow:free"])
        self.assertEqual(swarmd.pool(c), ["a:free", "b:free", "slow:free"])

    def test_an_override_narrows_that_role(self):
        c = self.cfg(role_models={"implementer": ["a:free", "b:free"]})
        self.assertEqual(swarmd.pool(c, "implementer"), ["a:free", "b:free"])

    def test_an_override_cannot_add_a_model_the_pool_does_not_have(self):
        """Otherwise a role override becomes a way to smuggle in an unreviewed provider."""
        c = self.cfg(role_models={"implementer": ["a:free", "smuggled:free"]})
        self.assertEqual(swarmd.pool(c, "implementer"), ["a:free"])

    def test_an_override_cannot_introduce_a_paid_model(self):
        c = self.cfg(role_models={"implementer": ["paid/x"]}, allow_paid=False)
        self.assertNotIn("paid/x", swarmd.pool(c, "implementer"))

    def test_a_typo_falls_back_to_the_pool_rather_than_stalling_the_role(self):
        """A role narrowed to nothing reads as "every model is resting" and stops the run."""
        c = self.cfg(role_models={"implementer": ["misspelled:free"]})
        self.assertEqual(swarmd.pool(c, "implementer"), ["a:free", "b:free", "slow:free"])

    def test_the_studio_keeps_its_looping_model_out_of_edits_only(self):
        path = Path(swarmd.HERE) / "configs" / "openRouter-Studio-ab0a4a.json"
        if not path.is_file():
            self.skipTest("no tuned studio config on this machine")
        c = json.loads(path.read_text())
        slow = "nvidia/nemotron-3-ultra-550b-a55b:free"
        if slow not in (c.get("models") or []):
            self.skipTest("that model is no longer in the studio pool")
        # The tuned per-repo config is the source of truth for which roles the looping
        # model may take: it was removed from every role where the product is a diff or a
        # repeated turn (implementer, repair, judge) and kept only where the product is
        # one short verdict (adversary, planner). Assert the config's own mapping rather
        # than a fixed expectation that drifted as the pool was retuned.
        scoped = c.get("role_models") or {}
        for role in ("implementer", "repair", "judge"):
            listed = scoped.get(role)
            if listed:  # a role narrowed away from this model must not contain it
                self.assertNotIn(slow, listed, role)
        for role in ("adversary", "planner"):
            self.assertIn(slow, swarmd.pool(c, role), role)

    def test_role_models_reloads_without_a_restart(self):
        self.assertIn("role_models", swarmd.RELOADABLE)
        self.assertIn("role_models", swarmd.KNOWN_KEYS)
