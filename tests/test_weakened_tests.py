"""Telling a weakened test suite from a change that was asked for.

The check counted every removed line containing "assert" under any path containing "test". All
four times it fired across 189 studio attempts it was wrong, and it carries the heaviest penalty
in the ledger (weight 6) and hangs the model on the rafters — so it punished models for doing
exactly what the owner asked. The owner's removal request failed in the swarm three times and
was done by hand in the end.

The four diffs are kept as fixtures, taken from the studio's own branches:
    git -C <studio> diff <branch>~1 <branch> > tests/fixtures/weakened/<name>.diff
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from swarm import swarmd

FIXTURES = Path(__file__).parent / "fixtures" / "weakened"


def fixture(name):
    return (FIXTURES / f"{name}.diff").read_text()


class TheFourFalsePositivesTests(unittest.TestCase):
    """Every one of these was a legitimate change that cost a model its reputation."""

    def test_deleting_the_games_the_owner_asked_to_delete(self):
        """swarm/t2788f961b026-e1659a98: games/reversi/ and games/star-catcher/ deleted, and the
        assertions that loaded them with them. The test files themselves survive."""
        self.assertEqual(swarmd.weakened_tests(fixture("remove-star-catcher-and-reversi")), 0)

    def test_the_same_removal_on_its_second_attempt(self):
        self.assertEqual(swarmd.weakened_tests(fixture("remove-star-catcher-and-reversi-retry")), 0)

    def test_the_owners_own_request_to_remove_two_games_and_add_chess(self):
        """swarm/t57ad75a4a71d-ee5a3053: "I want you to remove star catcher and reversi, add
        Chess…" — the removal is the task."""
        self.assertEqual(swarmd.weakened_tests(fixture("owner-asked-remove-and-add-chess")), 0)

    def test_an_assertion_retargeted_to_an_8x15_board(self):
        """swarm/t1bf36c30ecd7-120eeee2: b[112] became b[80]. An edited assertion, not a
        removed one: the same assertion function is called on the added line."""
        self.assertEqual(swarmd.weakened_tests(fixture("shard-stack-retarget-assertion")), 0)


class RealWeakeningTests(unittest.TestCase):
    """What the check is for. It must still fire on all of these."""

    def test_an_assertion_removed_with_nothing_in_its_place(self):
        self.assertEqual(swarmd.weakened_tests(fixture("synthetic-removed-assertion")), 1)

    def test_a_test_file_deleted_while_the_code_it_tests_stays(self):
        self.assertEqual(swarmd.weakened_tests(fixture("synthetic-deleted-test-file")), 1)

    def test_a_test_file_deleted_with_its_subject_is_not_a_weakening(self):
        diff = ('diff --git a/games/reversi/game.js b/games/reversi/game.js\n'
                'deleted file mode 100644\n--- a/games/reversi/game.js\n+++ /dev/null\n'
                '@@ -1,2 +0,0 @@\n-const rules = {};\n-module.exports = rules;\n'
                'diff --git a/tests/test_reversi.py b/tests/test_reversi.py\n'
                'deleted file mode 100644\n--- a/tests/test_reversi.py\n+++ /dev/null\n'
                '@@ -1,3 +0,0 @@\n-def test_legal_moves():\n-    assert legal(b, 1) == 4\n')
        self.assertEqual(swarmd.weakened_tests(diff), 0)

    def test_several_assertions_gone_are_counted(self):
        diff = ('diff --git a/tests/test_core.py b/tests/test_core.py\n'
                '--- a/tests/test_core.py\n+++ b/tests/test_core.py\n'
                '@@ -1,5 +1,2 @@\n'
                ' def test_x():\n-    assert a == 1\n-    assert b == 2\n-    assert c == 3\n')
        self.assertEqual(swarmd.weakened_tests(diff), 3)

    def test_a_net_loss_is_what_counts(self):
        """Three assertions out, two back: one was really lost."""
        diff = ('diff --git a/tests/test_core.py b/tests/test_core.py\n'
                '--- a/tests/test_core.py\n+++ b/tests/test_core.py\n'
                '@@ -1,5 +1,4 @@\n def test_x():\n'
                '-    assert a == 1\n-    assert b == 2\n-    assert c == 3\n'
                '+    assert a == 10\n+    assert b == 20\n')
        self.assertEqual(swarmd.weakened_tests(diff), 1)

    def test_a_different_assertion_function_is_not_a_replacement(self):
        """assertEqual swapped for a bare assertTrue(True) is how a suite gets hollowed out."""
        diff = ('diff --git a/tests/test_core.py b/tests/test_core.py\n'
                '--- a/tests/test_core.py\n+++ b/tests/test_core.py\n'
                '@@ -1,3 +1,3 @@\n def test_x(self):\n'
                '-        self.assertEqual(paginate(items, 10)[-1], items[9])\n'
                '+        self.assertTrue(paginate(items, 10))\n')
        self.assertEqual(swarmd.weakened_tests(diff), 1)

    def test_a_skip_marker_is_not_a_replacement_assertion(self):
        diff = ('diff --git a/tests/test_core.py b/tests/test_core.py\n'
                '--- a/tests/test_core.py\n+++ b/tests/test_core.py\n'
                '@@ -1,4 +1,4 @@\n def test_x():\n'
                '-    assert total == 42\n+    pass  # TODO\n')
        self.assertEqual(swarmd.weakened_tests(diff), 1)

    def test_code_outside_a_test_file_is_none_of_its_business(self):
        diff = ('diff --git a/src/app.py b/src/app.py\n--- a/src/app.py\n+++ b/src/app.py\n'
                '@@ -1,3 +1,2 @@\n-    assert total == 42\n')
        self.assertEqual(swarmd.weakened_tests(diff), 0)

    def test_an_empty_diff_is_not_a_weakening(self):
        self.assertEqual(swarmd.weakened_tests(""), 0)


