"""Tests for attach-mode turn-end backstop delivery."""
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from agent2telegram import attach as attach_mod
from agent2telegram.attach import AttachBridge
from agent2telegram.config import Config
from agent2telegram import readers

from tests.test_regrese_mostu import (
    _assistant_record,
    _regrese_bridge,
    _user_record,
    _write_transcript,
)
from tests.test_unik_do_chatu import _append_transcript


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


class LateFinalAnswerRealTranscriptAndReaderTests(unittest.TestCase):
    """The five tests above all stub `_last_assistant_text` AND `_drain_transcript`, so none of
    them exercises the drain cursor (`_tpos`), the tail scan from `_tg_since`, or a dedup key
    actually produced by a reader — exactly the interaction T-0404 lives in (measured 2026-09-21
    22:36 and 2026-09-22 19:12:30). This test drives a REAL `AttachBridge` through a REAL Claude
    Code transcript file on disk and the real `ClaudeCodeReader` (same harness as
    tests/test_regrese_mostu.py / tests/test_definitive_konec_tahu.py). Deterministic and
    thread-free: no stub sits between `_drain_transcript()` / `_finish_turn()` and the file."""

    def test_final_text_appended_after_the_interim_drain_still_arrives_exactly_once(self):
        with tempfile.TemporaryDirectory() as d:
            b = _regrese_bridge(d)
            transcript = Path(d) / "transcript.jsonl"
            b._transcript = transcript
            b._tpos = 0
            b._tg_since = 0
            b._turn_active.set()
            b._turn_from_tg = False
            b._turn_text_sent = False

            # The interim message: a real user record starting the turn, a real assistant text
            # record right after it.
            _write_transcript(transcript, [
                _user_record("[TG] how's it going"),
                _assistant_record("[tg] working on it..."),
            ])
            b._drain_transcript()

            # Proof this measures the real path, not a stub: the interim text actually left the
            # bridge through _handle_event → _send_final, and ITS OWN key (produced by the real
            # reader from the transcript record, not handed in by the test) is what landed in
            # _turn_sent_keys.
            self.assertEqual(b.tg.sent, ["working on it..."],
                             "the interim text did not forward through the real drain/reader path")
            self.assertTrue(b._turn_text_sent)
            self.assertTrue(getattr(b, "_turn_sent_keys", None),
                             "the interim message's real reader-derived key never reached "
                             "_turn_sent_keys")

            # The turn's REAL final answer is appended to the transcript file AFTER that drain
            # already ran — on disk, not through any stub — reproducing the exact shape measured
            # in production: the final line lands after the drain that would have forwarded it.
            _append_transcript(transcript, [
                _assistant_record("[tg] done — here is the final answer"),
            ])

            b._finish_turn()

            self.assertEqual(
                b.tg.sent,
                ["working on it...", "done — here is the final answer"],
                "the turn's real final answer, appended to the transcript after the interim "
                "drain, must arrive — exactly once",
            )

            # "Exactly once" was only half measured above: the docstring promised it but nothing
            # here actually drained AGAIN afterwards to check for a re-send. A later drain (the
            # outbound loop keeps calling it after the turn is over) must not touch anything —
            # `_unsent_final_text()` already moved `_tpos` past both records.
            b._drain_transcript()
            self.assertEqual(
                b.tg.sent,
                ["working on it...", "done — here is the final answer"],
                "a drain after _finish_turn() re-sent the turn's final answer",
            )


