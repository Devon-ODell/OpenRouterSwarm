"""Config hardening: null settings, the shipped template, and `--repo` aiming.

- `"goal_file": null` used to crash `swarm run` at startup with a TypeError: every reader does
  `c.get(key, default)`, which returns None for a key that is present but null.
- config.example.json listed a paid model while allow_paid was false, so preflight refused the
  template as shipped.
- `swarm stop` took no --repo and, like every `--repo` command on a repository without a tuned
  file, acted on whichever repository the default config named.
"""
import json
import signal
from types import SimpleNamespace as NS
from unittest.mock import patch

from swarm import swarmd
from tests.test_cli_contracts import RepoCase, _git


class NullSettingTests(RepoCase):
    def test_a_null_setting_falls_back_to_its_default(self):
        self.write_config(self.default, goal_file=None, test_timeout=None)
        c = swarmd.load_cfg()
        self.assertNotIn("goal_file", c)
        self.assertEqual(c["_nulls"], ["goal_file", "test_timeout"])
        (self.repo / "GOAL.md").write_text("Make the game fun.\n")
        swarmd.use_repo(c)
        self.assertIn("Make the game fun.", swarmd.read_goal(c))

    def test_null_settings_are_named_in_the_warnings(self):
        out = swarmd.config_warnings(swarmd.without_nulls({"goal_file": None, "workers": 1}))
        self.assertTrue(any("goal_file" in w and "null" in w for w in out), out)

    def test_saving_does_not_write_the_bookkeeping_key(self):
        self.write_config(self.default, goal_file=None)
        swarmd.save_cfg(swarmd.load_cfg())
        self.assertNotIn("_nulls", json.loads(self.default.read_text()))

    def test_a_live_edit_to_null_reverts_the_setting_to_its_default(self):
        c = swarmd.load_cfg()
        c["max_repairs"] = 5
        self.write_config(self.default, max_repairs=None)
        swarmd._cfg_seen["mtime"] = 1.0       # any earlier mtime: the edit counts as a change
        changed = swarmd.watch_config(c)
        self.assertIn("max_repairs", changed)
        self.assertNotIn("max_repairs", c)


class ShippedTemplateTests(RepoCase):
    def template(self):
        return json.loads((swarmd.Path(swarmd.__file__).parent / "config.example.json").read_text())

    def test_the_template_raises_no_config_warnings(self):
        self.assertEqual(swarmd.config_warnings(self.template()), [])

    def test_the_template_passes_preflights_free_models_check(self):
        c = self.template()
        self.assertFalse(c.get("allow_paid"))
        self.assertEqual([m for m in c["models"] if not m.endswith(":free")], [])
        self.assertFalse(c.get("paid_models"))


class RepoAimingTests(RepoCase):
    def other_repo(self):
        other = self.root / "other"
        other.mkdir()
        _git(other, "init", "-q", "-b", "trunkless")
        _git(other, "config", "user.email", "t@example.invalid")
        _git(other, "config", "user.name", "T")
        (other / "a.txt").write_text("a\n")
        _git(other, "add", "-A")
        _git(other, "commit", "-qm", "a")
        return other.resolve()

    def test_a_repo_without_a_tuned_file_is_the_one_acted_on(self):
        other = self.other_repo()        # no swarm/configs/<slug>.json: the default serves it
        c = swarmd._setup(NS(repo=str(other)))
        self.assertEqual(c["repo"], str(other))
        self.assertEqual(c["base_branch"], "trunkless")
        self.assertEqual(swarmd.STATE.name, swarmd.repo_slug(other))

    def test_stop_with_repo_signals_that_repositorys_daemon_only(self):
        other = self.other_repo()
        for repo, pid in ((self.repo, 111), (other, 222)):
            state = self.root / "state" / swarmd.repo_slug(repo)
            state.mkdir(parents=True, exist_ok=True)
            (state / "daemon.pid").write_text(json.dumps({"pid": pid, "started": 0}))
        sent = []
        with patch.object(swarmd, "daemon_note",
                          lambda: json.loads((swarmd.STATE / "daemon.pid").read_text())), \
                patch.object(swarmd.os, "kill", lambda pid, sig: sent.append((pid, sig))):
            self.run_cli_args(NS(repo=str(other), drain=True))
        self.assertEqual(sent, [(222, signal.SIGUSR1)])

    def run_cli_args(self, ns):
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):
            swarmd.cmd_stop(ns)

    def test_the_cli_accepts_stop_repo(self):
        args = swarmd.cli.build_parser(swarmd._cli_handlers()).parse_args(
            ["stop", "--repo", "/x", "--drain"])
        self.assertEqual((args.repo, args.drain), ("/x", True))


if __name__ == "__main__":
    import unittest
    unittest.main()
