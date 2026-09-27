"""What does and does not get into an implementer's prompt.

Attempt te418b5570dd3-4d356a96, "Add parked vehicle data structure and rendering", was sent an
MIT IDS.333 lecture on parking-garage real options, a 6.100L Python `Vehicle` class homework
with its answers, and a 14.41 public-finance transcript about a police vehicle. They matched on
the word "vehicle": one weak lecture-card hit unlocked source pages from three other courses.
Alongside them sat 4.1 KB of diffs from unrelated tasks and a JSON dump of absolute paths, cut
off mid-string. None of it was code from the file the task was about.
"""
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from swarm import learn, mit_corpus, swarmd


class UnaskedCorpusTests(unittest.TestCase):
    """Excerpts injected into a prompt nobody asked to have filled."""

    def card(self, course, lecture, text, rel=None):
        rel = rel or f"a/flint/cards/{course}.md"
        return {"kind": "card", "rel": rel, "path": f"/corpus/{rel}", "course": course,
                "lecture": lecture, "document": None, "heading": lecture, "text": text}

    def page(self, course, lecture, text, rel="a/references/documents/p.md"):
        return {"kind": "source", "rel": rel, "path": f"/corpus/{rel}", "course": course,
                "lecture": lecture, "document": None, "heading": "", "text": text}

    def test_a_card_matching_one_word_is_not_a_match(self):
        hits = [self.card("IDS.333 Risk and Decision Analysis", "L5: real options",
                          "a parking garage may be expanded later")]
        self.assertEqual(mit_corpus.unasked(hits, "Add parked vehicle data structure"), [])

    def test_two_real_terms_are_enough(self):
        hits = [self.card("CMS.608 Game Design", "L13: cybernetics",
                          "vehicle handling and player state in a driving game")]
        kept = mit_corpus.unasked(hits, "Add onFoot walk state with enter vehicle for a driving game")
        self.assertEqual(len(kept), 1)

    def test_stopwords_do_not_count(self):
        hits = [self.card("X", "L1", "the and for with that this from each other another")]
        self.assertEqual(mit_corpus.unasked(hits, "the and for with that this"), [])

    def test_only_the_matched_lecture_follows_the_card(self):
        card = self.card("CMS.608 Game Design", "L13: cybernetics",
                         "vehicle handling and player state in a driving game")
        same = self.page("CMS.608 Game Design", "L13: cybernetics", "slides for that lecture")
        other_lecture = self.page("CMS.608 Game Design", "L2: paper prototypes", "unrelated")
        other_course = self.page("14.41 Public Finance", "L7: police", "a police vehicle")
        kept = mit_corpus.unasked([card, same, other_lecture, other_course],
                                  "Add onFoot walk state with enter vehicle for a driving game")
        self.assertEqual([h["course"] + "|" + (h["lecture"] or "") for h in kept],
                         ["CMS.608 Game Design|L13: cybernetics", "CMS.608 Game Design|L13: cybernetics"])

    def test_a_page_with_no_lecture_label_does_not_qualify(self):
        card = self.card("C", "L1", "alpha beta gamma")
        loose = self.page("C", None, "alpha beta")
        self.assertEqual(mit_corpus.unasked([card, loose], "alpha beta"), [card])

    def test_a_transcript_is_never_injected(self):
        card = self.card("C", "L1", "alpha beta gamma")
        script = self.page("C", "L1", "alpha beta", rel="a/references/transcripts/x.md")
        self.assertEqual(mit_corpus.unasked([card, script], "alpha beta"), [card])

    def test_the_study_tool_is_not_narrowed(self):
        """A model that asks for material gets what it asked for, transcripts included."""
        hits = [self.page("14.41", "L7", "a police vehicle",
                          rel="a/references/transcripts/x.md")]
        with patch.object(mit_corpus, "search", return_value=hits):
            self.assertIn("police vehicle", mit_corpus.study("vehicle", require_card=False))
            self.assertEqual(mit_corpus.study("vehicle", require_card=True), "")