class LateFinalCheckDrainsBeforeScanningTests(unittest.TestCase):
    """c51dfe4: `_unsent_final_text()` now drains FIRST on every attempt, before it tail-scans.
    The old scan-only version returned just the transcript's LAST assistant text — so when TWO
    texts landed after the last drain, the first of them was silently dropped, the very shape of
    T-0404 one message further along. Draining first lets the normal per-record path forward every
    complete record it finds, oldest first, and moves `_tpos` past them, so a later drain cannot
    resend anything. Real transcript file, real `ClaudeCodeReader`, no stubs, no threads."""

    def test_two_texts_written_after_the_last_drain_arrive_in_order_exactly_once(self):
        with tempfile.TemporaryDirectory() as d:
            b = _regrese_bridge(d)
            transcript = Path(d) / "transcript.jsonl"
            b._transcript = transcript
            b._tpos = 0
            b._tg_since = 0
            b._turn_active.set()
            b._turn_from_tg = False
            b._turn_text_sent = False

            _write_transcript(transcript, [
                _user_record("[TG] status please"),
                _assistant_record("[tg] pracuju na tom"),
            ])
            b._drain_transcript()
            self.assertEqual(b.tg.sent, ["pracuju na tom"],
                             "test setup is wrong: the interim text should have forwarded")

            # TWO final texts land after that drain already ran — the old scan-only code returned
            # only the LAST of these and silently dropped the first.
            _append_transcript(transcript, [
                _assistant_record("[tg] cast PRVNI"),
                _assistant_record("[tg] cast DRUHA"),
            ])

            b._finish_turn()

            self.assertEqual(
                b.tg.sent, ["pracuju na tom", "cast PRVNI", "cast DRUHA"],
                "both parts written after the last drain must arrive, in order",
            )

            # Nothing must be sent twice: a further drain (the outbound loop keeps calling it)
            # must find the cursor already past both records.
            b._drain_transcript()
            self.assertEqual(
                b.tg.sent, ["pracuju na tom", "cast PRVNI", "cast DRUHA"],
                "a drain after _finish_turn() re-sent one of the late final texts",
            )


class LateFinalCheckWaitsOnlyForADefinitiveEndTests(unittest.TestCase):
    """23a2e65 — Codex review, high finding. The retry loop only waited when the transcript
    already held undrained bytes (`_transcript_size() > _tpos`). When the turn's final answer had
    not STARTED writing yet at the moment of the first read, that looked exactly like "the turn
    had nothing left to say" (size == _tpos), the loop broke on the very first attempt, and the
    answer was lost even though it arrived a fraction of a second later. `definitive` now decides
    whether to wait even without growth: a Stop-hook / task_complete end means the answer exists
    somewhere, so it is worth waiting for; a non-definitive end (idle fallback, a suspiciously
    fast signal) means the turn has not really ended, so waiting would only park the outbound
    thread — and with it every OTHER delivery — for nothing.

    Real transcript file, real `ClaudeCodeReader`; the concurrent write is simulated the same way
    tests/test_definitive_konec_tahu.py's `TurnSeqGuardsAFinishingBackstopTests` simulates a
    concurrent new turn: by monkeypatching `_wait_backstop_retry` to perform the action that, in
    production, happens on a different thread while this one sleeps — deterministic, no threads,
    no reliance on wall-clock timing."""

    def setUp(self):
        self._retry_delay = attach_mod.BACKSTOP_RETRY_DELAY
        attach_mod.BACKSTOP_RETRY_DELAY = 0.0

    def tearDown(self):
        attach_mod.BACKSTOP_RETRY_DELAY = self._retry_delay

    def test_a_definitive_end_waits_for_a_final_answer_that_has_not_started_writing_yet(self):
        with tempfile.TemporaryDirectory() as d:
            b = _regrese_bridge(d)
            transcript = Path(d) / "transcript.jsonl"
            b._transcript = transcript
            b._tpos = 0
            b._tg_since = 0
            b._turn_active.set()
            b._turn_from_tg = False
            b._turn_text_sent = False

            _write_transcript(transcript, [
                _user_record("[TG] status please"),
                _assistant_record("[tg] pracuju"),
            ])
            b._drain_transcript()
            self.assertEqual(b.tg.sent, ["pracuju"],
                             "test setup is wrong: the interim text should have forwarded")
            # Nothing undrained at this point — the file has NOT grown since the last drain, the
            # exact state that used to make the old check give up on its very first look.
            self.assertEqual(b._transcript_size(), b._tpos,
                             "test setup is wrong: the transcript must be fully drained here")

            def _final_answer_starts_writing_during_the_wait():
                _append_transcript(transcript, [_assistant_record("[tg] ZAVER PSANY POZDE")])

            b._wait_backstop_retry = _final_answer_starts_writing_during_the_wait

            b._finish_turn()                          # definitive=True by default

            self.assertEqual(
                b.tg.sent, ["pracuju", "ZAVER PSANY POZDE"],
                "a definitive end must wait for the final answer even when the transcript had "
                "not grown yet at the first read",
            )

    def test_a_non_definitive_end_never_waits(self):
        with tempfile.TemporaryDirectory() as d:
            b = _regrese_bridge(d)
            transcript = Path(d) / "transcript.jsonl"
            b._transcript = transcript
            b._tpos = 0
            b._tg_since = 0
            b._turn_active.set()
            b._turn_from_tg = False
            b._turn_text_sent = False

            _write_transcript(transcript, [
                _user_record("[TG] status please"),
                _assistant_record("[tg] pracuju"),
            ])
            b._drain_transcript()
            self.assertEqual(b.tg.sent, ["pracuju"],
                             "test setup is wrong: the interim text should have forwarded")

            waits = []
            b._wait_backstop_retry = lambda: waits.append(1)

            b._finish_turn(definitive=False)

            # Counted, not timed — wall-clock is unreliable in a test, and the point is not "it
            # was fast", it is "the wait path was never entered at all".
            self.assertEqual(
                waits, [],
                "a non-definitive end (idle fallback / suspiciously fast signal) must never wait "
                "— the turn has not really ended, and waiting would park the outbound loop for "
                "nothing",
            )


