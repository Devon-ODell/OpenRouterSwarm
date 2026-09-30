"""Phase 2/3 modules: receipts ledger, hardware doctor, routing score, undo, hooks, extensions.

These tests pin the contracts the new bridge commands (doctor, route, receipts, undo,
extension) and flint's hooks/undo wiring rely on. All of them run offline — no model
calls, no sockets — using tempdirs for ~/.flint and ~/.albatross.
"""
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

SWARM = Path(__file__).resolve().parents[1] / 'swarm'
sys.path.insert(0, str(SWARM))
import receipts
import routing
import undo as undo_mod
import hardware  # noqa: F401 (imported for its module-level presence)


class ReceiptsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = dict(os.environ, FLINT_HOME=self.tmp.name,
                        FLINT_ROUTES_FILE=str(Path(self.tmp.name) / 'albatross' / 'routes.jsonl'))

    def test_record_and_tail_roundtrip(self):
        receipts.record({"model": "a/b:free", "backend": "openrouter", "usd": 0.0, "local": False}, env=self.env)
        receipts.record({"model": "a/c", "backend": "openrouter", "usd": 0.02, "local": False}, env=self.env)
        t = receipts.tail(10, env=self.env)
        self.assertEqual(len(t), 2)
        self.assertEqual(t[0]["model"], "a/c")       # newest first
        self.assertAlmostEqual(t[0]["usd"], 0.02)

    def test_summarize_totals_and_local_are_separate(self):
        for i in range(3):
            receipts.record({"model": "m1", "usd": 0.01}, env=self.env)
        receipts.record({"model": "m2", "usd": 0.0, "local": True}, env=self.env)
        s = receipts.summarize(since=0, env=self.env)
        self.assertAlmostEqual(s["total_usd"], 0.03)
        self.assertEqual(s["requests"], 4)
        self.assertEqual(s["local_requests"], 1)
        self.assertEqual(s["paid_requests"], 3)
        by = {m["model"]: m for m in s["by_model"]}
        self.assertEqual(by["m1"]["requests"], 3)

    def test_albatross_rows_are_read(self):
        """The real albatross ledger uses kind/timestamp/requested_model — read it too."""
        path = Path(self.env["FLINT_ROUTES_FILE"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "kind": "modelCall", "timestamp": "2026-09-28T18:52:34.153211+00:00",
            "requested_model": "nvidia/nemotron-3-nano-30b-a3b", "$amount": 0.000104}) + "\n")
        s = receipts.summarize(since=0, env=self.env)
        self.assertAlmostEqual(s["total_usd"], 0.000104)
        self.assertEqual(s["by_model"][0]["model"], "nvidia/nemotron-3-nano-30b-a3b")

    def test_writes_0600(self):
        receipts.record({"model": "m", "usd": 0.0}, env=self.env)
        mode = stat.S_IMODE(os.stat(self.env["FLINT_ROUTES_FILE"]).st_mode)
        self.assertEqual(mode, 0o600)


class RoutingTests(unittest.TestCase):
    CATALOG = [
        {"id": "free/model", "pricing": {"prompt": 0, "completion": 0}},
        {"id": "cheap/model", "pricing": {"prompt": 0.0001, "completion": 0.0001}},
        {"id": "pricey/model", "pricing": {"prompt": 0.05, "completion": 0.05}},
    ]

    def test_free_wins_without_policy(self):
        best = routing.select(["cheap/model", "free/model", "pricey/model"],
                              catalog=self.CATALOG)
        self.assertEqual(best["model"], "free/model")
        self.assertIn("free", best["reasons"])

    def test_max_turn_excludes_pricey(self):
        best = routing.select(["cheap/model", "pricey/model"],
                              catalog=self.CATALOG, policy={"maxTurnUsd": 0.01})
        self.assertEqual(best["model"], "cheap/model")

    def test_unknown_cost_deny(self):
        best = routing.select(["unknown/model", "free/model"],
                              catalog=self.CATALOG, policy={"unknownCost": "deny"})
        self.assertEqual(best["model"], "free/model")
        ranked = routing.rank(["unknown/model"], catalog=self.CATALOG,
                              policy={"unknownCost": "deny"})
        self.assertEqual(ranked, [])

    def test_empty_pool(self):
        self.assertIsNone(routing.select([], catalog=self.CATALOG))
        self.assertEqual(routing.rank([], catalog=self.CATALOG), [])


class UndoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = dict(os.environ, FLINT_HOME=self.tmp.name)
        self.file = Path(self.tmp.name) / "a.py"
        self.file.write_text("v1\n")
        self.session = "turn-test-123"

    def test_restores_before_state(self):
        undo_mod.snapshot(self.session, self.file, after="v2\n", command="write_file", env=self.env)
        self.file.write_text("v2\n")
        restored, skipped = undo_mod.undo(self.session, env=self.env)
        self.assertEqual(self.file.read_text(), "v1\n")
        self.assertEqual(len(restored), 1)
        self.assertEqual(skipped, [])

    def test_does_not_clobber_later_edits(self):
        undo_mod.snapshot(self.session, self.file, after="v2\n", command="write_file", env=self.env)
        self.file.write_text("v3-user-kept\n")   # changed since the turn
        restored, skipped = undo_mod.undo(self.session, env=self.env)
        self.assertEqual(self.file.read_text(), "v3-user-kept\n")
        self.assertEqual(restored, [])
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0][1], "changed since the turn")

    def test_dry_run_changes_nothing(self):
        undo_mod.snapshot(self.session, self.file, after="v2\n", command="edit_file", env=self.env)
        self.file.write_text("v2\n")
        restored, skipped = undo_mod.undo(self.session, env=self.env, dry_run=True)
        self.assertEqual(self.file.read_text(), "v2\n")
        self.assertEqual(len(restored), 1)

    def test_list_sessions(self):
        undo_mod.snapshot(self.session, self.file, after="v2\n", env=self.env)
        sessions = undo_mod.list_sessions(env=self.env)
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["session"], self.session)


class HooksTests(unittest.TestCase):
    def test_missing_hook_is_reported_not_raised(self):
        from swarm import hooks  # noqa: F401  (module keeps its own imports lazy)
        import importlib
        hooks = importlib.import_module("swarm.hooks")
        results = hooks.run_hooks(["PostToolUse"], ".")
        # No agent.config.json hooks in a temp cwd → nothing to run, no error.
        self.assertIsInstance(results, list)

    def test_resolve_expands_home(self):
        import importlib
        hooks = importlib.import_module("swarm.hooks")
        self.assertIsNone(hooks._resolve("$HOME/definitely-missing-bin", "."))
        self.assertIsNone(hooks._resolve("", "."))


class ExtensionsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = dict(os.environ, FLINT_HOME=self.tmp.name)

    def _write_ext(self):
        root = Path(self.env["FLINT_HOME"]) / "extensions" / "hello"
        root.mkdir(parents=True)
        (root / "extension.json").write_text(json.dumps({
            "name": "hello", "command": [sys.executable, "tool.py"],
            "events": ["PostToolUse"]}))
        (root / "tool.py").write_text(
            "import sys, json\njson.load(sys.stdin)\nprint('ran')\n")

    def test_untrusted_extension_is_skipped(self):
        import importlib
        ext = importlib.import_module("swarm.extensions")
        self._write_ext()
        results = ext.run_extensions(["PostToolUse"], ".", allow_untrusted=False, env=self.env)
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0]["ok"])
        self.assertFalse(results[0]["trusted"])

    def test_trusted_extension_runs(self):
        import importlib
        ext = importlib.import_module("swarm.extensions")
        self._write_ext()
        p = ext.trust("hello", env=self.env)
        self.assertTrue(p)
        results = ext.run_extensions(["PostToolUse"], ".", env=self.env)
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["ok"])
        self.assertIn("ran", results[0]["output"])

    def test_changed_extension_loses_trust(self):
        import importlib
        ext = importlib.import_module("swarm.extensions")
        self._write_ext()
        ext.trust("hello", env=self.env)
        # Tamper with the script: the hash changes, trust must not carry over.
        root = Path(self.env["FLINT_HOME"]) / "extensions" / "hello"
        (root / "tool.py").write_text("print('pwned')\n")
        results = ext.run_extensions(["PostToolUse"], ".", env=self.env)
        self.assertFalse(results[0]["trusted"])


if __name__ == "__main__":
    unittest.main(verbosity=2)