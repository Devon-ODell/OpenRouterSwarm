"""Provisioning: `swarm init` writes a tuned config whose `prov` hooks turn a bare
git worktree (missing node_modules/, .venv/, nested repo content) into a green gate,
and the harness runs those hooks before the gate instead of failing every task on a
red baseline.

Regression: the dj-bot swarm spent a whole shift red-baselined because its test
command needed `spike/node_modules/@strudel/core` and `.venv/bin/python`, neither of
which git tracks. Every task was rejected with "Cannot find package '@strudel/core'"
before a model request. These tests pin the fix: probe the surroundings, build the
prov hooks, verify a bare worktree reaches green once provisioned, and that a config
with bad prov hooks fails loudly (not silently mid-shift).
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from swarm import swarmd


class ProvisioningTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.root = Path(d.name)
        self.configs = self.root / "configs"
        self.configs.mkdir()
        (self.root / "state").mkdir()
        (self.root / "logs").mkdir()
        self.default = self.root / "config.json"
        repo = self.root / "repo"
        repo.mkdir()
        self.repo = repo

        def git(*args):
            subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
        git("init", "-b", "main")
        git("config", "user.email", "test@example.invalid")
        git("config", "user.name", "Test")
        # A gitignored dependency the gate needs, exactly like dj-bot's spike/node_modules.
        (repo / ".gitignore").write_text("node_modules/\n.venv/\n")
        (repo / "app.txt").write_text("baseline\n")
        git("add", ".")
        git("commit", "-m", "baseline")
        (repo / "node_modules").mkdir()          # present locally, not in git
        (repo / "node_modules" / "dep.txt").write_text("dep\n")
        (repo / "requirements.txt").write_text("requests==2.31.0\n")

        for name, value in (("HERE", self.root), ("CONFIG", self.default),
                            ("STATE", self.root / "state"), ("LOGS", self.root / "logs"),
                            ("EXAMPLE", self.root / "config.example.json")):
            p = patch.object(swarmd, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.dict(os.environ, {}, clear=False)
        p.start()
        self.addCleanup(p.stop)
        os.environ.pop("FLINT_SWARM_CONFIG", None)

    # ------------------------------------------------------------- helpers

    def make_nested(self, name="nested"):
        n = self.repo / name
        n.mkdir()

        def git(*args):
            subprocess.run(["git", "-C", str(n), *args], check=True, capture_output=True)
        git("init", "-b", "main")
        git("config", "user.email", "test@example.invalid")
        git("config", "user.name", "Test")
        (n / "lib.txt").write_text("nested lib\n")
        git("add", ".")
        git("commit", "-m", "baseline")
        return n

    def write_config(self, path=None, **kw):
        c = {"repo": str(self.repo), "base_branch": "main", "test_cmd": "true",
             "models": ["a:free"], "python": sys.executable, "workers": 1}
        path = path or self.default
        path.write_text(json.dumps({**c, **kw}, indent=2) + "\n")
        return {**c, **kw}

    def bare_worktree(self):
        """A fresh `git worktree` of the repo, exactly what the harness builds: no
        node_modules/, no .venv/, no nested content."""
        wt = self.root / "wt"
        subprocess.run(["git", "-C", str(self.repo), "worktree", "add", "--detach",
                        str(wt), "main"], check=True, capture_output=True)
        self.addCleanup(subprocess.run, ["git", "-C", str(self.repo), "worktree",
                        "remove", "--force", str(wt)], check=True, capture_output=True)
        return wt

    # ------------------------------------------------------------ probe

    def test_probe_finds_nested_repos_and_ignored_runtime_deps(self):
        self.make_nested("strudel-src")
        s = swarmd.probe_surroundings(self.repo)
        self.assertIn("strudel-src", s["nested"])
        self.assertTrue(s["node_modules"], "node_modules present locally but must be reported")
        self.assertTrue(s["runtime"].get("requirements.txt"))
        self.assertIn("node_modules/", s["gitignore"])

    def test_probe_reports_nested_even_without_runtime_markers(self):
        self.make_nested("vendor")
        s = swarmd.probe_surroundings(self.repo)
        self.assertIn("vendor", s["nested"])

    # --------------------------------------------------------- prov block

    def test_build_prov_block_installs_python_deps_when_gate_does_not(self):
        s = swarmd.probe_surroundings(self.repo)
        prov = swarmd.build_prov_block(s, "pytest")
        self.assertTrue(any("requirements.txt" in h for h in prov["hooks"]),
                        f"expected a pip hook in {prov}")
        self.assertTrue(any("python3 -m venv" in h for h in prov["hooks"]))
        self.assertFalse(any("npm" in h for h in prov["hooks"]),
                         "no package.json marker: npm hook would be noise")

    def test_build_prov_block_skips_marker_when_gate_already_installs(self):
        s = swarmd.probe_surroundings(self.repo)
        # No package.json here, so npm is not a marker; what must hold is that "npm ci"
        # in the gate does NOT suppress the python-requirements hook (python != node).
        prov = swarmd.build_prov_block(s, "npm ci --silent && npm run check")
        self.assertTrue(any("requirements.txt" in h for h in prov["hooks"]),
                        "pip still needed: npm ci does not install python deps")
        # And with npm ci + a package.json, the npm marker must NOT be double-added.
        (self.repo / "package.json").write_text("{}\n")
        s2 = swarmd.probe_surroundings(self.repo)
        prov2 = swarmd.build_prov_block(s2, "npm ci --silent && npm run check")
        self.assertFalse(any("npm" in h for h in prov2["hooks"]),
                         "gate already runs npm ci; prov must not re-install npm deps")

    def test_unrelated_pip_install_does_not_suppress_node_provisioning(self):
        (self.repo / "package.json").write_text("{}\n")
        s = swarmd.probe_surroundings(self.repo)
        prov = swarmd.build_prov_block(s, "pip install -e . && pytest")
        self.assertTrue(any("npm ci" in h for h in prov["hooks"]), prov)

    def test_build_prov_block_wraps_subdir_marker_with_cd(self):
        """dj-bot shape: package.json one level down (spike/), so npm ci must run inside
        that subdir or a bare worktree has no manifest to install."""
        (self.repo / "spike").mkdir()
        (self.repo / "spike" / "package.json").write_text("{}\n")
        s = swarmd.probe_surroundings(self.repo)
        prov = swarmd.build_prov_block(s, "cd spike && npm run check")
        self.assertTrue(any("cd spike && npm ci" in h for h in prov["hooks"]),
                        f"expected the npm recipe wrapped in cd spike, got {prov}")

    def test_build_prov_block_does_not_treat_venv_reference_as_install(self):
        """dj-bot's gate runs `.venv/bin/python -m pytest` — that is a consumer. A skip
        rule that drops the venv recipe because the gate *mentions* .venv would recreate
        the exact red-baseline failure. Only an actual `python -m venv` in the gate skips."""
        s = swarmd.probe_surroundings(self.repo)
        gate = "cd spike && npm run check && cd .. && .venv/bin/python -m pytest -q tests/"
        prov = swarmd.build_prov_block(s, gate)
        self.assertTrue(any("python3 -m venv" in h for h in prov["hooks"]),
                        f"venv is consumed by the gate, not created by it; prov must add it: {prov}")

    def test_build_prov_block_skips_venv_when_gate_creates_it(self):
        s = swarmd.probe_surroundings(self.repo)
        prov = swarmd.build_prov_block(s, "python3 -m venv .venv && .venv/bin/python -m pytest")
        self.assertFalse(any("python3 -m venv" in h for h in prov["hooks"]),
                         "gate already creates the venv; prov must not duplicate it")

    def test_build_prov_block_adds_nested_repo_bootstrap(self):
        self.make_nested("strudel-src")
        s = swarmd.probe_surroundings(self.repo)
        prov = swarmd.build_prov_block(s, "pytest")
        self.assertTrue(any("strudel-src" in h for h in prov["hooks"]),
                        f"expected a nested-repo bootstrap hook in {prov}")

    def test_language_specific_recipes_are_valid_for_go_and_modern_python(self):
        cases = {
            "go.mod": "go mod download",
            "pyproject.toml": "pip install -q -e .",
            "Pipfile": "pipenv install --dev",
        }
        for marker, expected in cases.items():
            with self.subTest(marker=marker):
                runtime = {name: name == marker for name, _ in swarmd.RUNTIME_MARKERS}
                prov = swarmd.build_prov_block(
                    {"runtime": runtime, "subdirs": {}, "nested": []}, "pytest")
                self.assertEqual(len(prov["hooks"]), 1, prov)
                self.assertIn(expected, prov["hooks"][0])
                self.assertNotIn("requirements.txt", prov["hooks"][0])

    def test_prov_block_normalises_forms(self):
        self.assertEqual(swarmd.prov_block({"prov": {"hooks": ["a"], "env": {"K": "v"},
                                                     "timeout": 42}}),
                         {"hooks": ["a"], "env": {"K": "v"}, "timeout": 42})
        self.assertEqual(swarmd.prov_block({"prov": ["a", "b"]}),
                         {"hooks": ["a", "b"], "env": {}, "timeout": 1200})
        self.assertEqual(swarmd.prov_block({"prov": "not-a-dict"}),
                         {"hooks": [], "env": {}, "timeout": 1200})
        self.assertEqual(swarmd.prov_block({}), {"hooks": [], "env": {}, "timeout": 1200})

    # ----------------------------------------------------- run_prov_hooks

    def test_run_prov_hooks_makes_a_bare_worktree_green(self):
        wt = self.bare_worktree()
        # The gate insists the dep exists. A worktree starts without node_modules/; the
        # prov hook installs it (here: creates the file the gate wants).
        c = self.write_config(prov={"hooks": [
            "mkdir -p node_modules && printf dep > node_modules/dep.txt"
        ], "env": {}, "timeout": 120})
        ok, out = swarmd.run_prov_hooks(c, wt, "view")
        self.assertTrue(ok, out)
        self.assertTrue((wt / "node_modules" / "dep.txt").is_file(),
                        "prov hook must have materialised the dep inside the worktree")
        self.assertEqual((wt / "node_modules" / "dep.txt").read_text().strip(), "dep")

    def test_run_prov_hooks_propagates_env(self):
        wt = self.bare_worktree()
        c = self.write_config(prov={"hooks": ["printf %s > prov-env.txt \"$PROV_TEST\""],
                                    "env": {"PROV_TEST": "hello"}, "timeout": 120})
        ok, out = swarmd.run_prov_hooks(c, wt, "view")
        self.assertTrue(ok, out)
        self.assertEqual((wt / "prov-env.txt").read_text().strip(), "hello")

    def test_run_prov_hooks_failing_hook_returns_false_with_output(self):
        wt = self.bare_worktree()
        c = self.write_config(prov={"hooks": ["echo boom && exit 7"], "env": {},
                                    "timeout": 120})
        ok, out = swarmd.run_prov_hooks(c, wt, "view")
        self.assertFalse(ok)
        self.assertIn("exit 7", out)
        self.assertIn("boom", out)

    def test_run_prov_hooks_none_returns_true(self):
        c = self.write_config()
        ok, out = swarmd.run_prov_hooks(c, self.bare_worktree(), "view")
        self.assertTrue(ok)
        self.assertEqual(out, "")

    def test_nested_bootstrap_clones_content_and_fails_loudly(self):
        self.make_nested("vendor")
        s = swarmd.probe_surroundings(self.repo)
        nested_hooks = [h for h in swarmd.build_prov_block(s, "true")["hooks"]
                        if h.startswith("git clone")]
        c = self.write_config(prov={"hooks": nested_hooks, "env": {}, "timeout": 120})
        wt = self.bare_worktree()
        ok, out = swarmd.run_prov_hooks(c, wt, "attempt")
        self.assertTrue(ok, out)
        self.assertEqual((wt / "vendor" / "lib.txt").read_text(), "nested lib\n")

        bad = self.write_config(prov={"hooks": ["git clone --quiet /missing/repo ./vendor"],
                                      "env": {}, "timeout": 120})
        ok, out = swarmd.run_prov_hooks(bad, wt, "attempt")
        self.assertFalse(ok)
        self.assertIn("exit", out)

    # ------------------------------------------------ run_gate_with_prov

    def test_gate_with_prov_provisions_then_gates(self):
        wt = self.bare_worktree()
        # Gate that fails unless the dep is installed.
        dep_gate = ("test -f node_modules/dep.txt && test -f .venv/bin/python")
        c = self.write_config(test_cmd=dep_gate, prov={"hooks": [
            "mkdir -p node_modules && printf dep > node_modules/dep.txt",
            "mkdir -p .venv/bin && printf py > .venv/bin/python && chmod +x .venv/bin/python"
        ], "env": {}, "timeout": 120})
        ok, out = swarmd.run_gate_with_prov(c, wt, "attempt")
        self.assertTrue(ok, out)
        self.assertTrue((wt / "node_modules" / "dep.txt").is_file())
        self.assertTrue((wt / ".venv" / "bin" / "python").is_file())

    def test_gate_with_prov_stops_on_failed_provisioning(self):
        wt = self.bare_worktree()
        c = self.write_config(test_cmd="true", prov={"hooks": ["exit 3"], "env": {},
                                                     "timeout": 120})
        ok, out = swarmd.run_gate_with_prov(c, wt, "attempt")
        self.assertFalse(ok)
        self.assertIn("provisioning failed", out)

    def test_worker_provisions_once_before_baseline_and_reuses_dependencies(self):
        c = self.write_config(test_cmd="true", validation_commands=[])
        worker = swarmd.Worker(0, c, swarmd.Queue(), Mock(), Mock())
        worker.wt = self.repo
        worker.task = {}
        worker.evidence = Mock()
        worker.evidence.path = self.root
        worker.gate_count = 0
        worker.worktree_provisioned = False
        with patch.object(swarmd, "run_prov_hooks", return_value=(True, "installed")) as prov, \
             patch.object(swarmd, "run_gate", return_value=(True, "green")) as gate:
            self.assertTrue(worker.gate("baseline")[0])
            self.assertTrue(worker.gate("candidate-0")[0])
            self.assertTrue(worker.gate("candidate-1")[0])
        prov.assert_called_once_with(c, self.repo, "attempt", log_on_ok=True)
        self.assertEqual(gate.call_count, 3)

    def test_sync_trunk_uses_provisioned_gate(self):
        c = self.write_config(test_cmd="true", trunk="swarm/trunk")
        swarmd._sync_failed.clear()

        def fake_git(args, cwd=None, check=False):
            if args[:2] == ["merge-base", "--is-ancestor"]:
                return 1, ""
            if args[:2] == ["rev-parse", "main"]:
                return 0, "base-sha"
            if args[:2] == ["rev-parse", "swarm/trunk"]:
                return 0, "old-sha"
            if args[:2] == ["rev-parse", "HEAD"]:
                return 0, "new-sha"
            return 0, ""

        with patch.object(swarmd, "ensure_trunk"), \
             patch.object(swarmd, "git", side_effect=fake_git), \
             patch.object(swarmd, "checked_out_at", return_value=None), \
             patch.object(swarmd, "wt_root", return_value=self.root / "sync-wt"), \
             patch.object(swarmd, "run_gate_with_prov", return_value=(True, "green")) as gate:
            self.assertEqual(swarmd.sync_trunk(c), "synced")
        gate.assert_called_once()
        self.assertEqual(gate.call_args.args[0], c)
        self.assertEqual(gate.call_args.args[2], "sync")

    # ------------------------------------------------------- command init

    def make_args(self, repo=None, check=False):
        class A:
            pass
        a = A()
        a.repo = str(repo) if repo else None
        a.check = check
        return a

    def test_cmd_init_check_prints_surroundings_without_writing(self):
        self.make_nested("strudel-src")
        tuned = self.configs / f"{swarmd.repo_slug(self.repo)}.json"
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            swarmd.cmd_init(self.make_args(self.repo, check=True))
        out = buf.getvalue()
        self.assertIn("strudel-src", out)
        self.assertIn("nested repos", out)
        self.assertFalse(tuned.exists(), "--check must not write a config")

    def test_cmd_init_writes_tuned_config_with_prov_hooks(self):
        self.make_nested("strudel-src")
        tuned = self.configs / f"{swarmd.repo_slug(self.repo)}.json"
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            swarmd.cmd_init(self.make_args(self.repo))
        self.assertTrue(tuned.is_file(), f"init should have written {tuned}")
        c = json.loads(tuned.read_text())
        self.assertEqual(c["repo"], str(self.repo.resolve()))
        self.assertEqual(c["base_branch"], "main")
        self.assertTrue(c["prov"]["hooks"], "expected at least one prov hook")
        self.assertTrue(any("strudel-src" in h for h in c["prov"]["hooks"]))
        self.assertTrue(any("requirements.txt" in h for h in c["prov"]["hooks"]))
        self.assertIn("wrote", buf.getvalue())

    def test_cmd_init_is_idempotent(self):
        tuned = self.configs / f"{swarmd.repo_slug(self.repo)}.json"
        import io, contextlib
        for _ in range(2):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                swarmd.cmd_init(self.make_args(self.repo))
        c = json.loads(tuned.read_text())
        self.assertEqual(len(c["prov"]["hooks"]), len(json.loads(tuned.read_text())["prov"]["hooks"]))

    def test_cmd_init_fails_on_non_repo(self):
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), self.assertRaises(SystemExit):
            swarmd.cmd_init(self.make_args(self.root / "not-a-repo", check=True))


if __name__ == "__main__":
    unittest.main()
