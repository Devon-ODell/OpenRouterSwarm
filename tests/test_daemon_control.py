"""Starting and stopping one swarm among several.

Swarms for different repositories run side by side on this machine, on one OpenRouter key and
one shared dollar pot. Every control the panel offers has to name the repository it means.
"""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from swarm import bridge, swarmd

# bridge.py runs as a script and imports swarmd as a top-level module, so there are two module
# objects for it in a test process. Patch both or the bridge keeps reading the real checkout.
SWARMDS = (swarmd, bridge.swarmd)


class StopOneSwarmTests(unittest.TestCase):
    """`bridge.py stop --repo P` stops P's daemon and leaves every other swarm running.

    It used to SIGINT every process on the machine whose arguments looked like a swarm, so the
    Stop button on the studio panel also stopped the hedge-fund and kraken swarms beside it.
    """

    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.root = Path(d.name)
        (self.root / "configs").mkdir()
        for name in ("state", "logs"):
            (self.root / name).mkdir()
        self.default = self.root / "config.json"
        self.repos = {}
        for name in ("alpha", "beta"):
            self.repos[name] = self.root / name
            self.repos[name].mkdir()
        self.default.write_text(json.dumps(
            {"repo": str(self.repos["alpha"]), "base_branch": "main", "test_cmd": "true",
             "models": ["a:free"], "python": sys.executable, "workers": 1}))
        for mod in SWARMDS:
            for attr, value in (("HERE", self.root), ("CONFIG", self.default),
                                ("STATE", self.root / "state"), ("LOGS", self.root / "logs"),
                                ("EXAMPLE", self.root / "config.example.json")):
                p = patch.object(mod, attr, value)
                p.start()
                self.addCleanup(p.stop)
        os.environ.pop("FLINT_SWARM_CONFIG", None)

    def daemon(self, name):
        """A stand-in daemon: a live process whose arguments look like a swarm for that repo,
        plus the pid file the real daemon writes while it holds the repository's lock."""
        repo = str(self.repos[name].resolve())
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)",
                                 "swarmd.py", "grind", repo], stderr=subprocess.DEVNULL)
        self.addCleanup(self.reap, proc)
        state = self.root / "state" / swarmd.repo_slug(repo)
        state.mkdir(parents=True, exist_ok=True)
        (state / "daemon.pid").write_text(json.dumps(
            {"pid": proc.pid, "started": 0.0, "goal": "test", "repo": repo}))
        return proc, repo

    def reap(self, proc):
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)

    def run_bridge(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = bridge.main(list(argv))
        return rc, [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]

    def died(self, proc):
        """True once the process is gone. SIGINT reaches a `python -c` as KeyboardInterrupt."""
        try:
            proc.wait(timeout=10)
            return True
        except subprocess.TimeoutExpired:
            return False

    def test_stopping_one_repository_leaves_the_others_running(self):
        a, repo_a = self.daemon("alpha")
        b, _ = self.daemon("beta")
        rc, out = self.run_bridge("stop", "--repo", repo_a)
        self.assertEqual(rc, 0)
        self.assertEqual(out[0]["stopped"], [a.pid])
        self.assertTrue(self.died(a))
        self.assertIsNone(b.poll(), "the other repository's swarm was stopped too")

    def test_stop_needs_a_repository(self):
        rc, out = self.run_bridge("stop")
        self.assertEqual(rc, 2)
        self.assertIn("--repo", out[0]["error"])

    def test_all_still_stops_every_swarm(self):
        a, _ = self.daemon("alpha")
        b, _ = self.daemon("beta")
        rc, out = self.run_bridge("stop", "--all")
        self.assertEqual(rc, 0)
        self.assertEqual(out[0]["scope"], "all")
        self.assertEqual(sorted(out[0]["stopped"]), sorted([a.pid, b.pid]))
        self.assertTrue(self.died(a) and self.died(b))

    def test_no_daemon_is_not_an_error(self):
        rc, out = self.run_bridge("stop", "--repo", str(self.repos["alpha"]))
        self.assertEqual(rc, 0)
        self.assertEqual(out[0]["stopped"], [])
        self.assertIn("no swarm daemon", out[0]["reason"])

    def test_a_recycled_pid_is_not_signalled(self):
        """A pid file outlives the run that wrote it. This one now names an unrelated process."""
        other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                 stderr=subprocess.DEVNULL)
        self.addCleanup(self.reap, other)
        repo = str(self.repos["alpha"].resolve())
        state = self.root / "state" / swarmd.repo_slug(repo)
        state.mkdir(parents=True, exist_ok=True)
        (state / "daemon.pid").write_text(json.dumps(
            {"pid": other.pid, "started": 0.0, "goal": "test", "repo": repo}))
        rc, out = self.run_bridge("stop", "--repo", repo)
        self.assertEqual(rc, 2)
        self.assertEqual(out[0]["stopped"], [])
        self.assertIsNone(other.poll(), "signalled a process that is not a swarm daemon")

    def test_a_pid_file_from_another_repository_is_not_signalled(self):
        b, repo_b = self.daemon("beta")
        repo_a = str(self.repos["alpha"].resolve())
        state = self.root / "state" / swarmd.repo_slug(repo_a)
        state.mkdir(parents=True, exist_ok=True)
        (state / "daemon.pid").write_text(json.dumps(
            {"pid": b.pid, "started": 0.0, "goal": "test", "repo": repo_b}))
        rc, out = self.run_bridge("stop", "--repo", repo_a)
        self.assertEqual(rc, 2)
        self.assertIsNone(b.poll())

