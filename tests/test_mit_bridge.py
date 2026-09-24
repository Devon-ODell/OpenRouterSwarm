"""Offline tests for MIT retrieval, the MIT-corpus experiment, judge wiring, lenient parsing,
the final-answer round and the editor bridge. No real API calls."""
import contextlib
import io
import json
import os
from pathlib import Path
import random
import re
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import flint
from swarm import bridge, corpus_index, experiment, learn, mit_corpus, swarmd, workflow

SKILLS = Path("MIT OCW Courses") / "agent-skills"


def mini_corpus(root):
    """A tiny copy of the agent-skills layout: one course, one lecture card, one source page,
    one raw OCW export page and unrelated notes (so BM25 term weights behave)."""
    s = root / SKILLS
    (s / "flint" / "cards").mkdir(parents=True)
    docs = s / "mit-6-006-algorithms" / "references" / "documents"
    docs.mkdir(parents=True)
    (s / "flint" / "index.json").write_text(json.dumps({"courses": {"mit-6-006-algorithms": {
        "number": "6.006", "title": "Introduction to Algorithms", "term": "Spring 2020"}}}))
    (s / "mit-6-006-algorithms" / "references" / "accuracy.md").write_text("# Accuracy\n\nKnown errors.\n")
    (s / "flint" / "cards" / "mit-6-006-algorithms.md").write_text(
        "# 6.006 Introduction to Algorithms: lecture cards\n\nIntro paragraph about the cards and "
        "how evidence links work for the course collection.\n\n"
        "## 6.006 lecture 13: Dijkstra\n\nDijkstra relaxes edges in order of distance using a "
        "priority queue; nonnegative weights are required for correctness of the greedy order.\n\n"
        "Evidence for 6.006 lecture 13: [p. 2](../../mit-6-006-algorithms/references/documents/lec13.md#page-2)\n")
    (docs / "lec13.md").write_text(
        "# Lecture 13: Dijkstra\n\n## Page 2\n\nDijkstra priority queue relax edges: pop the vertex "
        "with smallest estimate, relax outgoing edges, decrease key in the priority queue. " * 3 + "\n")
    export = root / "MIT OCW Courses" / "Intro-To-Algorithms" / "resources"
    export.mkdir(parents=True)
    (export / "index.html").write_text("<html><body><p>Dijkstra priority queue lecture video page, "
                                       "relax edges, lecture listing and navigation.</p></body></html>")
    notes = root / "docs"
    notes.mkdir()
    for i, topic in enumerate(["sourdough starter hydration and flour", "tidal pools and anemones",
                               "bicycle gear ratios and cadence", "watercolor wash layering pigment",
                               "chess opening repertoire study plan", "orchid watering schedule roots"]):
        (notes / f"n{i}.md").write_text(f"# Note {i}\n\n" + (topic + " details. ") * 12)
    db = root / "corpus.db"
    corpus_index.build(root, db, verbose=False)
    return db


class CorpusCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "training"
        self.root.mkdir()
        self.db = mini_corpus(self.root)
        mit_corpus._catalog.cache_clear()


class MitCorpusTests(CorpusCase):
    def test_card_leads_with_labels_and_absolute_links(self):
        hits = mit_corpus.search("dijkstra priority queue relax edges", 3, self.db, self.root)
        card = hits[0]
        self.assertEqual(card["kind"], "card")
        self.assertEqual(card["course"], "6.006 Introduction to Algorithms (Spring 2020)")
        self.assertEqual(card["lecture"], "6.006 lecture 13: Dijkstra")
        doc = self.root / SKILLS / "mit-6-006-algorithms" / "references" / "documents" / "lec13.md"
        self.assertIn(f"]({doc}#page-2)", card["text"])
        self.assertTrue(Path(card["path"]).is_file())
        source = next(h for h in hits if h["kind"] == "source")
        self.assertEqual((source["lecture"], source["page"]), ("6.006 lecture 13: Dijkstra", 2))
        self.assertTrue(source["accuracy"].endswith("mit-6-006-algorithms/references/accuracy.md"))
        kinds = [h["kind"] for h in hits]
        if "export" in kinds:  # raw site pages only fill what better material left over
            self.assertEqual(kinds.index("export"), len(kinds) - 1)

    def test_prompt_injection_needs_a_matching_lecture_card(self):
        self.assertIn("lecture card", mit_corpus.study("dijkstra priority queue", 3, self.db, self.root,
                                                       require_card=True))
        self.assertTrue(mit_corpus.study("sourdough starter hydration", 3, self.db, self.root))
        self.assertEqual(mit_corpus.study("sourdough starter hydration", 3, self.db, self.root,
                                          require_card=True), "")

    def test_flint_study_tool_returns_openable_paths(self):
        with patch.object(flint, "CORPUS_DB", self.db), patch.dict(os.environ, {"FLINT_CORPUS_ROOT": str(self.root)}):
            out = flint.study("dijkstra priority queue")
        self.assertIn(f"file: {self.root / SKILLS / 'flint' / 'cards' / 'mit-6-006-algorithms.md'}", out)
        self.assertIn("6.006 lecture 13: Dijkstra", out)

    def test_missing_database_is_quiet(self):
        self.assertEqual(mit_corpus.search("dijkstra", 3, self.root / "absent.db", self.root), [])