class InjectCorpusTests(unittest.TestCase):
    """One switch a repository can set to keep lecture excerpts out of its prompts."""

    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.db = Path(d.name) / "corpus.db"
        self.db.write_text("not really a database, but it exists")

    def cfg(self, **kw):
        return {"corpus_db": str(self.db), **kw}

    def test_off_when_the_repository_says_no(self):
        self.assertEqual(swarmd.mit_arm(self.cfg(inject_corpus=False)), "off")
        self.assertEqual(swarmd.mit_arm(self.cfg(inject_corpus=False,
                                                 mit_experiment={"enabled": False})), "off")

    def test_on_by_default_for_a_repository_that_never_heard_of_it(self):
        self.assertEqual(swarmd.mit_arm(self.cfg(mit_experiment={"enabled": False})), "on")

    def test_no_corpus_still_wins(self):
        self.assertEqual(swarmd.mit_arm({"corpus_db": "/nonexistent.db"}), "none")

    def test_the_experiment_still_splits_when_the_corpus_is_wanted(self):
        arms = {swarmd.mit_arm(self.cfg(inject_corpus=True,
                                        mit_experiment={"enabled": True, "share_on": 0.5}))
                for _ in range(40)}
        self.assertEqual(arms, {"on", "off"})

    def test_the_studio_config_has_it_off(self):
        """The finish line for the studio: no excerpts, for any attempt."""
        c = json.loads((Path(swarmd.HERE) / "configs" / "openRouter-Studio-ab0a4a.json").read_text())
        self.assertIs(c["inject_corpus"], False)
        self.assertEqual(swarmd.mit_arm(c), "off")

    def test_the_template_documents_the_default(self):
        c = json.loads((Path(swarmd.HERE) / "config.example.json").read_text())
        self.assertIs(c["inject_corpus"], True)


class EarlierAttemptTests(unittest.TestCase):
    """Three lines about what went wrong, instead of a JSON dump of absolute paths."""

    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.root = Path(d.name)
        (self.root / "attempts").mkdir()
        p = patch.object(swarmd, "STATE", self.root)
        p.start()
        self.addCleanup(p.stop)

    def attempt(self, tid, name, phase, note, model="m:free", finished=1.0):
        d = self.root / "attempts" / name
        d.mkdir()
        (d / "attempt.json").write_text(json.dumps({
            "id": name, "task": {"id": tid}, "phase": phase, "note": note,
            "implementer": model, "finished": finished}))

    def test_one_line_per_attempt_with_the_class_and_the_failing_line(self):
        self.attempt("t1", "a", "tests_failed",
                     "repair budget\nFAIL: test_scores (tests.test_crosstown.T)\nAssertionError: 3 != 4",
                     model="qwen/qwen3-coder", finished=1_759_000_000)
        lines = swarmd.attempt_lines({"id": "t1"}).splitlines()
        self.assertEqual(len(lines), 1)
        self.assertIn("tests_failed on qwen/qwen3-coder", lines[0])
        self.assertIn("task", lines[0])
        self.assertIn("FAIL: test_scores", lines[0])

    def test_at_most_three_and_the_newest_of_them(self):
        for i in range(5):
            self.attempt("t1", f"a{i}", "no_change", f"note {i}", finished=float(i))
        lines = swarmd.attempt_lines({"id": "t1"}).splitlines()
        self.assertEqual(len(lines), 3)
        self.assertIn("note 4", lines[-1])
        self.assertIn("note 2", lines[0])

    def test_it_is_not_json_and_it_is_short(self):
        for i in range(3):
            self.attempt("t1", f"a{i}", "rejected", "x" * 4_000, finished=float(i))
        text = swarmd.attempt_lines({"id": "t1"})
        self.assertLessEqual(len(text), 1_200)
        self.assertFalse(text.lstrip().startswith(("[", "{")))
        self.assertTrue(text.startswith("- "))

    def test_the_evidence_path_is_not_repeated_at_the_model(self):
        self.attempt("t1", "a", "no_change",
                     "no implementation changes; evidence: /Users/x/swarm/state/attempts/a")
        line = swarmd.attempt_lines({"id": "t1"})
        self.assertNotIn("/Users/x", line)
        self.assertIn("no implementation changes", line)

    def test_a_parent_and_a_prerequisite_count_as_this_task(self):
        self.attempt("parent", "a", "rejected", "the parent failed", finished=1.0)
        self.attempt("dep", "b", "tests_failed", "the prerequisite failed", finished=2.0)
        self.attempt("stranger", "c", "rejected", "nothing to do with it", finished=3.0)
        text = swarmd.attempt_lines({"id": "t1", "parent": "parent", "depends_on": ["dep"]})
        self.assertIn("the parent failed", text)
        self.assertIn("the prerequisite failed", text)
        self.assertNotIn("nothing to do with it", text)

    def test_a_first_attempt_says_nothing(self):
        self.assertEqual(swarmd.attempt_lines({"id": "t1"}), "")

    def test_an_unfinished_attempt_is_not_a_result(self):
        d = self.root / "attempts" / "live"
        d.mkdir()
        (d / "attempt.json").write_text(json.dumps({"task": {"id": "t1"}, "phase": "implementing"}))
        self.assertEqual(swarmd.attempt_lines({"id": "t1"}), "")

    def test_an_unreadable_record_is_skipped(self):
        d = self.root / "attempts" / "broken"
        d.mkdir()
        (d / "attempt.json").write_text("{not json")
        self.attempt("t1", "ok", "rejected", "a real one")
        self.assertIn("a real one", swarmd.attempt_lines({"id": "t1"}))


