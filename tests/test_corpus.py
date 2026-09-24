"""Offline tests for the study-corpus indexer and retrieval."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from swarm import corpus_index
from swarm.corpus_index import MissingExtractor, build, search, walk


class CorpusBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "corpus"
        self.db = Path(self.tmp.name) / "corpus.db"
        self.root.mkdir()

    def write(self, rel, text):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return p


class WalkTests(CorpusBase):
    def test_web_export_bundles_are_not_indexed(self):
        """OCW offline exports ship webpack bundles that would drown BM25."""
        self.write("course/notes.md", "# Notes\n\n" + "real study material. " * 20)
        for rel in ("course/static/main.3f0b.js",      # hashed bundle
                    "course/runtime.a91f.js",           # hashed, outside static/
                    "course/vendor-legacy.js",          # conventional bundle name
                    "course/assets/app.js",             # under assets/
                    "course/script.min.js"):            # already-handled minified
            self.write(rel, "(function(){var a=1;})();" + "token " * 200)
        found = {str(p.relative_to(self.root)) for p in walk(self.root)}
        self.assertEqual(found, {"course/notes.md"})

    def test_genuine_course_code_is_still_indexed(self):
        """The bundle filter must not swallow real example source files."""
        self.write("course/lecture01/example.py", "def solve(xs):\n    return xs[-1]\n")
        self.write("course/lecture01/demo.js", "function solve(xs){return xs.at(-1);}")
        found = {Path(p).name for p in walk(self.root)}
        self.assertEqual(found, {"example.py", "demo.js"})


class SearchTests(CorpusBase):
    QUERY = "off-by-one at the final index of the buffer"

    def corpus(self):
        """Two subjects that share incidental vocabulary, as a mixed corpus does."""
        self.write("agent-skills/algorithms/SKILL.md",
                   "# Algorithms\n\n## Iterator invariants\n\n"
                   "An off-by-one error appears when the terminating index is inclusive "
                   "rather than exclusive. Check the final index so the last element of "
                   "the buffer is processed. The iterator must hold its invariant at the "
                   "boundary index on every pass.\n")
        # Shares 'final' and 'index' incidentally; the subject is unrelated.
        self.write("Intro-To-Psychology/lecture12.md",
                   "# Reward\n\n## Prediction error\n\n"
                   "The dopaminergic reward signal encodes a prediction error. An agent in "
                   "a given state updates expectations when received reward diverges from "
                   "predicted reward. Extinction follows the final session in the schedule, "
                   "and the index of trials is recorded.\n")
        build(self.root, self.db, verbose=False)

    def test_floor_drops_weak_cross_domain_hits(self):
        self.corpus()
        unfiltered = search(self.QUERY, k=4, db=self.db, floor=0)
        filtered = search(self.QUERY, k=4, db=self.db)
        self.assertTrue(any("Psychology" in h["path"] for h in unfiltered),
                        "fixture must produce a weak cross-domain hit to be meaningful")
        self.assertGreater(len(unfiltered), len(filtered))
        self.assertTrue(all("agent-skills" in h["path"] for h in filtered))

    def test_floor_keeps_the_best_hit_for_every_domain(self):
        """The floor must not suppress a query's genuinely relevant subject."""
        self.corpus()
        hits = search("dopaminergic reward prediction error", k=4, db=self.db)
        self.assertTrue(hits)
        self.assertIn("Psychology", hits[0]["path"])

    def test_prefix_restricts_to_one_subtree(self):
        self.corpus()
        hits = search("reward agent state", k=4, db=self.db, prefix="agent-skills", floor=0)
        self.assertTrue(all(h["path"].startswith("agent-skills/") for h in hits))

    def test_prefix_matching_is_anchored(self):
        """A prefix must not match a sibling that merely shares its opening text."""
        self.write("agent-skills/a/SKILL.md", "# A\n\n" + "boundary invariant text. " * 20)
        self.write("agent-skills-draft/b/SKILL.md", "# B\n\n" + "boundary invariant text. " * 20)
        build(self.root, self.db, verbose=False)
        hits = search("boundary invariant", k=8, db=self.db, prefix="agent-skills", floor=0)
        self.assertTrue(hits)
        self.assertTrue(all(h["path"].startswith("agent-skills/") for h in hits))


class ExtractorTests(CorpusBase):
    def test_missing_pdftotext_is_reported_not_silently_skipped(self):
        """Without this, every PDF indexes as zero chunks with no warning."""
        pdf = self.write("course/notes.pdf", "%PDF-1.4 stub")
        with patch.object(corpus_index.subprocess, "run", side_effect=FileNotFoundError):
            with self.assertRaisesRegex(MissingExtractor, "pdftotext"):
                corpus_index.extract(pdf)

    def test_unreadable_file_still_degrades_quietly(self):
        """A genuinely bad file is not a setup error and must not abort a build."""
        p = self.write("course/notes.md", "ok")
        with patch.object(Path, "read_text", side_effect=OSError("boom")):
            self.assertEqual(corpus_index.extract(p), "")


if __name__ == "__main__":
    unittest.main()
