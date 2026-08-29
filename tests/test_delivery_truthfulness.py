"""Tests for commit 37c7ce3 — the inbound path stops claiming delivery it never made.

`_handle` used to return a bare `True` for two very different situations: "the text really
went into the session" and "handled here, the session never saw it" (a duplicate reaction, a
bridge-level slash command, a sticker...). The worker's log then said "IN delivered to
session" about a message that was never delivered — an incident that cost an hour of
diagnosis on 2026-08-28, per the commit message.

This file checks three things, matching the commit's own numbering:

  A — `_handle` returns a reason STRING (not `True`) for every "handled but not delivered"
      case, so the caller can tell the two apart.
  B — `_inbound_worker_loop` logs "NOT delivered to the session: <reason>" for a string, and
      only logs "delivered to session" for a real `True`.
  C — `_nothing_to_forward` (sticker, video note, poll...) names the payload, logs it, and
      tells the user — and a broken `send_message` must not blow up the caller.
"""
import logging
import tempfile
import threading
import unittest
from pathlib import Path

import pytest

from agent2telegram.attach import AttachBridge
from agent2telegram.config import Config

LOGGER_NAME = "agent2telegram.attach"


class _FakeClient:
    def __init__(self):
        self.sent = []
        self.actions = []

    def send_chat_action(self, chat_id, action="typing"):
        self.actions.append((chat_id, action))

    def send_message(self, chat_id, text, parse_mode=None):
        self.sent.append((chat_id, text))


class _BrokenClient(_FakeClient):
    """A Telegram client whose send_message always blows up — used for the C 'don't crash' case."""

    def send_message(self, chat_id, text, parse_mode=None):
        self.sent.append((chat_id, text))
        raise RuntimeError("Telegram is unreachable right now")


class _FakeSession:
    def __init__(self):
        self.injected = []

    def inject(self, text):
        self.injected.append(text)

    def _capture(self):
        return ""


def _bridge(*, client=None, state_dir=None):
    """A hand-assembled bridge — same approach as tests/test_attach_queue.py."""
    b = object.__new__(AttachBridge)
    b.cfg = Config(agent="generic", token="1:2", allowed_user_ids=[7], tmux_session="a2t")
    b.tg = client or _FakeClient()
    b._allowed = {7}
    b._owner_chat = 7
    b._turn_end = None
    b._session = _FakeSession()
    b._sent_keys = set()
    b._pending_send = []
    b._pending_files = []
    b._turn_active = threading.Event()
    b._turn_from_tg = False
    b._transcript = None
    b._last_activity = 0.0
    b._status = {"mid": None, "shown": ""}
    b._last_typing = 0.0
    b._typing_count = 0
    b._turn_started = 0.0
    b._max_gap = 0.0
    b._last_pane_warning = 0.0
    b._status_path = None
    b._seen_tools = set()
    b._tui_seen = set()
    b._turn_text_sent = True
    b._pending_turn_end = False
    b._marker = "[tg]"
    b._stop = threading.Event()
    b._reaction_seen = {}
    if state_dir is not None:
        state = Path(state_dir)
        b._offset_file = state / "offset"
        b._processed_updates_file = state / "processed_updates"
        b._queue_path = state / "outbox.json"
        b._sent_path = state / "sent_uuids"
        b._processed_update_ids, b._processed_update_order = b._read_processed_updates()
    return b


def _msg(*, update_id=1, message_id=10, user_id=7, chat_id=7, **fields):
    m = {"message_id": message_id, "from": {"id": user_id}, "chat": {"id": chat_id}}
    m.update(fields)
    return {"update_id": update_id, "message": m}


def _reaction(*, update_id=1, user_id=7, chat_id=1, message_id=200, emoji="❤️", empty=False):
    new_reaction = [] if empty else [{"type": "emoji", "emoji": emoji}]
    return {"update_id": update_id, "message_reaction": {
        "user": {"id": user_id}, "chat": {"id": chat_id}, "message_id": message_id,
        "new_reaction": new_reaction}}


# --------------------------------------------------------------------------------------
# A — `_handle` names the reason instead of claiming delivery
# --------------------------------------------------------------------------------------
class HandleReturnsReasonNotTrueTests(unittest.TestCase):
    """Every case here used to return a bare `True` — indistinguishable from real delivery."""

    def _assert_reason(self, result, *, session=None, sent_to_user=False):
        self.assertIsInstance(result, str,
                               f"expected a reason string, not True/False, got {result!r}")
        self.assertNotEqual(result, "")
        if session is not None:
            self.assertEqual(session.injected, [], "the message must not have reached the session")

    def test_reaction_from_disallowed_user_is_a_reason_not_true(self):
        b = _bridge()
        r = b._handle(_reaction(user_id=999))
        self._assert_reason(r, session=b._session)
        self.assertEqual(b.tg.sent, [], "an unknown user must not even get a reply")

    def test_duplicate_reaction_is_a_reason_not_true(self):
        b = _bridge()
        first = b._handle(_reaction(update_id=1))
        self.assertIs(first, True, "the first reaction of its kind must be delivered normally")
        second = b._handle(_reaction(update_id=2))
        self._assert_reason(second)
        self.assertEqual(len(b._session.injected), 1,
                          "the duplicate must not trigger a second prompt to the agent")

    def test_reaction_without_emoji_is_a_reason_not_true(self):
        b = _bridge()
        r = b._handle(_reaction(empty=True))
        self._assert_reason(r, session=b._session)

    def test_update_without_message_or_reaction_is_a_reason_not_true(self):
        b = _bridge()
        r = b._handle({"update_id": 1})
        self._assert_reason(r, session=b._session)

    def test_sender_off_allow_list_is_a_reason_not_true(self):
        b = _bridge()
        r = b._handle(_msg(user_id=999, text="hi"))
        self._assert_reason(r, session=b._session)
        self.assertEqual(len(b.tg.sent), 1)
        self.assertIn("Not authorized", b.tg.sent[0][1])

    def test_bridge_level_command_is_a_reason_not_true(self):
        b = _bridge()
        r = b._handle(_msg(text="/start"))
        self._assert_reason(r, session=b._session)
        self.assertIn("/start", r)
        self.assertEqual(len(b.tg.sent), 1, "the /start reply must still go out")

    def test_sticker_is_a_reason_not_true(self):
        b = _bridge()
        r = b._handle(_msg(sticker={"file_id": "s1"}))
        self._assert_reason(r, session=b._session)


