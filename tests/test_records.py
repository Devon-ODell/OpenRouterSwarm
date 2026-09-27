"""Records that say what actually happened.

All 12 "sandbox-exec" errors in this window were really "timed out after 420 seconds", hidden
past character 500 of a str() that opened with the whole sandbox profile. The 10–25 KB prompt
went in argv, so it appeared in `ps` and in every exception. 391 paid_fallback rows were
recorded and not one carried a confirmed charge, so cost per landed commit could not be
computed at all. And the report's "Split or parked" section printed raw log fragments.
"""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import flint
from swarm import swarmd


class ErrorRecordTests(unittest.TestCase):
    def test_a_timeout_says_it_timed_out_and_for_how_long(self):
        row = swarmd.describe_error(subprocess.TimeoutExpired("sandbox-exec", 420))
        self.assertEqual(row["kind"], "TimeoutExpired")
        self.assertEqual(row["timeout"], 420)
        self.assertIn("timed out after 420 seconds", row["err"])

    def test_a_failed_command_says_what_it_exited_with(self):
        row = swarmd.describe_error(subprocess.CalledProcessError(3, ["x"], stderr="boom"))
        self.assertEqual((row["kind"], row["returncode"]), ("CalledProcessError", 3))
        self.assertIn("exited 3", row["err"])
        self.assertIn("boom", row["stderr"])

    def test_the_command_line_is_never_recorded(self):
        """argv has held a 25 KB prompt and the whole sandbox profile."""
        argv = ["/usr/bin/sandbox-exec", "-p", "(version 1)(allow default)" * 200, "HUGE PROMPT"]
        for e in (subprocess.TimeoutExpired(argv, 420),
                  subprocess.CalledProcessError(1, argv, stderr="real reason")):
            row = swarmd.describe_error(e)
            blob = json.dumps(row)
            self.assertNotIn("HUGE PROMPT", blob)
            self.assertNotIn("sandbox-exec", blob)
            self.assertNotIn("allow default", blob)

    def test_the_last_of_stderr_is_kept_not_the_first(self):
        row = swarmd.describe_error(
            subprocess.CalledProcessError(1, ["x"], stderr="noise\n" * 900 + "THE REAL REASON"))
        self.assertIn("THE REAL REASON", row["stderr"])
        self.assertLessEqual(len(row["stderr"]), 1000)

    def test_bytes_stderr_is_decoded(self):
        row = swarmd.describe_error(subprocess.CalledProcessError(1, ["x"], stderr=b"bytes boom"))
        self.assertIn("bytes boom", row["stderr"])

    def test_an_ordinary_exception_still_gets_its_message(self):
        row = swarmd.describe_error(ValueError("something went wrong"))
        self.assertEqual((row["kind"], row["err"]), ("ValueError", "something went wrong"))


