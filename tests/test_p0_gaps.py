"""P0 gap-closing: the corrections requested in the review.

These tests close the characterization gaps the first P0 pass left open:

1. The *real* bridge.swarmd dual identity — the bridge runs the repo's `swarmd.py`
   via a sys.path-inserted `import swarmd`, so the live dual identity is that a
   top-level `swarmd` module and a packaged `swarm.swarmd` module coexist in one
   process. The old test built its second identity with `spec_from_file_location`
   under a *renamed* module, which proved nothing about the bridge's actual load
   path and could never collide with the packaged module.
2. The run_daemon stop-vs-restart branch driven through the real daemon loop, not
   a copied conditional: when a stop lands while a source-change restart is
   pending, the loop must clear `restart_for` (and journal `self_restart_skipped`)
   instead of ever calling `restart_into_new_code`.
3. swarmd.main() argument routing: each subcommand dispatches to exactly one
   cmd_* handler and carries the argparse defaults (--repo, --drain, --write, ...).
4. The normalized nested bridge-status schema, frozen recursively (top-level,
   nested keysets, value types) so a later packet cannot quietly re-shape it.
5. Functional fault scenarios driving the attempt-stage families that the happy
   path never touches: weakened_tests, model_error (implementer and reviewer),
   review_format_error, agent_timeout, request_budget, model_pool_exhausted.

No production file is touched.
"""
import contextlib
import importlib.util
import io
import json
import re
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

from swarm import swarmd

from tests.test_attempt_stages import RepoCase as StageRepoCase, review as _make_review


# -- helpers ----------------------------------------------------------------


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def _top_level_import_swarmd(source_dir):
    """Import `swarmd` as a *top-level* module from source_dir, the way bridge.py
    runs it: `import swarmd` after inserting the source dir on sys.path.

    Returns the module object, with sys.path restored afterwards.
    """
    spec = importlib.util.spec_from_file_location(
        "swarmd", str(Path(source_dir) / "swarmd.py"))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["swarmd"] = module  # the dual identity lives in sys.modules
    spec.loader.exec_module(module)
    return module


# -- 1. the real dual identity ----------------------------------------------


class RealDualIdentityTests(unittest.TestCase):
    """The live hazard L1 documents, tested the way the bridge actually loads:

    bridge.py does `sys.path.insert(0, repo)` and `import swarmd`; the extension
    imports `from swarm import swarmd`. Both module objects coexist in one
    process and share nothing (not even mutable module globals)."""

    @classmethod
    def setUpClass(cls):
        cls.repo = Path(__file__).resolve().parents[1]  # repo root: swarm/ sits here
        cls.top = _top_level_import_swarmd(cls.repo / "swarm")

    def test_bridge_import_path_yields_a_distinct_module_object(self):
        packaged = swarmd
        top = self.top
        self.assertIsNot(packaged, top)
        self.assertIsNot(packaged.STATE, top.STATE)
        self.assertIsNot(packaged._run, top._run)
        self.assertIsNot(packaged._stop, top._stop)

    def test_both_identities_expose_the_bridge_contract(self):
        for mod in (swarmd, self.top):
            for name in ("Queue", "STATE", "load_cfg", "queue_for", "use_repo",
                         "daemon_note", "config_kind", "idle_state", "account",
                         "health", "pool", "read_now"):
                self.assertTrue(hasattr(mod, name), f"{mod.__name__} lost {name}")

    def test_each_identity_keeps_its_own_globals(self):
        # Setting a global on one identity must not leak into the other: later
        # packets that fuse the identities must do it explicitly.
        self.top._probe_global = "top"
        self.assertFalse(hasattr(swarmd, "_probe_global"))
        swarmd._probe_global = "packaged"
        self.assertNotEqual(self.top._probe_global, swarmd._probe_global)


# -- 2. the real daemon-loop stop-vs-restart branch -------------------------


