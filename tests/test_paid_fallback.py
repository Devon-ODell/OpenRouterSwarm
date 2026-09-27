"""The dollar budget and the free-first paid fallback.

Free models do the work. When free capacity runs out the swarm may continue on a paid
model, but only while its dollar budget lasts. A budget comes in two shapes: `daily_usd`
is one repository's own allowance for a UTC day, kept in its own ledger; `monthly_usd`
with `spend_reset_day` is a single pot shared by every swarm on the key, restarting on a
chosen day of the month. The shared shape exists so three swarms cannot each spend the
whole ceiling; the per-repository shape exists so they cannot spend each other's money.
"""
import datetime as dt
import json
import os
import sys
import tempfile
import time
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
        self.path.write_text(json.dumps({"period": "2020-01-01", "usd": 99.0, "requests": 7}))
        s = self.ledger()
        self.assertEqual(s.today(), 0.0)
        s.check()
        self.assertEqual(json.loads(self.path.read_text())["period"], UTC_TODAY)

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
        agent.model = "poolside/laguna-s-2.1"
        agent._charge(Usage())
        agent._charge(Usage())
        self.assertAlmostEqual(agent.total_cost, 0.005)
        self.assertAlmostEqual(self.ledger().today(), 0.005)
        # the charge names the model, so a wrong figure can be traced to its source
        rows = json.loads(self.path.read_text())["recent"]
        self.assertEqual([r["model"] for r in rows], ["poolside/laguna-s-2.1"] * 2)


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
        (Path(self.dir.name) / "spend.json").write_text(json.dumps({"period": day, "usd": usd}))

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
        self.assertEqual(swarmd.spend_used(self.c), 0.0)
        self.assertEqual(swarmd.spend_left(self.c), 1.0)

    def test_without_allow_paid_there_is_no_fallback(self):
        c = dict(self.c, allow_paid=False)
        self.assertEqual(swarmd.paid_pool(c), [])
        self.assertIsNone(swarmd.paid_stand_in(c, "poolside/laguna-s-2.1:free"))

    def test_a_corrupt_ledger_is_treated_as_nothing_spent(self):
        (Path(self.dir.name) / "spend.json").write_text("{not json")
        self.assertEqual(swarmd.spend_used(self.c), 0.0)

    def test_every_free_model_resting_draws_a_paid_stand_in(self):
        w = swarmd.Worker.__new__(swarmd.Worker)
        w.c = self.c
        w.ledger = type("L", (), {"pick": staticmethod(lambda *a, **k: None)})()
        self.assertEqual(w.pick(), "poolside/laguna-s-2.1")
        self.assertEqual(w.pick(exclude={"poolside/laguna-s-2.1"}),
                         "inclusionai/ling-3.0-flash-fin")
        self.spent(1.0)
        self.assertIsNone(w.pick())