class SwarmBase(CorpusCase):
    def setUp(self):
        super().setUp()
        for name in ("state", "logs"):
            (self.root / name).mkdir()
        for name, value in (("STATE", self.root / "state"), ("LOGS", self.root / "logs"), ("HERE", self.root)):
            p = patch.object(swarmd, name, value)
            p.start()
            self.addCleanup(p.stop)
        swarmd._stop.clear()

    def repo(self):
        repo = self.root / "repo"
        repo.mkdir()

        def git(*args):
            return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()
        git("init", "-b", "main")
        git("config", "user.email", "test@example.invalid")
        git("config", "user.name", "Test")
        (repo / "app.txt").write_text("baseline\n")
        git("add", ".")
        git("commit", "-m", "baseline")
        return repo, git

    def cfg(self, repo, **kw):
        return {"repo": str(repo), "base_branch": "main", "test_cmd": "true", "models": ["a:free", "b:free", "c:free"],
                "corpus_db": str(self.db), "corpus_root": str(self.root),
                "steps": {"architect": 0, "implementer": 2, "adversary": 2, "judge": 1, "decomposer": 1}, **kw}


def review(prompt, verdict="approve"):
    tree = re.search(r"GIT TREE TO REVIEW: (\w+)", prompt).group(1)
    ids = dict.fromkeys(re.findall(r'"id": "(C\d+)"', prompt))
    return json.dumps({"verdict": verdict, "tree": tree, "summary": "reviewed",
                       "checks": [{"criterion": i, "passed": True, "evidence": "tests"} for i in ids],
                       "findings": []})


JUDGE = json.dumps({"impact": 7, "creativity": 6, "quality": 8, "breakthrough": False, "why": "solid",
                    "lesson": "Keep priority-queue updates behind one helper so tests pin the order",
                    "follow_ups": []})


