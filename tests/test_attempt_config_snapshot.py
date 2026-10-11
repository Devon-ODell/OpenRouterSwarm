"""An attempt owns one immutable config view from claim through release."""

import ast
import inspect
import json
import os
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from swarm import swarmd


class AttemptConfigSnapshotTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.root = Path(d.name)
        self.state = self.root / "state"
        self.logs = self.root / "logs"
        self.state.mkdir()
        self.logs.mkdir()
        self.config = self.root / "config.json"
        for attr, value in (("HERE", self.root), ("CONFIG", self.config),
                            ("STATE", self.state), ("LOGS", self.logs)):
            p = patch.object(swarmd, attr, value)
            p.start()
            self.addCleanup(p.stop)
        swarmd._cfg_seen["mtime"] = 0.0
        self.addCleanup(swarmd._cfg_seen.update, {"mtime": 0.0})

    def write_config(self, **changes):
        base = {
            "repo": str(self.root),
            "test_cmd": "true",
            "workers": 1,
            "models": ["old:free"],
            "steps": {"implementer": 4},
            "trunk": "old/trunk",
            "daily_usd": 1.5,
            "daily_cap": 50,
            "reserve": 5,
            "owner_window": ["00:00", "00:00"],
        }
        self.config.write_text(json.dumps({**base, **changes}))
        return json.loads(self.config.read_text())

    def rewrite_after_baseline(self, **changes):
        fresh = self.write_config(**changes)
        newer = swarmd._cfg_seen["mtime"] + 10
        os.utime(self.config, (newer, newer))
        return fresh

    def test_reload_waits_for_next_claim_and_restart_only_trunk_stays_put(self):
        live = self.write_config()
        swarmd._cfg_seen["mtime"] = swarmd.config_mtime()
        shared_budget = swarmd.Budget(cap=50, reserve=5,
                                      owner_window=["00:00", "00:00"])
        queue = swarmd.Queue()
        queue.add("first", "first attempt")
        queue.add("second", "second attempt")
        worker = swarmd.Worker(
            0, live, queue, shared_budget, threading.Event(),
            tally=swarmd.Tally(limit=2),
        )
        seen = []

        def do_task(task, goal, attempt_config, attempt_budget):
            seen.append({
                "models": tuple(swarmd.pool(attempt_config)),
                "spend_cap": swarmd.spend_cap(attempt_config),
                "trunk": swarmd.trunk_name(attempt_config),
                "budget": attempt_budget,
            })
            # ConfigSnapshot is recursively immutable, not just a read-only outer dict.
            with self.assertRaises(TypeError):
                attempt_config["models"] = ("leak:free",)
            with self.assertRaises(TypeError):
                attempt_config["steps"]["implementer"] = 99
            if len(seen) == 1:
                self.rewrite_after_baseline(
                    models=["new:free"], steps={"implementer": 9},
                    trunk="new/trunk", daily_usd=7.5,
                    daily_cap=80, reserve=8,
                    owner_window=["01:00", "02:00"],
                )
            worker.stage = "accepted"
            worker.failure_class = "task"
            worker.attempt_requests = 0
            worker.last_model = worker.last_role = None
            worker.role_calls = 0
            worker.started = time.time()
            return True, "landed"

        with patch.object(worker, "do_task", side_effect=do_task), \
             patch.object(swarmd, "read_goal", return_value="goal"):
            worker.run()

        self.assertEqual(
            [(row["models"], row["spend_cap"], row["trunk"])
             for row in seen],
            [(('old:free',), 1.5, "old/trunk"),
             (('new:free',), 7.5, "old/trunk")],
        )
        self.assertIsNot(seen[0]["budget"], shared_budget)
        self.assertIsNot(seen[0]["budget"], seen[1]["budget"])
        self.assertEqual((seen[0]["budget"].cap, seen[0]["budget"].reserve), (50, 5))
        self.assertEqual((seen[1]["budget"].cap, seen[1]["budget"].reserve), (80, 8))
        self.assertEqual((shared_budget.cap, shared_budget.reserve), (80, 8))
        self.assertEqual(live["models"], ["new:free"])
        self.assertEqual(live["trunk"], "old/trunk")

    def test_attempt_helpers_cannot_reach_the_live_config_or_reload_it(self):
        """Keep live config access at the claim/direct-entry boundaries."""
        tree = ast.parse(textwrap.dedent(inspect.getsource(swarmd.Worker)))
        boundary = {"__init__", "_attempt_config", "_attempt_inputs", "run"}
        forbidden_calls = {"cfg", "load_cfg", "watch_config", "config_mtime"}
        violations = []
        # getsource() of a class parses to a module holding one ClassDef; its methods are in
        # that node's body. Iterating tree.body here once checked no method at all.
        methods = [n for n in tree.body[0].body if isinstance(n, ast.FunctionDef)]
        self.assertGreater(len(methods), 3)
        for method in methods:
            if method.name in boundary:
                continue
            for node in ast.walk(method):
                if (isinstance(node, ast.Attribute)
                        and isinstance(node.value, ast.Name)
                        and node.value.id == "self"
                        and node.attr in {"c", "budget"}):
                    violations.append(f"{method.name}: self.{node.attr}")
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                        and node.func.id in forbidden_calls):
                    violations.append(f"{method.name}: {node.func.id}()")
        self.assertEqual(violations, [])

    def test_attempt_runner_cannot_reach_the_live_config_or_reload_it(self):
        """The attempt helpers moved into AttemptRunner (P9), where the worker is `w` and
        daemon services are `d.<name>`. The same boundary has to hold there, or the guard
        above passes only because the code it was guarding has left the Worker."""
        from swarm import attempt_runner
        tree = ast.parse(textwrap.dedent(inspect.getsource(attempt_runner.AttemptRunner)))
        forbidden_calls = {"cfg", "load_cfg", "watch_config", "config_mtime"}
        violations = []
        methods = [n for n in tree.body[0].body if isinstance(n, ast.FunctionDef)]
        self.assertGreater(len(methods), 10)
        for method in methods:
            for node in ast.walk(method):
                if (isinstance(node, ast.Attribute)
                        and isinstance(node.value, ast.Name)
                        and node.value.id == "w"
                        and node.attr in {"c", "budget"}):
                    violations.append(f"{method.name}: w.{node.attr}")
                if isinstance(node, ast.Call):
                    name = (node.func.id if isinstance(node.func, ast.Name) else
                            node.func.attr if isinstance(node.func, ast.Attribute)
                            and isinstance(node.func.value, ast.Name)
                            and node.func.value.id == "d" else None)
                    if name in forbidden_calls:
                        violations.append(f"{method.name}: {name}()")
        self.assertEqual(violations, [])


if __name__ == "__main__":
    unittest.main()
