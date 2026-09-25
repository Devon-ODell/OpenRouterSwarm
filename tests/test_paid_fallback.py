"""The dollar budget and the free-first paid fallback.

Free models do the work. When free capacity runs out the swarm may continue on a paid
model, but only while that repository's own daily dollar budget lasts, so several swarms
on one OpenRouter key cannot spend each other's money.
"""
import datetime as dt
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import flint
from swarm import swarmd
from swarm.budget import Budget

UTC_TODAY = dt.datetime.now(dt.timezone.utc).date().isoformat()


class SpendLedgerTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = Path(self.dir.name) / "spend.json"

    def ledger(self, cap=1.0):
        return flint.Spend(str(self.path), cap)

    def test_costs_accumulate_and_the_cap_is_enforced(self):
        s = self.ledger()
        s.check()                       # nothing spent yet
        s.add(0.40)
        s.add(0.35)
        self.assertAlmostEqual(s.today(), 0.75)
        self.assertAlmostEqual(s.remaining(), 0.25)
        s.check()                       # still under
        s.add(0.30)
        with self.assertRaises(flint.SpendExhausted) as caught:
            s.check()
        self.assertAlmostEqual(caught.exception.cap, 1.0)
        self.assertEqual(s.remaining(), 0.0)

    def test_a_ledger_from_an_earlier_day_starts_the_new_day_at_zero(self):
        self.path.write_text(json.dumps({"day": "2020-01-01", "usd": 99.0, "requests": 7}))
        s = self.ledger()
        self.assertEqual(s.today(), 0.0)
        s.check()
        self.assertEqual(json.loads(self.path.read_text())["day"], UTC_TODAY)

    def test_free_replies_and_unusable_costs_are_ignored(self):
        s = self.ledger()
        for value in (0, 0.0, None, "", "free", float("nan") and None):
            s.add(value)
        self.assertEqual(s.today(), 0.0)

    def test_without_a_ledger_nothing_is_tracked_or_gated(self):
        s = flint.Spend(None, None)
        s.add(5.0)
        s.check()
        self.assertEqual(s.today(), 0.0)
        self.assertIsNone(s.remaining())

    def test_two_swarms_keep_separate_budgets(self):
        other = Path(self.dir.name) / "other" / "spend.json"
        a, b = self.ledger(), flint.Spend(str(other), 1.0)
        a.add(0.9)
        self.assertEqual(b.today(), 0.0)
        b.check()
        with self.assertRaises(flint.SpendExhausted):
            a.add(0.2)
            a.check()

    def test_a_reply_reports_what_openrouter_charged(self):
        class Usage:
            prompt_tokens = 10
            model_extra = {"cost": 0.0025}
        agent = flint.Agent.__new__(flint.Agent)
        agent.spend, agent.last_cost, agent.total_cost = self.ledger(), 0.0, 0.0
        agent._charge(Usage())
        agent._charge(Usage())
        self.assertAlmostEqual(agent.total_cost, 0.005)
        self.assertAlmostEqual(self.ledger().today(), 0.005)


class ThrottleTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        p = patch.object(flint, "STATE_DIR", Path(self.dir.name))
        p.start()
        self.addCleanup(p.stop)
        self.spent = json.dumps(dict(cap=10, reserve=0, owner_window=["00:00", "00:00"]))

    def counter(self):
        return json.loads((Path(self.dir.name) / "requests.json").read_text())

    def test_a_free_request_counts_against_the_free_allowance(self):
        t = flint.Throttle()
        t.acquire()
        self.assertEqual(self.counter()["count"], 1)

    def test_a_paid_request_is_not_counted_against_the_free_allowance(self):
        t = flint.Throttle()
        t.acquire()
        t.acquire(paid=True)
        t.acquire(paid=True)
        self.assertEqual(self.counter()["count"], 1)
        self.assertEqual(len(self.counter()["recent"]), 3, "still paced per minute")

    def test_a_paid_request_runs_even_though_the_free_allowance_is_spent(self):
        t = flint.Throttle()
        with patch.dict(os.environ, {"FLINT_SWARM_BUDGET": self.spent}):
            for _ in range(10):
                t.acquire()
            with self.assertRaises(flint.BudgetPaused):
                t.acquire()
            t.acquire(paid=True)        # the point of the fallback
        self.assertEqual(self.counter()["count"], 10)

    def test_a_paid_request_ignores_the_free_daily_cap_block(self):
        t = flint.Throttle()
        t.block(9e9)
        with self.assertRaises(flint.DailyCapReached):
            t.acquire()
        t.acquire(paid=True)


class StandInTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        p = patch.object(swarmd, "STATE", Path(self.dir.name))
        p.start()
        self.addCleanup(p.stop)
        self.c = {"allow_paid": True, "daily_usd": 1.0,
                  "models": ["dots-studio/dots-3-note-preview:free", "poolside/laguna-s-2.1:free"],
                  "paid_models": ["poolside/laguna-s-2.1", "inclusionai/ling-3.0-flash-fin"]}

    def spent(self, usd, day=UTC_TODAY):
        (Path(self.dir.name) / "spend.json").write_text(json.dumps({"day": day, "usd": usd}))

    def test_the_pool_stays_free_and_the_fallback_stays_paid(self):
        self.assertTrue(all(m.endswith(":free") for m in swarmd.pool(self.c)))
        self.assertEqual(swarmd.paid_pool(self.c), self.c["paid_models"])

    def test_a_model_falls_back_to_its_own_paid_twin(self):
        self.assertEqual(swarmd.paid_stand_in(self.c, "poolside/laguna-s-2.1:free"),
                         "poolside/laguna-s-2.1")

    def test_a_free_only_model_falls_back_to_another_paid_model(self):
        # dots-3 has no paid twin on OpenRouter; the turn continues on something else.
        self.assertEqual(swarmd.paid_stand_in(self.c, "dots-studio/dots-3-note-preview:free"),
                         "poolside/laguna-s-2.1")

    def test_nothing_is_offered_once_the_budget_is_spent(self):
        self.spent(1.0)
        self.assertEqual(swarmd.spend_left(self.c), 0.0)
        self.assertIsNone(swarmd.paid_stand_in(self.c, "poolside/laguna-s-2.1:free"))

    def test_yesterdays_spending_does_not_count_against_today(self):
        self.spent(50.0, day="2020-01-01")
        self.assertEqual(swarmd.spend_today(self.c), 0.0)
        self.assertEqual(swarmd.spend_left(self.c), 1.0)

    def test_without_allow_paid_there_is_no_fallback(self):
        c = dict(self.c, allow_paid=False)
        self.assertEqual(swarmd.paid_pool(c), [])
        self.assertIsNone(swarmd.paid_stand_in(c, "poolside/laguna-s-2.1:free"))

    def test_a_corrupt_ledger_is_treated_as_nothing_spent(self):
        (Path(self.dir.name) / "spend.json").write_text("{not json")
        self.assertEqual(swarmd.spend_today(self.c), 0.0)

    def test_every_free_model_resting_draws_a_paid_stand_in(self):
        w = swarmd.Worker.__new__(swarmd.Worker)
        w.c = self.c
        w.ledger = type("L", (), {"pick": staticmethod(lambda *a, **k: None)})()
        self.assertEqual(w.pick(), "poolside/laguna-s-2.1")
        self.assertEqual(w.pick(exclude={"poolside/laguna-s-2.1"}),
                         "inclusionai/ling-3.0-flash-fin")
        self.spent(1.0)
        self.assertIsNone(w.pick())


class OwnerWindowTests(unittest.TestCase):
    def test_paid_helps_when_the_allowance_is_spent(self):
        b = Budget(cap=10, reserve=0, owner_window=["00:00", "00:00"])
        state = {"day": UTC_TODAY, "count": 10}
        self.assertFalse(b.check(state=state)[0])
        with patch.object(Budget, "spent_today", staticmethod(lambda: 10)):
            self.assertTrue(b.paid_would_help())

    def test_paid_does_not_override_the_owner_window(self):
        # The window means "leave my machine alone", not "free requests ran out".
        b = Budget(cap=10, reserve=0, owner_window=["00:00", "23:59"])
        self.assertFalse(b.paid_would_help())

    def test_paid_is_not_reached_for_while_free_requests_remain(self):
        b = Budget(cap=10, reserve=0, owner_window=["00:00", "00:00"])
        with patch.object(Budget, "spent_today", staticmethod(lambda: 0)):
            self.assertFalse(b.paid_would_help())


class PreflightTests(unittest.TestCase):
    def guard(self, **cfg):
        c = {"models": ["a:free"], "paid_models": ["vendor/paid"], "daily_usd": 1.0,
             "allow_paid": True, **cfg}
        with patch.object(swarmd, "account", lambda: None), \
                patch.object(swarmd, "free_tool_models", lambda: ["a:free"]), \
                patch.object(swarmd, "log", lambda *a, **k: None):
            with self.assertRaises(SystemExit) as e:
                swarmd.preflight(c)
        return str(e.exception)

    def test_a_fallback_without_allow_paid_is_refused(self):
        self.assertIn("allow_paid", self.guard(allow_paid=False))

    def test_a_fallback_without_a_dollar_budget_is_refused(self):
        self.assertIn("daily_usd", self.guard(daily_usd=None))

    def test_a_free_id_in_the_paid_list_is_refused(self):
        self.assertIn("are free", self.guard(paid_models=["a:free"]))