class ExperimentArmTests(SwarmBase):
    def run_task(self, share_on):
        repo, _ = self.repo()
        c = self.cfg(repo, mit_experiment={"enabled": True, "share_on": share_on})
        seen = {}

        def fake(prompt, cwd, cc, role, worker, budget, steps, model):
            seen.setdefault(role, []).append((prompt, cc))
            if role == "implementer":
                (cwd / "app.txt").write_text("dijkstra with a priority queue\n")
                return "done"
            if role == "adversary":
                return review(prompt)
            return JUDGE if role == "judge" else "ok"
        w = swarmd.Worker(0, c, swarmd.Queue(), Mock(), threading.Event())
        with patch.object(swarmd, "flint", side_effect=fake):
            ok, note = w.do_task({"id": "t1", "title": "Shortest paths", "detail": "Dijkstra priority queue relax edges"}, "goal")
        self.assertTrue(ok, note)
        rows = [r for r in swarmd._read(swarmd.STATE / "journal.jsonl") if r["event"] == "attempt"]
        return seen, rows, w

    def test_on_arm_sees_mit_excerpts_and_the_study_tool(self):
        seen, rows, _ = self.run_task(1.0)
        prompt, cc = seen["implementer"][0]
        self.assertIn("6.006 lecture 13: Dijkstra", prompt)
        self.assertNotEqual(cc.get("study"), False)
        self.assertEqual((rows[0]["mit"], rows[0]["injected"]), ("on", True))

    def test_off_arm_gets_neither_and_every_role_loses_the_tool(self):
        seen, rows, _ = self.run_task(0.0)
        self.assertNotIn("MIT OpenCourseWare", seen["implementer"][0][0])
        self.assertTrue(all(cc.get("study") is False for calls in seen.values() for _, cc in calls))
        self.assertEqual((rows[0]["mit"], rows[0]["injected"]), ("off", False))

    def test_landed_work_is_judged_and_its_lesson_joins_the_playbook(self):
        seen, rows, w = self.run_task(1.0)
        self.assertIn("judge", seen)
        judge_model = rows[0]["judge"]
        self.assertNotIn(judge_model, (rows[0]["implementer"], rows[0]["reviewer"]))
        self.assertEqual(rows[0]["scores"]["impact"], 7.0)
        self.assertGreater(rows[0]["reward"], 0.6)
        lessons, _ = w.ledger.playbook()
        self.assertIn("priority-queue updates", lessons[0]["text"])

    def test_arm_assignment(self):
        rng = random.Random(3)
        c = {"corpus_db": str(self.db)}
        draws = [swarmd.mit_arm(dict(c, mit_experiment={"share_on": 0.5}), rng) for _ in range(400)]
        self.assertTrue(150 < draws.count("on") < 250)
        self.assertEqual(swarmd.mit_arm(dict(c, mit_experiment={"enabled": False})), "on")
        self.assertEqual(swarmd.mit_arm({"corpus_db": str(self.root / "absent.db")}), "none")

    def test_flint_turn_hides_study_and_counts_its_use(self):
        budget = Mock()
        budget.check.return_value = (True, 0, "ok")
        budget.cap, budget.reserve = 50, 10
        p = Mock(returncode=0)

        @contextlib.contextmanager
        def fake_process(cmd, **kw):
            self.assertEqual(kw["env"]["FLINT_CORPUS_DB"], swarmd.NO_CORPUS)
            kw["stderr"].write("round 1/3: requesting m\ntool: study\ntool: read_file\ntool: study\n")
            kw["stderr"].flush()
            p.communicate.return_value = ("answer", None)
            yield p
        swarmd.count_study("w9", reset=True)
        with patch.object(swarmd, "process", fake_process):
            swarmd.flint("q", self.root, {"study": False, "python": sys.executable}, "judge", "w9", budget, 3, "m")
        self.assertEqual(swarmd.count_study("w9"), 2)
        turn = [r for r in swarmd._read(swarmd.STATE / "journal.jsonl") if r["event"] == "turn"][-1]
        self.assertEqual(turn["study_calls"], 2)


class ExperimentStatsTests(unittest.TestCase):
    def write(self, rows):
        tmp = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
        self.addCleanup(os.unlink, tmp.name)
        tmp.write("".join(json.dumps({"event": "attempt", "t": 1, **r}) + "\n" for r in rows) + "not json\n")
        tmp.close()
        return tmp.name

    def test_summary_compares_arms_honestly(self):
        rows = ([{"mit": "on", "stage": "accepted", "reward": 0.8, "implementer": "a", "study_calls": 1,
                  "scores": {"impact": 6, "creativity": 5, "quality": 7}}] * 8
                + [{"mit": "on", "stage": "tests_failed", "reward": 0.0, "implementer": "a"}] * 2
                + [{"mit": "off", "stage": "accepted", "reward": 0.6, "implementer": "a"}] * 3
                + [{"mit": "off", "stage": "rejected", "reward": 0.0, "implementer": "b"}] * 7
                + [{"mit": "none", "stage": "accepted"}, {"event": "turn"}])
        s = experiment.summary(self.write(rows))
        self.assertEqual((s["on"]["attempts"], s["on"]["landed"], s["off"]["landed"]), (10, 8, 3))
        self.assertEqual(s["difference"]["landed_rate"], 0.5)
        self.assertAlmostEqual(s["p_value"], experiment.fisher(8, 2, 3, 7), places=4)
        self.assertIn("Too few attempts", s["verdict"])
        self.assertEqual(s["by_model"]["b"]["off"]["attempts"], 7)
        md = experiment.markdown(s)
        self.assertIn("| landed | 8 | 3 |", md)
        self.assertIn("observational", md)

    def test_reference_values(self):
        self.assertAlmostEqual(experiment.fisher(3, 1, 1, 3), 0.4857, places=4)
        lo, hi = experiment.wilson(8, 10)
        self.assertAlmostEqual(lo, 0.4902, places=4)
        self.assertAlmostEqual(hi, 0.9433, places=4)
        self.assertIn("No comparison yet", experiment.summary("/nonexistent/journal")["verdict"])