class PromptFileTests(unittest.TestCase):
    def test_flint_reads_the_prompt_from_a_file(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        f = Path(d.name) / "prompt.txt"
        f.write_text("THE PROMPT")
        out = subprocess.run(
            [flint.sys.executable, "flint.py", "--prompt-file", str(f), "--model", "x"],
            capture_output=True, text=True, timeout=60,
            env={**flint.os.environ, "OPENROUTER_API_KEY": ""})
        # No key, so it exits before any request — but it must have got that far.
        self.assertNotIn("could not read", out.stderr)

    def test_both_at_once_is_refused(self):
        out = subprocess.run(
            [flint.sys.executable, "flint.py", "--prompt-file", "/tmp/x", "-p", "y"],
            capture_output=True, text=True, timeout=60)
        self.assertIn("not both", out.stderr)

    def test_an_unreadable_file_is_an_argument_error_not_a_traceback(self):
        out = subprocess.run(
            [flint.sys.executable, "flint.py", "--prompt-file", "/nonexistent/x"],
            capture_output=True, text=True, timeout=60)
        self.assertIn("could not read --prompt-file", out.stderr)
        self.assertNotIn("Traceback", out.stderr)


class ChargeTests(unittest.TestCase):
    """What a turn cost, from OpenRouter's number, banked where the supervisor can read it."""

    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.root = Path(d.name)
        p = patch.object(swarmd, "STATE", self.root)
        p.start()
        self.addCleanup(p.stop)
        swarmd._charges.clear()

    def agent(self):
        a = flint.Agent.__new__(flint.Agent)
        a.model, a.wallet, a.spend_usd = "qwen/qwen3-coder", None, 0.0
        a.last_cost, a.total_cost, a.headless = 0.0, 0.0, True
        a.spend = flint.Spend(path="", cap="")
        return a

    def test_flint_writes_each_charge_where_it_was_told_to(self):
        path = self.root / "charges.jsonl"

        class Usage:
            cost = 0.0123
        with patch.dict(flint.os.environ, {"FLINT_CHARGE_FILE": str(path)}):
            self.agent()._charge(Usage())
        rows = [json.loads(l) for l in path.read_text().splitlines()]
        self.assertEqual(rows[0]["usd"], 0.0123)
        self.assertEqual(rows[0]["model"], "qwen/qwen3-coder")

    def test_a_free_reply_writes_nothing(self):
        path = self.root / "charges.jsonl"

        class Usage:
            cost = 0.0
        with patch.dict(flint.os.environ, {"FLINT_CHARGE_FILE": str(path)}):
            self.agent()._charge(Usage())
        self.assertFalse(path.exists())

    def test_no_charge_file_is_not_an_error(self):
        class Usage:
            cost = 0.5
        env = {k: v for k, v in flint.os.environ.items() if k != "FLINT_CHARGE_FILE"}
        with patch.dict(flint.os.environ, env, clear=True):
            self.agent()._charge(Usage())      # must not raise

    def test_the_swarm_banks_them_and_journals_one_row_each(self):
        path = self.root / "charges.jsonl"
        path.write_text('{"at": 1, "model": "a", "usd": 0.01}\n'
                        '{"at": 2, "model": "b", "usd": 0.02}\n')
        total = swarmd.read_charges(path, "implementer", "w0", "a")
        self.assertEqual(total, 0.03)
        self.assertEqual(swarmd.count_usd("w0"), 0.03)
        rows = [json.loads(l) for l in
                (self.root / "journal.jsonl").read_text().splitlines() if l.strip()]
        self.assertEqual([r["event"] for r in rows], ["charge", "charge"])
        self.assertEqual([r["model"] for r in rows], ["a", "b"])
        self.assertFalse(path.exists(), "the file is consumed, not left to double-count")

    def test_a_missing_or_corrupt_file_costs_nothing(self):
        self.assertEqual(swarmd.read_charges(self.root / "gone.jsonl", "r", "w0", "m"), 0.0)
        bad = self.root / "bad.jsonl"
        bad.write_text("{not json")
        self.assertEqual(swarmd.read_charges(bad, "r", "w0", "m"), 0.0)

    def test_an_attempts_total_accumulates_and_resets(self):
        swarmd.count_usd("w1", reset=True)
        swarmd.count_usd("w1", 0.01)
        swarmd.count_usd("w1", 0.02)
        self.assertEqual(swarmd.count_usd("w1"), 0.03)
        self.assertEqual(swarmd.count_usd("w1", reset=True), 0.03)
        self.assertEqual(swarmd.count_usd("w1"), 0.0)


class ReportableNoteTests(unittest.TestCase):
    def test_a_log_reference_and_its_round_counter_come_out(self):
        self.assertEqual(
            swarmd.reportable("_file tool: read_file round 3/26; log: /Users/d/swarm/logs/w0.log"),
            "_file tool: read_file")

    def test_an_evidence_path_comes_out(self):
        self.assertEqual(swarmd.reportable("no implementation changes; evidence: /Users/d/a/b"),
                         "no implementation changes")

    def test_a_truncated_sandbox_command_becomes_a_phrase(self):
        note = ("Command '['/usr/bin/sandbox-exec', '-p', '(version 1)\\n(allow default)\\n"
                "(deny file-write*)\\n(allow file-write* (subpath \"/dev\") (subpa")
        out = swarmd.reportable(note)
        self.assertEqual(out, "the sandboxed command")
        self.assertNotIn("file-write", out)

    def test_an_ordinary_note_is_left_readable(self):
        self.assertEqual(swarmd.reportable("2 tests failed: test_scores, test_seed"),
                         "2 tests failed: test_scores, test_seed")


class ReportTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.root = Path(d.name)
        for name in ("state", "logs"):
            (self.root / name).mkdir()
        for attr, value in (("STATE", self.root / "state"), ("LOGS", self.root / "logs")):
            p = patch.object(swarmd, attr, value)
            p.start()
            self.addCleanup(p.stop)
        self.repo = self.make_repo()

    def make_repo(self):
        repo = self.root / "repo"
        repo.mkdir()

        def git(*a):
            subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)
        git("init", "-b", "main")
        git("config", "user.email", "t@example.invalid")
        git("config", "user.name", "T")
        (repo / "a.txt").write_text("x\n")
        git("add", "-A")
        git("commit", "-m", "baseline")
        return repo

    def journal(self, *rows):
        with open(swarmd.STATE / "journal.jsonl", "a") as f:
            for r in rows:
                f.write(json.dumps({"t": __import__("time").time(),
                                    "iso": "now", **r}) + "\n")

    def done(self, *rows):
        with open(swarmd.STATE / "done.jsonl", "a") as f:
            for r in rows:
                f.write(json.dumps({"finished": __import__("time").time(), **r}) + "\n")

    def report(self, **cfg):
        c = {"repo": str(self.repo), "base_branch": "main", "test_cmd": "true", **cfg}
        return swarmd.build_report(c, hours=24)

    def test_cost_per_landed_commit_and_per_model(self):
        self.journal(
            {"event": "attempt", "id": "t1", "stage": "accepted",
             "implementer": "qwen/qwen3-coder", "usd": 0.04},
            {"event": "attempt", "id": "t2", "stage": "no_change",
             "implementer": "qwen/qwen3-coder", "usd": 0.02},
            {"event": "attempt", "id": "t3", "stage": "no_change",
             "implementer": "moonshotai/kimi-k2", "usd": 0.01})
        text = self.report()
        self.assertIn("## Cost", text)
        self.assertIn("$0.0700 across 3 attempt(s)", text)
        self.assertIn("$0.0700 per landed commit", text)
        self.assertIn("`qwen/qwen3-coder` | 2 | 1 | 0.0600", text)

    def test_a_window_with_no_paid_turns_says_so(self):
        self.assertIn("No paid turns were charged", self.report())

    def test_the_failure_classes_are_summarised(self):
        self.journal({"event": "task", "id": "t1", "ok": False, "stage": "no_change",
                      "failure_class": "harness"},
                     {"event": "task", "id": "t2", "ok": False, "stage": "tests_failed",
                      "failure_class": "task"})
        self.assertIn("harness 1", self.report())
        self.assertIn("Only `task` failures say the work was wrong", self.report())

    def test_split_and_parked_show_the_class_and_one_clean_line(self):
        self.journal({"event": "task", "id": "t1", "ok": False, "stage": "no_change",
                      "failure_class": "harness",
                      "note": "no implementation changes; evidence: /Users/d/state/attempts/x"})
        self.done({"id": "t1", "title": "Shard Stack M1", "status": "split",
                   "note": "no implementation changes; evidence: /Users/d/state/attempts/x"})
        text = self.report()
        self.assertIn("**split** · harness · no_change · Shard Stack M1", text)
        self.assertNotIn("/Users/d", text.split("## Split or parked")[1])

    def test_the_mit_section_is_hidden_when_no_experiment_runs(self):
        self.journal({"event": "attempt", "id": "t1", "stage": "accepted", "mit": "on"})
        self.assertNotIn("MIT corpus experiment",
                         self.report(mit_experiment={"enabled": False}))
        self.assertNotIn("MIT corpus experiment", self.report())
        self.assertNotIn("MIT corpus experiment",
                         self.report(mit_experiment={"enabled": True}, inject_corpus=False))

    def test_the_mit_section_is_there_while_one_does(self):
        self.journal({"event": "attempt", "id": "t1", "stage": "accepted", "mit": "on"})
        self.assertIn("MIT corpus experiment",
                      self.report(mit_experiment={"enabled": True, "share_on": 0.5}))


if __name__ == "__main__":
    unittest.main()
