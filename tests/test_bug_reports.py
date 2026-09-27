"""Player bug reports reaching the swarm.

studio/arcade.py says reports/bugs.jsonl is written "so a person or the swarm can read them",
and nothing under swarm/ ever opened it. A report from someone who actually played the game is
the best task this swarm can be handed, and it was going into a file nobody read.
"""
import contextlib
import io
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from swarm import bridge, swarmd

SWARMDS = (swarmd, bridge.swarmd)


def report(rid, **kw):
    return {"id": rid, "t": time.time(), "summary": "the car falls through the road",
            "details": "", "game": "crosstown", "version": "0.3.0", "severity": "broken",
            "state": None, "page": "/play.html?game=crosstown", "viewport": "1440x900",
            "agent": "Mozilla/5.0", **kw}


class BugTaskTests(unittest.TestCase):
    """What a report turns into."""

    def test_the_title_names_the_game_and_the_summary(self):
        title, detail, priority = swarmd.bug_task(report(1))
        self.assertEqual(title, "Bug in crosstown: the car falls through the road")
        self.assertEqual(priority, 2)
        self.assertIn("A player reported this", detail)

    def test_severity_sets_the_priority(self):
        self.assertEqual(swarmd.bug_task(report(1, severity="broken"))[2], 2)
        self.assertEqual(swarmd.bug_task(report(1, severity="annoying"))[2], 1)
        self.assertEqual(swarmd.bug_task(report(1, severity="cosmetic"))[2], 0)
        self.assertEqual(swarmd.bug_task(report(1, severity="what"))[2], 1)

    def test_the_seed_and_build_are_in_the_detail_because_they_reproduce_it(self):
        _, detail, _ = swarmd.bug_task(report(1, state={"seed": 7, "tick": 431}))
        self.assertIn('"seed": 7', detail)
        self.assertIn("use its seed to reproduce", detail)
        self.assertIn("version 0.3.0", detail)

    def test_what_the_player_typed_is_kept(self):
        _, detail, _ = swarmd.bug_task(report(1, details="it happens on the third junction"))
        self.assertIn("it happens on the third junction", detail)

    def test_it_asks_for_a_failing_test_first(self):
        self.assertIn("Reproduce it first with a failing test",
                      swarmd.bug_task(report(1))[1])

    def test_a_report_with_no_summary_is_not_a_task(self):
        self.assertIsNone(swarmd.bug_task(report(1, summary="   ")))
        self.assertIsNone(swarmd.bug_task("not a report"))

    def test_a_long_summary_does_not_run_away_with_the_title(self):
        title, _, _ = swarmd.bug_task(report(1, summary="x" * 500))
        self.assertLessEqual(len(title), 120)


class ImportTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.root = Path(d.name)
        (self.root / "state").mkdir()
        p = patch.object(swarmd, "STATE", self.root / "state")
        p.start()
        self.addCleanup(p.stop)
        self.repo = self.root / "repo"
        (self.repo / "reports").mkdir(parents=True)
        self.bugs = self.repo / "reports" / "bugs.jsonl"
        self.q = swarmd.Queue(max_queue=20)
        self.c = {"repo": str(self.repo)}

    def write(self, *rows):
        with open(self.bugs, "a") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")

    def test_a_new_report_becomes_a_queued_task(self):
        self.write(report(1))
        added = swarmd.import_bugs(self.c, self.q)
        self.assertEqual(len(added), 1)
        t = self.q.pending()[0]
        self.assertEqual(t["origin"], "player")
        self.assertEqual(t["kind"], "bugfix")
        self.assertEqual(t["priority"], 2)
        self.assertTrue(t["acceptance"])

    def test_the_same_report_is_not_queued_twice(self):
        self.write(report(1))
        self.assertEqual(len(swarmd.import_bugs(self.c, self.q)), 1)
        self.assertEqual(len(swarmd.import_bugs(self.c, self.q)), 0)
        self.assertEqual(len(self.q.pending()), 1)

    def test_only_reports_past_the_cursor(self):
        self.write(report(1), report(2, summary="the score never rises"))
        swarmd.import_bugs(self.c, self.q)
        self.write(report(3, summary="the pause button does nothing"))
        added = swarmd.import_bugs(self.c, self.q)
        self.assertEqual([t["title"] for t in added],
                         ["Bug in crosstown: the pause button does nothing"])

    def test_the_cursor_moves_past_a_report_that_could_not_be_queued(self):
        """An unusable report must not be retried for ever."""
        self.write(report(1, summary=""), report(2))
        swarmd.import_bugs(self.c, self.q)
        self.assertEqual(json.loads((swarmd.STATE / "bugs.cursor").read_text())["id"], 2)
        self.assertEqual(len(self.q.pending()), 1)

    def test_a_duplicate_title_is_not_queued_again(self):
        self.write(report(1))
        swarmd.import_bugs(self.c, self.q)
        self.write(report(2))                      # the same summary, filed twice
        self.assertEqual(swarmd.import_bugs(self.c, self.q), [])

    def test_at_most_a_handful_per_run(self):
        self.write(*[report(i, summary=f"report number {i}") for i in range(1, 12)])
        self.assertEqual(len(swarmd.import_bugs(self.c, self.q, limit=3)), 3)

    def test_no_reports_file_is_not_an_error(self):
        self.bugs.unlink(missing_ok=True)
        self.assertEqual(swarmd.import_bugs(self.c, self.q), [])

    def test_a_corrupt_line_is_skipped(self):
        with open(self.bugs, "a") as f:
            f.write("{not json\n")
        self.write(report(2))
        self.assertEqual(len(swarmd.import_bugs(self.c, self.q)), 1)

    def test_each_import_is_journalled(self):
        self.write(report(1))
        swarmd.import_bugs(self.c, self.q)
        rows = [json.loads(l) for l in
                (swarmd.STATE / "journal.jsonl").read_text().splitlines() if l.strip()]
        self.assertEqual(rows[0]["event"], "bug_imported")
        self.assertEqual((rows[0]["report"], rows[0]["game"]), (1, "crosstown"))

    def test_a_full_queue_does_not_lose_the_report(self):
        """The cursor must not skip past something that was refused for lack of room."""
        q = swarmd.Queue(max_queue=1)
        q.add("something else")
        self.write(report(1))
        self.assertEqual(swarmd.import_bugs(self.c, q), [])


class BridgeImportTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.root = Path(d.name)
        (self.root / "configs").mkdir()
        for name in ("state", "logs"):
            (self.root / name).mkdir()
        self.repo = self.root / "repo"
        (self.repo / "reports").mkdir(parents=True)
        for a in (["init", "-b", "main"], ["config", "user.email", "t@e.invalid"],
                  ["config", "user.name", "T"]):
            subprocess.run(["git", "-C", str(self.repo), *a], check=True, capture_output=True)
        (self.repo / "a.txt").write_text("x\n")
        for a in (["add", "-A"], ["commit", "-q", "-m", "baseline"]):
            subprocess.run(["git", "-C", str(self.repo), *a], check=True, capture_output=True)
        self.default = self.root / "config.json"
        self.default.write_text(json.dumps(
            {"repo": str(self.repo), "base_branch": "main", "test_cmd": "true",
             "models": ["a:free"], "python": sys.executable, "workers": 1}))
        for mod in SWARMDS:
            for attr, value in (("HERE", self.root), ("CONFIG", self.default),
                                ("STATE", self.root / "state"), ("LOGS", self.root / "logs"),
                                ("EXAMPLE", self.root / "config.example.json")):
                p = patch.object(mod, attr, value)
                p.start()
                self.addCleanup(p.stop)
        import os
        os.environ.pop("FLINT_SWARM_CONFIG", None)

    def run_bridge(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = bridge.main(list(argv))
        return rc, [json.loads(l) for l in out.getvalue().splitlines() if l.strip()]

    def test_import_bugs_over_the_wire(self):
        with open(self.repo / "reports" / "bugs.jsonl", "a") as f:
            f.write(json.dumps(report(1)) + "\n")
        rc, out = self.run_bridge("import-bugs", "--repo", str(self.repo))
        self.assertEqual((rc, out[0]["added"]), (0, 1))
        self.assertEqual(out[0]["tasks"][0]["priority"], 2)

    def test_it_finds_the_reports_without_being_told_where(self):
        rc, out = self.run_bridge("import-bugs", "--repo", str(self.repo))
        self.assertEqual((rc, out[0]["ok"], out[0]["added"]), (0, True, 0))

    def test_another_file_can_be_named(self):
        other = self.root / "elsewhere.jsonl"
        other.write_text(json.dumps(report(9, summary="from somewhere else")) + "\n")
        rc, out = self.run_bridge("import-bugs", "--repo", str(self.repo), "--file", str(other))
        self.assertEqual(out[0]["added"], 1)
        self.assertIn("from somewhere else", out[0]["tasks"][0]["title"])


if __name__ == "__main__":
    unittest.main()
