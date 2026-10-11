"""The code a task is about, put in the prompt instead of left for the model to go and find.

The implementer prompt was a median 16.6 KB with no code in it from the files the task touches,
so a turn of 12 tool rounds went 6 to 11 rounds on list_files, read_file and bash before the
first edit could happen. Everything the pack contains comes from paths the task names itself.
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import flint
from swarm import swarmd


class ContextPackTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.wd = Path(d.name)
        self.write("games/crosstown/manifest.json",
                   json.dumps({"slug": "crosstown", "targets": {"score": 900}}, indent=2) + "\n")
        self.write("games/crosstown/NOTES.md",
                   "".join(f"note line {i}\n" for i in range(1, 121)))
        self.write("games/crosstown/game.js",
                   "function init(seed) {\n  const s = { rng: seed };\n  return s;\n}\n"
                   "function step(state, input) {\n  return state;\n}\n")
        self.write("tests/test_crosstown.py",
                   "import unittest\n\n\nclass T(unittest.TestCase):\n"
                   "    def test_crosstown_starts_on_foot(self):\n        pass\n\n"
                   "    def test_crosstown_scores(self):\n        pass\n")
        self.write("tests/test_unrelated.py",
                   "def test_nothing_to_do_with_it():\n    pass\n")
        self.git("init", "-b", "main")
        self.git("config", "user.email", "t@example.invalid")
        self.git("config", "user.name", "T")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "baseline")

    def write(self, rel, text):
        p = self.wd / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return p

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.wd), *args], check=True,
                              capture_output=True, text=True).stdout

    def task(self, title, detail="", acceptance=None):
        return {"id": "t1", "title": title, "detail": detail,
                "acceptance": acceptance or ["it works"]}

    def pack(self, task, **kw):
        return swarmd.context_pack(task, self.wd, **kw)

    # ------------------------------------------------------- what it collects

    def test_a_file_the_task_names_is_in_the_prompt(self):
        pack = self.pack(self.task("Fix init", "games/crosstown/game.js has the wrong seed"))
        self.assertIn("--- games/crosstown/game.js (7 lines) ---", pack)
        self.assertIn("function init(seed) {", pack)

    def test_paths_come_from_the_acceptance_criteria_too(self):
        pack = self.pack(self.task("Retarget", acceptance=["games/crosstown/manifest.json sets score 1200"]))
        self.assertIn("manifest.json", pack)
        self.assertIn('"targets"', pack)

    def test_line_numbers_match_read_file_so_old_str_can_be_copied(self):
        """The one thing that makes the pack usable: an edit_file old_str copied out of it."""
        pack = self.pack(self.task("Fix", "games/crosstown/game.js"))
        with patch.object(flint, "_p", lambda p: self.wd / p):
            shown = flint.read_file("games/crosstown/game.js")
            self.assertIn(shown, pack)
            # And the copy round-trips: the text without numbering is what edit_file matches.
            body = "\n".join(line.split("\t", 1)[1] for line in shown.splitlines())
            self.assertIn("Edited", flint.edit_file("games/crosstown/game.js", body, "// replaced\n"))

    def test_a_game_directory_brings_its_manifest_notes_and_logic(self):
        pack = self.pack(self.task("Add onFoot walk state to games/crosstown/"))
        self.assertIn("games/crosstown/manifest.json", pack)
        self.assertIn("games/crosstown/NOTES.md (first 60 of 120 lines)", pack)
        self.assertIn("note line 60", pack)
        self.assertIn("note line 1\n", pack.replace("\t", "\n"))
        self.assertNotIn("note line 61", pack)
        self.assertIn("games/crosstown/game.js", pack)

    def test_a_long_file_becomes_an_outline_with_line_numbers(self):
        self.write("games/crosstown/game.js",
                   "function init(seed) {\n" + "  // filler\n" * 500 +
                   "}\nconst TABLE = [1, 2, 3];\nfunction step(s) { return s; }\n")
        pack = self.pack(self.task("Fix games/crosstown/game.js"))
        self.assertIn("definitions only, read_file for the rest", pack)
        self.assertIn("    1\tfunction init(seed) {", pack)
        self.assertIn("const TABLE", pack)
        self.assertIn("function step(s)", pack)
        self.assertNotIn("// filler", pack)

    def test_the_covering_test_module_is_named_with_its_test_names(self):
        pack = self.pack(self.task("Add onFoot walk state to games/crosstown/"))
        self.assertIn("tests/test_crosstown.py", pack)
        self.assertIn("def test_crosstown_starts_on_foot", pack)
        self.assertIn("run this module, not the whole suite", pack)
        self.assertNotIn("test_nothing_to_do_with_it", pack)

    def test_the_directory_listing_shows_what_else_is_there(self):
        pack = self.pack(self.task("Fix games/crosstown/game.js"))
        self.assertIn("--- files in games/crosstown ---", pack)
        self.assertIn("games/crosstown/manifest.json", pack)

    def test_a_file_is_shown_once_however_many_ways_it_is_named(self):
        pack = self.pack(self.task("games/crosstown/game.js and games/crosstown/ both",
                                   "games/crosstown/game.js again"))
        self.assertEqual(pack.count("--- games/crosstown/game.js"), 1)

    # ------------------------------------------------------- what it leaves out

    def test_a_path_that_does_not_exist_is_skipped(self):
        pack = self.pack(self.task("Create games/newgame/game.js", "it does not exist yet"))
        self.assertNotIn("--- games/newgame/game.js", pack)

    def test_a_task_naming_no_files_gets_no_pack(self):
        self.assertEqual(self.pack(self.task("Improve the studio's word lists")), "")

    def test_prose_that_looks_like_a_path_is_not_read(self):
        """Sentence-ending periods and bare words must not become file reads."""
        self.assertEqual(self.pack(self.task("Tidy up. Make it nice.", "See e.g. something")), "")

    def test_the_pack_is_capped(self):
        self.write("games/crosstown/manifest.json", json.dumps({"x": ["y"] * 20_000}))
        self.assertLess(len(self.pack(self.task("Fix games/crosstown/manifest.json"), limit=4_000)),
                        5_000)

    def test_a_file_too_wide_to_fit_is_outlined_rather_than_cut_in_half(self):
        """A game's logic is 300 lines of 200 characters. The line count alone is not a size,
        and half a file is worse than a map of one: the model cannot tell what it is missing."""
        wide = "".join(f"function f{i}(a) {{\n  return {'x + ' * 60}a;\n}}\n" for i in range(100))
        self.write("games/crosstown/game.js", wide)
        pack = self.pack(self.task("Fix games/crosstown/game.js"))
        self.assertIn("definitions only", pack)
        self.assertIn("    1\tfunction f0(a) {", pack)
        self.assertIn("  298\tfunction f99(a) {", pack)
        self.assertNotIn("chars not shown", pack)
        self.assertNotIn("x + x", pack, "the bodies are what would not fit")

    def test_a_long_rust_file_outlines_its_items_not_its_let_bindings(self):
        """SkyRing 2026-10-11: player.rs (1105 lines) outlined as 69 `let` lines and none of
        its 61 functions and types, so two implementers spent 15 requests looking for
        receive_hit and never edited."""
        filler = "".join(f"        let v{i} = {i};\n" for i in range(450))
        self.write("core/src/sim/player.rs",
                   "pub struct Player {\n    hp: f32,\n}\n\nimpl Player {\n"
                   "    pub fn update(&mut self) {\n" + filler + "    }\n\n"
                   "    pub(crate) fn receive_hit(&mut self, low: bool) -> HitResult {\n"
                   "        if low { HitResult::Dodged } else { HitResult::Hit }\n    }\n}\n\n"
                   "pub enum HitResult {\n    Hit,\n    Dodged,\n}\n")
        pack = self.pack(self.task("Pin how Player::receive_hit classifies hits",
                                   "Add core/tests/hits.rs; the logic is in core/src/sim/player.rs"))
        self.assertIn("definitions only", pack)
        for item in ("pub struct Player", "impl Player", "pub fn update", "pub enum HitResult"):
            self.assertIn(item, pack)
        self.assertNotIn("let v1 =", pack)
        # The function the task names comes in full, numbered as read_file numbers it.
        self.assertIn("(in full: the task names it)", pack)
        self.assertIn("  459\t    pub(crate) fn receive_hit(&mut self, low: bool) -> HitResult {", pack)
        self.assertIn("  460\t        if low { HitResult::Dodged } else { HitResult::Hit }", pack)

    def test_a_long_c_header_outlines_its_types_and_prototypes(self):
        self.write("core/include/api.h",
                   "#define API_VERSION 3\n" + "/* note */\n" * 450 +
                   "typedef struct ErWorld {\n    float x;\n} ErWorld;\n"
                   "ErHitResult er_receive_hit(ErCombat* combat, uint8_t low);\n")
        pack = self.pack(self.task("Fix core/include/api.h"))
        self.assertIn("#define API_VERSION 3", pack)
        self.assertIn("typedef struct ErWorld {", pack)
        self.assertIn("ErHitResult er_receive_hit(ErCombat* combat, uint8_t low);", pack)
        self.assertNotIn("/* note */", pack)

    def test_named_python_function_is_shown_whole_when_its_file_is_outlined(self):
        self.write("tools/big.py", "".join(f"def f{i}():\n    return {i}\n\n" for i in range(200))
                   + "def validate(doc):\n    errors = []\n    return errors\n\n\ndef after():\n"
                   "    pass\n")
        pack = self.pack(self.task("Make validate() in tools/big.py reject owner docs"))
        self.assertIn("  601\tdef validate(doc):", pack)
        self.assertIn("  603\t    return errors", pack)
        self.assertNotIn("  606\tdef after():\n  607", pack)

    def test_the_test_module_survives_a_long_file(self):
        """The reserved tail: which narrow test to run is the smallest, most useful section."""
        self.write("games/crosstown/game.js",
                   "".join(f"function f{i}(a) {{ return a; }}\n" for i in range(4_000)))
        pack = self.pack(self.task("Add onFoot walk state to games/crosstown/"), limit=6_000)
        self.assertIn("tests/test_crosstown.py", pack)
        self.assertIn("def test_crosstown_starts_on_foot", pack)

    def test_the_cap_stops_collecting_rather_than_truncating_the_lot(self):
        for i in range(30):
            self.write(f"src/mod{i}.py", "x = 1\n" * 200)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "more")
        names = " ".join(f"src/mod{i}.py" for i in range(30))
        pack = self.pack(self.task("Touch them all", names), limit=3_000)
        self.assertLess(len(pack), 4_000)
        self.assertIn("src/mod0.py", pack)

    def test_a_directory_that_is_not_a_git_repository_is_not_an_error(self):
        plain = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(plain, ignore_errors=True))
        (plain / "a.py").write_text("x = 1\n")
        pack = swarmd.context_pack(self.task("Fix a.py"), plain)
        self.assertIn("--- a.py (1 lines) ---", pack)

    # ------------------------------------------------------------ the prompts

    def test_the_implementer_prompt_carries_it(self):
        pack = self.pack(self.task("Fix games/crosstown/game.js"))
        prompt = swarmd.IMPLEMENTER.format(goal="g", spec="s", previous="", playbook="",
                                           corpus="", context=pack, test_cmd="true",
                                           title="Fix games/crosstown/game.js")
        self.assertIn("function init(seed) {", prompt)
        self.assertIn("CODE THIS TASK TOUCHES", prompt)
        # The task's own contract is the last thing the model reads, and the code comes before
        # the reference material rather than after it.
        self.assertLess(prompt.index("CODE THIS TASK TOUCHES"), prompt.index("THE TASK, IN FULL"))
        self.assertTrue(prompt.startswith("You are the IMPLEMENTER"))
        self.assertIn("YOUR TASK: Fix games/crosstown/game.js", prompt)

    def test_the_repair_prompt_carries_it(self):
        from swarm.workflow import REPAIR
        prompt = REPAIR.format(contract="{}", failure="boom", test_cmd="true",
                               context=self.pack(self.task("Fix games/crosstown/game.js")))
        self.assertIn("function init(seed) {", prompt)

    def test_the_fast_implementer_prompt_carries_it(self):
        prompt = swarmd.FAST_IMPLEMENTER.format(goal="g", spec="s", test_cmd="true", title="t",
                                                 context=self.pack(self.task("Fix games/crosstown/game.js")))
        self.assertIn("function init(seed) {", prompt)

    def test_every_prompt_placeholder_is_still_filled(self):
        """A missing keyword would raise KeyError mid-attempt, after the turn was paid for."""
        import re
        for name in ("IMPLEMENTER", "FAST_IMPLEMENTER"):
            keys = set(re.findall(r"\{(\w+)\}", getattr(swarmd, name)))
            self.assertLessEqual(keys, {"goal", "spec", "previous", "playbook", "corpus",
                                        "context", "test_cmd", "title"}, name)


class NamedPathsTests(unittest.TestCase):
    def paths(self, title, detail="", acceptance=None):
        return swarmd.named_paths({"title": title, "detail": detail,
                                   "acceptance": acceptance or []})

    def test_the_order_the_task_names_them_in_is_kept(self):
        self.assertEqual(self.paths("b.py then a.py"), ["b.py", "a.py"])

    def test_a_trailing_period_is_not_part_of_the_path(self):
        self.assertEqual(self.paths("Change src/app.py."), ["src/app.py"])

    def test_the_extensions_are_the_ones_this_swarm_writes(self):
        self.assertEqual(self.paths("a.py b.js c.json d.md e.html f.css"),
                         ["a.py", "b.js", "c.json", "d.md", "e.html", "f.css"])
        self.assertEqual(self.paths("photo.png binary.so archive.tar.gz"), [])

    def test_acceptance_criteria_are_read_as_text_or_as_rows(self):
        self.assertEqual(self.paths("t", acceptance=["src/a.py passes"]), ["src/a.py"])
        self.assertEqual(self.paths("t", acceptance=[{"id": "C1", "text": "src/b.py passes"}]),
                         ["src/b.py"])


if __name__ == "__main__":
    unittest.main()
