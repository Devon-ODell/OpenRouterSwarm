"""What the Cursor panel is given to show.

Its first line was a list of counters — free requests, a spend meter, an MIT chunk count,
experiment arms — and not one of them said whether the swarm was working, what on, or whether it
was going well. The landed rate sat at 10% for a day and a half with nothing on screen saying so.
"""
import contextlib
import io
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from swarm import bridge, swarmd

SWARMDS = (swarmd, bridge.swarmd)


class NowTests(unittest.TestCase):
    """`now.json`: the task, the role, the model, the round and how long it has been."""

    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.root = Path(d.name)
        (self.root / "logs").mkdir()
        for attr, value in (("STATE", self.root), ("LOGS", self.root / "logs")):
            p = patch.object(swarmd, attr, value)
            p.start()
            self.addCleanup(p.stop)
        swarmd._turn_logs.clear()
        swarmd._waiting.clear()
        swarmd._ran_on.clear()

    def worker(self, name="w0", title="Add onFoot walk state", doing="implementer with qwen/qwen3-coder",
               since=None, alive=True):
        w = Mock()
        w.name = name
        w.task = {"id": "t1", "title": title}
        w.started = since if since is not None else time.time() - 250
        w.doing = doing
        w.is_alive.return_value = alive
        return w

    def test_it_says_what_is_being_done_and_on_what(self):
        note = swarmd.write_now([self.worker()])
        row = note["workers"][0]
        self.assertEqual((row["task"], row["title"], row["role"]),
                         ("t1", "Add onFoot walk state", "implementer"))
        self.assertEqual(row["model"], "qwen/qwen3-coder")
        self.assertGreaterEqual(row["seconds"], 249)

    def test_the_round_comes_from_the_turns_own_log(self):
        log = self.root / "logs" / "turn.log"
        log.write_text("round 1/12: requesting qwen/qwen3-coder\n"
                       "tool: read_file\n"
                       "round 7/12: requesting qwen/qwen3-coder\n")
        swarmd._turn_logs["w0"] = log
        row = swarmd.write_now([self.worker()])["workers"][0]
        self.assertEqual((row["round"], row["rounds"]), (7, 12))

    def test_no_log_yet_is_not_an_error(self):
        row = swarmd.write_now([self.worker()])["workers"][0]
        self.assertIsNone(row["round"])

    def test_a_waiting_worker_says_what_it_is_waiting_for(self):
        swarmd._waiting["w0"] = "waiting for the allowance: day's allowance spent"
        row = swarmd.write_now([self.worker()])["workers"][0]
        self.assertIn("allowance", row["waiting"])

    def test_a_dead_or_idle_worker_is_not_listed(self):
        self.assertEqual(swarmd.write_now([self.worker(alive=False)])["workers"], [])
        idle = self.worker()
        idle.task = None
        self.assertEqual(swarmd.write_now([idle])["workers"], [])

    def test_every_model_resting_is_said_with_when_the_first_is_back(self):
        ledger = swarmd.Ledger(self.root / "learn.json")
        ledger.cool("a:free", "provider down", permanent=True)
        back = ledger.resting()[0][1]
        note = swarmd.write_now([], {"models": ["a:free"]})
        self.assertEqual(note["idle"], "all models resting")
        self.assertEqual(note["back"], back)

    def test_it_is_written_atomically_and_read_back(self):
        swarmd.write_now([self.worker()])
        self.assertTrue((self.root / "now.json").is_file())
        self.assertFalse((self.root / "now.tmp").exists())
        self.assertEqual(swarmd.read_now()["workers"][0]["task"], "t1")

    def test_a_stale_note_is_not_shown_as_what_is_happening_now(self):
        (self.root / "now.json").write_text(json.dumps({"at": time.time() - 3600, "workers": []}))
        self.assertIsNone(swarmd.read_now())
        self.assertIsNotNone(swarmd.read_now(max_age=7200))

    def test_a_missing_or_corrupt_note_is_just_absent(self):
        self.assertIsNone(swarmd.read_now())
        (self.root / "now.json").write_text("{not json")
        self.assertIsNone(swarmd.read_now())