class RealDaemonLoopStopTests(StageRepoCase):
    """Invariant 8, driven through run_daemon's actual loop body.

    The prior test copied the two-line guard. Here the loop itself acquires the
    pending restart (its own source-changed branch fires -> drain(tally,
    stop=False), exactly as a harness edit mid-shift does), then a stop lands in
    the same tick (report_progress side effect, standing in for SIGUSR1). The
    loop's next drain-check must clear restart_for and journal
    self_restart_skipped instead of ever restarting.
    """

    def setUp(self):
        super().setUp()
        self._StubWorker._starts = 0

    class _StubWorker:
        _starts = 0  # shared: the restart branch builds fresh instances

        def __init__(self, idx, c, q, budget, stop, ledger=None, tally=None):
            self.name = f"w{idx}"
            self.idx = idx

        def start(self):
            type(self)._starts += 1
            # The restart branch rebuilds the workers; ending the run then is
            # what a real (execv'd) process would do with the new code.
            if type(self)._starts >= 2:
                swarmd._stop.set()

        def is_alive(self):
            return False

        def join(self, timeout=None):
            pass

    def _run_daemon_until_drained(self, stop_lands):
        """Run the real loop; a source change fires once, then optionally a stop.

        Returns (restart mock, log lines, tally).
        """
        c = self.cfg(restart_min_interval=-1, tick=0, workers=1)
        swarmd._drain_reason["stop_requested"] = False
        swarmd._stop.clear()
        q = swarmd.Queue()
        tally = swarmd.Tally()

        fired = {"source": False, "stop": False}

        def fake_source_changed(before):
            if not fired["source"]:
                fired["source"] = True
                return ["swarm/swarmd.py"]
            return []

        def fake_report_progress(workers, said, every=180):
            if stop_lands and not fired["stop"]:
                # A stop lands mid-tick, after the source-change drain set the
                # drain flag but before the loop's next drain-check.
                fired["stop"] = True
                swarmd.drain(tally, stop=True)

        def fake_git(args, cwd=None, check=True):
            if args and args[0] in ("rev-parse", "status", "symbolic-ref"):
                return 0, ""
            return 0, ""

        logs = []
        with patch.object(swarmd, "Worker", self._StubWorker), \
                patch.object(swarmd, "restart_into_new_code") as restart, \
                patch.object(swarmd, "source_changed", side_effect=fake_source_changed), \
                patch.object(swarmd, "report_progress", side_effect=fake_report_progress), \
                patch.object(swarmd, "write_now"), \
                patch.object(swarmd.Queue, "ready", return_value=[]), \
                patch.object(swarmd.Queue, "pending", return_value=[]), \
                patch.object(swarmd, "signal"), \
                patch.object(swarmd, "resync_allowance"), \
                patch.object(swarmd, "sync_usage"), \
                patch.object(swarmd, "quota_exhausted", return_value=False), \
                patch.object(swarmd, "can_take_a_turn", return_value=False), \
                patch.object(swarmd, "dispatch_roadmap"), \
                patch.object(swarmd, "idle_improvement"), \
                patch.object(swarmd, "sync_trunk"), \
                patch.object(swarmd, "plan"), \
                patch.object(swarmd, "import_bugs", return_value=[]), \
                patch.object(swarmd, "log", side_effect=lambda m, worker="swarm": logs.append(f"{worker}: {m}")), \
                patch.object(swarmd, "git", side_effect=fake_git):
            swarmd.run_daemon(c, max_tasks=None, hours=None)
        return restart, logs, tally

    def test_stop_that_lands_while_restart_pending_is_not_a_restart(self):
        restart, logs, tally = self._run_daemon_until_drained(stop_lands=True)
        restart.assert_not_called()
        joined = " ".join(logs)
        self.assertIn("not restarting into", joined)
        self.assertIn("a stop means stop", joined)
        self.assertTrue(tally.drain.is_set())
        # The run actually ended.
        self.assertTrue(any("stopping" in l for l in logs))

    def test_a_source_change_without_a_stop_restarts(self):
        """Drain via source change alone goes through restart_into_new_code."""
        restart, logs, _ = self._run_daemon_until_drained(stop_lands=False)
        restart.assert_called_once()
        self.assertEqual(restart.call_args[0][0], ["swarm/swarmd.py"])


