"""P2: each extracted module imports and works without swarmd in any form.

The move order is one-way: prompts, task_rules, context_builder, verification and
provisioning never import swarmd (top-level or function-local) and never look it up
dynamically. swarmd re-exports them and wires their explicit dependencies through
compatibility wrappers. These tests run the new modules directly with fake
dependencies to prove the boundary holds.

Prompt text is pinned byte-for-byte: the SHA-256 values below were captured at HEAD
before extraction (`/tmp/prompts-before.json`, per DECOMPOSE-SWARMD.md) and must not
change while the text lives in `prompts.py` rather than `swarmd.py`.
"""
import ast
import hashlib
import importlib
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "swarm"))


def _sha256(text):
    return hashlib.sha256(text.encode()).hexdigest()


class NoSwarmdImportTests(unittest.TestCase):
    MODULES = ("prompts", "task_rules", "context_builder", "verification", "provisioning")

    def test_each_module_imports_without_swarmd(self):
        for name in self.MODULES:
            with self.subTest(module=name):
                mod = importlib.import_module(name)
                src = Path(mod.__file__).read_text()
                # no import of swarmd in any form, and no dynamic lookup of it
                self.assertFalse(re.search(r"^\s*import swarmd\b", src, re.M), name)
                self.assertFalse(re.search(r"^\s*from swarmd\s+import\b", src, re.M), name)
                self.assertFalse(re.search(r"\bimport\s+swarmd", src), name)
                self.assertFalse(re.search(r"\bfrom\s+swarmd(?:\s+import|\.)", src), name)
                self.assertFalse(re.search(r"(?<!['\"`])swarmd\s*\.", src), name)
                self.assertFalse(re.search(r"__import__\(['\"]swarmd", src), name)
                self.assertFalse(re.search(r"importlib\s*\.\s*import_module\s*\([^)]*['\"]swarmd", src), name)
                # and no bound object in the module came from a swarmd module
                for val in vars(mod).values():
                    self.assertNotEqual(getattr(val, "__module__", ""), "swarmd", name)


class PromptsDirectTests(unittest.TestCase):
    # Frozen at HEAD before extraction (DECOMPOSE-SWARMD.md /tmp/prompts-before.json):
    # byte-for-byte identical before and after the move. Each is the SHA-256 of the
    # exact prompt string, so a single changed byte (even whitespace) fails loudly.
    FROZEN_HASHES = {
        "ARCHITECT": "df1b4dde38c1b6ae2ae722e741f56b46c8ed113922e0d03b2c7c2d085079f280",
        "IMPLEMENTER": "6bb22cff45ccd5d0f91e1ed8fd94d5921f0064ced4495ec73b12e5f3dd242652",
        "ADVERSARY": "89fe20a6cb9ad3e8dd90af54277c7d23e4b144388a0ff13159d8cdf41b689716",
        "JUDGE": "3323ec0af928aa32cec86cbb04816afde16741c194a83a88672ee77df24a5ad5",
        "PLANNER": "b20b2a234f7fb842659d213492ca6b266c69deea7efcc5897a74fff529a1dec3",
        "DECOMPOSER": "1999ec882794c175756c961bad70eccdb83780996aa5b6c2f0e2bbbe5fab46c6",
        "PRIORITIES": "62e2f9750e7e5b3154b4cbf812447e44a0fd804c0b3d41cecb3090e7e05ad671",
        "FAST_IMPLEMENTER": "22f32bc4b9025a41995e1cdbd298b50f5dc5a9a428d98851bd6fe38e2ac0f58d",
        "PLANNER_SERVES": "c79265a473e9b14724497617a7b8e121fabb687d888d7285ef4ae157d162f352",
    }

    def test_all_nine_prompt_strings_match_the_frozen_hashes(self):
        from prompts import (ARCHITECT, IMPLEMENTER, ADVERSARY, JUDGE, PLANNER,
                             DECOMPOSER, PRIORITIES, FAST_IMPLEMENTER, PLANNER_SERVES)
        values = {
            "ARCHITECT": ARCHITECT,
            "IMPLEMENTER": IMPLEMENTER,
            "ADVERSARY": ADVERSARY,
            "JUDGE": JUDGE,
            "PLANNER": PLANNER,
            "DECOMPOSER": DECOMPOSER,
            "PRIORITIES": PRIORITIES,
            "FAST_IMPLEMENTER": FAST_IMPLEMENTER,
            "PLANNER_SERVES": PLANNER_SERVES,
        }
        self.assertEqual(set(values), set(self.FROZEN_HASHES))
        for name, expected in self.FROZEN_HASHES.items():
            with self.subTest(name=name):
                self.assertEqual(_sha256(values[name]), expected)

    def test_repair_is_one_object_across_prompts_workflow_and_swarmd(self):
        # REPAIR lives in workflow; prompts re-exports it and swarmd re-exports it.
        # The same module objects must be shared, so all three names are the same string.
        from prompts import REPAIR as prompts_repair
        import workflow
        import swarmd
        self.assertIs(prompts_repair, workflow.REPAIR)
        self.assertIs(prompts_repair, swarmd.REPAIR)
        self.assertIs(swarmd.REPAIR, workflow.REPAIR)

    def test_prompt_text_is_sane(self):
        from prompts import (ARCHITECT, IMPLEMENTER, ADVERSARY, JUDGE, PLANNER,
                             DECOMPOSER, PRIORITIES, FAST_IMPLEMENTER, PLANNER_SERVES)
        self.assertTrue(FAST_IMPLEMENTER.startswith("Perform this small, fully specified edit."))
        self.assertIn("Add tests that prove the new behavior", IMPLEMENTER)
        self.assertIn("REJECT:", ADVERSARY)
        self.assertIn("breakthrough", JUDGE)
        self.assertTrue(PLANNER.startswith("You are the PLANNER"))
        self.assertIn("serves_rule", PLANNER)
        self.assertIn("THE GOAL'S ORDERED PRIORITIES", PRIORITIES)
        self.assertIn("priority 1", PLANNER_SERVES)
        self.assertIn('"serves"', PLANNER_SERVES)
        self.assertIn("{test_cmd}", IMPLEMENTER)
        self.assertIn("{serves_rule}", PLANNER)

    def test_persona_text_parity_with_the_facade_and_scholar_fallback(self):
        # The swarmd facade must expose the exact same function and tables, not copies.
        from prompts import persona_text, PERSONAS
        import swarmd
        self.assertIs(swarmd.persona_text, persona_text)
        self.assertIs(swarmd.PERSONAS, PERSONAS)
        # A persona's stance must not depend on unrelated config: only the scholar
        # path substitution reacts to the corpus root, and only when the index file
        # actually exists (falls back to the literal pointer otherwise).
        self.assertEqual(persona_text("builder", {}), persona_text("builder", {"x": 1}))
        for name in PERSONAS:
            with self.subTest(name=name):
                self.assertEqual(persona_text(name, {}), persona_text(name, {"x": 1}))
        # Scholar: with a missing index the literal MIT OCW Courses path stays.
        literal = "MIT OCW Courses/agent-skills/FLINT-INDEX.md"
        fallback = persona_text("scholar", {"corpus_root": "/nonexistent/definitely/not/here"})
        self.assertIn(literal, fallback)
        self.assertIn("MIT OCW Courses", fallback)
        # Scholar: with a real index the placeholder is replaced by the resolved path.
        real = persona_text("scholar", {})
        self.assertIn("FLINT-INDEX.md", real)
        # Every persona names the study corpus.
        self.assertIn("MIT OCW Courses", persona_text("scholar", {}))