if __name__ == "__main__":
    unittest.main()


class HotReloadTests(unittest.TestCase):
    """A config edit reaching a running daemon, instead of waiting for the next one.

    There were 25 daemon starts in about 26 hours, most of them to apply a setting, and each one
    killed the attempt in flight: 54 attempts, 5.6 hours of model work.
    """

    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.root = Path(d.name)
        (self.root / "state").mkdir()
        self.path = self.root / "config.json"
        for attr, value in (("HERE", self.root), ("CONFIG", self.path),
                            ("STATE", self.root / "state")):
            p = patch.object(swarmd, attr, value)
            p.start()
            self.addCleanup(p.stop)
        # The baseline is a module global a daemon owns; no test may leave it set.
        swarmd._cfg_seen["mtime"] = 0.0
        self.addCleanup(swarmd._cfg_seen.update, {"mtime": 0.0})

    def test_a_process_that_is_not_the_daemon_reloads_nothing(self):
        """Only a running daemon reloads. Anything else holds a config it assembled itself —
        the bridge builds one per repository — and the file must not be poured over it."""
        c = self.write(test_cmd="the caller's own command")
        self.write(test_cmd="whatever is in the file")
        self.assertEqual(swarmd.watch_config(c), [])
        self.assertEqual(c["test_cmd"], "the caller's own command")

    def write(self, **kw):
        base = {"repo": str(self.root), "test_cmd": "true", "workers": 1,
                "models": ["a:free"], "steps": {"implementer": 12}}
        self.path.write_text(json.dumps({**base, **kw}))
        return json.loads(self.path.read_text())

    def running(self, **kw):
        """A config as a daemon holds it, with the baseline mtime already taken."""
        c = self.write(**kw)
        swarmd._cfg_seen["mtime"] = swarmd.config_mtime()
        return c

    def touch(self, **kw):
        """Rewrite the file with a newer mtime than the daemon has seen."""
        c = self.write(**kw)
        import os as _os
        _os.utime(self.path, (swarmd._cfg_seen["mtime"] + 10,) * 2)
        return c

    def test_an_unchanged_file_reloads_nothing(self):
        c = self.running()
        self.assertEqual(swarmd.watch_config(c), [])

    def test_the_model_pool_and_the_round_budget_take_effect(self):
        c = self.running()
        self.touch(models=["b:free", "c:free"], steps={"implementer": 20})
        self.assertEqual(sorted(swarmd.watch_config(c)), ["models", "steps"])
        self.assertEqual(c["models"], ["b:free", "c:free"])
        self.assertEqual(c["steps"], {"implementer": 20})

    def test_every_key_the_review_names_is_reloadable(self):
        for key in ("models", "paid_models", "steps", "role_timeouts", "turn_timeout",
                    "test_timeout", "max_repairs", "reviewer_exclude", "plan_cooldown",
                    "allowance_recheck", "monthly_usd", "owner_window", "max_queue",
                    "inject_corpus"):
            self.assertIn(key, swarmd.RELOADABLE, key)
        self.assertTrue(any(k.startswith("corpus_") for k in ("corpus_db", "corpus_k")))

    def test_a_corpus_setting_reloads_by_prefix(self):
        c = self.running()
        self.touch(corpus_k=9)
        self.assertEqual(swarmd.watch_config(c), ["corpus_k"])
        self.assertEqual(c["corpus_k"], 9)

    def test_the_change_is_logged_and_journalled(self):
        c = self.running()
        self.touch(plan_cooldown=120)
        lines = []
        with patch.object(swarmd, "log", lambda m, worker="swarm": lines.append(m)):
            swarmd.watch_config(c)
        self.assertTrue(any("config reloaded: plan_cooldown" in l for l in lines), lines)
        rows = [json.loads(l) for l in
                (swarmd.STATE / "journal.jsonl").read_text().splitlines() if l.strip()]
        self.assertEqual(rows[0]["event"], "config_reload")
        self.assertEqual(rows[0]["keys"], ["plan_cooldown"])

    def test_a_key_that_needs_a_restart_says_so_and_is_not_applied(self):
        c = self.running()
        self.touch(workers=4, repo="/somewhere/else")
        lines = []
        with patch.object(swarmd, "log", lambda m, worker="swarm": lines.append(m)):
            self.assertEqual(swarmd.watch_config(c), [])
        self.assertEqual((c["workers"], c["repo"]), (1, str(self.root)))
        self.assertTrue(any("restart needed for workers" in l for l in lines), lines)
        self.assertTrue(any("restart needed for repo" in l for l in lines), lines)
        rows = [json.loads(l) for l in
                (swarmd.STATE / "journal.jsonl").read_text().splitlines() if l.strip()]
        self.assertEqual(rows[0]["keys"], ["repo", "workers"])

    def test_a_key_deleted_from_the_file_goes_back_to_its_default(self):
        c = self.running(plan_cooldown=120)
        self.touch()
        self.assertEqual(swarmd.watch_config(c), ["plan_cooldown"])
        self.assertNotIn("plan_cooldown", c)

    def test_a_half_written_file_leaves_the_running_settings_alone(self):
        c = self.running()
        self.path.write_text('{"models": ["b:free"')
        import os as _os
        _os.utime(self.path, (swarmd._cfg_seen["mtime"] + 10,) * 2)
        lines = []
        with patch.object(swarmd, "log", lambda m, worker="swarm": lines.append(m)):
            self.assertEqual(swarmd.watch_config(c), [])
        self.assertEqual(c["models"], ["a:free"])
        self.assertTrue(any("could not be read" in l for l in lines), lines)

    def test_pacing_is_retuned_on_the_budget_that_is_running(self):
        c = self.running(daily_cap=50, reserve=10)
        budget = swarmd.Budget(cap=50, reserve=10, owner_window=["00:00", "00:00"])
        self.touch(daily_cap=200, reserve=20, owner_window=["20:00", "23:00"])
        swarmd.watch_config(c, budget)
        self.assertEqual((budget.cap, budget.reserve), (200, 20))
        self.assertEqual(budget.win_start, 20 * 3600)

    def test_an_impossible_pacing_edit_is_refused_not_applied_halfway(self):
        c = self.running(daily_cap=50, reserve=10)
        budget = swarmd.Budget(cap=50, reserve=10, owner_window=["00:00", "00:00"])
        self.touch(daily_cap=5, reserve=90)
        lines = []
        with patch.object(swarmd, "log", lambda m, worker="swarm": lines.append(m)):
            swarmd.watch_config(c, budget)
        self.assertEqual((budget.cap, budget.reserve), (50, 10))
        self.assertTrue(any("pacing left as it was" in l for l in lines), lines)

    def test_a_worker_checks_between_tasks_and_not_inside_one(self):
        c = self.running()
        q = swarmd.Queue()
        w = swarmd.Worker(0, c, q, Mock(), threading.Event())
        self.touch(models=["b:free"])
        with patch.object(w.stop, "wait", side_effect=lambda *a: w.stop.set()):
            w.run()                     # no task to claim: it checks, then waits, then stops
        self.assertEqual(c["models"], ["b:free"])