class LenientParsingTests(unittest.TestCase):
    TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
    ACC = [{"id": "C1", "text": "x"}]

    def body(self, **kw):
        return {"verdict": "approve", "tree": self.TREE, "summary": "ok",
                "checks": [{"criterion": "C1", "passed": True, "evidence": "test_x"}], "findings": [], **kw}

    def test_fences_prose_casing_and_short_tree_are_forgiven(self):
        text = "Here is my review:\n```json\n" + json.dumps(self.body(verdict="APPROVE", tree=self.TREE[:10])) + "\n```"
        self.assertEqual(workflow.parse_review(text, self.TREE, self.ACC)["tree"], self.TREE)
        missing = self.body()
        del missing["tree"]
        self.assertEqual(workflow.parse_review(json.dumps(missing), self.TREE, self.ACC)["verdict"], "approve")

    def test_a_different_tree_or_contradiction_is_still_refused(self):
        with self.assertRaisesRegex(ValueError, "different Git tree"):
            workflow.parse_review(json.dumps(self.body(tree="deadbeefdeadbeef")), self.TREE, self.ACC)
        bad = self.body(findings=[{"severity": "blocker", "path": "a.py", "line": "3", "issue": "x", "verification": "y"}])
        with self.assertRaisesRegex(ValueError, "contradicts"):
            workflow.parse_review(json.dumps(bad), self.TREE, self.ACC)
        with self.assertRaises(ValueError):
            workflow.parse_review("APPROVE: looks fine", self.TREE, self.ACC)

    def test_json_array_survives_brackets_in_prose(self):
        self.assertEqual(learn.json_array('See [docs](x).\n```json\n[{"title": "a"}]\n```\n[done]'), [{"title": "a"}])
        self.assertEqual(learn.json_array("Nothing to split: []"), [])
        self.assertIsNone(learn.json_array("no list"))


def agent(headless=True):
    a = flint.Agent.__new__(flint.Agent)
    a.headless, a.read_only, a.yolo, a.always = headless, True, False, set()
    a.messages, a.model, a.throttle, a.last_prompt_tokens, a.corpus = [], "m:free", Mock(), 0, False
    return a


class FinalAnswerTests(unittest.TestCase):
    def test_headless_turn_answers_without_tools_after_the_step_limit(self):
        a = agent()
        seen = []

        def complete():
            seen.append(a._request_kwargs().get("tool_choice"))
            if len(seen) == 1:
                return "", [{"id": "c1", "name": "read_file", "args": '{"path": "x"}'}]
            return '{"verdict": "approve"}', []
        a.complete = complete
        a.run_tool = Mock(return_value="file text")
        with patch.object(flint, "MAX_STEPS", 1), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(a.turn("review this"), '{"verdict": "approve"}')
        self.assertEqual(seen, [None, "none"])
        self.assertIn("Do not call any tools", a.messages[-2]["content"])
        self.assertIsNone(a._request_kwargs().get("tool_choice"))

    def test_interactive_turn_still_stops_at_the_limit(self):
        a = agent(headless=False)
        a.complete = Mock(return_value=("", [{"id": "c1", "name": "read_file", "args": "{}"}]))
        a.run_tool = Mock(return_value="ok")
        with patch.object(flint, "MAX_STEPS", 1), self.assertRaises(flint.StepLimitReached):
            a.turn("task")

    def test_provider_failing_mid_reply_is_retried_then_reported_as_unavailable(self):
        a = agent()
        err = flint.IncompleteResponse("x", "error")
        a._stream_once = Mock(side_effect=[err, ("fine", [])])
        with patch.object(flint.time, "sleep"), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(a.complete(), ("fine", []))
        a._stream_once = Mock(side_effect=[err, err, err])
        with patch.object(flint.time, "sleep"), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(flint.ProviderUnavailable):
                a.complete()
        a._stream_once = Mock(side_effect=flint.IncompleteResponse("x", "length"))
        with self.assertRaises(flint.IncompleteResponse):
            a.complete()


