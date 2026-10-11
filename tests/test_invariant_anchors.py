"""Every guard tests/mutate_invariants.py deletes must still exist, exactly once, in the source.

The mutation run is slow and runs nightly; this check is instant. If a refactor rewords or
moves a guard, update its anchor in mutate_invariants.MUTANTS rather than deleting the mutant.
"""
import unittest

from tests import mutate_invariants


class InvariantAnchorTests(unittest.TestCase):
    def test_every_guard_anchor_matches_exactly_once(self):
        counts = mutate_invariants.anchor_counts()
        self.assertEqual({mid: n for mid, n in counts.items() if n != 1}, {})

    def test_every_mutant_actually_changes_its_guard(self):
        for mid, _, _, old, new in mutate_invariants.MUTANTS:
            with self.subTest(mid=mid):
                self.assertNotEqual(old, new)

    def test_mutant_ids_are_unique(self):
        ids = [m[0] for m in mutate_invariants.MUTANTS]
        self.assertEqual(len(ids), len(set(ids)))


if __name__ == "__main__":
    unittest.main()