class HealthTests(unittest.TestCase):
    """The 24-hour numbers: is this swarm landing anything, and what is it costing."""

    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.root = Path(d.name)
        p = patch.object(swarmd, "STATE", self.root)
        p.start()
        self.addCleanup(p.stop)

    def attempts(self, *rows, age=60):
        with open(self.root / "journal.jsonl", "a") as f:
            for r in rows:
                f.write(json.dumps({"t": time.time() - age, "event": "attempt", **r}) + "\n")

    def test_it_counts_attempts_landed_and_the_rate(self):
        self.attempts({"stage": "accepted"}, {"stage": "no_change"}, {"stage": "no_change"},
                      {"stage": "tests_failed"})
        h = swarmd.health()
        self.assertEqual((h["attempts"], h["landed"], h["landed_rate"]), (4, 1, 0.25))

    def test_the_top_three_failure_classes(self):
        self.attempts(*[{"stage": "no_change", "failure_class": "harness"}] * 5,
                      *[{"stage": "tests_failed", "failure_class": "task"}] * 3,
                      *[{"stage": "model_error", "failure_class": "model"}] * 2)
        self.assertEqual(swarmd.health()["failures"],
                         [("harness", 5), ("task", 3), ("model", 2)])

    def test_an_older_row_classifies_from_its_stage(self):
        """Rows written before failure_class existed still have to be counted."""
        self.attempts({"stage": "no_change"}, {"stage": "tests_failed"})
        self.assertEqual(dict(swarmd.health()["failures"]), {"harness": 1, "task": 1})

    def test_cost_and_cost_per_landed_commit(self):
        self.attempts({"stage": "accepted", "usd": 0.04}, {"stage": "no_change", "usd": 0.02})
        h = swarmd.health()
        self.assertEqual((h["usd"], h["usd_per_landed"]), (0.06, 0.06))

    def test_unhealthy_needs_both_a_low_rate_and_enough_attempts(self):
        self.attempts(*[{"stage": "no_change"}] * 9)
        self.assertFalse(swarmd.health()["unhealthy"], "nine attempts is not a rate")
        self.attempts({"stage": "no_change"})
        self.assertTrue(swarmd.health()["unhealthy"])

    def test_a_healthy_rate_is_not_flagged(self):
        self.attempts(*[{"stage": "accepted"}] * 5, *[{"stage": "no_change"}] * 5)
        self.assertFalse(swarmd.health()["unhealthy"])

    def test_rows_outside_the_window_do_not_count(self):
        self.attempts({"stage": "accepted"}, age=48 * 3600)
        self.assertEqual(swarmd.health(24)["attempts"], 0)

    def test_no_attempts_at_all_is_not_a_division_by_zero(self):
        h = swarmd.health()
        self.assertEqual((h["attempts"], h["landed_rate"], h["unhealthy"]), (0, None, False))


class LandedTests(unittest.TestCase):
    """The Landed tab: the swarm's own commits, with who wrote each and what it earned."""

    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.root = Path(d.name)
        p = patch.object(swarmd, "STATE", self.root)
        p.start()
        self.addCleanup(p.stop)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-b", "main")
        self.git("config", "user.email", "t@example.invalid")
        self.git("config", "user.name", "T")
        self.commit("baseline", trailers=None)
        self.git("branch", "swarm/trunk")

    def git(self, *a):
        return subprocess.run(["git", "-C", str(self.repo), *a], check=True,
                              capture_output=True, text=True).stdout

    def commit(self, subject, trailers=("t1", "qwen/qwen3-coder", "kimi"), branch=None):
        if branch:
            self.git("checkout", "-q", branch)
        (self.repo / "f.txt").write_text(subject)
        self.git("add", "-A")
        msg = f"swarm: {subject}"
        if trailers:
            msg += (f"\n\nSwarm-Task: {trailers[0]}\nSwarm-Implementer: {trailers[1]}"
                    f"\nSwarm-Reviewer: {trailers[2]}")
        self.git("commit", "-q", "-m", msg)

    def cfg(self):
        return {"repo": str(self.repo), "base_branch": "main"}

    def journal(self, *rows):
        with open(self.root / "journal.jsonl", "a") as f:
            for r in rows:
                f.write(json.dumps({"t": time.time(), **r}) + "\n")

    def test_a_commit_carries_its_model_reward_and_goal_item(self):
        self.commit("Shard Stack M1", branch="swarm/trunk")
        self.journal({"event": "attempt", "id": "t1", "stage": "accepted", "usd": 0.03},
                     {"event": "reward", "id": "t1", "reward": 0.84, "goal_item": 1})
        row = swarmd.landed_commits(self.cfg())[0]
        self.assertEqual(row["subject"], "Shard Stack M1")
        self.assertEqual(row["model"], "qwen/qwen3-coder")
        self.assertEqual((row["reward"], row["goal_item"], row["usd"]), (0.84, 1, 0.03))

    def test_commits_that_are_not_the_swarms_are_left_out(self):
        self.git("checkout", "-q", "swarm/trunk")
        self.commit("someone's own work", trailers=None)
        self.commit("Shard Stack M1")
        self.assertEqual([r["subject"] for r in swarmd.landed_commits(self.cfg())],
                         ["Shard Stack M1"])

    def test_a_merged_trunk_still_shows_what_landed(self):
        """base..trunk is empty once trunk is merged; the history is not."""
        self.commit("Shard Stack M1", branch="swarm/trunk")
        self.git("checkout", "-q", "main")
        self.git("merge", "-q", "--no-edit", "swarm/trunk")
        self.assertEqual(len(swarmd.landed_commits(self.cfg())), 1)

    def test_the_newest_come_first_and_the_limit_holds(self):
        self.git("checkout", "-q", "swarm/trunk")
        for i in range(5):
            self.commit(f"change {i}", trailers=(f"t{i}", "m:free", "r:free"))
        rows = swarmd.landed_commits(self.cfg(), limit=3)
        self.assertEqual([r["subject"] for r in rows], ["change 4", "change 3", "change 2"])

    def test_no_trunk_at_all_is_an_empty_list(self):
        self.git("branch", "-D", "swarm/trunk")
        self.assertEqual(swarmd.landed_commits(self.cfg()), [])