class TurnTests(unittest.TestCase):
    """The whole path: free capacity gone -> the turn runs, on a paid model."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        root = Path(self.dir.name)
        for name in ("state", "logs"):
            (root / name).mkdir()
        for target, name, value in ((swarmd, "STATE", root / "state"), (swarmd, "LOGS", root / "logs")):
            pt = patch.object(target, name, value)
            pt.start()
            self.addCleanup(pt.stop)
        swarmd._stop.clear()
        self.c = {"allow_paid": True, "daily_usd": 1.0, "python": sys.executable,
                  "models": ["dots-studio/dots-3-note-preview:free"],
                  "paid_models": ["poolside/laguna-s-2.1"], "test_cmd": "true"}
        self.cmds = []

    def run_turn(self, budget):
        class FakeProc:
            returncode = 0

            def communicate(self, timeout=None):
                return "done", ""

        import contextlib

        @contextlib.contextmanager
        def fake_process(cmd, **kw):
            self.cmds.append(cmd)
            yield FakeProc()

        with patch.object(swarmd, "process", fake_process), \
                patch.object(swarmd, "sandboxed", lambda cmd, cwd, c: cmd), \
                patch.object(swarmd, "log", lambda *a, **k: None):
            return swarmd.flint("prompt", self.dir.name, self.c, "implementer", "w0",
                                budget, 5, "dots-studio/dots-3-note-preview:free")

    def budget(self, allowed, paid_helps=True):
        class B:
            cap, reserve = 500, 100

            def check(self, need=1, state=None):
                return (True, 0, "ok") if allowed else (False, 900, "day's allowance spent")

            def paid_would_help(self, state=None):
                return paid_helps
        return B()

    def test_free_capacity_intact_keeps_the_free_model(self):
        self.run_turn(self.budget(allowed=True))
        self.assertIn("dots-studio/dots-3-note-preview:free", self.cmds[0])

    def test_the_turn_switches_to_a_paid_model_when_the_allowance_is_spent(self):
        self.run_turn(self.budget(allowed=False))
        self.assertIn("poolside/laguna-s-2.1", self.cmds[0])
        self.assertNotIn("dots-studio/dots-3-note-preview:free", self.cmds[0])
        rows = [json.loads(l) for l in (swarmd.STATE / "journal.jsonl").read_text().splitlines()]
        fallback = [r for r in rows if r["event"] == "paid_fallback"]
        self.assertEqual(fallback[0]["to"], "poolside/laguna-s-2.1")
        self.assertEqual([r["model"] for r in rows if r["event"] == "turn"],
                         ["poolside/laguna-s-2.1"], "the paid model gets the credit")

    def test_the_turn_hands_flint_this_repos_ledger_and_cap(self):
        captured = {}
        real = swarmd.subprocess

        class FakeProc:
            returncode = 0

            def communicate(self, timeout=None):
                return "done", ""
        import contextlib

        @contextlib.contextmanager
        def fake_process(cmd, **kw):
            captured.update(kw.get("env") or {})
            yield FakeProc()
        with patch.object(swarmd, "process", fake_process), \
                patch.object(swarmd, "sandboxed", lambda cmd, cwd, c: cmd), \
                patch.object(swarmd, "log", lambda *a, **k: None):
            swarmd.flint("p", self.dir.name, self.c, "implementer", "w0",
                         self.budget(allowed=True), 5, "dots-studio/dots-3-note-preview:free")
        self.assertEqual(captured["FLINT_SPEND_FILE"], str(swarmd.STATE / "spend.json"))
        self.assertEqual(captured["FLINT_SPEND_CAP"], "1.0")
        void = real

    def test_the_owner_window_is_not_overridden_by_the_budget(self):
        # paid_would_help() is False in the window, so the worker waits instead of spending.
        swarmd._stop.set()
        self.addCleanup(swarmd._stop.clear)
        with self.assertRaises(swarmd.Stopped):
            self.run_turn(self.budget(allowed=False, paid_helps=False))
        self.assertEqual(self.cmds, [])


if __name__ == "__main__":
    unittest.main()
