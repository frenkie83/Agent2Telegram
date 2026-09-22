"""Tests for attach-mode turn-end backstop delivery."""
import tempfile
import threading
import time
import unittest
from pathlib import Path

from agent2telegram import attach as attach_mod
from agent2telegram.attach import AttachBridge
from agent2telegram.config import Config


class _FakeClient:
    def __init__(self):
        self.sent = []
        self.deleted = []

    def send_message(self, chat_id, text, parse_mode=None):
        self.sent.append((chat_id, text))

    def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))

    def send_chat_action(self, chat_id, action):
        pass

    def send_plain_id(self, chat_id, text, parse_mode=None):
        self.sent.append((chat_id, text))
        return 55

    def edit_plain(self, chat_id, message_id, text, parse_mode=None):
        pass


class _FakeSession:
    """Records what got injected into the pane; injection always succeeds."""

    def __init__(self):
        self.injected = []

    def inject(self, text):
        self.injected.append(text)


def _reaction(message_id=42, emoji="❤"):
    return {"message_reaction": {"user": {"id": 7}, "message_id": message_id,
                                 "new_reaction": [{"type": "emoji", "emoji": emoji}]}}


def _bridge(tmpdir):
    b = object.__new__(AttachBridge)
    b.cfg = Config(agent="generic", token="1:2", allowed_user_ids=[7], tmux_session="a2t")
    b.tg = _FakeClient()
    b._owner_chat = 7
    b._marker = "[tg]"
    b._signal = Path(tmpdir) / "answer.txt"
    b._turn_end = None
    b._transcript = Path(tmpdir) / "transcript.jsonl"
    b._turn_active = threading.Event()
    b._turn_active.set()
    b._turn_from_tg = True
    b._turn_text_sent = False
    b._pending_turn_end = False
    b._turn_started = time.monotonic() - 1.0
    b._typing_count = 7
    b._max_gap = 0.0
    b._status = {"mid": None, "shown": ""}
    b._status_path = None
    b._seen_tools = set()
    b._stop = threading.Event()
    b._sent_keys = set()
    b._pending_send = []
    b._queue_path = None
    b._use_durable_outbox = False       # focused unit test: no disk delivery side effects
    b._allowed = {7}
    b._session = _FakeSession()
    b._last_activity = 0.0
    b._tui_seen = set()
    b._turn_is_reaction = False
    b._pending_files = []
    b._sent_path = Path(tmpdir) / "sent_uuids"
    return b