class RetryAndRequeueTests(unittest.TestCase):
    """Retry now, and putting a parked task back."""

    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        p = patch.object(swarmd, "STATE", Path(d.name))
        p.start()
        self.addCleanup(p.stop)
        self.q = swarmd.Queue(max_depth=2, max_queue=3)

    def test_retry_now_clears_the_backoff(self):
        t = self.q.add("a task")
        self.q.claim()
        held = self.q.release(t["id"], False, "tests failed", failure_class="task")
        self.assertGreater(held["not_before"], time.time())
        self.assertNotIn("not_before", self.q.retry_now(t["id"]))
        self.assertIsNotNone(self.q.claim())

    def test_retrying_a_task_that_is_not_there(self):
        with self.assertRaises(KeyError):
            self.q.retry_now("nope")

    def test_a_parked_task_comes_back_with_its_attempts_cleared(self):
        t = self.q.add("Shard Stack M1")
        for _ in range(2):
            self.q.claim()
            rows = swarmd._read(self.q.path)
            if rows:
                rows[0].pop("not_before", None)
                swarmd._write(self.q.path, rows)
            out = self.q.release(t["id"], False, "tests failed", failure_class="task")
        self.assertEqual(out["status"], "split")
        back = self.q.requeue(t["id"])
        self.assertEqual((back["attempts"], back["origin"]), (0, "human"))
        self.assertNotIn("status", back)
        self.assertNotIn("not_before", back)
        self.assertEqual([r["id"] for r in self.q.pending()], [t["id"]])
        self.assertFalse([r for r in swarmd._read(self.q.done) if r["id"] == t["id"]])

    def test_requeueing_something_that_did_not_finish(self):
        with self.assertRaises(KeyError):
            self.q.requeue("nope")

    def test_requeueing_twice_finds_nothing_the_second_time(self):
        t = self.q.add("a task")
        self.q.claim()
        self.q.release(t["id"], True, "landed")
        self.q.requeue(t["id"])
        with self.assertRaises(KeyError):
            self.q.requeue(t["id"])      # it is in the queue now, not among the finished

    def test_a_task_that_is_somehow_in_both_places_is_not_duplicated(self):
        t = self.q.add("a task")
        with open(self.q.done, "a") as f:
            f.write(json.dumps(dict(t, status="parked", finished=time.time())) + "\n")
        with self.assertRaisesRegex(ValueError, "already in the queue"):
            self.q.requeue(t["id"])

    def test_a_full_queue_refuses(self):
        t = self.q.add("Shard Stack M1")
        self.q.claim()
        self.q.release(t["id"], True, "landed")
        for i in range(3):
            self.q.add(f"filler {i}")
        with self.assertRaisesRegex(ValueError, "queue is full"):
            self.q.requeue(t["id"])


