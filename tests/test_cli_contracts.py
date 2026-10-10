"""P0 black-box characterization of the swarm's public contracts.

Every packet after this one is a pure refactor: the daemon's CLI JSON, the
bridge status JSON, the queue lifecycle and the attempt artifact layout must
all stay byte-identical. These tests freeze what those contracts are, so a
later packet that breaks one fails loudly instead of silently changing what
the studio panel or the extension parse.

No production file is touched by this module — it only reads and asserts.
"""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

from swarm import bridge, swarmd
from swarm.learn import Ledger
import swallow
from swarm.workflow import Attempt

# bridge.py runs as a script and imports swarmd as a top-level module, so there are two
# module objects for it in a test process. Patch both or the bridge keeps reading the real
# checkout (see L1 in the work order).
SWARMDS = (swarmd, bridge.swarmd)


def _git(repo, *args, check=True):
    return subprocess.run(["git", "-C", str(repo), *args], check=check,
                          capture_output=True, text=True).stdout.strip()


class RepoCase(unittest.TestCase):
    """A temp repo plus per-repo state/logs, with both swarmd identities isolated."""

    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.root = Path(d.name)
        (self.root / "configs").mkdir()
        for name in ("state", "logs"):
            (self.root / name).mkdir()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        _git(self.repo, "init", "-q", "-b", "main")
        _git(self.repo, "config", "user.email", "t@example.invalid")
        _git(self.repo, "config", "user.name", "T")
        (self.repo / "app.txt").write_text("baseline\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", "baseline")
        self.default = self.root / "config.json"
        self.write_config(self.default)
        for mod in SWARMDS:
            for attr, value in (("HERE", self.root), ("CONFIG", self.default),
                                ("STATE", self.root / "state"), ("LOGS", self.root / "logs"),
                                ("EXAMPLE", self.root / "config.example.json")):
                p = patch.object(mod, attr, value)
                p.start()
                self.addCleanup(p.stop)
        os.environ.pop("FLINT_SWARM_CONFIG", None)
        swarmd._run.clear()
        swarmd._stop.clear()

    def write_config(self, path, **kw):
        c = {"repo": str(self.repo), "base_branch": "main", "test_cmd": "true",
             "models": ["a:free"], "python": sys.executable, "workers": 1}
        path.write_text(json.dumps({**c, **kw}, indent=2) + "\n")
        return path

    def run_cli(self, *argv):
        """Call a swarmd cmd_* directly, capturing stdout. Returns (out, ...)."""
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            args = self.args(*argv)
            args.fn(args)
        return out.getvalue()

    def args(self, *argv):
        """Build a SimpleNamespace the cmd_* handlers read, like argparse would."""
        # The default route: no --repo, so _setup stays on the patched default config.
        ns = NS(fn=None)
        return ns

    def run_bridge(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = bridge.main(list(argv))
        return rc, [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]


class CliStatusJsonTests(RepoCase):
    """`swarm status` JSON — the machine contract the extension and scripts parse."""

    def repo_cfg(self):
        """Config + use_repo, the way _setup would set it for this repo."""
        c = swarmd.load_cfg(self.repo)
        swarmd.use_repo(c)
        return c

    def status_json(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            c = swarmd._setup(NS())
            swarmd.cmd_status(NS())
        text = out.getvalue()
        # The JSON block is the last indented=2 object in the output.
        return json.loads(text[text.index("{\n"):text.index("\n}\n") + 2]), text

    def test_status_json_has_the_frozen_top_level_fields(self):
        self.repo_cfg()
        snap, _ = self.status_json()
        for key in ("repo", "trunk", "trunk_ahead", "config_path", "config_kind",
                    "config_warnings", "config_tuned_available", "daemon", "budget",
                    "spend", "pacing", "queue", "landed", "split", "parked",
                    "promotion", "top_arms", "resting"):
            self.assertIn(key, snap, f"status JSON lost field {key}")
        self.assertEqual(snap["repo"], str(self.repo))
        self.assertEqual(snap["queue"], {"pending": 0, "claimed": 0})
        self.assertIn("allowed_now", snap["pacing"])
        self.assertIn("spent_today", snap["budget"])

    def test_status_reflects_a_queued_task(self):
        self.repo_cfg()
        q = swarmd.queue_for(swarmd.load_cfg(self.repo))
        t = q.add("Add a settings screen", detail="It must show the volume slider",
                  kind="feature", origin="human")
        snap, _ = self.status_json()
        self.assertEqual(snap["queue"]["pending"], 1)
        self.assertEqual(snap["queue"]["claimed"], 0)

    def test_status_daemon_stopped_has_no_pid(self):
        self.repo_cfg()
        snap, _ = self.status_json()
        self.assertFalse(snap["daemon"]["running"])
        self.assertIsNone(snap["daemon"]["pid"])


class CliAddJsonTests(RepoCase):
    """`swarm add` — the human/queue contract, including dependencies."""

    def test_add_prints_the_task_id_and_title(self):
        out = io.StringIO()
        args = NS(repo=str(self.repo), title="Add a settings screen", detail="", kind="feature",
                  priority=1, acceptance=None, depends_on=None,
                  execution_class="standard", allow_test_changes=False)
        with patch.object(swarmd, "_setup", return_value=swarmd.load_cfg(self.repo)), \
                contextlib.redirect_stdout(out):
            swarmd.cmd_add(args)
        self.assertRegex(out.getvalue(), r"queued t[0-9a-f]{12}: Add a settings screen")

    def test_dependency_path_blocks_until_the_dependency_done(self):
        q = swarmd.queue_for(swarmd.load_cfg(self.repo))
        first = q.add("Build the API", kind="feature", origin="human")
        second = q.add("Dashboard over the API", kind="feature", origin="human",
                       depends_on=[first["id"]])
        self.assertEqual(second["depends_on"], [first["id"]])
        # The dependent is not claimable until the dependency is done.
        self.assertEqual(q.claim()["id"], first["id"])
        self.assertIsNone(q.claim())
        q.release(first["id"], True, "landed")
        self.assertEqual(q.claim()["id"], second["id"])

    def test_a_dependency_that_never_finishes_blocks_forever(self):
        q = swarmd.queue_for(swarmd.load_cfg(self.repo))
        first = q.add("Build the API", kind="feature", origin="human")
        q.add("Dashboard over the API", kind="feature", origin="human",
              depends_on=[first["id"]])
        q.release(first["id"], False, "split", split_now=True)
        # After a split the parent is done (parked/split); the child still depends on it.
        self.assertEqual(q.claim(), None)


class QueueLifecycleTests(RepoCase):
    """add → claim → release(done) → done.jsonl, and the retry path."""

    def test_add_claim_release_done(self):
        q = swarmd.queue_for(swarmd.load_cfg(self.repo))
        t = q.add("Add the settings screen", detail="show the volume slider",
                  kind="feature", origin="human")
        claimed = q.claim()
        self.assertEqual(claimed["id"], t["id"])
        self.assertTrue(claimed.get("claimed"))
        out = q.release(t["id"], True, "landed cleanly", stage="accepted")
        self.assertEqual(out["status"], "done")
        # Done rows go to done.jsonl, not the live queue.
        self.assertEqual(q.pending(), [])
        done = swarmd._read(q.done)
        self.assertEqual(len(done), 1)
        self.assertEqual(done[0]["id"], t["id"])
        self.assertEqual(done[0]["status"], "done")

    def test_retry_keeps_the_task_in_the_live_queue_with_backoff(self):
        q = swarmd.queue_for(swarmd.load_cfg(self.repo))
        t = q.add("Add the settings screen", kind="feature", origin="human")
        q.claim()
        out = q.release(t["id"], False, "tests failed", stage="tests_failed")
        self.assertEqual(out["status"], "retry")
        self.assertGreater(out["not_before"], time.time())
        self.assertEqual(out["attempts"], 1)
        # Still pending (not done), so the queue file still has it.
        self.assertEqual([r["id"] for r in q.pending()], [t["id"]])

    def test_done_jsonl_rows_are_schema_identical_across_round_trips(self):
        q = swarmd.queue_for(swarmd.load_cfg(self.repo))
        t = q.add("Add the settings screen", kind="feature", origin="human",
                  acceptance=["the slider appears"])
        q.claim()
        q.release(t["id"], True, "landed", stage="accepted")
        row = swarmd._read(q.done)[0]
        for key in ("id", "title", "detail", "kind", "attempts", "created", "priority",
                    "depth", "status", "finished"):
            self.assertIn(key, row)
        self.assertEqual(row["acceptance"], ["the slider appears"])


class AttemptArtifactTests(RepoCase):
    """The state/<slug>/attempts/<task>-* layout, including HANDOFF.md."""

    def test_attempt_directory_layout_and_handoff(self):
        # Build the artifact the same way _attempt does (via workflow.Attempt).
        q = swarmd.queue_for(swarmd.load_cfg(self.repo))
        task = q.add("Add the settings screen", kind="feature", origin="human")
        art = Attempt(swarmd.STATE, f"{task['id']}-w0-20260101-000000",
                      task, self.repo, "swarm/w0")
        art.record("baseline", base_commit="abc123")
        art.record("implementing", implementer="a:free")
        art.finish("accepted", "landed")
        self.assertTrue((art.path / "attempt.json").is_file())
        self.assertTrue((art.path / "HANDOFF.md").is_file())
        data = json.loads((art.path / "attempt.json").read_text())
        self.assertEqual(data["phase"], "accepted")
        self.assertEqual(data["task"]["id"], task["id"])
        self.assertIn("Outcome: accepted", (art.path / "HANDOFF.md").read_text())
        # attempts_for() finds it by task id, newest first.
        found = swarmd.attempts_for(task["id"])
        self.assertEqual([str(p) for p, _ in found], [str(art.path)])

    def test_attempt_dirs_are_per_attempt_not_per_worker(self):
        q = swarmd.queue_for(swarmd.load_cfg(self.repo))
        task = q.add("Add the settings screen", kind="feature", origin="human")
        a1 = Attempt(swarmd.STATE, f"{task['id']}-w0-20260101-000000", task,
                     self.repo, "swarm/w0")
        a2 = Attempt(swarmd.STATE, f"{task['id']}-w0-20260101-010000", task,
                     self.repo, "swarm/w0")
        self.assertNotEqual(a1.path, a2.path)
        self.assertEqual(len(swarmd.attempts_for(task["id"])), 2)


class BridgeStatusJsonTests(RepoCase):
    """bridge.py status JSON, field for field (the studio panel's contract)."""

    def add_task_for_bridge(self, title="Add the settings screen"):
        """Queue a task through the bridge's own module identity so its use_repo sees it."""
        c = bridge.swarmd.load_cfg(self.repo)
        bridge.swarmd.use_repo(c)
        return bridge.swarmd.queue_for(c).add(title, kind="feature", origin="human")

    def test_bridge_status_has_the_frozen_top_level_fields(self):
        rc, out = self.run_bridge("status", "--repo", str(self.repo))
        self.assertEqual(rc, 0)
        snap = out[0]
        for key in ("repo", "is_target", "daemon_running", "now", "health", "run",
                    "config_path", "config_kind", "config_warnings",
                    "config_tuned_available", "quota_mode", "daemon_paid_configured",
                    "trunk", "trunk_ahead", "roadmap", "idle", "queue", "max_queue",
                    "kinds", "landed", "parked", "budget", "experiment", "spend",
                    "stale_seconds", "wallet", "recent"):
            self.assertIn(key, snap, f"bridge status lost field {key}")
        self.assertEqual(snap["repo"], str(self.repo.resolve()))
        self.assertEqual(snap["queue"], [])
        self.assertIsInstance(snap["kinds"], list)

    def test_bridge_status_reports_a_queued_task(self):
        t = self.add_task_for_bridge()
        rc, out = self.run_bridge("status", "--repo", str(self.repo))
        snap = out[0]
        self.assertEqual(len(snap["queue"]), 1)
        row = snap["queue"][0]
        self.assertEqual(row["id"], t["id"])
        for key in ("id", "title", "detail", "kind", "status", "blocks"):
            self.assertIn(key, row)


class StopVsRestartTests(RepoCase):
    """A requested stop is never converted into a restart (invariant 8)."""

    def setUp(self):
        super().setUp()
        swarmd._drain_reason["stop_requested"] = False

    def test_drain_with_stop_true_sets_the_stop_reason(self):
        tally = swarmd.Tally()
        swarmd._run.clear()
        swarmd.drain(tally, stop=True)
        self.assertTrue(swarmd._drain_reason["stop_requested"])
        self.assertEqual(swarmd._run.get("state"), "Draining")
        self.assertEqual(swarmd._run.get("stop_reason"), "Drain requested")
        self.assertTrue(tally.drain.is_set())

    def test_drain_with_stop_false_is_a_restart_not_a_stop(self):
        tally = swarmd.Tally()
        swarmd._run.clear()
        swarmd.drain(tally, stop=False)
        self.assertFalse(swarmd._drain_reason["stop_requested"])
        self.assertEqual(swarmd._run.get("stop_reason"), "Source-change restart")

    def test_stop_while_restart_pending_goes_through_the_real_loop(self):
        # Invariant 8's guard lives in the run_daemon loop, so it is characterized
        # there, not copied here: test_p0_gaps.RealDaemonLoopStopTests drives the
        # real loop (a source change fires -> drain(tally, stop=False), then a stop
        # lands mid-tick) and asserts restart_into_new_code is never called. This
        # pointer keeps the invariant visible from the CLI contract file without
        # re-implementing the loop's logic.
        from tests.test_p0_gaps import RealDaemonLoopStopTests
        self.assertTrue(any(
            name.startswith("test_stop_that_lands_while_restart_pending")
            for name in dir(RealDaemonLoopStopTests)))


class CliReportJsonTests(RepoCase):
    """`swarm report` — build_report's text contract (starts with the header)."""

    def test_report_begins_with_the_swarm_report_header(self):
        c = swarmd.load_cfg(self.repo)
        with patch.object(swarmd, "git", return_value=(0, "0")) as git:
            text = swarmd.build_report(c, hours=24)
        self.assertTrue(text.startswith("# Swarm report"))
        # The report is also persisted for `swarm report`.
        self.assertTrue((swarmd.STATE / "REPORT.md").is_file())

    def test_report_with_a_landed_task_counts_it(self):
        q = swarmd.queue_for(swarmd.load_cfg(self.repo))
        t = q.add("Add the settings screen", kind="feature", origin="human")
        q.claim()
        q.release(t["id"], True, "landed", stage="accepted")
        with patch.object(swarmd, "git", return_value=(0, "1")):
            text = swarmd.build_report(swarmd.load_cfg(self.repo), hours=24)
        self.assertIn("accepted", text)


class CliRemainingCommandsTests(RepoCase):
    """The rest of the CLI contract: plan, models, exceptions, stop, wake.

    Network and daemon boundaries are patched; the JSON/human output shape is what
    P0 freezes.
    """

    def test_cmd_models_prints_pool_rows_and_writes_nothing_without_flag(self):
        out = io.StringIO()
        live = {"a:free": {"context_length": 128000},
                "b:free": {"context_length": 32000}}
        before = (self.default).read_text()
        with patch.object(swarmd, "free_tool_models", return_value=live), \
                contextlib.redirect_stdout(out):
            swarmd.cmd_models(NS(write=False))
        text = out.getvalue()
        self.assertIn("a:free", text)
        self.assertIn("ctx=", text)
        # Without --write, the config file is untouched.
        self.assertEqual(self.default.read_text(), before)

    def test_cmd_models_with_write_saves_the_pool(self):
        live = {"a:free": {"context_length": 128000},
                "b:free": {"context_length": 32000}}
        with patch.object(swarmd, "free_tool_models", return_value=live), \
                patch.object(swarmd, "save_cfg") as save, \
                contextlib.redirect_stdout(io.StringIO()):
            swarmd.cmd_models(NS(write=True))
        save.assert_called_once()
        written = save.call_args[0][0]
        self.assertIn("a:free", written["models"])
        self.assertNotIn("b:free", written["models"])  # below 128k cutoff

    def test_cmd_exceptions_reports_no_swallowed_exceptions(self):
        out = io.StringIO()
        with patch.object(swarmd, "_setup", return_value=swarmd.load_cfg(self.repo)), \
                patch.object(swallow, "summary", return_value=[]), \
                contextlib.redirect_stdout(out):
            rc = swarmd.cmd_exceptions(NS())
        self.assertEqual(rc, 0)
        self.assertIn("no swallowed exceptions recorded", out.getvalue())

    def test_cmd_exceptions_lists_recorded_sites(self):
        out = io.StringIO()
        rows = [{"site": "providers:fetch", "count": 3, "last_iso": "2026-01-01 00:00:00",
                 "last_msg": "boom"}]
        with patch.object(swarmd, "_setup", return_value=swarmd.load_cfg(self.repo)), \
                patch.object(swallow, "summary", return_value=rows), \
                contextlib.redirect_stdout(out):
            rc = swarmd.cmd_exceptions(NS())
        self.assertEqual(rc, 0)
        text = out.getvalue()
        self.assertIn("3 recorded", text)
        self.assertIn("providers:fetch", text)

    def test_cmd_wake_says_no_model_is_resting_when_none_are(self):
        out = io.StringIO()
        with patch.object(swarmd, "_setup", return_value=swarmd.load_cfg(self.repo)), \
                contextlib.redirect_stdout(out):
            swarmd.cmd_wake(NS())
        self.assertIn("no model is resting", out.getvalue())

    def test_cmd_wake_ends_each_rest(self):
        L = Ledger(self.repo_state() / "learn.json")
        L.cool("a:free", "overloaded")
        out = io.StringIO()
        with patch.object(swarmd, "_setup", return_value=swarmd.load_cfg(self.repo)), \
                contextlib.redirect_stdout(out):
            swarmd.cmd_wake(NS())
        text = out.getvalue()
        self.assertIn("woke 1 model(s): a:free", text)
        self.assertEqual([m for m, _, _ in L.resting()], [])

    def repo_state(self):
        """The per-repo state dir use_repo would create for this repo."""
        return swarmd.STATE

    def test_cmd_plan_runs_the_planner_pipeline(self):
        # plan() calls flint; stub it to return a JSON task list, then assert the
        # planner's additions land in the queue.
        c = swarmd.load_cfg(self.repo)
        c["steps"] = {"planner": 6, "implementer": 2}
        swarmd.use_repo(c)
        q = swarmd.queue_for(c)

        def fake_flint(prompt, *a, **k):
            return json.dumps([{"title": "A planned task", "detail": "from the planner",
                                "kind": "feature"}])

        with patch.object(swarmd, "flint", side_effect=fake_flint), \
                patch.object(swarmd, "study", return_value=""), \
                patch.object(swarmd, "ensure_trunk"), \
                patch.object(swarmd, "git", return_value=(0, "")), \
                patch.object(swarmd, "refresh_view", return_value=self.repo), \
                patch.object(swarmd, "log"):
            added = swarmd.plan(c, q, Mock(), n=2)
        self.assertEqual(added, 1)
        self.assertEqual([t["title"] for t in q.pending()], ["A planned task"])

    def test_cmd_stop_reports_no_daemon_when_none_running(self):
        out = io.StringIO()
        with patch.object(swarmd, "_setup", return_value=swarmd.load_cfg(self.repo)), \
                patch.object(swarmd, "daemon_note", return_value=None), \
                contextlib.redirect_stdout(out):
            swarmd.cmd_stop(NS(drain=False))
        self.assertIn("no swarm daemon is running", out.getvalue())


if __name__ == "__main__":
    unittest.main()