class FailureClassTests(unittest.TestCase):
    """Whose problem a failure is. Everything downstream of this decides a task's life."""

    def test_a_wrong_change_is_the_tasks(self):
        for stage in ("tests_failed", "rejected", "weakened_tests"):
            self.assertEqual(swarmd.failure_class(stage, "whatever"), "task", stage)

    def test_running_out_of_rounds_is_the_harnesss(self):
        self.assertEqual(swarmd.failure_class("no_change", "no implementation changes"), "harness")
        self.assertEqual(swarmd.failure_class("agent_timeout", "exceeded 900s"), "harness")

    def test_editing_and_still_changing_nothing_is_the_models(self):
        self.assertEqual(swarmd.failure_class("no_change", "edits cancelled out", edited=True), "model")

    def test_a_provider_that_did_not_answer_is_not_the_model_answering_badly(self):
        self.assertEqual(swarmd.failure_class("model_error", "provider unavailable: 503"), "harness")
        self.assertEqual(swarmd.failure_class("model_error", "empty response"), "harness")
        self.assertEqual(swarmd.failure_class("model_error", "returned no usable JSON"), "model")

    def test_a_broken_baseline_a_moved_trunk_and_a_restart_are_the_harnesss(self):
        for stage in ("baseline", "conflict", "refused", "interrupted", "deferred"):
            self.assertEqual(swarmd.failure_class(stage, ""), "harness", stage)

    def test_a_reviewer_that_could_not_answer(self):
        self.assertEqual(swarmd.failure_class("review_error", "no usable JSON"), "model")
        self.assertEqual(swarmd.failure_class("review_error", "provider kept failing"), "harness")

    def test_the_first_failing_line_is_the_one_worth_repeating(self):
        self.assertEqual(swarmd.first_failing_line("ran 4 tests\nFAIL: test_x (a.B)\nmore"),
                         "FAIL: test_x (a.B)")
        # One line, so the whitespace pytest pads it with is collapsed.
        self.assertEqual(swarmd.first_failing_line("E   AssertionError: 3 != 4"),
                         "E AssertionError: 3 != 4")
        self.assertEqual(swarmd.first_failing_line("everything was fine"), "")


class PromptSizeTests(unittest.TestCase):
    """The review's finish line: the prompt, excluding the context pack, is at most 7 KB."""

    def test_the_prompt_around_the_code_is_small(self):
        lessons = [{"text": "Keep priority-queue updates behind one helper", "pinned": False}] * 6
        pitfalls = ([{"kind": "pitfall", "text": "the gate ran the whole suite"}] * 4 +
                    [{"kind": "shame", "model": "m:free", "stage": "rejected", "title": "t",
                      "text": "off-by-one at the last index",
                      "exhibit": "# src/other.py\n" + "+x = 1\n" * 200}] * 3)
        prompt = swarmd.IMPLEMENTER.format(
            goal="GOAL.md" + "\n".join(f"{i}. a priority" for i in range(1, 8)),
            spec=json.dumps({"task": "t1", "title": "Add onFoot walk state",
                             "acceptance": [{"id": "C1", "text": "it walks"}]}, indent=2),
            previous="\n".join("- 09-26 11:32 no_change on qwen/qwen3-coder: harness" for _ in range(3)),
            playbook=learn.format_playbook(lessons, pitfalls, {"games/crosstown/game.js"}),
            corpus="", context="", test_cmd="python3 -m unittest discover -s tests",
            title="Add onFoot walk state with exit/enter vehicle for Crosstown")
        self.assertLessEqual(len(prompt), 7_000, f"{len(prompt)} chars around the code")

    def test_the_context_pack_is_the_only_thing_allowed_to_be_large(self):
        placeholders = set(re.findall(r"\{(\w+)\}", swarmd.IMPLEMENTER))
        self.assertIn("context", placeholders)
        self.assertLess(len(swarmd.IMPLEMENTER), 3_000, "the template itself is boilerplate")


if __name__ == "__main__":
    unittest.main()
