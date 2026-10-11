"""P5 contracts for the queue's store, pure policy, and orchestration layers."""

import builtins
import ast
import copy
import dataclasses
import inspect
import json
import multiprocessing
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from swarm import swarmd
from swarm.task_queue import JsonlTaskStore, TaskPolicy, TaskQueue, TaskTransition


def _locked_append(pending, done, lock_path, worker, count):
    store = JsonlTaskStore(pending, done, lock_path)
    for number in range(count):
        with store.locked():
            rows = store.pending_rows()
            rows.append({"id": f"{worker}-{number}", "title": "row"})
            store.write_pending(rows)


class JsonlTaskStoreTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.root = Path(d.name)
        self.store = JsonlTaskStore(
            self.root / "queue.jsonl", self.root / "done.jsonl",
            self.root / "queue.lock")

    def test_existing_jsonl_round_trips_with_the_same_schema_and_encoding(self):
        rows = [
            {"id": "t1", "title": "first", "attempts": 0, "depends_on": []},
            {"id": "t2", "title": "second", "status": "parked", "note": "why"},
        ]
        expected = "".join(json.dumps(row) + "\n" for row in rows)
        self.store.pending.write_text(expected)
        self.assertEqual(self.store.pending_rows(), rows)
        self.store.write_pending(self.store.pending_rows())
        self.assertEqual(self.store.pending.read_text(), expected)
        self.assertFalse(self.store.pending.with_suffix(".tmp").exists())

    def test_processes_share_the_fcntl_lock_and_atomic_replace(self):
        ctx = multiprocessing.get_context("spawn")
        processes = [ctx.Process(target=_locked_append, args=(
            self.store.pending, self.store.done, self.store.lock_path, worker, 15))
            for worker in range(4)]
        for process in processes:
            process.start()
        for process in processes:
            process.join(20)
            self.assertEqual(process.exitcode, 0)
        rows = self.store.pending_rows()
        self.assertEqual(len(rows), 60)
        self.assertEqual(len({row["id"] for row in rows}), 60)
        self.assertFalse(self.store.pending.with_suffix(".tmp").exists())


class TaskPolicyTests(unittest.TestCase):
    def test_transition_contract_is_frozen_and_exact(self):
        self.assertEqual(
            [field.name for field in dataclasses.fields(TaskTransition)],
            ["status", "not_before", "failure_class", "cool_model", "counters", "note"],
        )
        transition = TaskTransition("held", None, "harness", None, {}, None)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            transition.status = "retry"

    def test_claim_ranking_is_pure_and_matches_the_public_score(self):
        rows = [
            {"id": "old", "priority": 1, "created": 1, "attempts": 0},
            {"id": "verified", "priority": 1, "created": 2, "attempts": 0,
             "verification_commands": ["pytest"], "allowed_paths": ["src/"]},
            {"id": "lower", "priority": 0, "created": 0, "must_run_first": True},
        ]
        before = copy.deepcopy(rows)
        policy = TaskPolicy()
        self.assertEqual(policy.claim(rows, set(), now=100), "verified")
        self.assertEqual(rows, before)
        self.assertEqual(policy.selection_score(rows[1]), swarmd.Queue.selection_score(rows[1]))

    def test_release_is_pure_and_cannot_open_a_file(self):
        task = {"id": "t1", "attempts": 0, "depth": 0}
        before = copy.deepcopy(task)
        with patch.object(builtins, "open", side_effect=AssertionError("policy did I/O")):
            transition = TaskPolicy(max_depth=2, max_attempts=2).release(
                task, False, "timed out", failure_class="harness",
                requests=2, turn_seconds=4.5, role_turns=1, now=100,
            )
        self.assertEqual(task, before)
        self.assertEqual(transition.status, "retry")
        self.assertEqual(transition.not_before, 1900)
        self.assertEqual(transition.counters["attempts"], 0)
        self.assertEqual(transition.counters["harness_failures"], 1)

    def test_policy_source_has_no_store_run_state_or_ledger_dependency(self):
        tree = ast.parse(inspect.getsource(TaskPolicy))
        names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        calls = {node.func.id for node in ast.walk(tree)
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
        self.assertTrue({"JsonlTaskStore", "_run", "ledger"}.isdisjoint(names))
        self.assertTrue({"open", "Path"}.isdisjoint(calls))


class TaskQueueCompositionTests(unittest.TestCase):
    def test_swarmd_compatibility_queue_composes_the_three_layers(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        root = Path(d.name)
        with patch.object(swarmd, "STATE", root):
            queue = swarmd.Queue()
        self.assertIsInstance(queue, TaskQueue)
        self.assertIsInstance(queue.store, JsonlTaskStore)
        self.assertIsInstance(queue.policy, TaskPolicy)


if __name__ == "__main__":
    unittest.main()