class LateFinalCheckReaderMemoryIsPreservedTests(unittest.TestCase):
    """c51dfe4: the tail scan inside `_last_assistant_text()` now runs the reader's `parse()`
    under `_reader_unchanged()`. `CodexReader` keeps a bounded `deque` of the last few message
    hashes so an old Codex build's duplicate log line is not forwarded twice; re-feeding already-
    drained records into that window (as a plain, unguarded re-scan would) shifts it and can make
    the reader mistake the NEXT real reply for a duplicate and silently drop it. The scan is a
    read-only question and must leave that memory exactly as it found it."""

    def test_recent_msgs_deque_is_byte_for_byte_unchanged_after_the_scan(self):
        with tempfile.TemporaryDirectory() as d:
            b = _bridge(d)
            b._reader = readers.for_agent("codex")
            records = [
                {"type": "response_item", "timestamp": "t1",
                 "payload": {"type": "message", "role": "assistant",
                             "content": [{"type": "output_text", "text": "[tg] first reply"}]}},
                {"type": "response_item", "timestamp": "t2",
                 "payload": {"type": "message", "role": "assistant",
                             "content": [{"type": "output_text", "text": "[tg] second reply"}]}},
            ]
            b._transcript.write_text(
                "\n".join(json.dumps(r) for r in records) + "\n", "utf-8")

            before_msgs = list(b._reader._recent_msgs)
            before_users = list(b._reader._recent_users)

            text = b._last_assistant_text()

            self.assertEqual(text, "[tg] second reply",
                             "test setup is wrong: the scan should have found the last reply")
            self.assertEqual(list(b._reader._recent_msgs), before_msgs,
                             "the read-only tail scan shifted the reader's live dedup memory "
                             "(_recent_msgs)")
            self.assertEqual(list(b._reader._recent_users), before_users,
                             "the read-only tail scan shifted the reader's live dedup memory "
                             "(_recent_users)")


class LateFinalCheckLogsWhenNothingToSendTests(unittest.TestCase):
    """c51dfe4: when the late-final check finds nothing new to forward, it must say so — a quiet
    outcome that looks exactly like a broken one is how T-0404 stayed invisible for a day."""

    def setUp(self):
        self._retry_delay = attach_mod.BACKSTOP_RETRY_DELAY
        attach_mod.BACKSTOP_RETRY_DELAY = 0.0

    def tearDown(self):
        attach_mod.BACKSTOP_RETRY_DELAY = self._retry_delay

    def test_logs_a_line_when_the_transcripts_last_text_was_already_forwarded(self):
        with tempfile.TemporaryDirectory() as d:
            b = _regrese_bridge(d)
            transcript = Path(d) / "transcript.jsonl"
            b._transcript = transcript
            b._tpos = 0
            b._tg_since = 0
            b._turn_active.set()
            b._turn_from_tg = False
            b._turn_text_sent = False

            _write_transcript(transcript, [
                _user_record("[TG] status please"),
                _assistant_record("[tg] the only reply this turn sent"),
            ])
            b._drain_transcript()
            self.assertEqual(b.tg.sent, ["the only reply this turn sent"],
                             "test setup is wrong: the interim text should have forwarded")

            with self.assertLogs("agent2telegram.attach", level="INFO") as logs:
                b._finish_turn()

            self.assertEqual(b.tg.sent, ["the only reply this turn sent"],
                             "nothing new should have been sent")
            self.assertTrue(
                any("TURN END late final: nothing left to send" in line for line in logs.output),
                "the quiet branch must log that it found nothing, not stay silent",
            )