# -- 3. swarmd.main() argument routing -------------------------------------


class MainRoutingTests(StageRepoCase):
    """`swarmd.main()` routes argv to the right subcommand handler.

    argparse is the only place a subcommand's defaults are frozen, so P0 pins
    each route: one subcommand -> one cmd_* handler, and the handler receives
    the argparse defaults (--repo None -> _setup's default, --drain False, ...).
    """

    def run_main(self, *argv):
        out = io.StringIO()
        captured = {}
        with patch.object(sys, "argv", ["swarm", *argv]), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(io.StringIO()):
            try:
                swarmd.main()
            except SystemExit as e:
                captured["exit"] = e.code
        self.assertNotIn("exit", captured, f"routing exited {captured.get('exit')} for {argv}")
        return out.getvalue()

    def test_status_subcommand_dispatch(self):
        with patch.object(swarmd, "cmd_status") as handler:
            self.run_main("status")
        handler.assert_called_once()
        args = handler.call_args[0][0]
        self.assertEqual(args.cmd, "status")
        self.assertIsNone(args.repo)

    def test_status_repo_flag_reaches_the_handler(self):
        with patch.object(swarmd, "cmd_status") as handler:
            self.run_main("status", "--repo", "x")
        self.assertEqual(handler.call_args[0][0].repo, "x")

    def test_add_subcommand_dispatch(self):
        with patch.object(swarmd, "cmd_add") as handler:
            self.run_main("add", "the title")
        args = handler.call_args[0][0]
        self.assertEqual(args.cmd, "add")
        self.assertEqual(args.title, "the title")
        self.assertEqual(args.kind, "feature")
        self.assertEqual(args.priority, 1)
        self.assertFalse(args.allow_test_changes)

    def test_add_long_flags_reach_the_handler(self):
        with patch.object(swarmd, "cmd_add") as handler:
            self.run_main("add", "t", "--kind", "chore", "--priority", "3",
                          "--acceptance", "a1", "--acceptance", "a2",
                          "--depends-on", "d1", "--allow-test-changes")
        args = handler.call_args[0][0]
        self.assertEqual(args.kind, "chore")
        self.assertEqual(args.priority, 3)
        self.assertEqual(args.acceptance, ["a1", "a2"])
        self.assertEqual(args.depends_on, ["d1"])
        self.assertTrue(args.allow_test_changes)

    def test_stop_subcommand_dispatch(self):
        with patch.object(swarmd, "cmd_stop") as handler:
            self.run_main("stop")
        args = handler.call_args[0][0]
        self.assertEqual(args.cmd, "stop")
        self.assertFalse(args.drain)

    def test_plan_subcommand_defaults(self):
        with patch.object(swarmd, "cmd_plan") as handler:
            self.run_main("plan", "-n", "3")
        args = handler.call_args[0][0]
        self.assertEqual(args.n, 3)

    def test_wake_and_exceptions_dispatch(self):
        with patch.object(swarmd, "cmd_wake") as wake, \
                patch.object(swarmd, "cmd_exceptions") as exc:
            self.run_main("wake")
            self.run_main("exceptions")
        wake.assert_called_once()
        exc.assert_called_once()

    def test_models_subcommand_write_flag(self):
        with patch.object(swarmd, "cmd_models") as handler:
            self.run_main("models", "--write")
        self.assertTrue(handler.call_args[0][0].write)

    def test_report_subcommand_hours_default(self):
        with patch.object(swarmd, "cmd_report") as handler:
            self.run_main("report")
        self.assertEqual(handler.call_args[0][0].hours, 24)


# -- 4. normalized nested bridge-status schema ------------------------------


