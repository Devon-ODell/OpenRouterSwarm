import types
import unittest
from unittest.mock import Mock, patch

from swarm import swarmd  # noqa: F401
from swarm import supervisor


class PeriodicTests(unittest.TestCase):
    def test_due_fires_and_records(self):
        p = supervisor.Periodic(300)
        with patch.object(supervisor.time, "time", return_value=301):
            self.assertTrue(p.due())
        with patch.object(supervisor.time, "time", return_value=400):
            self.assertFalse(p.due())
        with patch.object(supervisor.time, "time", return_value=602):
            self.assertTrue(p.due())

    def test_due_with_last(self):
        p = supervisor.Periodic(900, last=1000)
        with patch.object(supervisor.time, "time", return_value=1500):
            self.assertFalse(p.due())
        with patch.object(supervisor.time, "time", return_value=1901):
            self.assertTrue(p.due())


def make_d():
    return types.SimpleNamespace(
        plan=Mock(return_value=0),
        sync_trunk=Mock(),
        can_take_a_turn=Mock(return_value=True),
        log=Mock(),
        rest=Mock(),
        shutdown=Mock(),
    )


def make_q():
    q = types.SimpleNamespace(ready=Mock(return_value=[]))
    return q


class PlannerCadenceTests(unittest.TestCase):
    def test_cooldown_backs_off(self):
        cad = supervisor.PlannerCadence()
        d = make_d()
        q = make_q()
        c = {"workers": 1, "plan_cooldown": 600}
        budget = ledger = None
        stop = types.SimpleNamespace(wait=Mock())

        with patch.object(supervisor.time, "time", return_value=1000):
            cad.tick(c, q, budget, ledger, stop, d=d)
        self.assertEqual(d.plan.call_count, 1)
        self.assertEqual(cad.dry_runs, 1)

        with patch.object(supervisor.time, "time", return_value=1000 + 1199):
            cad.tick(c, q, budget, ledger, stop, d=d)
        self.assertEqual(d.plan.call_count, 1)

        with patch.object(supervisor.time, "time", return_value=1000 + 1201):
            cad.tick(c, q, budget, ledger, stop, d=d)
        self.assertEqual(d.plan.call_count, 2)

    def test_failed_plan_redraws_after_60(self):
        cad = supervisor.PlannerCadence()
        d = make_d()
        d.plan.side_effect = supervisor.ModelError("x")
        q = make_q()
        c = {"workers": 1, "plan_cooldown": 600}
        budget = ledger = None
        stop = types.SimpleNamespace(wait=Mock())

        with patch.object(supervisor.time, "time", return_value=1000):
            cad.tick(c, q, budget, ledger, stop, d=d)
        self.assertEqual(cad.plan_fails, 1)
        # cooldown was 600 at that tick (dry_runs 0); redraw at now - cooldown + 60
        self.assertEqual(cad.last_plan, 1000 - 600 + 60)
        # plan_fails resets after 3 more failures, so force 2 more to check counting
        with patch.object(supervisor.time, "time", return_value=1000 + 61):
            cad.tick(c, q, budget, ledger, stop, d=d)
        with patch.object(supervisor.time, "time", return_value=1000 + 122):
            cad.tick(c, q, budget, ledger, stop, d=d)
        self.assertEqual(cad.plan_fails, 3)

    def test_four_failures_back_off(self):
        cad = supervisor.PlannerCadence()
        d = make_d()
        d.plan.side_effect = supervisor.ModelError("x")
        q = make_q()
        c = {"workers": 1, "plan_cooldown": 600}
        budget = ledger = None
        stop = types.SimpleNamespace(wait=Mock())

        # fail 4 times, each 61s apart (past the redraw cooldown)
        t = 1000
        for _ in range(4):
            with patch.object(supervisor.time, "time", return_value=t):
                cad.tick(c, q, budget, ledger, stop, d=d)
            t += 61
        self.assertEqual(cad.plan_fails, 0)
        self.assertEqual(cad.dry_runs, 1)


if __name__ == "__main__":
    unittest.main()