class DrainTests(unittest.TestCase):
    """Stopping after the task in flight, instead of killing it."""

    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        p = patch.object(swarmd, "STATE", Path(d.name))
        p.start()
        self.addCleanup(p.stop)

    def test_drain_sets_the_flag_once_and_says_so(self):
        tally = swarmd.Tally()
        lines = []
        with patch.object(swarmd, "log", lambda m, worker="swarm": lines.append(m)):
            swarmd.drain(tally)
            swarmd.drain(tally)
        self.assertTrue(tally.drain.is_set())
        self.assertEqual(len([l for l in lines if "draining" in l]), 1)
        rows = [json.loads(l) for l in
                (swarmd.STATE / "journal.jsonl").read_text().splitlines() if l.strip()]
        self.assertEqual([r["event"] for r in rows], ["drain_requested"])

    def test_a_draining_worker_finishes_its_task_and_stops(self):
        tally = swarmd.Tally()
        q = swarmd.Queue()
        q.add("one thing", "do it")
        c = {"repo": ".", "test_cmd": "true", "models": ["a:free"], "workers": 1}
        w = swarmd.Worker(0, c, q, Mock(), threading.Event(), tally=tally)
        done = []

        def do_task(task, goal):
            done.append(task["id"])
            swarmd.drain(tally)         # the signal arrives mid-task
            return True, "landed"
        with patch.object(w, "do_task", side_effect=do_task), \
             patch.object(swarmd, "read_goal", return_value="goal"), \
             patch.object(swarmd, "watch_config", return_value=[]):
            w.run()
        self.assertEqual(len(done), 1, "the task in flight was abandoned or another was claimed")
        self.assertEqual(swarmd._read(q.done)[-1]["status"], "done")


