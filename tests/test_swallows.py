"""The swallowed-exception recorder: never raises, is thread-safe, is bounded, and
groups by site so `swarm exceptions` can show a table instead of a pile of lines."""
import tempfile
import unittest
from pathlib import Path

from swarm import swallow


class SwallowedRecorderTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._old_state = swallow._STATE
        swallow._STATE = Path(self._tmp.name)   # redirect the recorder off the real state dir
        self.addCleanup(setattr, swallow, "_STATE", self._old_state)

    def test_record_writes_and_recent_reads_back(self):
        try:
            1 / 0
        except Exception as e:
            swallow.record("tests.zero_division", e)
        rows = swallow.recent()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["site"], "tests.zero_division")
        self.assertEqual(rows[0]["kind"], "ZeroDivisionError")
        self.assertIn("division by zero", rows[0]["msg"])

    def test_recent_is_newest_first_and_summary_groups_by_site(self):
        for i in range(3):
            swallow.record("tests.site_a", ValueError(f"a{i}"))
        swallow.record("tests.site_b", KeyError("b"))
        rows = swallow.recent()
        self.assertEqual(rows[0]["site"], "tests.site_b")     # newest first
        self.assertEqual(len(rows), 4)
        summ = swallow.summary()
        by_site = {s["site"]: s for s in summ}
        self.assertEqual(by_site["tests.site_a"]["count"], 3)
        self.assertEqual(by_site["tests.site_b"]["count"], 1)
        self.assertIn("a2", by_site["tests.site_a"]["last_msg"])

    def test_recorder_never_raises_even_when_state_is_unwritable(self):
        # Point at a path that cannot be created (a file in the way), then record.
        blocker = Path(self._tmp.name) / "blocker"
        blocker.write_text("x")
        swallow._STATE = blocker            # mkdir will fail
        try:
            swallow.record("tests.unwritable", RuntimeError("nope"))   # must not raise
        finally:
            swallow._STATE = Path(self._tmp.name)

    def test_bounded_to_recent_200(self):
        for i in range(210):
            swallow.record("tests.flood", ValueError(f"n{i}"))
        self.assertEqual(len(swallow.recent(1000)), 200)
        self.assertEqual(swallow.recent()[0]["msg"], "n209")    # newest retained


if __name__ == "__main__":
    unittest.main()