class BridgeSurfaceTests(unittest.TestCase):
    """The commands the panel actually calls."""

    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.root = Path(d.name)
        (self.root / "configs").mkdir()
        for name in ("state", "logs"):
            (self.root / name).mkdir()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        for a in (["init", "-b", "main"], ["config", "user.email", "t@e.invalid"],
                  ["config", "user.name", "T"]):
            subprocess.run(["git", "-C", str(self.repo), *a], check=True, capture_output=True)
        (self.repo / "a.txt").write_text("x\n")
        for a in (["add", "-A"], ["commit", "-q", "-m", "baseline"]):
            subprocess.run(["git", "-C", str(self.repo), *a], check=True, capture_output=True)
        self.default = self.root / "config.json"
        self.default.write_text(json.dumps(
            {"repo": str(self.repo), "base_branch": "main", "test_cmd": "true",
             "models": ["a:free"], "python": sys.executable, "workers": 1}))
        for mod in SWARMDS:
            for attr, value in (("HERE", self.root), ("CONFIG", self.default),
                                ("STATE", self.root / "state"), ("LOGS", self.root / "logs"),
                                ("EXAMPLE", self.root / "config.example.json")):
                p = patch.object(mod, attr, value)
                p.start()
                self.addCleanup(p.stop)
        import os
        os.environ.pop("FLINT_SWARM_CONFIG", None)

    def run_bridge(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = bridge.main(list(argv))
        return rc, [json.loads(l) for l in out.getvalue().splitlines() if l.strip()]

    def test_status_carries_now_and_health(self):
        rc, out = self.run_bridge("status", "--repo", str(self.repo))
        self.assertEqual(rc, 0)
        self.assertIn("now", out[0])
        self.assertIn("health", out[0])
        self.assertEqual(out[0]["health"]["attempts"], 0)
        self.assertIs(out[0]["daemon_paid_configured"], False)

    def test_status_says_when_the_daemon_paid_fallback_is_fully_configured(self):
        config = json.loads(self.default.read_text())
        config.update({"allow_paid": True, "paid_models": ["example/paid"],
                       "monthly_usd": 5})
        self.default.write_text(json.dumps(config))
        rc, out = self.run_bridge("status", "--repo", str(self.repo))
        self.assertEqual(rc, 0)
        self.assertIs(out[0]["daemon_paid_configured"], True)

    def test_landed_and_parked_answer_on_an_empty_repository(self):
        for name in ("landed", "parked"):
            rc, out = self.run_bridge(name, "--repo", str(self.repo))
            self.assertEqual(rc, 0, name)
            self.assertTrue(out[0]["ok"], name)

    def test_a_queue_row_says_what_the_panel_needs(self):
        swarmd.use_repo({"repo": str(self.repo)})
        q = swarmd.Queue()
        t = q.add("a task", origin="human", allow_test_changes=True)
        rc, out = self.run_bridge("queue-get", "--repo", str(self.repo), "--id", t["id"])
        row = out[0]["task"]
        self.assertIs(row["allow_test_changes"], True)
        self.assertEqual(row["harness_failures"], 0)

    def test_retry_and_requeue_over_the_wire(self):
        swarmd.use_repo({"repo": str(self.repo)})
        q = swarmd.Queue()
        t = q.add("a task")
        q.claim()
        q.release(t["id"], False, "tests failed", failure_class="task")
        rc, out = self.run_bridge("queue-retry", "--repo", str(self.repo), "--id", t["id"])
        self.assertEqual((rc, out[0]["ok"]), (0, True))
        rc, out = self.run_bridge("queue-retry", "--repo", str(self.repo), "--id", "nope")
        self.assertEqual((rc, out[0]["ok"]), (2, False))
        rc, out = self.run_bridge("queue-requeue", "--repo", str(self.repo), "--id", "nope")
        self.assertEqual((rc, out[0]["ok"]), (2, False))

    def test_parked_rows_carry_the_class_and_a_clean_reason(self):
        swarmd.use_repo({"repo": str(self.repo)})
        with open(swarmd.STATE / "done.jsonl", "a") as f:
            f.write(json.dumps({"id": "t1", "title": "Shard Stack M1", "status": "parked",
                                "finished": time.time(),
                                "note": "no implementation changes; evidence: /Users/d/x"}) + "\n")
        rc, out = self.run_bridge("parked", "--repo", str(self.repo))
        row = out[0]["parked"][0]
        self.assertEqual(row["why"], "no implementation changes")
        self.assertIsNone(row["handoff"])
        self.assertIsNone(row["failure_class"], "an unrecorded outcome must not be guessed at")
        with open(swarmd.STATE / "journal.jsonl", "a") as f:
            f.write(json.dumps({"t": time.time(), "event": "task", "id": "t1", "ok": False,
                                "stage": "no_change", "failure_class": "harness"}) + "\n")
        rc, out = self.run_bridge("parked", "--repo", str(self.repo))
        self.assertEqual(out[0]["parked"][0]["failure_class"], "harness")


if __name__ == "__main__":
    unittest.main()


class NowPhaseTests(NowTests):
    """`doing` is "<role> with <model>" during a turn and plain prose between turns."""

    def test_a_turn_reports_its_role_and_model(self):
        row = swarmd.write_now([self.worker(doing="implementer with qwen/qwen3-coder")])["workers"][0]
        self.assertEqual((row["role"], row["model"]), ("implementer", "qwen/qwen3-coder"))

    def test_the_gate_is_not_reported_as_a_model(self):
        row = swarmd.write_now([self.worker(doing="running the tests (baseline)")])["workers"][0]
        self.assertEqual(row["role"], "running the tests (baseline)")
        self.assertIsNone(row["model"], "the phase was being shown as the model's name")

    def test_a_paid_stand_in_is_the_model_that_actually_ran(self):
        swarmd._ran_on["w0"] = "qwen/qwen3-coder"
        self.addCleanup(swarmd._ran_on.clear)
        row = swarmd.write_now([self.worker(doing="implementer with a:free")])["workers"][0]
        self.assertEqual(row["model"], "qwen/qwen3-coder")
