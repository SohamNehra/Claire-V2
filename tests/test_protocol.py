import unittest

from claire_core import protocol

TOOLS = ["read_file", "write_file", "edit_file", "list_files", "search_files",
         "run_command", "done", "lookup"]


class ParseTests(unittest.TestCase):
    def test_single_call_with_rationale(self):
        calls = protocol.parse_tool_calls(
            "Reading it first.\n<read_file>\n<path>a.py</path>\n<start>3</start>\n</read_file>",
            TOOLS)
        self.assertEqual(calls, [("read_file", {"path": "a.py", "start": "3"})])

    def test_read_only_calls_batch_up_to_three(self):
        text = "".join(f"<read_file><path>{n}.py</path></read_file>" for n in "abcd")
        calls = protocol.parse_tool_calls(text, TOOLS)
        self.assertEqual([a["path"] for _, a in calls], ["a.py", "b.py", "c.py"])

    def test_a_batch_with_a_write_collapses_to_the_first_call(self):
        text = ("<read_file><path>a.py</path></read_file>"
                "<run_command><command>rm a.py</command></run_command>")
        calls = protocol.parse_tool_calls(text, TOOLS)
        self.assertEqual(calls, [("read_file", {"path": "a.py"})])

    def test_unclosed_write_file_is_recovered(self):
        calls = protocol.parse_tool_calls(
            "<write_file>\n<path>x.txt</path>\n<content>\nline one\nline two", TOOLS)
        self.assertEqual(calls[0][0], "write_file")
        self.assertEqual(calls[0][1]["content"], "line one\nline two")

    def test_tags_inside_a_body_are_not_read_as_parameters(self):
        text = ("<write_file>\n<content>\n<path>decoy.txt</path>\n<x/>\n</content>\n"
                "<path>real.xml</path>\n</write_file>")
        name, args = protocol.parse_tool_calls(text, TOOLS)[0]
        self.assertEqual(args["path"], "real.xml")
        self.assertIn("decoy.txt", args["content"])

    def test_reasoning_is_stripped_before_parsing(self):
        text = "<think>maybe <done> here</think>ok\n<done><summary>hi</summary></done>"
        self.assertEqual(protocol.parse_tool_calls(text, TOOLS),
                         [("done", {"summary": "hi"})])

    def test_orphan_close_think_before_the_call_is_dropped(self):
        text = "stray reasoning</think>\n<done><summary>hi</summary></done>"
        self.assertEqual(protocol.parse_tool_calls(text, TOOLS)[0][1]["summary"], "hi")

    def test_workshop_tool_declares_its_own_parameters(self):
        calls = protocol.parse_tool_calls(
            "<lookup><ticker>ACME</ticker><days>5</days></lookup>", TOOLS,
            {"lookup": ("ticker", "days")})
        self.assertEqual(calls, [("lookup", {"ticker": "ACME", "days": "5"})])

    def test_prose_outside_keeps_only_the_prose(self):
        text = "Here is the answer.\n<done>\n</done>"
        self.assertEqual(protocol.prose_outside(text, TOOLS), "Here is the answer.")

    def test_partial_closing_tag_is_held_back_mid_stream(self):
        self.assertEqual(protocol.trim_partial_close("almost done</summ"), "almost done")
        self.assertEqual(protocol.trim_partial_close("a </b> tag"), "a </b> tag")
        self.assertEqual(protocol.streaming_summary("<done><summary>\nhel"), "hel")

    def test_phase_names_the_activity(self):
        self.assertEqual(protocol.phase("<read_file><path>a.py")[0], "calling read_file")
        self.assertEqual(protocol.phase("<write_file><path>a.py</path><content>\nx\ny")[0],
                         "writing")
        self.assertEqual(protocol.phase("<done><summary>hello there")[0], "answering")


if __name__ == "__main__":
    unittest.main()
