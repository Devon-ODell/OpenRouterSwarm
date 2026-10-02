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

    def test_setup_with_a_repo_uses_the_tuned_file(self):
        """`add/plan/report/status --repo` must be ground on the repository's own
        config, not whatever swarm/config.json happens to name (TODO 2026-10-01 #2)."""
        tuned = self.tuned(test_cmd="tuned-tests", max_queue=3)
        from types import SimpleNamespace as NS
        c = swarmd._setup(NS(repo=str(self.repo)))
        self.assertEqual(str(swarmd.CONFIG), str(tuned))
        self.assertEqual(c["test_cmd"], "tuned-tests")
        self.assertEqual(c["max_queue"], 3)

    def test_setup_without_a_repo_stays_on_the_default(self):
        self.tuned(test_cmd="tuned-tests", max_queue=3)
        from types import SimpleNamespace as NS
        c = swarmd._setup(NS())
        self.assertEqual(str(swarmd.CONFIG), str(self.default))
        self.assertEqual(c["test_cmd"], "default-tests")

    def test_cmd_add_with_a_repo_uses_the_tuned_max_queue(self):
        """The TODO's acceptance: `add --repo <studio>` obeys the tuned max_queue,
        not the default one. A tuned max_queue of 1 accepts exactly one task."""
        tuned = self.tuned(max_queue=1)
        from types import SimpleNamespace as NS

        def args(title):
            return NS(repo=str(self.repo), title=title, detail="d", kind="feature",
                      priority=1, acceptance=None, depends_on=None,
                      execution_class="standard", allow_test_changes=False)
        with contextlib.redirect_stdout(io.StringIO()):
            swarmd.cmd_add(args("First"))
        q = swarmd.Queue(max_queue=1)
        self.assertEqual(len(q.pending()), 1)
        # After _setup(a) the queue uses the tuned max_queue=1, so the second add
        # must be refused rather than silently append.
        with contextlib.redirect_stdout(io.StringIO()), \
                self.assertRaises(SystemExit) as caught:
            swarmd.cmd_add(args("Second"))
        self.assertIn("not queued", str(caught.exception))
        # And it did not touch the real queue: only the first task is present.
        self.assertEqual(len(q.pending()), 1)
        self.assertEqual([t["title"] for t in q.pending()], ["First"])


if __name__ == "__main__":
    unittest.main()
if __name__ == "__main__":
    unittest.main()


class ConfigWarningTests(unittest.TestCase):
    """A config is read in silence, so everything that will not do what it looks like it does
    has to be said out loud at startup."""

    def cfg(self, **kw):
        return {"repo": "/tmp/x", "test_cmd": "true", "models": ["a:free"], **kw}

    def test_an_unknown_key_is_named(self):
        out = swarmd.config_warnings(self.cfg(plan_cooldownn=600))
        self.assertTrue(any("plan_cooldownn" in w for w in out), out)
        self.assertTrue(any("typo or a rename" in w for w in out), out)

    def test_every_key_the_shipped_template_uses_is_known(self):
        template = json.loads((Path(swarmd.HERE) / "config.example.json").read_text())
        self.assertFalse([w for w in swarmd.config_warnings(template) if "unknown setting" in w])

    def test_every_key_the_studio_config_uses_is_known(self):
        self.assertFalse([w for w in swarmd.config_warnings(self.studio())
                          if "unknown setting" in w])

    def studio(self):
        """The studio's tuned config. swarm/configs/ is gitignored — the swarm rewrites these
        files — so this is per-machine and the test skips where it does not exist."""
        path = Path(swarmd.HERE) / "configs" / "openRouter-Studio-ab0a4a.json"
        if not path.is_file():
            self.skipTest("no tuned studio config on this machine")
        return json.loads(path.read_text())

    def test_a_private_key_is_left_alone(self):
        self.assertFalse(swarmd.config_warnings(self.cfg(_comment="why this is tuned so")))

    def test_rounds_that_cannot_fit_their_timeout_are_flagged(self):
        """26 rounds at ~47s could never fit 900s, and every implementer was SIGKILLed."""
        out = swarmd.config_warnings(self.cfg(steps={"implementer": 26},
                                              role_timeouts={"implementer": 900}))
        self.assertTrue(any("steps.implementer is 26" in w and "900s timeout" in w for w in out), out)

    def test_rounds_that_do_fit_are_not_flagged(self):
        self.assertFalse(swarmd.config_warnings(self.cfg(steps={"implementer": 12},
                                                         role_timeouts={"implementer": 900})))

    def test_an_all_paid_pool_under_allow_paid_is_flagged(self):
        out = swarmd.config_warnings(self.cfg(models=["qwen/qwen3-coder"], allow_paid=True))
        self.assertTrue(any("every turn is paid" in w for w in out), out)

    def test_a_pool_with_a_free_model_is_not(self):
        self.assertFalse(swarmd.config_warnings(
            self.cfg(models=["a:free", "qwen/qwen3-coder"], allow_paid=True)))

    def test_paid_ids_without_allow_paid_are_someone_elses_problem(self):
        """preflight refuses that outright; this is not the place to say it twice."""
        self.assertFalse(swarmd.config_warnings(self.cfg(models=["qwen/qwen3-coder"])))

    def test_the_warnings_are_logged_and_journalled(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        lines = []
        with patch.object(swarmd, "STATE", Path(d.name)), \
             patch.object(swarmd, "log", lambda m, worker="swarm": lines.append(m)):
            swarmd.warn_about_config(self.cfg(nonsense=1), record=True)
            rows = [json.loads(l) for l in
                    (Path(d.name) / "journal.jsonl").read_text().splitlines() if l.strip()]
        self.assertTrue(any("WARNING config" in l for l in lines), lines)
        self.assertEqual(rows[0]["event"], "config_warnings")

    def test_a_clean_config_journals_nothing(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        with patch.object(swarmd, "STATE", Path(d.name)), patch.object(swarmd, "log"):
            swarmd.warn_about_config(self.cfg(), record=True)
        self.assertFalse((Path(d.name) / "journal.jsonl").exists())


class ConfigChangelogTests(unittest.TestCase):
    """The JSON holds values; the reasons live beside it."""

    def test_the_studio_config_is_values_not_history(self):
        path = Path(swarmd.HERE) / "configs" / "openRouter-Studio-ab0a4a.json"
        if not path.is_file():
            self.skipTest("no tuned studio config on this machine")
        c = json.loads(path.read_text())
        self.assertNotIn("_comment", c)
        for key, value in c.items():
            self.assertLess(len(str(value)), 600, f"{key} reads like prose, not a setting")

    def test_the_history_is_kept_where_it_can_be_corrected(self):
        log = Path(swarmd.HERE) / "configs" / "openRouter-Studio-ab0a4a.CHANGELOG.md"
        if not (Path(swarmd.HERE) / "configs" / "openRouter-Studio-ab0a4a.json").is_file():
            self.skipTest("no tuned studio config on this machine")
        self.assertTrue(log.is_file(), "the config's history must live beside it")
        text = log.read_text()
        self.assertIn("qwen/qwen3-coder", text)
        self.assertIn("Corrections", text, "the stale claims are corrected, not just moved")