class LateFinalCheckRequiresATranscriptTests(unittest.TestCase):
    """c51dfe4: the late-final branch now gates on `self._transcript is not None`, not on
    `_has_turn_end_backstop_source()` (transcript OR signal file). A bridge configured with only a
    signal file has nothing this check can read — it must not run at all, and must not crash."""

    def test_no_transcript_configured_the_check_does_not_run(self):
        with tempfile.TemporaryDirectory() as d:
            b = _bridge(d)
            b._transcript = None
            b._signal.write_text("[tg] stale signal text", "utf-8")
            b._turn_text_sent = True
            b._turn_sent_keys = {"interim-key"}
            b.tg.sent.append((7, "interim reply"))

            def unexpected_scan(*a, **k):
                raise AssertionError("the late-final check ran with no transcript configured")

            b._unsent_final_text = unexpected_scan

            b._finish_turn()                     # must not raise

            self.assertEqual(b.tg.sent, [(7, "interim reply")],
                             "a configuration with only a signal file must not be read by the "
                             "late-final check")
            self.assertFalse(b._turn_active.is_set())


class DurableOutboxRepliesAreNotResentByTheLateFinalCheckTests(unittest.TestCase):
    """Replaces test_final_answer_already_queued_to_the_durable_outbox_is_not_sent_twice, which
    had "durable outbox" in its name but ran with `_use_durable_outbox=False` and hand-set
    `_turn_sent_keys` itself — it never touched a real outbox, so the name was a lie about what it
    measured. This one turns the durable outbox ON and drives a REAL `DurableOutbox`: the interim
    reply is genuinely enqueued to disk and NOT yet confirmed by Telegram (`_sent_keys` stays
    empty), and the late-final check at turn end must recognise it as already forwarded — from
    `_turn_sent_keys`/the real outbox record — and not resend it.

    First cut of this test was green even with `_turn_forwarded` disabled entirely (as if the
    turn's key check did not exist), because `DurableOutbox.enqueue()` has its OWN dedup by key
    and silently absorbed the "duplicate" — so the test measured `durable.py`, not this fix. It
    asserts on the log line now, which pins WHICH branch ran: "nothing left to send" (the key was
    recognised as already forwarded, the check that actually belongs to this fix) versus "late
    final → forwarded" (the check thought it was new and tried to resend it, and got saved only by
    the outbox's own dedup)."""

    def test_reply_genuinely_enqueued_to_the_durable_outbox_is_not_resent(self):
        with tempfile.TemporaryDirectory() as d:
            b = _regrese_bridge(d)
            b._use_durable_outbox = True          # the thing the old test's name promised
            transcript = Path(d) / "transcript.jsonl"
            b._transcript = transcript
            b._tpos = 0
            b._tg_since = 0
            b._turn_active.set()
            b._turn_from_tg = False
            b._turn_text_sent = False

            _write_transcript(transcript, [
                _user_record("[TG] status please"),
                _assistant_record("[tg] queued reply"),
            ])
            b._drain_transcript()

            # Proof this measures a real outbox, not a stub: a real record sits on disk, and
            # Telegram has not confirmed it yet — only the outbound consumer may do that.
            queued = b._ensure_outbox().head()
            self.assertIsNotNone(queued, "the interim reply was never enqueued to the outbox")
            self.assertEqual(b.tg.sent, [], "the outbox consumer, not the producer, must send")
            self.assertTrue(b._turn_text_sent)

            with self.assertLogs("agent2telegram.attach", level="INFO") as logs:
                b._finish_turn()

            self.assertTrue(
                any("TURN END late final: nothing left to send" in line for line in logs.output),
                "the late-final check did not recognise the queued reply as already forwarded by "
                "THIS turn — with the key check disabled it would still look clean here, because "
                "durable.py's own enqueue() dedup absorbs the resend silently",
            )
            self.assertFalse(
                any("TURN END late final → forwarded" in line for line in logs.output),
                "the late-final check tried to resend a reply already sitting in the outbox — "
                "only durable.py's own key dedup kept it from going out twice",
            )
            self.assertEqual(b.tg.sent, [],
                             "a reply already sitting in the durable outbox was forwarded again "
                             "by the late-final check")
            still_queued = b._ensure_outbox().head()
            self.assertIsNotNone(still_queued,
                                 "the late-final check must not touch a record it did not send")


