"""An agent's own file-sending tool must reach the user.

Claude Code has a built-in tool for handing a file to the user. The harness runs it itself, so
the bridge never sees a call — only its record in the transcript. Until 2026-08-24 the bridge
ignored that record, so the file silently vanished: the reply arrived, the attachment did not,
and nothing in the log said so. It happened three times in one day before anyone noticed.

The fix recognises the tool BY NAME (not by guessing from the reply text, which would be
language-dependent and full of false positives) and delivers the files through the same
allowlist as a [tg-file] line.
"""
import threading
import unittest

from agent2telegram.readers import ClaudeCodeReader

# Shape taken verbatim from a real transcript record.
TOOL_CALL = {
    "type": "assistant",
    "uuid": "u1",
    "message": {"content": [{
        "type": "tool_use", "id": "toolu_abc", "name": "SendUserFile",
        "input": {"files": ["/tmp/report.pdf", "/tmp/chart.png"],
                  "caption": "report", "status": "normal"},
    }]},
}
ORDINARY_TOOL = {
    "type": "assistant",
    "uuid": "u2",
    "message": {"content": [{"type": "tool_use", "id": "toolu_def",
                             "name": "Read", "input": {"file_path": "/tmp/x.py"}}]},
}


class ReaderTests(unittest.TestCase):
    def test_file_tool_becomes_a_files_event_with_the_paths(self):
        evs = list(ClaudeCodeReader().parse(TOOL_CALL))
        self.assertEqual([e.kind for e in evs], ["files"])
        self.assertEqual(evs[0].files, ("/tmp/report.pdf", "/tmp/chart.png"))

    def test_a_file_tool_is_not_reported_as_a_tool_bubble(self):
        """Otherwise the user sees a 'ran a tool' bubble and never the file."""
        self.assertNotIn("tool", [e.kind for e in ClaudeCodeReader().parse(TOOL_CALL)])

    def test_ordinary_tools_are_unaffected(self):
        evs = list(ClaudeCodeReader().parse(ORDINARY_TOOL))
        self.assertEqual([e.kind for e in evs], ["tool"])

    def test_a_single_path_string_is_accepted(self):
        rec = {"type": "assistant", "uuid": "u3", "message": {"content": [{
            "type": "tool_use", "id": "t", "name": "SendUserFile", "input": {"file": "/tmp/a.pdf"}}]}}
        self.assertEqual(list(ClaudeCodeReader().parse(rec))[0].files, ("/tmp/a.pdf",))

    def test_a_call_without_paths_is_not_a_files_event(self):
        rec = {"type": "assistant", "uuid": "u4", "message": {"content": [{
            "type": "tool_use", "id": "t", "name": "SendUserFile", "input": {"caption": "x"}}]}}
        self.assertEqual([e.kind for e in ClaudeCodeReader().parse(rec)], ["tool"])


class BridgeTests(unittest.TestCase):
    def _bridge(self):
        from agent2telegram.attach import AttachBridge
        b = object.__new__(AttachBridge)
        b._origins = ("[TG]",)
        b._turn_active = threading.Event()
        b._turn_from_tg = True
        b._owner_chat = 7
        b._seen_tools = set()
        b.cfg = type("C", (), {"agent": "claude-code"})()
        b.sent = []
        b._send_files = lambda paths: b.sent.append(list(paths))
        return b

    def _ev(self, files, key="k1"):
        from agent2telegram.readers import Ev
        return Ev("files", key=key, files=tuple(files))

    def test_the_files_are_delivered(self):
        b = self._bridge()
        b._handle_event(self._ev(["/tmp/a.pdf", "/tmp/b.png"]))
        self.assertEqual(b.sent, [["/tmp/a.pdf", "/tmp/b.png"]])

    def test_the_same_call_is_not_delivered_twice(self):
        """The transcript is re-read on resume — the file must not arrive again."""
        b = self._bridge()
        b._handle_event(self._ev(["/tmp/a.pdf"]))
        b._handle_event(self._ev(["/tmp/a.pdf"]))
        self.assertEqual(len(b.sent), 1)

    def test_nothing_is_delivered_for_a_terminal_turn(self):
        b = self._bridge()
        b._turn_from_tg = False
        b._handle_event(self._ev(["/tmp/a.pdf"]))
        self.assertEqual(b.sent, [])


if __name__ == "__main__":
    unittest.main()