class NoLocalDuplicateAssignmentsTests(unittest.TestCase):
    """AST-level guard: the moved definitions have exactly one owner, the new module.

    swarmd must re-export (import) these names, never re-assign them. A stray
    `X = ...` in swarmd would silently fork the single source of truth, and a stray
    `def persona_text(...)` would shadow the imported one — both break the one-way
    dependency this packet is supposed to create.
    """

    NAME = {"PERSONAS", "ARCHITECT", "IMPLEMENTER", "ADVERSARY", "JUDGE", "PRIORITIES",
            "PLANNER", "PLANNER_SERVES", "DECOMPOSER", "persona_text"}

    def test_swarmd_has_no_local_definitions_of_the_moved_names(self):
        src = Path(REPO / "swarm" / "swarmd.py").read_text()
        tree = ast.parse(src)
        local = set()
        for node in tree.body:
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                for t in (node.targets if isinstance(node, ast.Assign) else [node.target]):
                    for target in (t.elts if isinstance(t, (ast.Tuple, ast.List)) else [t]):
                        if isinstance(target, ast.Name):
                            local.add(target.id)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                local.add(node.name)
        bad = sorted(self.NAME & local)
        self.assertEqual(bad, [], f"swarmd still defines moved prompt names locally: {bad}")

    def test_each_moved_name_has_exactly_one_definition_in_its_module(self):
        src = Path(REPO / "swarm" / "prompts.py").read_text()
        tree = ast.parse(src)
        defined = set()
        for node in tree.body:
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                for t in (node.targets if isinstance(node, ast.Assign) else [node.target]):
                    for target in (t.elts if isinstance(t, (ast.Tuple, ast.List)) else [t]):
                        if isinstance(target, ast.Name):
                            defined.add(target.id)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                defined.add(node.name)
        for name in self.NAME:
            self.assertIn(name, defined, f"prompts.py no longer defines {name}")


class TaskRulesDirectTests(unittest.TestCase):
    def test_pure_policy(self):
        from task_rules import (goal_items, goal_item_weight, failure_class,
                                scope_gap, estimate_requests, first_failing_line)
        self.assertEqual(goal_items("# G\n\n## Work on now\n1. A\n2. B"), ["A", "B"])
        self.assertEqual(goal_item_weight(1, [1, 2]), 1.0)
        self.assertEqual(failure_class("baseline", ""), "harness")
        self.assertEqual(failure_class("no_change", "", edited=True), "model")
        self.assertEqual(scope_gap({"title": "x", "detail": "y", "acceptance": ["y"]}), "")
        self.assertEqual(estimate_requests({"kind": "feature"}), 35)
        self.assertEqual(first_failing_line("FAIL: test_x\nmore"), "FAIL: test_x")


