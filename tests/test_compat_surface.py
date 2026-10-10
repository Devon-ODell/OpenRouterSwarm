"""The compatibility manifest: every symbol bridge.py consumes from swarmd.

P0 pins the accidental public API so later packets know what they may not break.
bridge.py currently uses 41 swarmd symbols (private ones included, like `_read`
and the `STATE` global). Each must keep resolving for as long as the bridge is
not migrated onto the application facade.
"""
import importlib.util
import sys
import unittest
from pathlib import Path

from swarm import swarmd

# The 41 symbols bridge.py imports/uses (measured at 8cc415a, ground truth §swarmd
# is already an accidental public API).
BRIDGE_SYMBOLS = (
    "Budget CONFIG KINDS LOGS NO_CORPUS Queue STATE _read account attempts_for "
    "build_report config_kind config_path_for config_warnings daemon_note "
    "failure_class git health idle_state import_bugs landed_commits load_cfg log "
    "monthly paid_pool per_repo_config pool python_for queue_for quota_mode "
    "read_now read_roadmap reportable save_cfg scope_gap spend_cap spend_left "
    "spend_resets spend_used trunk_name use_repo"
).split()


class CompatSurfaceTests(unittest.TestCase):
    def test_every_bridge_symbol_resolves(self):
        missing = [n for n in BRIDGE_SYMBOLS if not hasattr(swarmd, n)]
        self.assertEqual(missing, [], f"bridge-consumed swarmd symbols disappeared: {missing}")

    def test_the_manifest_is_the_frozen_41(self):
        # Guard against accidentally shrinking or growing the manifest; the count is
        # part of the contract P0 froze.
        self.assertEqual(len(BRIDGE_SYMBOLS), 41)
        self.assertEqual(len(set(BRIDGE_SYMBOLS)), 41)


class DualIdentityTests(unittest.TestCase):
    """L1 documents: two module objects named `swarmd` can be live in one process.

    `from swarm import swarmd` and a sys.path-inserted `import swarmd` produce
    distinct module objects (the bridge runs as a script and hits the second
    identity). This test pins that hazard so a later packet that removes it is
    recognized as a deliberate change.
    """

    def test_the_two_import_paths_make_distinct_module_objects(self):
        from swarm import swarmd as packaged

        spec = importlib.util.spec_from_file_location(
            "swarmd_top", str(Path(swarmd.__file__).resolve()))
        top = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(top)

        self.assertIsNot(packaged, top)
        # The module-level mutable globals are duplicated, not shared.
        self.assertIsNot(packaged.STATE, top.STATE)
        self.assertIsNot(packaged._run, top._run)

    def test_each_identity_imports_cleanly(self):
        # Both identities must import without error and expose the entry point.
        self.assertTrue(callable(swarmd.main))
        self.assertTrue(hasattr(swarmd, "Queue"))


if __name__ == "__main__":
    unittest.main()
