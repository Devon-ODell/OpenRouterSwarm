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
        repo = self.root / "repo"
        repo.mkdir()
        self.daemon.load_cfg.return_value = {"repo": str(repo.resolve()), "max_queue": 5}
        app = app_module.SwarmApplication.for_repo(str(repo), daemon=self.daemon)
        self.daemon.load_cfg.assert_called_once_with(str(repo))
        self.daemon.use_repo.assert_called_once_with({"repo": str(repo.resolve()), "max_queue": 5})
        self.assertIsInstance(app, app_module.SwarmApplication)

    def test_an_untuned_repo_is_not_answered_for_by_the_default_configs_repo(self):
        # load_cfg falls back to config.json, which names a different repository.
        repo = self.root / "untuned"
        repo.mkdir()
        self.daemon.load_cfg.return_value = {"repo": "/somewhere/else", "max_queue": 5}
        app = app_module.SwarmApplication.for_repo(str(repo), daemon=self.daemon)
        selected = self.daemon.use_repo.call_args.args[0]
        self.assertEqual(selected["repo"], str(repo.resolve()))
        app.add_task("Do one thing")
        self.assertEqual(self.daemon.queue_for.call_args.args[0]["repo"], str(repo.resolve()))
        self.daemon.cfg.assert_not_called()

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

    def test_operations_act_on_the_selected_config_not_a_fresh_read(self):
        app = app_module.SwarmApplication(self.daemon, {"repo": "/selected"})
        app.add_task("Do one thing", "detail", "bugfix", priority=2)
        self.assertEqual(self.daemon.queue_for.call_args.args[0], {"repo": "/selected"})
        self.daemon.queue_for.return_value.add.assert_called_once_with(
            "Do one thing", "detail", "bugfix", priority=2)
        self.assertEqual(app.report(12), "report")
        self.daemon.build_report.assert_called_once_with({"repo": "/selected"}, 12)
        self.daemon.cfg.assert_not_called()

    def test_operations_refuse_without_a_selected_repository(self):
        app = app_module.SwarmApplication(self.daemon)
        with self.assertRaises(RuntimeError):
            app.add_task("Do one thing")

    def test_status_reads_the_run_record_without_writing_it(self):
        state = self.daemon.STATE
        state.mkdir()
        (state / "run.json").write_text('{"id": "live-run", "requests_started": 7}')
        self.daemon.read_now = Mock(return_value={"workers": []})
        self.daemon.run_status = Mock()
        snap = app_module.SwarmApplication(self.daemon).status()
        self.assertEqual(snap["run"]["id"], "live-run")
        self.assertEqual(snap["now"], {"workers": []})
        self.daemon.run_status.assert_not_called()
        self.assertEqual((state / "run.json").read_text(), '{"id": "live-run", "requests_started": 7}')

    def test_the_facade_does_not_run_the_daemon_lifecycle_in_process(self):
        self.assertFalse(hasattr(app_module.SwarmApplication, "start"))
        self.assertFalse(hasattr(app_module.SwarmApplication, "stop"))


if __name__ == "__main__":
    unittest.main()