class TechnicalBubbleIsClearedAfterTheLateFinalDrainTests(unittest.TestCase):
    """N1 (03d3f1d): the drain `_unsent_final_text()` runs at turn end can hand `_handle_event` a
    `tool_use` record — AFTER the `_status_clear()` at the top of `_finish_turn()`, and while
    `_turn_active` is still set (it is not cleared until the very end). Nothing used to clear the
    bubble `_status_push()` creates for it there: the turn's Telegram origin drops a few lines
    below, so no LATER drain may touch it either, and it hung in the chat under the answer until
    the next Telegram turn ended or a restart swept the orphan — a recurrence of the 2026-08-02
    "stuck bubble" incident through a different door.

    Measured on 6b5f8ec (before this fix): ``status {'mid': 55, 'shown': '🛠️ make build'}``,
    ``deleted []``.

    Needs a client with `send_plain_id`/`edit_plain` (the status-bubble API) — the
    `tests.test_attach_backstop._FakeClient`, not `tests.test_v2_durability._Client` that
    `_regrese_bridge()` wires in by default."""

    def test_bubble_created_by_the_late_final_drain_is_cleared(self):
        with tempfile.TemporaryDirectory() as d:
            b = _regrese_bridge(d)
            b.tg = _FakeClient()
            transcript = Path(d) / "transcript.jsonl"
            b._transcript = transcript
            b._tpos = 0
            b._tg_since = 0
            b._turn_active.set()
            b._turn_from_tg = False
            b._turn_text_sent = False

            _write_transcript(transcript, [
                _user_record("[TG] build it"),
                _assistant_record("[tg] working on it..."),
            ])
            b._drain_transcript()
            self.assertEqual(b.tg.sent, [(7, "working on it...")],
                             "test setup is wrong: the interim text should have forwarded")

            # ONE assistant record carrying both the turn's real final text and a tool call —
            # written after the interim drain already ran. The reader emits text first, then the
            # tool call, so the late-final drain forwards the final answer AND pushes a technical
            # bubble for the tool call, in that order, inside the same _finish_turn().
            _append_transcript(transcript, [{
                "type": "assistant",
                "message": {"content": [
                    {"type": "text", "text": "[tg] done building"},
                    {"type": "tool_use", "id": "toolu_1", "name": "Bash",
                     "input": {"description": "make build"}},
                ]},
            }])

            b._finish_turn()

            self.assertIn((7, "done building"), b.tg.sent,
                          "test setup is wrong: the late final text should still have forwarded")
            self.assertEqual(b._status, {"mid": None, "shown": ""},
                             "the technical bubble the late-final drain created was left behind")
            self.assertIn((7, 55), b.tg.deleted,
                          "the technical bubble created by the late-final drain was never deleted")


class DrainFailureInsideTheLateFinalCheckIsLoudTests(unittest.TestCase):
    """N2 (03d3f1d): a `_drain_transcript()` failure inside `_unsent_final_text()`'s retry loop
    used to log at DEBUG — below the operational floor — even though `_drain_transcript` moves
    `_tpos` past the WHOLE chunk it read before it processes a single record, so a failure there
    loses everything in that chunk except whatever the tail scan can still recover. The very same
    failure from the outbound loop's own drain call logs as ERROR; this one is now WARNING."""

    def setUp(self):
        self._retry_delay = attach_mod.BACKSTOP_RETRY_DELAY
        attach_mod.BACKSTOP_RETRY_DELAY = 0.0

    def tearDown(self):
        attach_mod.BACKSTOP_RETRY_DELAY = self._retry_delay

    def test_drain_failure_logs_a_warning_and_the_turn_still_finishes(self):
        with tempfile.TemporaryDirectory() as d:
            b = _bridge(d)
            b._turn_text_sent = True                 # an interim reply already went out
            b._turn_sent_keys = {"interim-key"}
            b._sent_keys.add("interim-key")
            b.tg.sent.append((7, "interim reply"))

            def broken_drain():
                raise OSError("transcript file vanished mid-read")

            b._drain_transcript = broken_drain
            # The tail scan is the only thing left once the drain is broken — it must still find
            # and deliver the turn's real final answer.
            b._last_assistant_text = lambda: "[tg] the real final answer"
            b._last_backstop_key = "final-key"

            with self.assertLogs("agent2telegram.attach", level="WARNING") as logs:
                b._finish_turn()

            self.assertTrue(
                any("turn-end final check: transcript drain failed" in line
                    for line in logs.output),
                "a broken drain inside the late-final check must be logged at WARNING, not "
                "swallowed at DEBUG below the operational floor",
            )
            self.assertEqual(
                b.tg.sent, [(7, "interim reply"), (7, "the real final answer")],
                "the turn's real final answer must still arrive via the tail scan even when the "
                "drain inside the late-final check is broken",
            )
            self.assertFalse(b._turn_active.is_set(), "the turn must still finish despite the "
                             "drain failure")


