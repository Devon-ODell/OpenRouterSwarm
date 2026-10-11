"""SwarmApplication fronts whichever repository the daemon has selected.

It must read the daemon's paths when asked, never cache them: the bridge selects a repository
with use_repo and then asks the facade where its state lives.
"""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from swarm import swarmd  # noqa: F401  (puts swarm/ on sys.path for app's plain imports)
from swarm import app as app_module


class SwarmApplicationTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.root = Path(d.name)
        self.daemon = SimpleNamespace(
            STATE=self.root / "state-a", LOGS=self.root / "logs-a", CONFIG=self.root / "a.json",
            _read=Mock(return_value=[{"event": "x"}]), use_repo=Mock(), load_cfg=Mock(return_value={"repo": "r"}),
            cfg=Mock(return_value={"repo": "r"}), queue_for=Mock(), build_report=Mock(return_value="report"))

    def test_for_repo_selects_the_repository_on_the_daemon(self):
        app = app_module.SwarmApplication.for_repo("/some/repo", daemon=self.daemon)
        self.daemon.load_cfg.assert_called_once_with("/some/repo")
        self.daemon.use_repo.assert_called_once_with({"repo": "r"})
        self.assertIsInstance(app, app_module.SwarmApplication)

    def test_paths_follow_a_later_repository_switch(self):
        app = app_module.SwarmApplication(self.daemon)
        self.assertEqual(app.state_dir, self.root / "state-a")
        self.daemon.STATE, self.daemon.LOGS = self.root / "state-b", self.root / "logs-b"
        self.daemon.CONFIG = self.root / "b.json"
        self.assertEqual(app.state_dir, self.root / "state-b")
        self.assertEqual(app.log_path("2026-10-11"), self.root / "logs-b" / "2026-10-11.log")
        self.assertEqual(app.config_path, self.root / "b.json")

    def test_records_are_read_through_the_daemon_reader(self):
        app = app_module.SwarmApplication(self.daemon)
        self.assertEqual(app.journal(), [{"event": "x"}])
        self.daemon._read.assert_called_with(self.root / "state-a" / "journal.jsonl")
        app.done()
        self.daemon._read.assert_called_with(self.root / "state-a" / "done.jsonl")

    def test_add_task_and_report_use_the_live_config(self):
        app = app_module.SwarmApplication(self.daemon)
        app.add_task("Do one thing", "detail", "bugfix", priority=2)
        self.daemon.queue_for.return_value.add.assert_called_once_with(
            "Do one thing", "detail", "bugfix", priority=2)
        self.assertEqual(app.report(12), "report")
        self.daemon.build_report.assert_called_once_with({"repo": "r"}, 12)


if __name__ == "__main__":
    unittest.main()
