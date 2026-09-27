"""Which config file a repository is ground on.

A repository with a tuned file in `swarm/configs/<slug>.json` must be ground on that file from
every entry point: `swarm grind <repo>`, the bridge, and the Start button in the Cursor panel.
Before this, only `start-studio-swarm.command` found one, through `FLINT_SWARM_CONFIG`; a bare
restart read `swarm/config.json` instead and quietly used the wrong test gate, the wrong plan
cooldown and the wrong paid fallback.
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


class ConfigResolutionTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.root = Path(d.name)
        self.configs = self.root / "configs"
        self.configs.mkdir()
        (self.root / "state").mkdir()
        (self.root / "logs").mkdir()
        self.default = self.root / "config.json"
        self.repo = self.make_repo("target")
        self.write(self.default, test_cmd="default-tests")
        for mod in SWARMDS:
            for name, value in (("HERE", self.root), ("CONFIG", self.default),
                                ("STATE", self.root / "state"), ("LOGS", self.root / "logs"),
                                ("EXAMPLE", self.root / "config.example.json")):
                p = patch.object(mod, name, value)
                p.start()
                self.addCleanup(p.stop)
        p = patch.dict(os.environ, {}, clear=False)
        p.start()
        self.addCleanup(p.stop)
        os.environ.pop("FLINT_SWARM_CONFIG", None)

    # ------------------------------------------------------------- helpers

    def make_repo(self, name):
        repo = self.root / name
        repo.mkdir()

        def git(*args):
            subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
        git("init", "-b", "main")
        git("config", "user.email", "test@example.invalid")
        git("config", "user.name", "Test")
        (repo / "app.txt").write_text("baseline\n")
        git("add", ".")
        git("commit", "-m", "baseline")
        return repo

    def write(self, path, **kw):
        c = {"repo": str(self.repo), "base_branch": "main", "test_cmd": "true",
             "models": ["a:free"], "python": sys.executable, "workers": 1}
        path.write_text(json.dumps({**c, **kw}, indent=2) + "\n")
        return path

    def tuned(self, **kw):
        return self.write(self.configs / f"{swarmd.repo_slug(self.repo)}.json", **kw)

    def run_bridge(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = bridge.main(list(argv))
        return rc, [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]

    # --------------------------------------------------------------- order

    def test_the_environment_variable_wins(self):
        self.tuned(test_cmd="tuned-tests")
        chosen = self.write(self.root / "chosen.json", test_cmd="chosen-tests")
        os.environ["FLINT_SWARM_CONFIG"] = str(chosen)
        self.assertEqual(swarmd.config_path_for(self.repo), chosen)
        self.assertEqual(swarmd.load_cfg(self.repo)["test_cmd"], "chosen-tests")

    def test_a_repository_with_a_tuned_file_is_ground_on_it(self):
        tuned = self.tuned(test_cmd="tuned-tests", plan_cooldown=120)
        self.assertEqual(swarmd.config_path_for(self.repo), tuned)
        c = swarmd.load_cfg(self.repo)
        self.assertEqual((c["test_cmd"], c["plan_cooldown"]), ("tuned-tests", 120))
        self.assertEqual(swarmd.CONFIG, tuned)
        self.assertEqual(swarmd.config_kind(), "per-repo")

    def test_the_default_is_the_fallback(self):
        self.assertEqual(swarmd.config_path_for(self.repo), self.default)
        self.assertEqual(swarmd.load_cfg(self.repo)["test_cmd"], "default-tests")
        self.assertEqual(swarmd.config_kind(), "default")

    def test_another_repositorys_tuned_file_is_not_used(self):
        other = self.make_repo("other")
        self.write(self.configs / f"{swarmd.repo_slug(other)}.json", test_cmd="other-tests")
        self.assertEqual(swarmd.config_path_for(self.repo), self.default)
        self.assertEqual(swarmd.load_cfg(self.repo)["test_cmd"], "default-tests")

    def test_the_slug_matches_the_state_directory(self):
        """The config file is named by the same slug as state, logs and worktrees."""
        swarmd.use_repo({"repo": str(self.repo)})
        self.assertEqual(swarmd.SLUG, swarmd.repo_slug(self.repo))
        self.assertEqual(swarmd.per_repo_config(self.repo).stem, swarmd.SLUG)

    def test_saving_writes_back_to_the_file_it_loaded(self):
        tuned = self.tuned(test_cmd="tuned-tests")
        c = swarmd.load_cfg(self.repo)
        c["plan_cooldown"] = 42
        swarmd.save_cfg(c)
        self.assertEqual(json.loads(tuned.read_text())["plan_cooldown"], 42)
        self.assertNotIn("plan_cooldown", json.loads(self.default.read_text()))

    def test_the_path_stays_out_of_the_file(self):
        """A copied config must not drag another checkout's config path along with it."""
        self.tuned()
        swarmd.save_cfg(swarmd.load_cfg(self.repo))
        saved = json.loads(swarmd.CONFIG.read_text())
        self.assertFalse([k for k in saved if "config" in k.lower()])

    # ------------------------------------------------------------ mismatch

    def test_a_tuned_file_that_was_not_loaded_is_reported(self):
        tuned = self.tuned()
        swarmd.use_repo({"repo": str(self.repo)})
        lines = []
        with patch.object(swarmd, "log", lambda msg, worker="swarm": lines.append(msg)):
            self.assertEqual(swarmd.warn_config_mismatch(str(self.repo), record=True), tuned)
        self.assertTrue(any("WARNING" in line and tuned.name in line for line in lines), lines)
        rows = [json.loads(line) for line in
                (swarmd.STATE / "journal.jsonl").read_text().splitlines() if line.strip()]
        self.assertEqual([r["event"] for r in rows], ["config_mismatch"])
        self.assertEqual(rows[0]["tuned"], str(tuned))

    def test_no_mismatch_when_the_tuned_file_is_the_loaded_one(self):
        self.tuned()
        swarmd.load_cfg(self.repo)
        swarmd.use_repo({"repo": str(self.repo)})
        self.assertIsNone(swarmd.warn_config_mismatch(str(self.repo), record=True))
        self.assertFalse((swarmd.STATE / "journal.jsonl").exists())

    # -------------------------------------------------------------- bridge

    def test_bridge_config_for_returns_the_tuned_test_cmd(self):
        self.tuned(test_cmd="tuned-tests")
        self.assertEqual(bridge.config_for(str(self.repo))["test_cmd"], "tuned-tests")

    def test_bridge_config_for_without_a_tuned_file_uses_the_default(self):
        self.assertEqual(bridge.config_for(str(self.repo))["test_cmd"], "default-tests")

    def test_grind_cmd_carries_the_tuned_config(self):
        tuned = self.tuned()
        rc, out = self.run_bridge("grind-cmd", "--repo", str(self.repo), "--goal", "GOAL.md")
        self.assertEqual(rc, 0)
        self.assertTrue(out[0]["command"].startswith("FLINT_SWARM_CONFIG="), out[0]["command"])
        self.assertIn(str(tuned), out[0]["command"])
        self.assertEqual((out[0]["config"], out[0]["config_kind"]), (str(tuned), "per-repo"))

    def test_grind_cmd_without_a_tuned_config_is_unprefixed(self):
        rc, out = self.run_bridge("grind-cmd", "--repo", str(self.repo))
        self.assertEqual(rc, 0)
        self.assertFalse(out[0]["command"].startswith("FLINT_SWARM_CONFIG="))
        self.assertEqual(out[0]["config_kind"], "default")

    def test_bridge_status_says_which_config_is_loaded(self):
        """The finish line for P0-1: `bridge.py status --repo <studio>` with no environment
        variable set reports the repository's own config."""
        tuned = self.tuned(test_cmd="tuned-tests")
        rc, out = self.run_bridge("status", "--repo", str(self.repo))
        self.assertEqual(rc, 0)
        self.assertEqual(out[0]["config_kind"], "per-repo")
        self.assertEqual(out[0]["config_path"], str(tuned))
        self.assertEqual(out[0]["config_tuned_available"], str(tuned))

    def test_bridge_status_flags_the_default_while_a_tuned_file_exists(self):
        tuned = self.tuned()
        os.environ["FLINT_SWARM_CONFIG"] = str(self.default)
        rc, out = self.run_bridge("status", "--repo", str(self.repo))
        self.assertEqual(rc, 0)
        self.assertEqual(out[0]["config_kind"], "environment")
        self.assertEqual(out[0]["config_path"], str(self.default))
        self.assertEqual(out[0]["config_tuned_available"], str(tuned))


if __name__ == "__main__":
    unittest.main()
if __name__ == "__main__":
    unittest.main()