class BridgeTests(SwarmBase):
    def setUp(self):
        super().setUp()
        self.config = self.root / "config.json"
        self.config.write_text(json.dumps({"repo": "__REPO__", "models": ["a:free", "b:free", "c:free", "paid/x"],
                                           "corpus_db": str(self.db), "python": sys.executable}))
        # bridge.py runs as a script and imports swarmd as a top-level module: patch that copy
        # too, or its state would land in the real swarm/state folder.
        self.sw = bridge.swarmd
        patches = [(swarmd, "CONFIG", self.config), (bridge, "CONSULT", self.root / "consult.json")]
        patches += [(self.sw, name, value) for name, value in (
            ("CONFIG", self.config), ("STATE", self.root / "state"), ("LOGS", self.root / "logs"), ("HERE", self.root))]
        for target, name, value in patches:
            p = patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)

    def run_bridge(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = bridge.main(list(argv))
        return rc, [json.loads(l) for l in out.getvalue().splitlines() if l.strip()]

    def test_selection_context_names_the_file_and_lines(self):
        repo, _ = self.repo()
        (repo / "m.py").write_text("a = 1\nb = 2\nc = 3\n")
        self.assertEqual(bridge.selection_context(str(repo), str(repo / "m.py"), 2, 3), ("m.py lines 2-3", "b = 2\nc = 3"))

    def test_task_for_the_code_you_point_at_is_queued_for_that_repo(self):
        repo, _ = self.repo()
        (repo / "m.py").write_text("def f(x):\n    return x[:-1]\n")
        rc, out = self.run_bridge("task", "--repo", str(repo), "--title", "Fix f", "--detail",
                                  "f drops the last element", "--file", str(repo / "m.py"), "--start", "1", "--end", "2")
        self.assertEqual(rc, 0)
        self.assertTrue(out[0]["ok"])
        self.assertFalse(out[0]["is_target"])
        task = self.sw.Queue().pending()[0]
        self.assertEqual((task["origin"], task["priority"], task["acceptance"]), ("cursor", 1, ["f drops the last element"]))
        self.assertIn("m.py lines 1-2", task["detail"])
        self.assertIn("return x[:-1]", task["detail"])
        self.assertTrue(str(self.sw.STATE).startswith(str(self.root / "state" / "repo-")))

    def test_ask_streams_answers_then_a_synthesis_and_votes_reinforce(self):
        repo, _ = self.repo()
        calls = []

        def fake_run(prompt, repo_, model, steps, study=True, timeout=300, on_progress=None):
            calls.append((prompt, model, study))
            on_progress("round 1/8: requesting " + model)
            return {"ok": True, "model": model, "text": f"answer from {model}", "secs": 1, "study_calls": 1,
                    "tool_calls": 2, "requests": 3, "exit": 0}
        with patch.object(bridge, "run_flint", side_effect=fake_run), patch.dict(os.environ, {"OPENROUTER_API_KEY": "k"}):
            rc, events = self.run_bridge("ask", "--repo", str(repo), "--question", "why?", "--models", "2")
        self.assertEqual(rc, 0)
        kinds = [e["event"] for e in events]
        self.assertEqual(kinds[0], "context")
        self.assertEqual(kinds.count("answer"), 2)
        self.assertGreaterEqual(kinds.count("progress"), 3)
        self.assertEqual(kinds[-2:], ["synthesis", "done"])
        self.assertNotIn("paid/x", events[0]["models"])
        self.assertTrue(events[0]["study"])
        self.assertIn("answer from", calls[-1][0])  # the merger sees every answer
        rc, out = self.run_bridge("vote", "--model", "a:free", "--useful", "1")
        self.assertEqual(out[0]["leaderboard"][0]["arm"], "a:free")

    def test_grind_command_is_shell_quoted(self):
        rc, out = self.run_bridge("grind-cmd", "--repo", str(self.root), "--goal", "it's a game")
        self.assertIn("grind", out[0]["command"])
        self.assertIn("'it'\"'\"'s a game'", out[0]["command"])


if __name__ == "__main__":
    unittest.main()