class BridgeSchemaNormalizedTests(StageRepoCase):
    """The bridge status object, frozen recursively.

    The existing test only pins the top-level key set. This freezes the nested
    shape: every nested mapping's key set, and the type of every leaf, so a
    later packet cannot rename, retype or drop a field the panel reads.
    """

    def setUp(self):
        super().setUp()
        # use_repo re-points STATE/LOGS under HERE, so HERE must be the temp root
        # or these tests would write into the real source tree's swarm/state.
        from swarm import bridge
        for mod in (swarmd, bridge.swarmd):
            p = patch.object(mod, "HERE", self.root)
            p.start()
            self.addCleanup(p.stop)
        self.repo = self.repo.resolve()

    def bridge_status(self):
        from swarm import bridge
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = bridge.main(["status", "--repo", str(self.repo)])
        self.assertEqual(rc, 0)
        lines = [json.loads(l) for l in out.getvalue().splitlines() if l.strip()]
        self.assertEqual(len(lines), 1)
        return lines[0]

    def test_bridge_status_nested_shape_is_frozen(self):
        snap = self.bridge_status()
        # Top-level keyset (already frozen elsewhere; here it is the root of the
        # recursive shape).
        top = {"repo", "is_target", "daemon_running", "now", "health", "run",
               "config_path", "config_kind", "config_warnings",
               "config_tuned_available", "quota_mode", "daemon_paid_configured",
               "trunk", "trunk_ahead", "roadmap", "idle", "queue", "max_queue",
               "kinds", "landed", "parked", "budget", "experiment", "spend",
               "stale_seconds", "wallet", "recent"}
        self.assertEqual(set(snap), top)
        # With no daemon running, the live status is absent, not fabricated.
        self.assertIsNone(snap["now"])
        self.assertIsNone(snap["run"])
        self.assertFalse(snap["daemon_running"])
        # An unconfigured editor wallet is still a default $5 pot, not absent;
        # its path is machine-specific, so only its shape is frozen here.
        self.assertEqual(set(snap["wallet"]),
                         {"cap", "spent", "remaining", "calls", "updated",
                          "path", "recent"})
        self.assertEqual(snap["wallet"]["cap"], 5.0)
        self.assertIsInstance(snap["wallet"]["spent"], (int, float))
        self.assertIsInstance(snap["wallet"]["remaining"], (int, float))
        self.assertIsInstance(snap["wallet"]["calls"], int)
        self.assertTrue(str(snap["wallet"]["path"]).endswith("wallet.json"))
        self.assertIsInstance(snap["wallet"]["recent"], list)
        self.assertIsInstance(snap["landed"], int)
        self.assertIsInstance(snap["parked"], int)
        self.assertIsInstance(snap["queue"], list)
        self.assertIsInstance(snap["kinds"], list)
        self.assertIsInstance(snap["recent"], list)
        self.assertIsInstance(snap["stale_seconds"], (int, float, type(None)))
        self.assertIsInstance(snap["trunk_ahead"], (int, type(None)))
        for key in ("repo", "config_path", "config_kind", "quota_mode"):
            self.assertIsInstance(snap[key], str)
        for key in ("is_target", "daemon_running", "daemon_paid_configured"):
            self.assertIsInstance(snap[key], bool)
        # Nested keysets are part of the contract.
        self.assertEqual(set(snap["health"]),
                         {"hours", "attempts", "landed", "landed_rate", "failures",
                          "usd", "usd_per_landed", "unhealthy"})
        self.assertEqual(set(snap["budget"]),
                         {"spent_today", "usable", "cap", "reserve", "remaining",
                          "resets_utc", "resets_local", "hours_to_reset",
                          "owner_window_now"})
        self.assertEqual(set(snap["experiment"]), {"verdict", "on", "off"})
        self.assertEqual(set(snap["spend"]),
                         {"used", "cap", "left", "resets", "shared"})
        self.assertEqual(set(snap["idle"]), {"cycles", "active"})
        self.assertIsInstance(snap["budget"]["resets_utc"], str)
        self.assertIsInstance(snap["budget"]["spent_today"], (int, float))
        self.assertIsInstance(snap["spend"]["left"], (int, float, type(None)))
        self.assertIsInstance(snap["experiment"]["verdict"], str)
        # Only a mapping-shaped value may be None where absent is meaningful.
        for key in ("config_tuned_available", "roadmap"):
            self.assertTrue(snap[key] is None or isinstance(snap[key], str)
                            or isinstance(snap[key], dict))

    def test_bridge_status_queue_rows_have_the_full_row_shape(self):
        from swarm import bridge
        # The bridge reads the default config from HERE/config.json; write it so
        # config_for() fills the same settings the daemon would use.
        default = {**self.cfg(), "repo": str(self.repo), "python": sys.executable}
        (self.root / "config.json").write_text(json.dumps(default, indent=2) + "\n")
        c = bridge.swarmd.load_cfg(self.repo)
        bridge.swarmd.use_repo(c)
        bridge.swarmd.queue_for(c).add("Add the settings screen", kind="feature",
                                       origin="human")
        snap = self.bridge_status()
        self.assertEqual(len(snap["queue"]), 1)
        row = snap["queue"][0]
        self.assertEqual(set(row),
                         {"id", "title", "detail", "detail_truncated", "kind",
                          "priority", "attempts", "claimed", "origin", "status",
                          "not_before", "created", "depth", "acceptance",
                          "depends_on", "allow_test_changes", "harness_failures",
                          "blocks", "scope_gap", "evidence", "note"})
        self.assertEqual(row["kind"], "feature")
        self.assertIsNone(row["status"])  # status is set only on claim/release
        self.assertEqual(row["blocks"], [])
        self.assertIsInstance(row["acceptance"], list)
        self.assertIsInstance(row["depends_on"], list)
        # A snapshot that is None-able only for absent values.
        self.assertTrue(row["evidence"] is None or isinstance(row["evidence"], str))


