"""The rounds an implementer turn gets, and the edit it must not lose.

Half the studio swarm's attempts ended `no_change`, and 60 of those 95 ran out of tool rounds
before editing anything: the model reads for eleven rounds of twelve because nothing tells it how
many it has, and then flint's tool-free final round throws away the edit it finally writes — as
reply markup, because tools were off. These tests pin the three places that leaked the work:
a visible round budget, an edit-only round before the answer, and edits recovered from text.
"""
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import flint

FIXTURE = Path(__file__).parent / "fixtures" / "text_call_implementer.txt"


def agent(headless=True, final_edit=False, read_only=True):
    a = flint.Agent.__new__(flint.Agent)
    # An editing role is launched with --yolo, as swarmd launches the implementer and repair turns.
    a.headless, a.read_only, a.yolo, a.always = headless, read_only, not read_only, set()
    a.messages, a.model, a.throttle, a.last_prompt_tokens, a.corpus = [], "m:free", Mock(), 0, False
    a.edits, a.only_tools, a.final_edit = 0, None, final_edit
    return a


def offered(a):
    """The tool names a request would offer right now."""
    return [t["function"]["name"] for t in a._request_kwargs()["tools"]]


@contextlib.contextmanager
def quiet():
    with contextlib.redirect_stderr(io.StringIO()):
        yield