class StopDrainTests(unittest.TestCase):
    """`stop --drain` asks for that, over the wire."""

    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.root = Path(d.name)
        (self.root / "configs").mkdir()
        for name in ("state", "logs"):
            (self.root / name).mkdir()
        self.default = self.root / "config.json"
        self.repo = self.root / "alpha"
        self.repo.mkdir()
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
        os.environ.pop("FLINT_SWARM_CONFIG", None)

    def test_a_drain_sends_sigusr1_and_a_plain_stop_sends_sigint(self):
        import signal as sig
        repo = str(self.repo.resolve())
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)",
                                 "swarmd.py", "grind", repo], stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: (proc.kill(), proc.wait(timeout=10)))
        state = self.root / "state" / swarmd.repo_slug(repo)
        state.mkdir(parents=True, exist_ok=True)
        (state / "daemon.pid").write_text(json.dumps(
            {"pid": proc.pid, "started": 0.0, "goal": "t", "repo": repo}))
        sent = []
        with patch.object(bridge.os, "kill", side_effect=lambda pid, s: sent.append((pid, s))):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                bridge.main(["stop", "--repo", repo, "--drain"])
            drained = json.loads(out.getvalue())
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                bridge.main(["stop", "--repo", repo])
            stopped = json.loads(out.getvalue())
        self.assertEqual([s for _, s in sent if s], [sig.SIGUSR1, sig.SIGINT])
        self.assertEqual((drained["how"], drained["draining"]), ("drain", [proc.pid]))
        self.assertEqual(drained["stopped"], [])
        self.assertEqual((stopped["how"], stopped["stopped"]), ("stop", [proc.pid]))
