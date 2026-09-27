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
import unittest
from pathlib import Path
from unittest.mock import patch

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