class AssertionCountingTests(unittest.TestCase):
    def test_the_shapes_this_repository_and_the_studio_actually_write(self):
        self.assertEqual(swarmd.assert_counts([
            "self.assertEqual(a, b)", "assert.equal(x, y)", "assert x == 1",
            "expect(x).toBe(1)", "require(x)", "t.Fatalf('no')", "should.equal(a, b)",
        ]), {"assertEqual": 1, "assert.equal": 1, "assert": 1, "expect": 1,
             "require": 1, "t.Fatalf": 1, "should": 1})

    def test_an_assertion_is_counted_once_per_call_not_once_per_line(self):
        self.assertEqual(swarmd.assert_counts(
            ["assert.equal(b.length, 120); assert.equal(b[112], 3);"]), {"assert.equal": 2})

    def test_a_line_with_no_assertion_counts_nothing(self):
        self.assertEqual(swarmd.assert_counts(["const rev = load('reversi');", "h.pump(30);"]), {})

    def test_the_names_that_identify_a_deleted_path(self):
        names = swarmd.identifiers("games/star-catcher/game.js")
        self.assertIn("games/star-catcher/game.js", names)
        self.assertIn("star-catcher", names)
        self.assertIn("games/star-catcher", names)
        self.assertNotIn("games", names, "a generic directory identifies nothing")

    def test_a_top_level_file_does_not_drag_its_directory_in(self):
        self.assertNotIn("tests", swarmd.identifiers("tests/test_core.py"))

    def test_a_reference_written_as_path_join_is_still_a_reference(self):
        """The studio's tests load a game with path.join(__dirname, '..', 'games', 'slug', …)."""
        line = "const star = fs.readFileSync(path.join(__dirname, '..', 'games', 'star-catcher', 'game.js'), 'utf8');"
        self.assertTrue(swarmd.names_deleted(line, {"star-catcher"}))
        self.assertFalse(swarmd.names_deleted(line, {"rune-garden"}))

    def test_a_partial_word_is_not_a_reference(self):
        self.assertFalse(swarmd.names_deleted("load('reversible')", {"reversi"}))
        self.assertTrue(swarmd.names_deleted("load('reversi')", {"reversi"}))


class AllowTestChangesTests(unittest.TestCase):
    """The owner can say a task is allowed to change tests. Nothing else can."""

    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        p = patch.object(swarmd, "STATE", Path(d.name))
        p.start()
        self.addCleanup(p.stop)
        self.q = swarmd.Queue(max_depth=2)

    def test_the_flag_turns_the_check_off(self):
        diff = fixture("synthetic-removed-assertion")
        self.assertEqual(swarmd.weakened_tests(diff), 1)
        self.assertEqual(swarmd.weakened_tests(diff, allow_test_changes=True), 0)

    def test_a_person_can_set_it(self):
        t = self.q.add("Remove star catcher and reversi", origin="human", allow_test_changes=True)
        self.assertIs(t["allow_test_changes"], True)

    def test_the_editor_can_set_it(self):
        t = self.q.add("Remove reversi", origin="cursor", allow_test_changes=True)
        self.assertIs(t["allow_test_changes"], True)

    def test_a_planner_cannot(self):
        for origin in ("plan", "split", "follow_up", None):
            with self.assertRaises(ValueError) as caught:
                self.q.add(f"Make the suite pass ({origin})", origin=origin,
                           allow_test_changes=True)
            self.assertIn("only a person", str(caught.exception))

    def test_a_decomposer_cannot_inherit_it_from_the_task_it_splits(self):
        parent = self.q.add("Remove reversi", origin="human", allow_test_changes=True)
        with self.assertRaises(ValueError):
            self.q.add("Remove reversi, part one", origin="split", parent=parent["id"],
                       allow_test_changes=True)
        child = self.q.add("Remove reversi, part two", origin="split", parent=parent["id"])
        self.assertNotIn("allow_test_changes", child)

    def test_a_task_without_it_is_unchanged(self):
        self.assertNotIn("allow_test_changes", self.q.add("Ordinary work", origin="human"))

    def test_the_queue_edit_form_can_grant_and_take_it_back(self):
        t = self.q.add("Remove reversi", origin="human")
        self.assertIs(self.q.edit(t["id"], allow_test_changes=True)["allow_test_changes"], True)
        self.assertNotIn("allow_test_changes", self.q.edit(t["id"], allow_test_changes=False))

    def test_editing_something_else_leaves_it_alone(self):
        t = self.q.add("Remove reversi", origin="human", allow_test_changes=True)
        self.assertIs(self.q.edit(t["id"], title="Remove reversi and star catcher")
                      ["allow_test_changes"], True)


if __name__ == "__main__":
    unittest.main()