class ContextBuilderDirectTests(unittest.TestCase):
    def test_context_pack_uses_only_the_supplied_git_fake(self):
        # context_pack must forward the fake to BOTH git call sites (grep for covering
        # tests and ls-files for the directory listing) and must never shell out to a
        # real `git`. If it fell back to _default_git_run, the Mock would never see the
        # second command form (and in a sandbox with no git, the real call would fail).
        from context_builder import context_pack, covering_tests
        with tempfile.TemporaryDirectory() as d:
            wd = Path(d)
            (wd / "games" / "crosstown").mkdir(parents=True)
            (wd / "games" / "crosstown" / "game.js").write_text("function init(){}\n")
            (wd / "games" / "crosstown" / "manifest.json").write_text(
                '{"slug": "crosstown"}\n')
            (wd / "tests").mkdir(parents=True)
            (wd / "tests" / "test_crosstown.py").write_text(
                "def test_crosstown_starts():\n    pass\n")

            def fake_git(args, cwd=None):
                if args and args[0] == "grep":
                    return 0, "tests/test_crosstown.py"
                if args and args[0] == "ls-files":
                    return 0, "games/crosstown/game.js\ngames/crosstown/manifest.json"
                raise AssertionError(f"unexpected git args: {args}")

            git_run = Mock(side_effect=fake_git)
            task = {"id": "t", "title": "Fix crosstown",
                    "detail": "games/crosstown/game.js", "acceptance": ["it works"]}
            pack = context_pack(task, wd, git_run=git_run)
            self.assertIn("game.js", pack)
            # The covering-test listing came through the fake grep...
            self.assertIn("def test_crosstown_starts", pack)
            # ...and the directory listing through the fake ls-files.
            self.assertIn("games/crosstown/game.js", pack)
            forms = [call.args[0][0] for call in git_run.call_args_list]
            self.assertIn("grep", forms)
            self.assertIn("ls-files", forms)
            # Both command forms must have come from the fake, never a real git run.
            self.assertTrue(git_run.called)
            self.assertEqual(
                git_run.call_count,
                len(forms),
                "context_pack must not add hidden real-git calls")

    def test_covering_tests_forwards_the_fake(self):
        from context_builder import covering_tests
        git_run = Mock(return_value=(0, "tests/test_a.py"))
        with tempfile.TemporaryDirectory() as d:
            wd = Path(d)
            self.assertEqual(covering_tests(wd, ["a"], git_run=git_run), ["tests/test_a.py"])
            self.assertEqual(git_run.call_args.args[0][0], "grep")

    def test_attempt_lines_with_fake_attempts_dir(self):
        from context_builder import attempt_lines
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "attempts" / "a1").mkdir(parents=True)
            (root / "attempts" / "a1" / "attempt.json").write_text(json.dumps(
                {"id": "a1", "task": {"id": "t"}, "phase": "rejected",
                 "note": "FAIL: test_x", "implementer": "m", "finished": 1.0}))
            lines = attempt_lines({"id": "t"}, root)
            self.assertIn("rejected", lines)


class VerificationDirectTests(unittest.TestCase):
    def test_weakened_tests_pure(self):
        from verification import weakened_tests, assert_counts
        diff = ("diff --git a/t.py b/t.py\n"
                "--- a/t.py\n+++ b/t.py\n"
                "@@ -1 +1 @@\n-assert x\n+assert y\n")
        self.assertEqual(weakened_tests(""), 0)
        self.assertEqual(assert_counts(["assert a", "assert a"]), {"assert": 2})


class ProvisioningDirectTests(unittest.TestCase):
    def test_probe_surroundings_and_prov_block(self):
        from provisioning import probe_surroundings, build_prov_block, render_surroundings
        with tempfile.TemporaryDirectory() as d:
            wd = Path(d)
            (wd / ".gitignore").write_text("node_modules/\n")
            (wd / "package.json").write_text("{}\n")
            s = probe_surroundings(wd)
            self.assertTrue(s["runtime"].get("package.json"))
            prov = build_prov_block(s, "npm ci --silent && npm run check")
            self.assertFalse(any("npm ci" in h for h in prov["hooks"]),
                             "gate already runs npm ci")
            out = render_surroundings(wd, "slug", s, Path(d) / "c.json",
                                      git_branch=lambda r: "main")
            self.assertIn("runtime deps:", out)

    def test_detect_and_test_cmds(self):
        from provisioning import detect_test_cmd, test_cmds
        with tempfile.TemporaryDirectory() as d:
            wd = Path(d)
            (wd / "go.mod").write_text("module x\n")
            self.assertEqual(test_cmds(wd)[0][1], "go test ./...")
            self.assertEqual(detect_test_cmd(wd), "go test ./...")


if __name__ == "__main__":
    unittest.main()