class TurnSeqGuardsTheLateFinalCleanupTests(unittest.TestCase):
    """3f6be5a — Codex review, high finding. `_finish_turn` runs on the outbound thread and can
    WAIT inside `_unsent_final_text()`: it drains and sleeps for a record still being written. If
    a Telegram message lands in that window, the inbound thread opens a NEW turn — bumps
    `_turn_seq`, sets `_turn_active`, and that new turn gets its own status bubble as ITS OWN
    drain forwards a tool call. The OLD turn, still sitting in the late-final check, used to reach
    the bottom of `_finish_turn()` and unconditionally clear `_turn_active` and the status bubble
    — taking away the NEW turn's typing indicator and bubble. At the new turn's own end,
    `was_active` then reads False, so NEITHER backstop runs and ITS final answer is lost without a
    trace: the exact defect this whole file exists to close, reproduced by the fix meant to close
    it. `_status_clear()`/`_turn_active.clear()` are now guarded by the same `_turn_seq ==
    seq_at_entry` check `_turn_from_tg`/`_tg_since` already had.

    Placed here rather than next to `TurnSeqGuardsAFinishingBackstopTests` in
    test_definitive_konec_tahu.py: that class drives the guard through a heavier real-transcript-
    via-`_handle()` harness for the OLD backstop path (`_retry_last_assistant_text`); this guards
    the NEW late-final branch (`_unsent_final_text`) that the rest of this file's `LateFinalCheck*`
    classes already measure with the lightweight `_bridge()` harness and stubbed
    `_last_assistant_text`/`_drain_transcript` — reproducing the heavier harness here for one
    shared guard line would not buy more assurance, so this follows the same deterministic,
    thread-free simulation the rest of the file already uses."""

    def setUp(self):
        self._retry_delay = attach_mod.BACKSTOP_RETRY_DELAY
        attach_mod.BACKSTOP_RETRY_DELAY = 0.0

    def tearDown(self):
        attach_mod.BACKSTOP_RETRY_DELAY = self._retry_delay

    def test_a_turn_that_begins_during_the_late_final_wait_keeps_its_own_state(self):
        with tempfile.TemporaryDirectory() as d:
            b = _bridge(d)
            b._turn_text_sent = True                 # an interim reply already went out
            b._turn_sent_keys = {"interim-key"}
            b._sent_keys.add("interim-key")
            b.tg.sent.append((7, "interim reply"))
            seq_at_entry = b._turn_seq = 1

            def _new_turn_begins_while_the_old_one_waits():
                # Stands in for the inbound thread: a Telegram message lands WHILE this
                # _finish_turn() is still inside _unsent_final_text(), opens a brand-new turn
                # (_begin_turn() bumps _turn_seq and sets _turn_active), which then gets its OWN
                # status bubble from its own drain forwarding a tool call.
                b._turn_seq = seq_at_entry + 1
                b._turn_active.set()
                b._status = {"mid": 55, "shown": "🛠️ the new turn's own tool call"}
                b._last_backstop_key = "final-key"
                return "[tg] the real final answer"

            b._last_assistant_text = _new_turn_begins_while_the_old_one_waits
            b._drain_transcript = lambda: None

            b._finish_turn()

            self.assertTrue(
                b._turn_active.is_set(),
                "the finishing turn cleared _turn_active out from under the NEW turn that began "
                "while it was still waiting inside the late-final check",
            )
            self.assertEqual(
                b._status, {"mid": 55, "shown": "🛠️ the new turn's own tool call"},
                "the finishing turn deleted the NEW turn's own status bubble",
            )
            self.assertEqual(b.tg.deleted, [],
                             "the new turn's status bubble was deleted by the OLD turn finishing")

    def test_without_a_turn_seq_change_the_old_turns_own_bubble_is_still_cleared(self):
        """Negative control for the guard above, measured from the other side: with the
        finishing turn's OWN bubble appearing during the same late-final wait (its own drain
        forwarding a tool_use record) and NO new turn opening in between, the second cleanup must
        still run — the guard must not quietly turn into "the second cleanup never happens"."""
        with tempfile.TemporaryDirectory() as d:
            b = _bridge(d)
            b._turn_text_sent = True
            b._turn_sent_keys = {"interim-key"}
            b._sent_keys.add("interim-key")
            b.tg.sent.append((7, "interim reply"))
            b._turn_seq = 1

            def _own_bubble_appears_but_no_new_turn_starts():
                # Same shape as the drain inside _unsent_final_text() pushing a bubble for a
                # tool_use record belonging to THIS turn — _turn_seq stays put, nothing new began.
                b._status = {"mid": 77, "shown": "🛠️ this turn's own tool call"}
                b._last_backstop_key = "final-key"
                return "[tg] the real final answer"

            b._last_assistant_text = _own_bubble_appears_but_no_new_turn_starts
            b._drain_transcript = lambda: None

            b._finish_turn()

            self.assertFalse(b._turn_active.is_set(),
                             "the guard blocked cleanup even though no new turn ever started")
            self.assertEqual(b._status, {"mid": None, "shown": ""},
                             "the guard blocked the finishing turn's own bubble cleanup")
            self.assertIn((7, 77), b.tg.deleted,
                          "the finishing turn's own bubble, created during the same wait, was "
                          "never cleared — the seq guard must not mean cleanup stops altogether")

    def test_a_new_turns_pending_turn_end_survives_the_old_turn_finishing(self):
        """23a2e65 — Codex review, medium finding. The drain inside the late-final check can read
        the NEW turn's own `turn_end` marker (Codex writes `task_complete` to the rollout) while
        the OLD turn is still finishing. Dropping `_pending_turn_end` / consuming the `_turn_end`
        marker file unconditionally threw the new turn's own end away: it never finishes
        definitively, its Telegram origin never lowers, and the next purely local output in the
        pane would leak into the chat. `_pending_turn_end = False` and `_consume_turn_end()` are
        now guarded by the same `_turn_seq == seq_at_entry` check as the rest of this cleanup."""
        with tempfile.TemporaryDirectory() as d:
            b = _bridge(d)
            b._turn_text_sent = True
            b._turn_sent_keys = {"interim-key"}
            b._sent_keys.add("interim-key")
            b.tg.sent.append((7, "interim reply"))
            b._turn_end = Path(d) / "turn_end"
            seq_at_entry = b._turn_seq = 1

            def _new_turns_task_complete_is_read_mid_check():
                # Stands in for the drain: the NEW turn's own end-of-turn signal shows up while
                # this _finish_turn() (belonging to the OLD turn) is still inside the check.
                b._turn_seq = seq_at_entry + 1
                b._pending_turn_end = True
                b._turn_end.write_text("", "utf-8")
                b._last_backstop_key = "final-key"
                return "[tg] the real final answer"

            b._last_assistant_text = _new_turns_task_complete_is_read_mid_check
            b._drain_transcript = lambda: None

            b._finish_turn()

            self.assertTrue(b._pending_turn_end,
                            "the finishing turn discarded the NEW turn's own end-of-turn signal")
            self.assertTrue(b._turn_end.exists(),
                            "the finishing turn consumed the NEW turn's own end-of-turn marker "
                            "file")

    def test_without_a_turn_seq_change_the_pending_turn_end_is_still_consumed(self):
        """Negative control for the guard above, measured from the other side: with NO new turn
        opening in between, the finishing turn's own end-of-turn bookkeeping must still be
        cleared — the guard must not mean this cleanup stops altogether either."""
        with tempfile.TemporaryDirectory() as d:
            b = _bridge(d)
            b._turn_text_sent = True
            b._turn_sent_keys = {"interim-key"}
            b._sent_keys.add("interim-key")
            b.tg.sent.append((7, "interim reply"))
            b._turn_end = Path(d) / "turn_end"
            b._turn_seq = 1

            def _this_turns_own_end_marker_is_read():
                b._pending_turn_end = True
                b._turn_end.write_text("", "utf-8")
                b._last_backstop_key = "final-key"
                return "[tg] the real final answer"

            b._last_assistant_text = _this_turns_own_end_marker_is_read
            b._drain_transcript = lambda: None

            b._finish_turn()

            self.assertFalse(b._pending_turn_end,
                             "the guard blocked cleanup even though no new turn ever started")
            self.assertFalse(b._turn_end.exists(),
                             "the guard blocked consuming the finishing turn's own end-of-turn "
                             "marker file")


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
