"""Work order §2: turns are sized from measured latency, not configured hope.

A 900 s implementer timeout with a ~60 s EWMA must yield at most
floor((900 - 180) / 60) = 12 rounds, never the configured 16; fast models keep
their configured maximum when it fits; and the EWMA persists in learn.json.
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from swarm import pacing
from swarm.learn import Ledger


class AffordableStepTests(unittest.TestCase):
    def test_a_slow_model_is_capped_by_the_timeout_and_reserve(self):
        steps = pacing.affordable_steps(900, 60, pacing.IMPL_RESERVE, max_steps=16)
        self.assertEqual(steps, 12)

    def test_a_fast_model_keeps_the_configured_maximum(self):
        steps = pacing.affordable_steps(900, 20, pacing.IMPL_RESERVE, max_steps=16)
        self.assertEqual(steps, 16)

    def test_never_more_than_configured(self):
        steps = pacing.affordable_steps(1_000_000, 1, pacing.IMPL_RESERVE, max_steps=10)
        self.assertEqual(steps, 10)

    def test_read_only_roles_get_a_smaller_reserve(self):
        impl = pacing.affordable_steps(900, 60, pacing.IMPL_RESERVE, max_steps=30)
        ro = pacing.affordable_steps(900, 60, pacing.READ_ONLY_RESERVE, max_steps=30)
        self.assertGreater(ro, impl)

    def test_an_impossible_timeout_returns_the_floor(self):
        self.assertEqual(pacing.affordable_steps(30, 60, pacing.IMPL_RESERVE, max_steps=16), 1)

    def test_reserve_for_role(self):
        self.assertEqual(pacing.reserve_for_role("implementer"), pacing.IMPL_RESERVE)
        self.assertEqual(pacing.reserve_for_role("repair"), pacing.IMPL_RESERVE)
        self.assertEqual(pacing.reserve_for_role("judge"), pacing.READ_ONLY_RESERVE)


class EwmaTests(unittest.TestCase):
    def test_ewma_blends(self):
        self.assertEqual(pacing.ewma_update(None, 50.0), 50.0)
        self.assertAlmostEqual(pacing.ewma_update(50.0, 60.0, alpha=0.2), 52.0)

    def test_learner_reads_and_writes_learn_json(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        ledger = Ledger(Path(d.name) / "learn.json")
        learner = pacing.LatencyLearner(ledger)
        self.assertIsNone(learner.seconds_per_request("m:free", "implementer"))
        learner.update("m:free", "implementer", secs=600, requests=10)
        self.assertAlmostEqual(learner.seconds_per_request("m:free", "implementer"), 60.0)
        learner.update("m:free", "implementer", secs=660, requests=11)
        self.assertAlmostEqual(learner.seconds_per_request("m:free", "implementer"),
                               pacing.ewma_update(60.0, 60.0))
        # A different model/role has no prior.
        self.assertIsNone(learner.seconds_per_request("other:free", "judge"))

    def test_update_ignores_empty_requests(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        ledger = Ledger(Path(d.name) / "learn.json")
        learner = pacing.LatencyLearner(ledger)
        self.assertIsNone(learner.update("m", "implementer", secs=100, requests=0))


class EffectiveStepTests(unittest.TestCase):
    def test_effective_steps_uses_the_measured_ewma(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        ledger = Ledger(Path(d.name) / "learn.json")
        learner = pacing.LatencyLearner(ledger)
        learner.update("m:free", "implementer", secs=600, requests=10)
        steps = pacing.effective_steps("implementer", "m:free", 900, 16, ledger=ledger)
        self.assertEqual(steps, 12)

    def test_effective_steps_falls_back_to_the_constant(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        ledger = Ledger(Path(d.name) / "learn.json")
        steps = pacing.effective_steps("implementer", "m:free", 900, 16, ledger=ledger)
        self.assertEqual(steps, 15)  # floor((900 - 180) / 47)

    def test_effective_steps_respects_the_configured_max(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        ledger = Ledger(Path(d.name) / "learn.json")
        learner = pacing.LatencyLearner(ledger)
        learner.update("m:free", "implementer", secs=60, requests=1)
        steps = pacing.effective_steps("implementer", "m:free", 900, 8, ledger=ledger)
        self.assertEqual(steps, 8)

    def test_effective_steps_zero_when_configured_zero(self):
        self.assertEqual(pacing.effective_steps("implementer", "m", 900, 0), 0)


if __name__ == "__main__":
    unittest.main()