class AttachBackstopTests(unittest.TestCase):
    def setUp(self):
        self._retry_delay = attach_mod.BACKSTOP_RETRY_DELAY
        attach_mod.BACKSTOP_RETRY_DELAY = 0.0

    def tearDown(self):
        attach_mod.BACKSTOP_RETRY_DELAY = self._retry_delay

    def test_retry_reads_transcript_after_initial_empty_result(self):
        with tempfile.TemporaryDirectory() as d:
            b = _bridge(d)
            seen = []
            answers = iter(["", "[tg] final from transcript"])

            def last_text():
                return next(answers)

            def drain():
                seen.append("drain")

            b._last_assistant_text = last_text
            b._drain_transcript = drain

            b._finish_turn()

            self.assertEqual(seen, ["drain"])
            self.assertEqual(b.tg.sent, [(7, "final from transcript")])
            self.assertTrue(b._turn_text_sent)
            self.assertFalse(b._turn_active.is_set())

    def test_fallback_sends_signal_file_when_transcript_stays_empty(self):
        with tempfile.TemporaryDirectory() as d:
            b = _bridge(d)
            b._signal.write_text("[tg] final from signal", "utf-8")
            b._last_assistant_text = lambda: ""
            b._drain_transcript = lambda: None

            b._finish_turn()

            self.assertEqual(b.tg.sent, [(7, "final from signal")])
            self.assertTrue(b._turn_text_sent)
            self.assertFalse(b._signal.exists())

    def test_empty_transcript_and_signal_logs_error_without_crashing(self):
        with tempfile.TemporaryDirectory() as d:
            b = _bridge(d)
            b._last_assistant_text = lambda: ""
            b._drain_transcript = lambda: None

            with self.assertLogs("agent2telegram.attach", level="ERROR") as logs:
                b._finish_turn()

            self.assertEqual(b.tg.sent, [])
            self.assertFalse(b._turn_active.is_set())
            self.assertTrue(any("Telegram turn ended without an answer" in line for line in logs.output))
            self.assertTrue(any("typing_count=7" in line for line in logs.output))

    def test_last_sent_text_matching_the_transcripts_final_message_is_not_resent(self):
        """T-0404 changed the contract this test pins. It used to assert the opposite of what is
        checked below: that the turn-end path must not even READ the transcript once
        `_turn_text_sent` is True. That exact assumption was the bug — it made the bridge blind to
        a DIFFERENT final answer landing in the transcript after the interim send (measured
        2026-09-21 22:36 and 2026-09-22 19:12:30, see LateFinalAnswerAfterInterimTextTests below).
        Reading the transcript at turn end is fine now and expected; what must still never happen
        is a duplicate send when the transcript's last message IS the one already forwarded."""
        with tempfile.TemporaryDirectory() as d:
            b = _bridge(d)
            b._turn_text_sent = True
            b._turn_sent_keys = {"already-key"}      # this turn already forwarded this key
            b.tg.sent.append((7, "already sent"))

            b._last_assistant_text = lambda: "[tg] already sent"
            b._last_backstop_key = "already-key"      # same message, same dedup key
            b._drain_transcript = lambda: None

            b._finish_turn()

            self.assertEqual(b.tg.sent, [(7, "already sent")],
                             "the turn's own already-forwarded final message was sent again")
            self.assertFalse(b._turn_active.is_set())


class LateFinalAnswerAfterInterimTextTests(unittest.TestCase):
    """T-0404: `_turn_text_sent` says "something went out this turn", not "the answer went out".
    Once ANY interim message was forwarded, the old backstop (guarded by `not _turn_text_sent`)
    never looked at the transcript again — so when the turn's real final answer landed in the
    transcript in the SAME SECOND the turn ended (after the drain that would have forwarded it
    had already run), it vanished with no forward, no warning, no log line at all. Measured
    2026-09-21 22:36 and 2026-09-22 19:12:30.

    These tests reproduce that exact shape: an interim text already forwarded this turn (so
    `_turn_text_sent` is True and its key is in `_turn_sent_keys`), then a DIFFERENT final text
    sitting in the transcript that this turn never forwarded."""

    def setUp(self):
        self._retry_delay = attach_mod.BACKSTOP_RETRY_DELAY
        attach_mod.BACKSTOP_RETRY_DELAY = 0.0

    def tearDown(self):
        attach_mod.BACKSTOP_RETRY_DELAY = self._retry_delay

    def test_final_answer_written_after_the_last_drain_is_still_delivered(self):
        with tempfile.TemporaryDirectory() as d:
            b = _bridge(d)
            b._turn_text_sent = True                       # an interim reply already went out
            b._turn_sent_keys = {"interim-key"}
            b._sent_keys.add("interim-key")
            b.tg.sent.append((7, "interim reply"))

            # The turn's REAL final answer, written to the transcript after that drain already
            # ran — the one race the old `not _turn_text_sent` guard could never see.
            b._last_assistant_text = lambda: "[tg] the real final answer"
            b._last_backstop_key = "final-key"
            b._drain_transcript = lambda: None

            b._finish_turn()

            self.assertEqual(
                b.tg.sent, [(7, "interim reply"), (7, "the real final answer")],
                "the turn's real final answer, written after the interim text was sent, was lost",
            )
            self.assertFalse(b._turn_active.is_set())

    def test_final_answer_already_queued_to_the_durable_outbox_is_not_sent_twice(self):
        """`_turn_sent_keys` must cover a reply this turn already HANDED to the durable outbox,
        even before Telegram confirms it — `_sent_keys` alone is marked too late for this check
        (it is only set once the outbox/Telegram send actually succeeds), so relying on it here
        would re-queue a reply still sitting in the outbox."""
        with tempfile.TemporaryDirectory() as d:
            b = _bridge(d)
            b._turn_text_sent = True
            b._turn_sent_keys = {"queued-key"}   # forwarded to the outbox by THIS turn already
            # b._sent_keys stays empty on purpose: Telegram has not confirmed delivery yet

            b._last_assistant_text = lambda: "[tg] the reply already queued"
            b._last_backstop_key = "queued-key"  # same text, same key — this IS what was sent
            b._drain_transcript = lambda: None

            b._finish_turn()

            self.assertEqual(b.tg.sent, [],
                             "a reply already queued by this turn was forwarded a second time")

    def test_terminal_originated_turn_is_never_late_final_forwarded(self):
        """A turn that did not come from Telegram must stay silent, exactly like the original
        backstop — the late-final check is not a new leak for local turns."""
        with tempfile.TemporaryDirectory() as d:
            b = _bridge(d)
            b._turn_from_tg = False
            b._turn_text_sent = True
            b._turn_sent_keys = set()

            b._last_assistant_text = lambda: "[tg] some local final text"
            b._last_backstop_key = "local-key"
            b._drain_transcript = lambda: None

            b._finish_turn()

            self.assertEqual(b.tg.sent, [], "a terminal-originated turn's text was forwarded")

    def test_reaction_turn_stays_exempt_from_the_late_final_check(self):
        """A turn opened by a reaction is a deliberate exemption from the backstop — the late-
        final check must not quietly re-enable it."""
        with tempfile.TemporaryDirectory() as d:
            b = _bridge(d)
            b._turn_is_reaction = True
            b._turn_text_sent = True
            b._turn_sent_keys = set()

            b._last_assistant_text = lambda: "[tg] the agent's internal note"
            b._last_backstop_key = "reaction-key"
            b._drain_transcript = lambda: None

            b._finish_turn()

            self.assertEqual(b.tg.sent, [], "a reaction turn was forwarded by the late-final check")