# --------------------------------------------------------------------------------------
# B — the worker loop logs what actually happened, not a blanket "delivered"
# --------------------------------------------------------------------------------------
class WorkerLoopLogsTruthfullyTests(unittest.TestCase):
    @pytest.fixture(autouse=True)
    def _inject_caplog(self, caplog):
        self.caplog = caplog

    def _wait(self, b, timeout=2.0):
        # queue.Queue.join() only returns once task_done() has run for every item, and in the
        # worker loop the logging call happens BEFORE task_done() — so this guarantees the log
        # line we are about to check for has already been emitted.
        b._inbound_queue.join()

    def test_real_delivery_is_logged_as_delivered(self):
        with tempfile.TemporaryDirectory() as td:
            self.caplog.set_level(logging.INFO, logger=LOGGER_NAME)
            b = _bridge(state_dir=td)
            b._ensure_inbound_worker_state()
            b._handle = lambda upd: True

            b._handle_update_once(_msg(update_id=1, text="hi"), 1)
            self._wait(b)
            b._stop.set()

            self.assertIn("IN  delivered to session", self.caplog.text)
            self.assertNotIn("NOT delivered", self.caplog.text)

    def test_handled_but_undelivered_is_logged_with_its_reason(self):
        with tempfile.TemporaryDirectory() as td:
            self.caplog.set_level(logging.INFO, logger=LOGGER_NAME)
            b = _bridge(state_dir=td)
            b._ensure_inbound_worker_state()
            b._handle = lambda upd: "sender is not on the allow-list"

            b._handle_update_once(_msg(update_id=1, text="hi"), 1)
            self._wait(b)
            b._stop.set()

            self.assertIn("NOT delivered to the session: sender is not on the allow-list",
                          self.caplog.text)
            self.assertNotIn("IN  delivered to session", self.caplog.text)


# --------------------------------------------------------------------------------------
# C — `_nothing_to_forward`: name it, log it, tell the user, never crash on the telling
# --------------------------------------------------------------------------------------
class NothingToForwardTests(unittest.TestCase):
    @pytest.fixture(autouse=True)
    def _inject_caplog(self, caplog):
        self.caplog = caplog

    def test_names_the_kind_in_the_return_value(self):
        b = _bridge()
        r = b._nothing_to_forward({"message_id": 42, "sticker": {"file_id": "s1"}}, 7)
        self.assertIn("sticker", r)

    def test_message_with_no_recognised_field_gets_a_generic_label(self):
        b = _bridge()
        r = b._nothing_to_forward({"message_id": 43}, 7)
        self.assertIn("no text and no caption", r)

    def test_logs_the_kind(self):
        self.caplog.set_level(logging.INFO, logger=LOGGER_NAME)
        b = _bridge()
        b._nothing_to_forward({"message_id": 44, "video_note": {"file_id": "v1"}}, 7)
        self.assertIn("video_note", self.caplog.text)
        self.assertIn("44", self.caplog.text)

    def test_tells_the_user_in_chat(self):
        b = _bridge()
        b._nothing_to_forward({"message_id": 45, "poll": {"question": "?"}}, 7)
        self.assertEqual(len(b.tg.sent), 1)
        self.assertEqual(b.tg.sent[0][0], 7)
        self.assertIn("poll", b.tg.sent[0][1])

    def test_multiple_unreadable_fields_are_all_named(self):
        b = _bridge()
        r = b._nothing_to_forward(
            {"message_id": 46, "location": {"latitude": 1}, "contact": {"phone_number": "x"}}, 7)
        self.assertIn("location", r)
        self.assertIn("contact", r)

    def test_broken_send_message_does_not_crash_the_caller(self):
        b = _bridge(client=_BrokenClient())
        try:
            r = b._nothing_to_forward({"message_id": 47, "sticker": {"file_id": "s1"}}, 7)
        except Exception as e:
            self.fail(f"_nothing_to_forward must swallow a failed send_message, raised {e!r}")
        self.assertIsInstance(r, str)
        self.assertIn("sticker", r)
        # It DID try to tell the user — the failure is what we're tolerating, not skipping the
        # attempt.
        self.assertEqual(len(b.tg.sent), 1)

    def test_broken_send_message_still_logs_what_happened(self):
        self.caplog.set_level(logging.INFO, logger=LOGGER_NAME)
        b = _bridge(client=_BrokenClient())
        b._nothing_to_forward({"message_id": 48, "sticker": {"file_id": "s1"}}, 7)
        self.assertIn("sticker", self.caplog.text)


if __name__ == "__main__":
    unittest.main()