class RoundBudgetTests(unittest.TestCase):
    """What the model is told about the rounds it has left."""

    def scripted(self, a, rounds):
        """A model that calls read_file every round and never edits."""
        def complete():
            return "", [{"id": f"c{len(a.messages)}", "name": "read_file", "args": '{"path": "x"}'}]
        a.complete = complete
        a.run_tool = Mock(return_value="file text")
        a._final_answer = Mock(return_value="done")
        with patch.object(flint, "MAX_STEPS", rounds), quiet():
            a.turn("implement this")

    def tools(self, a):
        return [m["content"] for m in a.messages if m["role"] == "tool"]

    def test_every_round_says_which_one_it_is_and_how_many_are_left(self):
        a = agent()
        self.scripted(a, 4)
        notes = [line for m in self.tools(a) for line in m.splitlines() if line.startswith("[round")]
        self.assertEqual(notes, ["[round 1 of 4; 3 left]", "[round 2 of 4; 2 left]",
                                 "[round 3 of 4; 1 left]", "[round 4 of 4; 0 left]"])

    def test_the_note_rides_on_the_last_result_of_a_round_not_every_one(self):
        a = agent()
        a.complete = lambda: ("", [{"id": "c1", "name": "read_file", "args": '{"path": "x"}'},
                                   {"id": "c2", "name": "read_file", "args": '{"path": "y"}'}])
        a.run_tool = Mock(return_value="file text")
        a._final_answer = Mock(return_value="done")
        with patch.object(flint, "MAX_STEPS", 1), quiet():
            a.turn("implement this")
        results = self.tools(a)
        self.assertEqual(len(results), 2)
        self.assertNotIn("[round", results[0])
        self.assertIn("[round 1 of 1; 0 left]", results[1])

    def test_an_interactive_turn_is_not_annotated(self):
        a = agent(headless=False)
        a.complete = lambda: ("", [{"id": "c1", "name": "read_file", "args": '{"path": "x"}'}])
        a.run_tool = Mock(return_value="file text")
        with patch.object(flint, "MAX_STEPS", 2), quiet():
            with self.assertRaises(flint.StepLimitReached):
                a.turn("do this")
        self.assertFalse([m for m in self.tools(a) if "[round" in m])

    def test_the_nudge_fires_once_with_three_rounds_left_and_nothing_edited(self):
        a = agent()
        self.scripted(a, 8)
        nudges = [m for m in a.messages if m["role"] == "user" and m["content"] == flint.Agent.NO_EDIT_YET]
        self.assertEqual(len(nudges), 1)
        # Round 5 of 8 leaves three: the nudge lands right after that round's tool result.
        after = a.messages.index(nudges[0])
        self.assertIn("[round 5 of 8; 3 left]", a.messages[after - 1]["content"])
        self.assertIn("Make the edit now", nudges[0]["content"])

    def test_no_nudge_once_a_file_has_changed(self):
        a = agent()
        a.run_tool = Mock(side_effect=lambda c: (setattr(a, "edits", 1), "Edited x (1 replacement).")[1])
        a.complete = lambda: ("", [{"id": "c1", "name": "edit_file", "args": '{"path": "x"}'}])
        a._final_answer = Mock(return_value="done")
        with patch.object(flint, "MAX_STEPS", 8), quiet():
            a.turn("implement this")
        self.assertFalse([m for m in a.messages
                          if m["role"] == "user" and m["content"] == flint.Agent.NO_EDIT_YET])

    def test_an_applied_edit_is_counted(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        (Path(d.name) / "m.py").write_text("a = 1\n")
        a = agent(read_only=False)
        with patch.object(flint, "_p", lambda p: Path(d.name) / p), quiet():
            a.run_tool({"id": "c1", "function": {"name": "edit_file", "arguments":
                                                json.dumps({"path": "m.py", "old_str": "a = 1", "new_str": "a = 2"})}})
            self.assertEqual(a.edits, 1)
            a.run_tool({"id": "c2", "function": {"name": "edit_file", "arguments":
                                                json.dumps({"path": "m.py", "old_str": "nope", "new_str": "x"})}})
        self.assertEqual(a.edits, 1, "a failed edit must not count as a change")


class FinalEditRoundTests(unittest.TestCase):
    """The last chance to write the change down, for a role whose product is a diff."""

    def test_the_final_round_offers_exactly_the_two_edit_tools(self):
        a = agent(final_edit=True, read_only=False)
        seen = []

        def complete():
            seen.append(offered(a))
            if len(seen) == 1:
                return "", [{"id": "c1", "name": "edit_file",
                             "args": '{"path": "x", "old_str": "a", "new_str": "b"}'}]
            return "I made the edit.", []
        a.complete = complete
        a.run_tool = Mock(return_value="Edited x (1 replacement).")
        with quiet():
            self.assertEqual(a._final_answer(), "I made the edit.")
        self.assertEqual(sorted(seen[0]), ["edit_file", "write_file"])
        self.assertIn("read_file", seen[1], "the tool-free round is back to the full schema")
        self.assertIn("Make the edit now", a.messages[0]["content"])
        a.run_tool.assert_called_once()

    def test_reading_is_not_on_offer_in_the_final_round(self):
        a = agent(final_edit=True, read_only=False)
        a.only_tools = set(flint.Agent.EDIT_TOOLS)
        self.assertNotIn("read_file", offered(a))
        self.assertNotIn("bash", offered(a))
        a.only_tools = None
        self.assertIn("read_file", offered(a))

    def test_a_turn_that_already_edited_goes_straight_to_the_answer(self):
        a = agent(final_edit=True, read_only=False)
        a.edits = 1
        a.complete = Mock(return_value=("done", []))
        with quiet():
            self.assertEqual(a._final_answer(), "done")
        a.complete.assert_called_once()
        self.assertNotIn("Make the edit now", json.dumps(a.messages))

    def test_a_read_only_role_gets_no_edit_round(self):
        """A reviewer's product is a verdict; it must not be handed edit tools at the limit."""
        a = agent(final_edit=False)
        a.complete = Mock(return_value=('{"verdict": "approve"}', []))
        with quiet():
            self.assertEqual(a._final_answer(), '{"verdict": "approve"}')
        a.complete.assert_called_once()

    def test_a_failed_edit_round_still_reaches_the_answer(self):
        a = agent(final_edit=True, read_only=False)
        calls = []

        def complete():
            calls.append(1)
            if len(calls) == 1:
                raise flint.IncompleteResponse("empty", "length")
            return "here is what I found", []
        a.complete = complete
        with quiet():
            self.assertEqual(a._final_answer(), "here is what I found")
        self.assertEqual(a.messages[0]["content"], flint.Agent.FINAL_NUDGE,
                         "the failed round's nudge is not left in the history")

    def test_a_spent_budget_still_stops_the_turn(self):
        a = agent(final_edit=True, read_only=False)
        a.complete = Mock(side_effect=flint.SpendExhausted(1.0, 1.0))
        with quiet(), self.assertRaises(flint.SpendExhausted):
            a._final_answer()

    def test_an_empty_answer_is_still_the_step_limit(self):
        a = agent(final_edit=True, read_only=False)
        a.edits = 1
        a.complete = Mock(return_value=("   ", []))
        with quiet(), self.assertRaises(flint.StepLimitReached):
            a._final_answer()


class RecoveredEditTests(unittest.TestCase):
    """The log this work came from: 12 rounds of reading, then the edit written as markup.

    swarm/logs/openRouter-Studio-ab0a4a/w0-implementer-1790428940089493000.log, saved as
    tests/fixtures/text_call_implementer.txt. It was dropped twice over: the markup said `file`
    where the schema says `path`, so nothing was recovered, and the final round discarded any
    recovered call anyway.
    """

    def setUp(self):
        self.text = FIXTURE.read_text()
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.wd = Path(d.name)

    def call(self):
        calls, rest = flint.recover_text_calls(self.text, flint.TOOL_SCHEMAS)
        self.assertEqual(len(calls), 1, "the fixture holds exactly one edit")
        return calls[0], json.loads(calls[0]["args"]), rest

    def test_the_markup_recovers_under_the_schemas_own_parameter_name(self):
        call, args, rest = self.call()
        self.assertEqual(call["name"], "edit_file")
        self.assertEqual(args["path"], "games/crosstown/game.js")
        self.assertNotIn("file", args)
        self.assertTrue(args["old_str"].startswith("function init(seed) {"))
        self.assertNotIn("<function=", rest)
        self.assertNotIn("<tool_call>", rest)
        self.assertTrue(rest.startswith("Based on my investigation"))

    def test_an_alias_never_displaces_the_real_parameter(self):
        calls, _ = flint.recover_text_calls(
            "<function=read_file><parameter=file>a.py</parameter>"
            "<parameter=path>b.py</parameter></function>", flint.TOOL_SCHEMAS)
        self.assertEqual(json.loads(calls[0]["args"])["path"], "b.py")

    def test_a_parameter_the_tool_really_has_is_left_alone(self):
        """write_file takes `content`; the alias for it must not fire."""
        calls, _ = flint.recover_text_calls(
            "<function=write_file><parameter=path>a.py</parameter>"
            "<parameter=content>x = 1</parameter></function>", flint.TOOL_SCHEMAS)
        self.assertEqual(json.loads(calls[0]["args"]), {"path": "a.py", "content": "x = 1"})

    def test_the_final_answers_markup_becomes_an_applied_edit(self):
        _, args, _ = self.call()
        target = self.wd / "games" / "crosstown" / "game.js"
        target.parent.mkdir(parents=True)
        target.write_text("// header\n" + args["old_str"] + "\n// footer\n")
        a = agent(final_edit=True, read_only=False)
        a.complete = Mock(return_value=(self.text, []))
        a.final_answer = False

        def complete():
            # The tool-free round: tools are off, so recover_text_calls is what produced these.
            calls, rest = flint.recover_text_calls(self.text, flint.TOOL_SCHEMAS)
            return rest, calls
        a.complete = complete
        a.edits = 1        # the edit round is not what is under test here
        with patch.object(flint, "_p", lambda p: self.wd / p), quiet():
            answer = a._final_answer()
        self.assertIn("Based on my investigation", answer)
        text = target.read_text()
        self.assertIn(args["new_str"], text)
        self.assertNotIn(args["old_str"], text)
        self.assertEqual(a.edits, 2)
        self.assertEqual([m["role"] for m in a.messages[-2:]], ["assistant", "tool"])
        self.assertIn("Edited", a.messages[-1]["content"])

    def test_a_recovered_call_that_is_not_an_edit_is_still_not_run(self):
        a = agent(final_edit=True, read_only=False)
        a.edits = 1
        a.run_tool = Mock()
        a.complete = Mock(return_value=("reading on", [{"id": "c1", "name": "bash",
                                                       "args": '{"command": "rm -rf /"}'}]))
        with quiet(), self.assertRaises(flint.StepLimitReached):
            a._final_answer()
        a.run_tool.assert_not_called()

    def test_a_read_only_role_never_has_a_recovered_edit_applied(self):
        a = agent(final_edit=False)
        a.run_tool = Mock()
        a.complete = Mock(return_value=("", [{"id": "c1", "name": "edit_file",
                                             "args": '{"path": "x", "old_str": "a", "new_str": "b"}'}]))
        with quiet(), self.assertRaises(flint.StepLimitReached):
            a._final_answer()
        a.run_tool.assert_not_called()


class ReadFilesTests(unittest.TestCase):
    """One call for several files, because a weak model makes one call per round."""

    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.wd = Path(d.name)
        (self.wd / "a.py").write_text("a = 1\nb = 2\n")
        (self.wd / "b.js").write_text("const c = 3;\n")
        p = patch.object(flint, "_p", lambda x: self.wd / x)
        p.start()
        self.addCleanup(p.stop)

    def test_several_files_come_back_in_one_call(self):
        out = flint.read_files(["a.py", "b.js"])
        self.assertIn("=== a.py ===", out)
        self.assertIn("=== b.js ===", out)
        self.assertIn("    1\ta = 1", out)
        self.assertIn("    1\tconst c = 3;", out)

    def test_the_numbering_matches_read_file_so_old_str_can_be_copied(self):
        self.assertIn(flint.read_file("a.py"), flint.read_files(["a.py"]))

    def test_a_missing_file_is_reported_without_losing_the_others(self):
        out = flint.read_files(["a.py", "gone.py"])
        self.assertIn("a = 1", out)
        self.assertIn("gone.py does not exist", out)

    def test_a_string_of_paths_is_accepted(self):
        """Text markup and weaker models hand over one string, not an array."""
        for given in ("a.py,b.js", "a.py\nb.js", '["a.py", "b.js"]'):
            out = flint.read_files(given)
            self.assertIn("=== a.py ===", out, given)
            self.assertIn("=== b.js ===", out, given)

    def test_no_paths_is_an_error_not_a_crash(self):
        self.assertTrue(flint.read_files([]).startswith("Error:"))
        self.assertTrue(flint.read_files("").startswith("Error:"))

    def test_the_output_is_capped(self):
        (self.wd / "big.py").write_text("x = 1\n" * 40_000)
        out = flint.read_files(["big.py", "big.py", "big.py"])
        self.assertLessEqual(len(out), flint.MAX_TOOL_OUTPUT + 2000)

    def test_the_tool_is_registered_and_described_as_taking_several(self):
        self.assertIs(flint.TOOLS["read_files"], flint.read_files)
        schema = next(t["function"] for t in flint.TOOL_SCHEMAS if t["function"]["name"] == "read_files")
        self.assertEqual(schema["parameters"]["properties"]["paths"]["type"], "array")
        self.assertNotIn("read_files", flint.NEEDS_APPROVAL)
        with patch.dict(os.environ, {"FLINT_CORPUS_DB": "/nonexistent"}):
            self.assertIn("read_files at once", flint.build_system_prompt())

    def test_a_read_only_role_may_still_use_it(self):
        a = agent(read_only=True)
        self.assertIn("read_files", offered(a))


if __name__ == "__main__":
    unittest.main()