class ReactionTurnBackstopTests(unittest.TestCase):
    """A heart always deserves a short answer, and it must never be answered with the agent's
    INTERNAL text. The prompt asks for a one-liner; the backstop exemption is the safety net for
    when the agent stays silent anyway. Every test goes through the real _handle() path."""

    def setUp(self):
        self._retry_delay = attach_mod.BACKSTOP_RETRY_DELAY
        attach_mod.BACKSTOP_RETRY_DELAY = 0.0

    def tearDown(self):
        attach_mod.BACKSTOP_RETRY_DELAY = self._retry_delay

    def test_reaction_without_a_reply_forwards_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            b = _bridge(d)
            b._turn_active.clear()                  # nothing running when the reaction lands
            b._turn_from_tg = False
            b._last_assistant_text = lambda: "No response requested."
            b._drain_transcript = lambda: None

            b._handle(_reaction())
            self.assertTrue(b._session.injected, "the reaction never reached the session")
            b._finish_turn()

            self.assertEqual(b.tg.sent, [],
                             "the agent's internal note leaked to the user after a reaction")
            self.assertFalse(b._turn_active.is_set())

    def test_reaction_turn_still_delivers_an_explicit_reply(self):
        with tempfile.TemporaryDirectory() as d:
            b = _bridge(d)
            b._turn_active.clear()
            b._turn_from_tg = False
            b._last_assistant_text = lambda: "No response requested."
            b._drain_transcript = lambda: None

            b._handle(_reaction())
            b._send_final("thanks!")                # the agent decided to answer anyway
            b._finish_turn()

            self.assertEqual(b.tg.sent, [(7, "thanks!")],
                             "an explicit reply to a reaction must still go out, exactly once")

    def test_reaction_prompt_asks_for_a_short_answer(self):
        """A heart must ALWAYS get a reply, just a very short one."""
        with tempfile.TemporaryDirectory() as d:
            b = _bridge(d)
            b._turn_active.clear()
            b._turn_from_tg = False
            b._last_assistant_text = lambda: ""
            b._drain_transcript = lambda: None

            b._handle(_reaction())

            vyzva = b._session.injected[0].lower()
            self.assertIn("always answer", vyzva,
                          "the reaction prompt no longer asks for a reply at all")
            self.assertIn("short", vyzva,
                          "the reaction prompt does not ask for a SHORT reply")
            self.assertNotIn("no need to reply", vyzva)


    def test_reaction_during_a_running_turn_keeps_the_backstop_armed(self):
        """A heart landing mid-answer must not disarm the backstop for the real question."""
        with tempfile.TemporaryDirectory() as d:
            b = _bridge(d)                          # _turn_active is set = a question is running
            b._last_assistant_text = lambda: "[tg] the real answer"
            b._drain_transcript = lambda: None

            b._handle(_reaction())
            b._finish_turn()

            self.assertEqual(b.tg.sent, [(7, "the real answer")],
                             "a reaction mid-turn swallowed the answer to the real question")



