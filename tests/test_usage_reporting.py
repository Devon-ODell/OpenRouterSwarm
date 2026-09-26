"""The sidebar's usage figures: OpenRouter's own totals, kept apart from the local ledger."""
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from swarm import bridge


class UsageReportingTests(unittest.TestCase):
    def info(self, account):
        out = []
        with patch.object(bridge.swarmd, "load_cfg", return_value={"corpus_db": "/nonexistent/corpus.db"}), \
                patch.object(bridge.swarmd, "account", return_value=account), \
                patch.object(bridge.swarmd, "pool", return_value=[]), \
                patch.object(bridge, "wallet_for", return_value=None), \
                patch.object(bridge.sandbox, "available", return_value=False), \
                patch.object(bridge, "emit", side_effect=out.append):
            bridge.cmd_info(types.SimpleNamespace(quota=True))
        return out[0]["provider_usage"]

    def test_provider_totals_and_scope(self):
        u = self.info({"usage_daily": 1.23, "usage_monthly": 4.56, "usage": 8.9,
                       "label": "SECRET-SHOULD-NOT-LEAK"})
        self.assertEqual(u["usage_daily"], 1.23)
        self.assertEqual(u["usage_monthly"], 4.56)
        self.assertTrue(u["available"])
        self.assertEqual(u["scope"], "configured API key")
        self.assertNotIn("label", u)

    def test_monthly_sandbox_uses_shared_ledger_and_daily_uses_repo_ledger(self):
        with patch.object(bridge.swarmd, "HERE", Path("/engine/swarm")), \
                patch.object(bridge.swarmd, "STATE", Path("/engine/swarm/state/project")):
            self.assertEqual(bridge.swarmd.spend_ledger_paths({"monthly_usd": 20}),
                             [Path("/engine/swarm/state/spend" + ext) for ext in (".json", ".lock", ".tmp")])
            self.assertEqual(bridge.swarmd.spend_ledger_paths({"daily_usd": 1}),
                             [Path("/engine/swarm/state/project/spend" + ext) for ext in (".json", ".lock", ".tmp")])
            with patch.object(bridge.swarmd.sandbox, "git_paths", return_value=[]):
                profile = bridge.swarmd.sandbox_profile("/work", {"monthly_usd": 20})
            self.assertIn('(subpath "/engine/swarm/state/spend.lock")', profile)
            self.assertNotIn('(subpath "/engine/swarm/state")', profile)

    def test_unavailable_is_not_zero(self):
        u = self.info(None)
        self.assertFalse(u["available"])
        self.assertIsNone(u["usage_monthly"])

    def test_zero_is_real_data(self):
        u = self.info({"usage_daily": 0, "usage_monthly": 0})
        self.assertTrue(u["available"])
        self.assertEqual(u["usage_daily"], 0)


if __name__ == "__main__":
    unittest.main()