class MonthlyBudgetTests(unittest.TestCase):
    """One shared pot over a month that restarts on a chosen day.

    `monthly_usd` is deliberately not per repository: the whole point is that three
    swarms on one key cannot each spend the ceiling. `spend_reset_day` moves the
    boundary off the 1st so the budget can follow a real billing date.
    """

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.here = Path(self.dir.name)
        (self.here / "state").mkdir(parents=True)
        for attr, value in (("HERE", self.here), ("STATE", self.here / "state" / "one-repo")):
            patcher = patch.object(swarmd, attr, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.shared = self.here / "state" / "spend.json"
        self.c = {"allow_paid": True, "monthly_usd": 20.0, "spend_reset_day": 5,
                  "models": ["poolside/laguna-s-2.1:free"],
                  "paid_models": ["poolside/laguna-s-2.1"]}

    def ledger(self, usd, period=None):
        self.shared.write_text(json.dumps(
            {"period": period or flint.spend_period(5), "usd": usd, "requests": 1}))

    def test_a_window_is_named_by_the_date_it_began(self):
        for day, want in (("2026-09-05", "2026-09-05"), ("2026-09-26", "2026-09-05"),
                          ("2026-10-04", "2026-09-05"), ("2026-10-05", "2026-10-05"),
                          ("2026-01-03", "2025-12-05")):
            self.assertEqual(flint.spend_period(5, dt.date.fromisoformat(day)), want, day)

    def test_a_month_too_short_for_the_reset_day_begins_on_its_last_day(self):
        # There is no 31st of February, so the window must not be skipped entirely.
        self.assertEqual(flint.spend_period(31, dt.date(2026, 2, 15)), "2026-01-31")
        self.assertEqual(flint.spend_period(31, dt.date(2026, 2, 28)), "2026-02-28")

    def test_no_reset_day_keeps_the_window_a_single_utc_day(self):
        self.assertEqual(flint.spend_period(None, dt.date(2026, 9, 26)), "2026-09-26")

    def test_a_shared_budget_uses_one_ledger_and_a_daily_one_keeps_its_own(self):
        self.assertEqual(swarmd.spend_file(self.c), self.shared)
        self.assertEqual(swarmd.spend_file({"daily_usd": 1.0}), swarmd.STATE / "spend.json")
        self.assertTrue(swarmd.monthly(self.c))
        self.assertFalse(swarmd.monthly({"daily_usd": 1.0}))

    def test_what_is_left_is_the_cap_less_the_shared_pot(self):
        self.ledger(17.5)
        self.assertEqual(swarmd.spend_cap(self.c), 20.0)
        self.assertAlmostEqual(swarmd.spend_used(self.c), 17.5)
        self.assertAlmostEqual(swarmd.spend_left(self.c), 2.5)

    def test_an_earlier_window_does_not_count_against_this_one(self):
        self.ledger(99.0, period="2020-01-05")
        self.assertEqual(swarmd.spend_used(self.c), 0.0)
        self.assertAlmostEqual(swarmd.spend_left(self.c), 20.0)

    def test_a_spent_month_offers_no_paid_stand_in(self):
        self.ledger(20.0)
        self.assertEqual(swarmd.spend_left(self.c), 0.0)
        self.assertIsNone(swarmd.paid_stand_in(self.c, "poolside/laguna-s-2.1:free"))

    def test_the_window_ends_on_the_reset_day_of_the_following_month(self):
        start = dt.date.fromisoformat(swarmd.spend_window(self.c))
        end = dt.date.fromisoformat(swarmd.spend_resets(self.c))
        self.assertEqual((start.day, end.day), (5, 5))
        self.assertLess(start, end)
        self.assertLessEqual((end - start).days, 31)

    def test_a_daily_budget_has_no_reset_date(self):
        self.assertIsNone(swarmd.spend_resets({"daily_usd": 1.0}))
        self.assertIsNone(swarmd.spend_reset_day({"daily_usd": 1.0}))

    def test_monthly_overrides_a_stray_daily_figure(self):
        self.assertEqual(swarmd.spend_cap(dict(self.c, daily_usd=1.0)), 20.0)

    def test_the_ledger_records_what_each_model_cost(self):
        s = flint.Spend(str(self.shared), 20.0, reset_day=5)
        s.add(0.004, "poolside/laguna-s-2.1")
        s.add(1.5, "qwen/qwen3.8-27b")
        rows = json.loads(self.shared.read_text())["recent"]
        self.assertEqual([r["model"] for r in rows],
                         ["poolside/laguna-s-2.1", "qwen/qwen3.8-27b"])
        self.assertEqual(rows[-1]["usd"], 1.5)
        self.assertAlmostEqual(s.today(), 1.504)

    def test_the_audit_trail_cannot_grow_without_limit(self):
        s = flint.Spend(str(self.shared), 200.0, reset_day=5)
        for i in range(60):
            s.add(0.001, f"model-{i}")
        rows = json.loads(self.shared.read_text())["recent"]
        self.assertEqual(len(rows), 50)
        self.assertEqual(rows[-1]["model"], "model-59")

    def test_a_new_window_clears_the_audit_trail_with_the_total(self):
        s = flint.Spend(str(self.shared), 20.0, reset_day=5)
        s.add(1.0, "m")
        self.ledger(1.0, period="2020-01-05")
        self.assertEqual(s.today(), 0.0)
        self.assertEqual(json.loads(self.shared.read_text())["recent"], [])


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


class SandboxLedgerTests(unittest.TestCase):
    """A sandboxed turn has to be able to record what it spent.

    The ledger lives under swarm/state/<repo>/, which is outside the worktree the agent is
    confined to. When the swarm works on its own checkout that path is inside the repo under
    test as well, and before this the first paid turn died on spend.lock with a PermissionError
    and the model was scored for it.
    """

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.state = Path(self.dir.name) / "state"
        self.state.mkdir()
        p = patch.object(swarmd, "STATE", self.state)
        p.start()
        self.addCleanup(p.stop)

    def test_the_ledger_is_writable_from_inside_the_sandbox(self):
        prof = swarmd.sandbox_profile(self.dir.name, {"sandbox": True})
        for ext in (".json", ".lock", ".tmp"):
            self.assertIn(str(self.state / f"spend{ext}"), prof,
                          f"a sandboxed turn could not write spend{ext}")

    def test_the_rest_of_the_state_directory_stays_read_only(self):
        """Only the three ledger files are opened up: an agent that could rewrite its own
        queue or journal would be a different thing entirely."""
        prof = swarmd.sandbox_profile(self.dir.name, {"sandbox": True})
        self.assertNotIn(f'(subpath "{self.state}")', prof)
        for name in ("queue.jsonl", "journal.jsonl", "done.jsonl", "learn.json"):
            self.assertNotIn(str(self.state / name), prof)

    def test_configured_extra_paths_are_still_honoured(self):
        extra = str(Path(self.dir.name) / "elsewhere")
        prof = swarmd.sandbox_profile(self.dir.name, {"sandbox": True, "sandbox_write": [extra]})
        self.assertIn(extra, prof)
        self.assertIn(str(self.state / "spend.lock"), prof)

    def test_the_ledger_follows_the_repo_the_swarm_is_working_on(self):
        """STATE is rebound per target repository, so the paths are read when the profile is
        built rather than captured at import."""
        other = Path(self.dir.name) / "other-repo-state"
        other.mkdir()
        with patch.object(swarmd, "STATE", other):
            prof = swarmd.sandbox_profile(self.dir.name, {"sandbox": True})
        self.assertIn(str(other / "spend.lock"), prof)
        self.assertNotIn(str(self.state / "spend.lock"), prof)


class PlannerGateTests(unittest.TestCase):
    """The planner may use the paid fallback, like any other turn.

    A swarm whose queue has drained and whose free allowance is spent has nothing to work
    on and, if the planner alone is held to free capacity, no way to think of anything
    either. It would sit idle until midnight with its dollar budget untouched.
    """

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        p = patch.object(swarmd, "STATE", Path(self.dir.name))
        p.start()
        self.addCleanup(p.stop)
        self.c = {"allow_paid": True, "daily_usd": 1.0,
                  "models": ["poolside/laguna-s-2.1:free"],
                  "paid_models": ["poolside/laguna-s-2.1"]}

    def budget(self, spent, window=("00:00", "00:00")):
        b = Budget(cap=100, reserve=0, owner_window=window)
        patcher = patch.object(Budget, "spent_today", staticmethod(lambda: spent))
        patcher.start()
        self.addCleanup(patcher.stop)
        return b

    def spent_usd(self, usd):
        (Path(self.dir.name) / "spend.json").write_text(
            json.dumps({"period": UTC_TODAY, "usd": usd}))

    def test_free_capacity_lets_a_turn_start(self):
        self.assertTrue(swarmd.can_take_a_turn(self.c, self.budget(0)))

    def test_a_spent_allowance_still_allows_a_paid_turn(self):
        b = self.budget(100)
        self.assertFalse(b.check()[0])
        self.assertTrue(swarmd.can_take_a_turn(self.c, b))

    def test_nothing_starts_once_both_allowances_are_gone(self):
        self.spent_usd(1.0)
        self.assertFalse(swarmd.can_take_a_turn(self.c, self.budget(100)))

    def test_a_free_only_swarm_still_waits_for_the_reset(self):
        c = {"allow_paid": False, "models": ["poolside/laguna-s-2.1:free"]}
        self.assertFalse(swarmd.can_take_a_turn(c, self.budget(100)))

    def test_the_owner_window_is_not_overridden(self):
        """The owner's window is a request to leave the machine alone, not a shortage."""
        b = self.budget(0, window=("00:00", "23:59"))
        self.assertFalse(swarmd.can_take_a_turn(self.c, b))


class StandInAttributionTests(unittest.TestCase):
    """Whoever answered gets the score.

    `paid_stand_in` falls back to the first paid model when the chosen one has no paid
    twin, so a turn can run on a model the bandit did not pick. Before this, the worker
    still credited its own pick: every turn handed away scored the model that sat it out,
    and the model that did the work got nothing. A model with no paid twin — qwen, until
    its twin was configured — collected a run of zero rewards for other models' failures.
    """

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        p = patch.object(swarmd, "STATE", Path(self.dir.name))
        p.start()
        self.addCleanup(p.stop)
        swarmd._ran_on.clear()
        self.addCleanup(swarmd._ran_on.clear)
        self.c = {"allow_paid": True, "daily_usd": 1.0,
                  "models": ["qwen/qwen3.8-27b:free", "poolside/laguna-s-2.1:free"],
                  "paid_models": ["poolside/laguna-s-2.1", "qwen/qwen3.8-27b"]}

    def test_a_model_with_a_paid_twin_keeps_its_own_turn(self):
        """The whole point of configuring the twin: the bandit's choice actually runs."""
        self.assertEqual(swarmd.paid_stand_in(self.c, "qwen/qwen3.8-27b:free"),
                         "qwen/qwen3.8-27b")

    def test_without_a_twin_the_turn_is_handed_to_another_model(self):
        c = dict(self.c, paid_models=["poolside/laguna-s-2.1"])
        self.assertEqual(swarmd.paid_stand_in(c, "qwen/qwen3.8-27b:free"),
                         "poolside/laguna-s-2.1")

    def test_the_turn_publishes_the_model_that_answered(self):
        """`_ran_on` is what the worker reads to decide who to credit."""
        (Path(self.dir.name) / "logs").mkdir(exist_ok=True)
        c = dict(self.c, python=sys.executable, paid_models=["poolside/laguna-s-2.1"])
        with patch.object(swarmd, "LOGS", Path(self.dir.name) / "logs"):
            self.run_flint(c, "qwen/qwen3.8-27b:free")
        self.assertEqual(swarmd._ran_on.get("w0"), "poolside/laguna-s-2.1",
                         "the worker would otherwise credit qwen for laguna's turn")

    def test_a_turn_on_its_own_model_publishes_no_stand_in(self):
        (Path(self.dir.name) / "logs").mkdir(exist_ok=True)
        with patch.object(swarmd, "LOGS", Path(self.dir.name) / "logs"):
            self.run_flint(dict(self.c, python=sys.executable),
                           "qwen/qwen3.8-27b:free", allowed=True)
        self.assertNotIn("w0", swarmd._ran_on)

    def run_flint(self, c, model, allowed=False):
        import contextlib

        class FakeProc:
            returncode = 0

            def communicate(self, timeout=None):
                return "done", ""

        @contextlib.contextmanager
        def fake_process(cmd, **kw):
            yield FakeProc()

        class B:
            cap, reserve = 500, 100

            def check(self, need=1, state=None):
                return (True, 0, "ok") if allowed else (False, 900, "spent")

            def paid_would_help(self, state=None):
                return True

        with patch.object(swarmd, "process", fake_process), \
                patch.object(swarmd, "sandboxed", lambda cmd, cwd, c: cmd), \
                patch.object(swarmd, "log", lambda *a, **k: None):
            swarmd._stop.clear()
            return swarmd.flint("p", self.dir.name, c, "implementer", "w0", B(), 5, model)

    def test_a_stale_stand_in_does_not_leak_into_the_next_turn(self):
        """flint() clears the record up front, so a turn that runs on the model it was
        given is never credited to the previous turn's stand-in."""
        swarmd._ran_on["w0"] = "poolside/laguna-s-2.1"
        with patch.object(swarmd, "pool", lambda c: ["qwen/qwen3.8-27b:free"]), \
                patch.object(swarmd, "_stop") as stop:
            stop.is_set.return_value = True
            with self.assertRaises(swarmd.Stopped):
                swarmd.flint("p", self.dir.name, self.c, "implementer", "w0", None, 5,
                             "qwen/qwen3.8-27b:free")
        self.assertNotIn("w0", swarmd._ran_on)


class AllowanceRecheckTests(unittest.TestCase):
    """`allowance_recheck`: while the free allowance looks spent, ask OpenRouter whether it is.

    The local count only climbs until UTC midnight and counts requests OpenRouter never
    charged, so on 2026-09-26 it read 1025/1000 while OpenRouter reported 964, and every turn
    for hours went to a paid model. OpenRouter's count is the one that decides."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        root = Path(self.dir.name)
        for name in ("flint", "state", "logs"):
            (root / name).mkdir()
        self.requests = root / "flint" / "requests.json"
        budget_module = sys.modules[swarmd.Budget.__module__]
        for target, name, value in ((flint, "STATE_DIR", root / "flint"),
                                    (budget_module, "REQUESTS_JSON", self.requests),
                                    (swarmd, "STATE", root / "state"), (swarmd, "LOGS", root / "logs")):
            pt = patch.object(target, name, value)
            pt.start()
            self.addCleanup(pt.stop)
        self.logged = []
        for name, value in (("log", lambda msg, *a, **k: self.logged.append(msg)),
                            ("_rechecked", {"at": 0.0, "said": False})):
            pt = patch.object(swarmd, name, value)
            pt.start()
            self.addCleanup(pt.stop)
        swarmd._stop.clear()
        self.asked = 0
        self.reply = None
        pt = patch.object(swarmd, "account", self.fake_account)
        pt.start()
        self.addCleanup(pt.stop)
        self.c = {"allowance_recheck": 300, "allow_paid": True, "daily_usd": 1.0,
                  "python": sys.executable, "models": ["dots-studio/dots-3-note-preview:free"],
                  "paid_models": ["poolside/laguna-s-2.1"], "test_cmd": "true"}
        self.budget = swarmd.Budget(cap=1000, reserve=0, owner_window=["00:00", "00:00"])

    def fake_account(self):
        self.asked += 1
        return self.reply

    def counted(self, count, **extra):
        self.requests.write_text(json.dumps({"day": UTC_TODAY, "count": count, **extra}))

    def openrouter(self, used, limit=1000):
        self.reply = {"free_model_daily_requests": {"used": used, "limit": limit,
                                                    "remaining": max(0, limit - used)}}

    def state(self):
        return json.loads(self.requests.read_text())

    def test_a_lower_count_from_openrouter_brings_free_turns_back(self):
        self.counted(1025)
        self.openrouter(964)
        self.assertFalse(self.budget.check()[0])
        self.assertTrue(swarmd.recheck_allowance(self.c, self.budget))
        self.assertEqual(self.state()["count"], 964)
        self.assertTrue(self.budget.check()[0])
        rows = [json.loads(l) for l in (swarmd.STATE / "journal.jsonl").read_text().splitlines()]
        self.assertEqual([(r["used"], r["counted"]) for r in rows if r["event"] == "allowance_restored"],
                         [(964, 1025)])

    def test_a_daily_cap_block_openrouter_no_longer_backs_is_lifted(self):
        self.counted(40, blocked_until=time.time() + 6 * 3600)
        self.openrouter(40)
        self.assertTrue(swarmd.recheck_allowance(self.c, self.budget))
        self.assertNotIn("blocked_until", self.state())
        self.assertTrue(self.budget.check()[0])

    def test_a_spent_allowance_stays_spent_and_says_so_once(self):
        until = time.time() + 6 * 3600
        self.counted(1000, blocked_until=until)
        self.openrouter(1000)
        self.assertFalse(swarmd.recheck_allowance(self.c, self.budget))
        self.assertEqual(self.state()["blocked_until"], until)
        self.assertFalse(self.budget.check()[0])
        swarmd._rechecked["at"] = 0.0            # the next check is due
        self.assertFalse(swarmd.recheck_allowance(self.c, self.budget))
        self.assertEqual(self.asked, 2)
        self.assertEqual(sum("allowance spent" in m for m in self.logged), 1)

    def test_openrouter_is_asked_no_more_often_than_the_setting(self):
        self.counted(1000)
        self.openrouter(1000)
        for _ in range(5):
            swarmd.recheck_allowance(self.c, self.budget)
        self.assertEqual(self.asked, 1)
        swarmd._rechecked["at"] = time.time() - 301
        self.openrouter(990)
        self.assertTrue(swarmd.recheck_allowance(self.c, self.budget))
        self.assertEqual(self.asked, 2)

    def test_unset_or_zero_never_asks(self):
        self.counted(1025)
        self.openrouter(0)
        for c in ({}, {"allowance_recheck": 0}, {"allowance_recheck": "soon"}):
            self.assertEqual(swarmd.allowance_recheck(c), 0)
            self.assertFalse(swarmd.recheck_allowance(c, self.budget))
        self.assertEqual(self.asked, 0)
        self.assertEqual(self.state()["count"], 1025)

    def test_nothing_is_asked_while_free_requests_remain(self):
        self.counted(10)
        self.openrouter(10)
        self.assertFalse(swarmd.recheck_allowance(self.c, self.budget))
        self.assertEqual(self.asked, 0)

    def test_the_owner_window_is_not_a_shortage_to_recheck(self):
        budget = swarmd.Budget(cap=1000, reserve=0, owner_window=["00:00", "23:59"])
        self.counted(1025)
        self.openrouter(0)
        self.assertFalse(swarmd.recheck_allowance(self.c, budget))
        self.assertEqual(self.asked, 0)

    def test_an_unreachable_openrouter_changes_nothing(self):
        self.counted(1025)
        self.reply = None
        self.assertFalse(swarmd.recheck_allowance(self.c, self.budget))
        self.assertEqual(self.state()["count"], 1025)

    def test_the_reserve_still_holds_after_a_recheck(self):
        budget = swarmd.Budget(cap=1000, reserve=100, owner_window=["00:00", "00:00"])
        self.counted(1000)
        self.openrouter(950)
        self.assertFalse(swarmd.recheck_allowance(self.c, budget))
        self.assertEqual(self.state()["count"], 950)
        self.assertFalse(budget.check()[0], "950 of 900 usable is still spent")

    def test_a_turn_runs_free_instead_of_paid_once_the_recheck_restores_the_allowance(self):
        self.counted(1025)
        self.openrouter(964)
        cmds = []

        class FakeProc:
            returncode = 0

            def communicate(self, timeout=None):
                return "done", ""

        import contextlib

        @contextlib.contextmanager
        def fake_process(cmd, **kw):
            cmds.append(cmd)
            yield FakeProc()

        with patch.object(swarmd, "process", fake_process), \
                patch.object(swarmd, "sandboxed", lambda cmd, cwd, c: cmd):
            swarmd.flint("prompt", self.dir.name, self.c, "implementer", "w0",
                         self.budget, 5, "dots-studio/dots-3-note-preview:free")
        self.assertIn("dots-studio/dots-3-note-preview:free", cmds[0])
        self.assertNotIn("poolside/laguna-s-2.1", cmds[0])
        rows = [json.loads(l) for l in (swarmd.STATE / "journal.jsonl").read_text().splitlines()]
        self.assertEqual([r for r in rows if r["event"] == "paid_fallback"], [])