class StatusBubbleLifetimeTests(unittest.TestCase):
    """A technical bubble is deleted at turn end. One created with NO turn running has nothing
    to delete it and hangs in the chat — "Editing MEMORY.md" was seen stuck for eight minutes
    after a bridge restart drained the transcript outside a turn (2026-08-02)."""

    def test_no_bubble_is_created_outside_a_turn(self):
        with tempfile.TemporaryDirectory() as d:
            b = _bridge(d)
            b._turn_active.clear()

            b._status_push("\u270f\ufe0f Editing MEMORY.md")

            self.assertIsNone(b._status["mid"],
                              "a bubble was created with no turn running — nothing will delete it")

    def test_bubble_is_still_created_during_a_turn(self):
        with tempfile.TemporaryDirectory() as d:
            b = _bridge(d)                      # _turn_active is set
            b._status_push("\U0001f4c4 Read foo.py")

            self.assertEqual(b._status["mid"], 55, "the live progress bubble stopped working")
            self.assertTrue(b.tg.sent)



class BackstopDedupTests(unittest.TestCase):
    """The backstop must never re-send a message the normal path already delivered. It reads the
    LAST assistant text in the transcript, which — when a turn ends before its own answer lands —
    is the PREVIOUS turn's answer. That duplicate was observed on a live bridge (2026-08-02:
    the 15:51 reply arrived again at 16:04, right after a voice note)."""

    def setUp(self):
        self._retry_delay = attach_mod.BACKSTOP_RETRY_DELAY
        attach_mod.BACKSTOP_RETRY_DELAY = 0.0

    def tearDown(self):
        attach_mod.BACKSTOP_RETRY_DELAY = self._retry_delay

    def test_already_delivered_message_is_not_sent_again(self):
        with tempfile.TemporaryDirectory() as d:
            b = _bridge(d)
            b._sent_keys.add("msg-1")                    # the normal path delivered it earlier
            b._last_assistant_text = lambda: "[tg] the previous answer"
            b._drain_transcript = lambda: None
            b._last_backstop_key = "msg-1"

            b._finish_turn()

            self.assertEqual(b.tg.sent, [],
                             "the backstop re-sent a message that had already been delivered")

    def test_a_genuinely_new_answer_still_goes_out(self):
        with tempfile.TemporaryDirectory() as d:
            b = _bridge(d)
            b._sent_keys.add("msg-1")
            b._last_assistant_text = lambda: "[tg] a brand new answer"
            b._drain_transcript = lambda: None
            b._last_backstop_key = "msg-2"

            b._finish_turn()

            self.assertEqual(b.tg.sent, [(7, "a brand new answer")],
                             "the backstop stopped delivering genuinely new answers")

    def test_backstop_key_comes_from_the_transcript_scan(self):
        """The key must be filled by _last_assistant_text itself, not by the caller."""
        with tempfile.TemporaryDirectory() as d:
            b = _bridge(d)
            b._transcript.write_text("", "utf-8")

            b._last_assistant_text()                     # no assistant text anywhere

            self.assertIsNone(b._last_backstop_key,
                              "an empty transcript must leave the key empty, never crash")


if __name__ == "__main__":
    unittest.main()