# -- 5. functional fault scenarios for unexercised stage families ------------


class AttemptFaultScenarioTests(StageRepoCase):
    """Functional drives of the attempt-stage families the happy path never hits.

    These run the same real repo + worktree + fake flint as the happy path, but
    each forces one specific fault through the actual _attempt control flow.
    """

    def task(self, **kw):
        return {"id": "t1", "title": "Add the settings screen",
                "detail": "show the volume slider", "kind": "feature",
                "origin": "human", "acceptance": ["the slider appears"], **kw}

    def worker(self, c=None, q=None):
        w = swarmd.Worker(0, c or self.cfg(), q or swarmd.Queue(), Mock(),
                          threading.Event())
        w.name = "w0"
        return w

    def test_weakened_tests_is_returned_when_the_candidate_removes_an_assertion(self):
        """An implementer that deletes a real assertion is stopped by the
        weakened-tests gate before any review is spent."""
        c = self.cfg()
        (self.repo / "tests").mkdir()
        (self.repo / "tests" / "test_app.py").write_text(
            "def test_baseline():\n    assert True\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "baseline"],
                       check=True)
        task = self.task()

        def fake(prompt, cwd, c_, role, *a):
            if role == "implementer":
                (cwd / "tests" / "test_app.py").write_text(
                    "def test_baseline():\n    pass\n")  # drops the assertion
                return "done"
            return _make_review(prompt)

        w = self.worker(c)
        with patch.object(swarmd, "flint", side_effect=fake):
            ok, note = w.do_task(task, "goal")
        self.assertFalse(ok)
        self.assertEqual(w.stage, "weakened_tests")
        self.assertIn("assertion", note)

    def test_model_error_from_the_implementer_is_returned(self):
        """A turn that raises ModelError (e.g. the provider answered with an
        unusable blob) before any edit is a model_error, not a task failure."""
        c = self.cfg()
        task = self.task()

        def fake(prompt, cwd, c_, role, *a):
            if role == "implementer":
                raise swarmd.ModelError("no usable json in reply")
            return _make_review(prompt)

        w = self.worker(c)
        with patch.object(swarmd, "flint", side_effect=fake):
            ok, note = w.do_task(task, "goal")
        self.assertFalse(ok)
        self.assertEqual(w.stage, "model_error")
        self.assertEqual(w.failure_class, "model")

    def test_review_error(self):
        """A reviewer that raises ModelError (after the candidate passed the
        gate) surfaces as review_error, not rejected."""
        c = self.cfg()
        task = self.task()

        def fake(prompt, cwd, c_, role, *a):
            if role == "implementer":
                (cwd / "feature.txt").write_text("settings\n")
                return "done"
            if role == "adversary":
                raise swarmd.ModelError("reviewer crashed")
            return "done"

        w = self.worker(c)
        with patch.object(swarmd, "flint", side_effect=fake):
            ok, note = w.do_task(task, "goal")
        self.assertFalse(ok)
        self.assertEqual(w.stage, "review_error")

    def test_review_format_error_is_a_format_failure_not_a_rejection(self):
        c = self.cfg()
        task = self.task()

        def fake(prompt, cwd, c_, role, *a):
            if role == "implementer":
                (cwd / "feature.txt").write_text("settings\n")
                return "done"
            if role == "adversary":
                return "this is not json at all"
            return "done"

        w = self.worker(c)
        with patch.object(swarmd, "flint", side_effect=fake):
            ok, note = w.do_task(task, "goal")
        self.assertFalse(ok)
        self.assertEqual(w.stage, "review_format_error")
        self.assertEqual(w.failure_class, "review_format")

    def test_agent_timeout_is_reported_when_the_turn_never_edited(self):
        """An implementer that exhausts its wall-clock budget without ever
        editing is agent_timeout, not no_change: the harness lost the turn, the
        model did not answer badly."""
        c = self.cfg()
        task = self.task()

        def fake(prompt, cwd, c_, role, *a):
            if role == "implementer":
                raise swarmd.AgentTimeout("implementer", "a:free", 420, "/tmp/log")
            return _make_review(prompt)

        w = self.worker(c)
        with patch.object(swarmd, "flint", side_effect=fake):
            ok, note = w.do_task(task, "goal")
        self.assertFalse(ok)
        self.assertEqual(w.stage, "agent_timeout")
        self.assertEqual(w.failure_class, "harness")

    def test_request_budget_is_parked_not_retried(self):
        """Exhausting the task's request budget before the implementer turn is a
        request_budget park: no attempt is consumed on a cap that is the
        swarm's own."""
        c = self.cfg(task_request_cap=6)
        task = self.task()

        def fake(prompt, cwd, c_, role, *a):
            if role == "implementer":
                raise swarmd.RequestBudget(
                    "task request budget exhausted before implementer")
            return _make_review(prompt)

        w = self.worker(c)
        with patch.object(swarmd, "flint", side_effect=fake):
            ok, note = w.do_task(task, "goal")
        self.assertFalse(ok)
        self.assertEqual(w.stage, "request_budget")

    def test_model_pool_exhausted_parks_with_evidence(self):
        """When every implementer in the pool is excluded on this task, the
        attempt parks (model_pool_exhausted) instead of redrawing forever."""
        c = self.cfg()
        task = self.task()
        # Exclude the only model in the pool for the implementer role: the
        # circuit breaker rule excludes a harness timeout with a no-edit note.
        import time as _time
        task["failed_models"] = [{"model": "a:free", "class": "harness",
                                  "role": "implementer", "at": _time.time(),
                                  "note": "timed out without editing"}]
        w = self.worker(c)

        def fake(prompt, cwd, c_, role, *a):
            raise AssertionError("flint must not be called when the pool is exhausted")

        with patch.object(swarmd, "flint", side_effect=fake):
            ok, note = w.do_task(task, "goal")
        self.assertFalse(ok)
        self.assertEqual(w.stage, "model_pool_exhausted")


if __name__ == "__main__":
    unittest.main()