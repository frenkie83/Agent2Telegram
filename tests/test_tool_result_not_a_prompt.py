"""A tool result must never be mistaken for something the user typed.

Claude Code files tool results under `type: "user"`. The bridge decides from the last user text
whether the turn came from Telegram, so a tool result arriving mid-turn silently reclassified a
live Telegram turn as terminal-originated: the answer was never forwarded, the backstop never
ran, and NOTHING appeared in the log. Reproduced from the real transcript record on 2026-08-23,
where the agent read an image during the turn.
"""
import threading
import unittest

from agent2telegram.readers import ClaudeCodeReader

# Shape taken verbatim from a real transcript, trimmed to the fields that matter.
TOOL_RESULT = {
    "type": "user",
    "toolUseResult": {"type": "image"},
    "message": {"content": [{"tool_use_id": "toolu_011EtqadNnd41qkrYeR45kvJ",
                             "type": "tool_result",
                             "content": "[Image: original 3300x1880, displayed at 2000x1139.]"}]},
}
# A tool result that does not carry the toolUseResult key — only the block type gives it away.
TOOL_RESULT_BLOCK_ONLY = {
    "type": "user",
    "message": {"content": [{"tool_use_id": "x", "type": "tool_result", "content": "output"}]},
}
REAL_PROMPT = {"type": "user", "message": {"content": "[TG] what is the status?"}}


class ToolResultIsNotAPromptTests(unittest.TestCase):
    def test_tool_result_is_not_user_text(self):
        self.assertIsNone(ClaudeCodeReader().user_text(TOOL_RESULT))
        self.assertIsNone(ClaudeCodeReader().user_text(TOOL_RESULT_BLOCK_ONLY))

    def test_tool_result_emits_no_user_event(self):
        self.assertEqual(list(ClaudeCodeReader().parse(TOOL_RESULT)), [])
        self.assertEqual(list(ClaudeCodeReader().parse(TOOL_RESULT_BLOCK_ONLY)), [])

    def test_a_real_prompt_still_counts(self):
        self.assertEqual(ClaudeCodeReader().user_text(REAL_PROMPT), "[TG] what is the status?")
        self.assertEqual([e.kind for e in ClaudeCodeReader().parse(REAL_PROMPT)], ["user"])


class TurnOriginTests(unittest.TestCase):
    """Second layer: even if some other synthetic record slips through, a running turn that is
    already known to come from Telegram must not be downgraded to local."""

    def _bridge(self):
        from agent2telegram.attach import AttachBridge
        b = object.__new__(AttachBridge)
        b._origins = ("[TG]", "Telegram:")
        b._turn_active = threading.Event()
        b._turn_from_tg = False
        return b

    def _user(self, text):
        from agent2telegram.readers import Ev
        return Ev("user", text=text)

    def test_a_prefixed_message_marks_the_turn(self):
        b = self._bridge()
        b._handle_event(self._user("[TG] hello"))
        self.assertTrue(b._turn_from_tg)

    def test_an_unprefixed_record_mid_turn_does_not_downgrade_it(self):
        b = self._bridge()
        b._handle_event(self._user("[TG] hello"))
        b._turn_active.set()                       # the turn is running
        b._handle_event(self._user("[Image: 3300x1880 …]"))
        self.assertTrue(b._turn_from_tg,
                        "a mid-turn record reclassified a Telegram turn as local — "
                        "the reply would never be forwarded")

    def test_a_terminal_turn_is_still_recognised_as_local(self):
        b = self._bridge()
        b._turn_from_tg = True                     # left over from a previous turn
        b._turn_active.clear()                     # no turn running
        b._handle_event(self._user("just typed in the terminal"))
        self.assertFalse(b._turn_from_tg)


if __name__ == "__main__":
    unittest.main()
