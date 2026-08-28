"""Delivery guarantees of the bridge — reproductions of the 2026-07-31 audit findings.

Every test here corresponds to one finding and is written DELIBERATELY so that it fails against
the code as it stood. Only then was the code fixed. The letters are the audit's own labels:

  A – an inbound message is lost when the write into tmux fails
  B – Telegram is told the update was handled before the message is safely stored
  C – a turn ended by silence bypasses the guard against an unanswered turn
  D – if the network fails mid-way through a long message, already delivered parts are re-sent
  F – attachments are not in the durable queue, so they are lost or land on the wrong reply
  K – state is not separated per bot, only by an environment variable

Background: messages were disappearing without a trace. A monitor was killing the bridge four
times a day, and finding B says every one of those kills could have eaten an arriving message
for good.
"""
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

from agent2telegram import attach as attach_mod
from agent2telegram.attach import AttachBridge
from agent2telegram.compat import AlreadyRunning, single_instance_lock
from agent2telegram.config import Config, _state_dir
from agent2telegram.session import SessionError
from agent2telegram.telegram import TelegramError, split_message


class _Client:
    """A minimal fake Telegram client — records what actually went out."""

    def __init__(self):
        self.sent = []
        self.files = []
        self.actions = []

    def send_chat_action(self, chat_id, action="typing"):
        self.actions.append((chat_id, action))

    def send_message(self, chat_id, text, parse_mode=None):
        self.sent.append(text)

    def send_file(self, chat_id, path, caption=None, **kw):
        self.files.append(path)

    def delete_message(self, chat_id, message_id):
        pass


class _ChunkingClient(_Client):
    """A faithful model of how a long message is split.

    Telegram receives the chunks one by one and **does not undo the ones already delivered** when
    a later chunk fails. That behaviour is exactly what turns "just send the whole thing again"
    into a duplicate.
    """

    def __init__(self, fail_on_chunk=2):
        super().__init__()
        self.delivered = []
        self.fail_on_chunk = fail_on_chunk
        self.fail_armed = True

    def send_message(self, chat_id, text, parse_mode=None):
        for i, chunk in enumerate(split_message(text) or [text], start=1):
            if self.fail_armed and i == self.fail_on_chunk:
                self.fail_armed = False          # the network recovers after the outage
                raise TelegramError("connection reset by peer")
            self.delivered.append(chunk)


class _DeadSession:
    """A tmux pane that cannot be written to — a frozen TUI, a full buffer."""

    def __init__(self, exc=None):
        self.exc = exc or SessionError(
            "Command ['tmux', 'send-keys', ...] timed out after 10 seconds"
        )
        self.injected = []

    def inject(self, text):
        raise self.exc


class _OkSession:
    def __init__(self):
        self.injected = []

    def inject(self, text):
        self.injected.append(text)


def _bridge(state_dir, client=None, session=None):
    """A hand-assembled bridge — same approach as in test_attach_queue.py."""
    b = object.__new__(AttachBridge)
    b.cfg = Config(agent="generic", token="1:2", allowed_user_ids=[7], tmux_session="a2t")
    b.tg = client or _Client()
    b._allowed = {7}
    b._owner_chat = 7
    b._turn_end = None
    b._session = session or _OkSession()
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
    state = Path(state_dir)
    b._offset_file = state / "offset"
    b._processed_updates_file = state / "processed_updates"
    b._queue_path = state / "outbox.json"
    b._sent_path = state / "sent_uuids"        # dedup ledger (used by _mark_sent)
    b._processed_update_ids, b._processed_update_order = b._read_processed_updates()
    return b


def _msg(update_id, text="hi"):
    return {
        "update_id": update_id,
        "message": {"message_id": update_id, "from": {"id": 7}, "chat": {"id": 7}, "text": text},
    }


# --------------------------------------------------------------------------------------
# A – the write into tmux fails
# --------------------------------------------------------------------------------------
class InjectFailureTests(unittest.TestCase):
    def test_failed_inject_does_not_silently_drop_the_message(self):
        """When the write into the window fails, the message MUST NOT vanish without a trace.

        Taken from production:
          19:04:41 ERROR inject failed: '[TG] are you there?' timed out after 10 seconds
        That message was lost and the sender never found out.

        Either outcome is acceptable: the message is kept for a retry, or the user is told.
        """
        with tempfile.TemporaryDirectory() as td:
            b = _bridge(td, session=_DeadSession())

            ok = b._inject("[TG] are you there?")
            self.assertFalse(ok, "inject was supposed to fail — that is the premise of this test")

            # Matched against the text the bridge really sends, not a guess: _notify_inject_failed
            # says "Couldn't deliver your message … failed after N attempts".
            notified = any("couldn't deliver" in s.lower() or "failed after" in s.lower()
                           for s in b.tg.sent)
            retained = bool(getattr(b, "_pending_inbound", None)) or bool(
                list(Path(td).glob("inbox/*"))
            )
            self.assertTrue(
                notified or retained,
                "the message vanished: not stored for a retry and the user was not told",
            )


# --------------------------------------------------------------------------------------
# B – the Telegram acknowledgement runs ahead of processing
# --------------------------------------------------------------------------------------
class InboundDurabilityTests(unittest.TestCase):
    def test_crash_between_ack_and_queue_does_not_lose_the_update(self):
        """A crash after the offset moves but before the update is queued.

        Telegram treats an update as handled the moment a higher offset is requested — it never
        sends it again. If the process dies exactly here (which a monitor's SIGTERM really did),
        the message is gone for good.
        """
        with tempfile.TemporaryDirectory() as td:
            b = _bridge(td)

            def _die(upd, record_id=None):
                raise SystemExit("killed mid-processing")

            b._submit_inbound_update = _die
            with self.assertRaises(SystemExit):
                b._handle_update_once(_msg(1000), 1000)

            # Restart over the same state:
            b2 = _bridge(td)
            offset = b2._load_offset()
            inbox = list(Path(td).glob("inbox/*"))
            self.assertTrue(
                offset <= 1000 or inbox,
                f"update 1000 is irretrievably gone: offset={offset} and no durable inbox",
            )

    def test_pending_message_is_delivered_after_restart(self):
        """A stored message must ACTUALLY be delivered after a restart, not merely sit on disk.

        Found in cross-review: the write worked, but nothing read the store at startup. Telegram
        will not send the message again, so it would have lain there until retention expired.
        The test therefore checks `session.injected`, not the presence of a file — a file proves
        nothing.
        """
        with tempfile.TemporaryDirectory() as td:
            b = _bridge(td)

            def _die(upd, record_id=None):
                raise SystemExit("killed mid-processing")

            b._submit_inbound_update = _die
            with self.assertRaises(SystemExit):
                b._handle_update_once(_msg(4000, "do not lose me"), 4000)

            # restart over the same state
            session = _OkSession()
            b2 = _bridge(td, session=session)
            b2._ensure_inbound_worker_state()
            b2._replay_pending_inbound()
            for _ in range(100):
                if session.injected:
                    break
                time.sleep(0.02)
            b2._stop.set()
            time.sleep(0.3)

            self.assertTrue(session.injected, "the message stayed on disk and was never delivered")
            self.assertIn("do not lose me", "\n".join(session.injected))

    def test_handler_failure_keeps_the_message_for_retry(self):
        """When processing fails, the worker must not just log the error and drop the message."""
        with tempfile.TemporaryDirectory() as td:
            b = _bridge(td)
            b._ensure_inbound_worker_state()
            calls = []

            def _boom(upd):
                calls.append(upd)
                raise RuntimeError("tmux is dead")

            b._handle = _boom
            # Through _handle_update_once rather than a direct submit — otherwise the message
            # would bypass the inbox reservation and the test would measure something other than
            # what happens in production.
            b._handle_update_once(_msg(2000), 2000)
            for _ in range(50):
                if calls:
                    break
                time.sleep(0.02)
            b._stop.set()
            time.sleep(0.3)

            self.assertTrue(calls, "the handler never ran — the test itself is wrong")
            retained = bool(getattr(b, "_pending_inbound", None)) or bool(
                list(Path(td).glob("inbox/*"))
            )
            self.assertTrue(retained, "the message was dropped after the handler failed, with no retry")

    def test_undelivered_message_stays_for_retry(self):
        """Processing completes without raising, but the message never reaches the session.

        Added after a mutation check: mutating `if delivered is False:` into `if False:` survived,
        because the tests only covered a handler raising, not a silent failure. This is precisely
        the case that occurs when tmux is frozen — the most common one in production.
        """
        with tempfile.TemporaryDirectory() as td:
            b = _bridge(td)
            b._ensure_inbound_worker_state()
            calls = []

            def _not_delivered(upd):
                calls.append(upd)
                return False

            b._handle = _not_delivered
            b._handle_update_once(_msg(3000), 3000)
            for _ in range(50):
                if calls:
                    break
                time.sleep(0.02)
            b._stop.set()
            time.sleep(0.3)

            self.assertTrue(calls, "the handler never ran — the test itself is wrong")
            self.assertTrue(
                list(Path(td).glob("inbox/*")),
                "an undelivered message vanished: the record was deleted although it never reached the session",
            )
            retained = bool(getattr(b, "_pending_inbound", None)) or bool(
                list(Path(td).glob("inbox/*"))
            )
            self.assertTrue(retained, "the message was dropped after the handler failed, with no retry")


# --------------------------------------------------------------------------------------
# C – an idle turn end bypasses the backstop
# --------------------------------------------------------------------------------------
class TurnEndBackstopTests(unittest.TestCase):
    def test_idle_turn_end_still_runs_the_backstop(self):
        """A turn ended by silence must pass the same backstop as a turn ended by the hook.

        Of the three turn-end branches, only two used to call the backstop. In the third (90 s of
        silence) the turn closed quietly — no reply was sent and not a line was left in the log.
        """
        with tempfile.TemporaryDirectory() as td:
            b = _bridge(td)
            finished = []
            # `*a, **k`: _finish_turn takes a `definitive` keyword — an idle end must run
            # the backstop (what this test measures) but must NOT lower the turn's
            # Telegram origin, because silence is not the end of the agent's work.
            b._finish_turn = lambda *a, **k: finished.append(True)
            for name in ("_maybe_reresolve", "_flush_pending", "_drain_transcript",
                         "_drain_signal", "_beat", "_status_clear"):
                setattr(b, name, lambda *a, **k: None)

            b._turn_active.set()
            b._turn_from_tg = True
            b._turn_text_sent = False
            b._last_activity = time.monotonic() - 10_000     # silent for a long time

            orig_idle = attach_mod.IDLE_DONE
            attach_mod.IDLE_DONE = 0.01
            try:
                t = threading.Thread(target=b._outbound_loop, daemon=True)
                t.start()
                time.sleep(0.8)
                b._stop.set()
                t.join(timeout=3)
            finally:
                attach_mod.IDLE_DONE = orig_idle

            self.assertTrue(
                finished,
                "the turn ended in silence and the backstop never ran — the reply was lost without a trace",
            )


# --------------------------------------------------------------------------------------
# D – already delivered parts of a long message are duplicated
# --------------------------------------------------------------------------------------
class ChunkRedeliveryTests(unittest.TestCase):
    def test_confirmed_chunks_are_not_resent_after_a_mid_message_failure(self):
        """If the second part of a long message fails, the first is already delivered and must not repeat."""
        with tempfile.TemporaryDirectory() as td:
            client = _ChunkingClient(fail_on_chunk=2)
            b = _bridge(td, client=client)
            long_text = "\n".join(f"line {i}" for i in range(3000))
            self.assertGreater(len(split_message(long_text)), 1, "the text must actually be split")

            b._send_final(long_text)          # durable enqueue only
            b._flush_pending()                # chunk 1 goes through, chunk 2 fails
            b._flush_pending()                # network recovered -> deliver the rest

            first = split_message(long_text)[0]
            self.assertEqual(
                client.delivered.count(first), 1,
                f"the first part arrived {client.delivered.count(first)}×; "
                "the whole text is re-queued instead of only the undelivered remainder",
            )


# --------------------------------------------------------------------------------------
# F – attachments outside the durable queue
# --------------------------------------------------------------------------------------
class AttachmentDurabilityTests(unittest.TestCase):
    def test_attachment_survives_a_failed_text_send(self):
        """When the text fails and is queued, the attachment must go with it, not stay in memory."""
        with tempfile.TemporaryDirectory() as td:
            payload = Path(td) / "waveform.png"
            payload.write_bytes(b"PNG")

            class _FailingOnce(_Client):
                def __init__(self):
                    super().__init__()
                    self.armed = True

                def send_message(self, chat_id, text, parse_mode=None):
                    if self.armed:
                        self.armed = False
                        raise TelegramError("connection reset by peer")
                    self.sent.append(text)

            client = _FailingOnce()
            b = _bridge(td, client=client)
            b.cfg.file_marker = "[tg-file]"
            # Without this the bridge would refuse the attachment as a path outside the allowed
            # directories (correctly) and the test would measure that guard, not queue durability.
            b.cfg.outbox_dirs = [td]

            b._send_final(f"Here is the waveform\n[tg-file] {payload}")
            b._flush_pending()  # first outbound tick: the text fails
            b._flush_pending()  # next tick: both text and attachment are delivered

            self.assertIn("Here is the waveform", "\n".join(client.sent), "the text did not arrive even on the second attempt")
            # resolve() on both sides: on macOS /tmp is a symlink to /private/tmp and the bridge
            # resolves the path. On Linux they are identical, so the comparison holds on both.
            self.assertEqual(
                [Path(p).resolve() for p in client.files], [payload.resolve()],
                "the attachment was not stored with the queue — after a restart it would vanish, "
                "or land on a completely different reply",
            )


# --------------------------------------------------------------------------------------
# K – state separated per bot
# --------------------------------------------------------------------------------------
class StateNamespaceTests(unittest.TestCase):
    def test_env_less_default_dir_is_shared_by_design(self):
        """Finding K, rejected: the default path is DELIBERATELY shared (see `_state_dir`).

        A per-bot default would break upgrades (an empty path resets the offset, which replays
        the whole backlog) and would only help a user who does not exist (two different bots with
        no env var). What protects against two instances over one path is NOT separate
        directories but the lock — guarded by `test_second_process_cannot_take_the_same_state_dir`
        below. The token never appears in the path.
        """
        old = os.environ.get("AGENT2TELEGRAM_STATE")
        os.environ.pop("AGENT2TELEGRAM_STATE", None)   # default path, as with a manual start
        try:
            a = _state_dir(Config(agent="generic", token="111:AAA", tmux_session="a"))
            c = _state_dir(Config(agent="generic", token="222:BBB", tmux_session="b"))
        finally:
            if old is not None:
                os.environ["AGENT2TELEGRAM_STATE"] = old

        self.assertEqual(str(a), str(c), "the default path is meant to be shared (finding K was rejected)")
        for path in (str(a), str(c)):
            self.assertNotIn("111:AAA", path, "the token must not be in the path — it shows up in ps and in backups")
            self.assertNotIn("222:BBB", path)

    def test_second_process_cannot_take_the_same_state_dir(self):
        """An explicit `AGENT2TELEGRAM_STATE` wins (a keepalive sets it per bridge), so sharing
        cannot be ruled out by the path. The lock has to rule it out — otherwise two pollers fight
        over getUpdates (409) and messages disappear."""
        with tempfile.TemporaryDirectory() as td:
            lock = Path(td) / "bridge.lock"
            with single_instance_lock(lock):
                with self.assertRaises(AlreadyRunning):
                    with single_instance_lock(lock):
                        self.fail("a second instance over the same state should not have started")
            # once released the lock must be takeable again, or a restart would block itself
            with single_instance_lock(lock):
                pass


if __name__ == "__main__":
    unittest.main()


# --------------------------------------------------------------------------------------
# Second round – findings from cross-review
# --------------------------------------------------------------------------------------
class OutboxBlockingTests(unittest.TestCase):
    def test_permanently_rejected_file_does_not_block_later_replies(self):
        """An attachment that is pointless to retry must not block the FIFO queue.

        `send_file` raises TelegramError for PERMANENT cases too (a file over 50 MB, a deleted
        file, HTTP 400). The drain treated those as transient and kept the record at the head of
        the queue, so it retried forever and NO further reply reached the user — the
        "Telegram has been down all day" class of failure.
        """
        with tempfile.TemporaryDirectory() as td:
            oversized = Path(td) / "clip.mov"
            oversized.write_bytes(b"x")

            class _RejectsFile(_Client):
                def send_file(self, chat_id, path, caption=None, **kw):
                    raise TelegramError("file is too big")

            client = _RejectsFile()
            b = _bridge(td, client=client)
            b.cfg.file_marker = "[tg-file]"
            b.cfg.outbox_dirs = [td]

            b._send_final(f"[tg-file] {oversized}")
            b._send_final("this message must arrive anyway")
            for _ in range(6):        # the attempt cap has to be exhausted within the loop
                b._flush_pending()

            self.assertIn("this message must arrive anyway", "\n".join(client.sent),
                          "a stuck attachment blocked every later reply")
            self.assertTrue(any("Couldn't send" in s for s in client.sent),
                            "a refused attachment must be reported, not silently dropped")
            # An abandoned attachment MUST NOT be recorded as delivered — that would be a silent
            # loss inside our own bookkeeping (a regression caught in a later review round).
            # It belongs in dead-letter, where it can be found.
            # Since the collision fix the queue has its own "queue" subdirectory
            # (see OutboxDirCollisionTests).
            dead = list((Path(td) / "queue" / "dead-letter").rglob("*.json"))
            self.assertTrue(dead, "the abandoned attachment was not written to dead-letter")
            self.assertNotIn(str(oversized), [str(p) for p in client.files],
                             "the attachment never went out, so it must not be recorded as sent")

    def test_file_only_reply_goes_through_the_durable_queue(self):
        """A reply consisting of ONLY an attachment must go through the queue like any other.

        Found in review: empty text sent the reply down the old path, ahead of the queue, and that
        path clears the file path from memory before the upload succeeds. A network blip then lost
        the attachment even though durability was supposedly finished.
        """
        with tempfile.TemporaryDirectory() as td:
            payload = Path(td) / "waveform.png"
            payload.write_bytes(b"PNG")

            class _FailsOnce(_Client):
                def __init__(self):
                    super().__init__()
                    self.armed = True

                def send_file(self, chat_id, path, caption=None, **kw):
                    if self.armed:
                        self.armed = False
                        raise TelegramError("connection reset by peer")
                    self.files.append(path)

            client = _FailsOnce()
            b = _bridge(td, client=client)
            b.cfg.file_marker = "[tg-file]"
            b.cfg.outbox_dirs = [td]

            b._send_final(f"[tg-file] {payload}")   # durable enqueue only
            b._flush_pending()                       # first outbound tick: the network fails
            b._flush_pending()                       # next tick: the network has recovered

            self.assertEqual([Path(p).resolve() for p in client.files], [payload.resolve()],
                             "an attachment with no text was lost after a network blip")


class LiveRetryTests(unittest.TestCase):
    def test_undelivered_message_is_retried_without_a_restart(self):
        """An undelivered message must be retried WHILE RUNNING, not only after a restart.

        Found in review: delivery of stored messages lived only in the startup path. For a service
        that runs for weeks that means "practically never" — the message would wait for the next
        crash or update.
        """
        with tempfile.TemporaryDirectory() as td:
            class _DeadThenAlive:
                """A tmux that wakes up after a while — like a frozen pane coming back."""
                def __init__(self):
                    self.injected = []
                    self.alive = False

                def inject(self, text):
                    if not self.alive:
                        raise SessionError("tmux send-keys timed out")
                    self.injected.append(text)

            session = _DeadThenAlive()
            b = _bridge(td, session=session)
            b._ensure_inbound_worker_state()

            b._handle_update_once(_msg(5000, "deliver me later"), 5000)
            for _ in range(60):
                if not b._inbound_inflight():
                    break
                time.sleep(0.02)
            self.assertFalse(session.injected, "the message should not have been delivered — tmux was dead")
            self.assertTrue(list(Path(td).glob("inbox/*")), "the message was not stored for a retry")

            session.alive = True         # the pane woke up, WITHOUT restarting the bridge
            b._replay_pending_inbound()
            for _ in range(100):
                if session.injected:
                    break
                time.sleep(0.02)
            b._stop.set()
            time.sleep(0.3)

            self.assertTrue(session.injected, "the message was never retried while running")
            self.assertIn("deliver me later", "\n".join(session.injected))

    def test_live_retry_does_not_deliver_the_same_message_twice(self):
        """A retry must not queue the same message a second time while it is being processed."""
        with tempfile.TemporaryDirectory() as td:
            session = _OkSession()
            b = _bridge(td, session=session)
            b._ensure_inbound_worker_state()
            b._handle_update_once(_msg(6000, "only once"), 6000)
            for _ in range(5):
                b._replay_pending_inbound()      # repeated outbound-loop cycles
            for _ in range(100):
                if session.injected:
                    break
                time.sleep(0.02)
            b._stop.set()
            time.sleep(0.4)

            self.assertEqual(
                "\n".join(session.injected).count("only once"), 1,
                "the message was delivered more than once — the retry ignores in-flight records",
            )


class OutboxCompletionOrderingTests(unittest.TestCase):
    def test_crash_while_writing_sent_ledger_keeps_the_completed_record(self):
        """Once Telegram has confirmed, the record must not vanish before its dedup key does.

        A crash while writing the ledger has to leave the complete record in the outbox. After a
        restart it can then be cleaned up safely; with neither the record nor the key, a replay
        would send the whole reply again.
        """
        with tempfile.TemporaryDirectory() as td:
            b = _bridge(td)
            outbox = b._ensure_outbox()
            record_id = outbox.enqueue(["reply only once"], [], "reply-1")

            def crash_during_ledger(_key):
                raise SystemExit("crash while writing the sent ledger")

            b._mark_sent = crash_during_ledger
            with self.assertRaises(SystemExit):
                b._flush_pending()

            retained = outbox.head()
            self.assertIsNotNone(retained, "the outbox record vanished before the dedup key was stored")
            self.assertEqual(retained.id, record_id)
            self.assertTrue(retained.complete, "parts confirmed by Telegram must stay marked")


class SingleOutboxConsumerTests(unittest.TestCase):
    def test_send_final_only_enqueues_until_the_outbound_drain_runs(self):
        """The producer must not also send the durable record.

        Only the outbound consumer may talk to Telegram. Otherwise the inbound worker and the
        outbound loop can both read the same head and send the same part twice.
        """
        with tempfile.TemporaryDirectory() as td:
            b = _bridge(td)
            b._sent_path = Path(td) / "sent-ledger"

            b._send_final("send once", key="single-owner")

            self.assertEqual(b.tg.sent, [], "the producer bypassed the single outbound consumer")
            queued = b._ensure_outbox().head()
            self.assertIsNotNone(queued, "the reply was lost instead of being enqueued")
            self.assertEqual(queued.chunks, ("send once",))

            b._flush_pending()

            self.assertEqual(b.tg.sent, ["send once"])
            self.assertIsNone(b._ensure_outbox().head())


class DurableTurnCoverageTests(unittest.TestCase):
    def test_durable_enqueue_prevents_a_duplicate_backstop_record(self):
        """A stored reply already covers the turn, even while Telegram is unreachable."""
        with tempfile.TemporaryDirectory() as td:
            b = _bridge(td)
            b._turn_active.set()
            b._turn_from_tg = True
            b._turn_text_sent = False
            b._transcript = Path(td) / "transcript.jsonl"
            b._last_assistant_text = lambda: "one reply"

            b._send_final("one reply", key="answer-key")
            self.assertTrue(b._turn_text_sent, "a durable reply does not count as covering the turn")

            b._finish_turn()

            pending = b._ensure_outbox().pending()
            self.assertEqual(len(pending), 1, "the backstop enqueued a second copy of the stored reply")
            self.assertEqual(pending[0].key, "answer-key")

    def test_non_turn_notification_never_suppresses_the_backstop(self):
        """A technical message with turn_text=False must not close the turn, even once sent."""
        with tempfile.TemporaryDirectory() as td:
            b = _bridge(td)
            b._turn_text_sent = False

            b._send_final("technical notice", turn_text=False)
            self.assertFalse(b._turn_text_sent)

            b._flush_pending()

            self.assertEqual(b.tg.sent, ["technical notice"])
            self.assertFalse(b._turn_text_sent, "a technical message wrongly suppressed the backstop")


class NoSignalOutboxTests(unittest.TestCase):
    def test_file_only_reply_without_signal_file_survives_a_restart(self):
        """The stop-hook signal is optional; it must not decide whether a durable outbox exists."""
        with tempfile.TemporaryDirectory() as td:
            old_state = os.environ.get("AGENT2TELEGRAM_STATE")
            os.environ["AGENT2TELEGRAM_STATE"] = td
            try:
                payload = Path(td) / "result.png"
                payload.write_bytes(b"PNG")

                b = _bridge(td)
                b._queue_path = None       # same as an attach config without signal_file
                b.cfg.file_marker = "[tg-file]"
                b.cfg.outbox_dirs = [td]

                b._send_final(f"[tg-file] {payload}")

                self.assertEqual(b.tg.files, [], "the producer must not upload outside the outbound loop")
                queued = b._ensure_outbox().head()
                self.assertIsNotNone(queued, "without signal_file the attachment was not stored")
                self.assertEqual(queued.files, (str(payload),))

                # A new object over the same state simulates a process restart.
                restarted = _bridge(td)
                restarted._queue_path = None
                restarted.cfg.file_marker = "[tg-file]"
                restarted.cfg.outbox_dirs = [td]
                restarted._flush_pending()

                self.assertEqual(
                    [Path(p).resolve() for p in restarted.tg.files],
                    [payload.resolve()],
                    "a file-only reply without signal_file did not survive a restart",
                )
                self.assertIsNone(restarted._ensure_outbox().head())
            finally:
                if old_state is None:
                    os.environ.pop("AGENT2TELEGRAM_STATE", None)
                else:
                    os.environ["AGENT2TELEGRAM_STATE"] = old_state


class InstanceLockWiringTests(unittest.TestCase):
    """The lock must hold on the PRODUCTION path, not merely as a helper function.

    A test that exercises a helper says nothing about whether the helper is ever called. That is
    exactly how the lock came to be written, tested and reported as done — while nothing called
    it. A mutation check exposed it: removing `with self._instance_lock()` broke no test at all.
    """

    def test_bridge_refuses_second_instance_over_same_state(self):
        with tempfile.TemporaryDirectory() as td:
            first, second = _bridge(td), _bridge(td)
            holding, release = threading.Event(), threading.Event()

            def _hold():
                with first._instance_lock():
                    holding.set()
                    release.wait(5)

            t = threading.Thread(target=_hold, daemon=True)
            t.start()
            self.assertTrue(holding.wait(5), "the first instance never took the lock — the test is wrong")
            try:
                with self.assertRaises(RuntimeError):
                    with second._instance_lock():
                        self.fail("a second instance over the same state should not have started")
            finally:
                release.set()
                t.join(timeout=3)

            # once released it must be takeable again, or the bridge would not come back up
            with second._instance_lock():
                pass

    def test_run_actually_takes_the_lock(self):
        """`run()` MUST take the lock — not merely be capable of taking it.

        The previous test called `_instance_lock()` directly, so removing the lock from `run()`
        went unnoticed (the mutation survived). This is the version that guards the wiring.
        """
        import contextlib
        with tempfile.TemporaryDirectory() as td:
            b = _bridge(td)
            used = []

            @contextlib.contextmanager
            def _spy():
                used.append(True)
                yield

            b._instance_lock = _spy
            b._run_locked = lambda: None
            b.run()
            self.assertTrue(used, "run() never took the lock — the two-instance guard is dead")


class OutboxDirCollisionTests(unittest.TestCase):
    def test_durable_queue_never_touches_the_user_outbox_folder(self):
        """The queue MUST NOT claim the directory user attachments are sent from.

        `DurableOutbox` creates an "outbox" folder under the root it is given — and
        `<state>/outbox` is also where the bridge sends files from. Several megabytes of real user
        recordings were sitting there. The queue would have counted them against its quota, moved
        the foreign `.json` files to dead-letter and deleted them after 90 days. The single
        destructive finding of that review round.
        """
        with tempfile.TemporaryDirectory() as td:
            user_dir = Path(td) / "outbox"
            user_dir.mkdir()
            recording = user_dir / "episode-recording.mp3"
            recording.write_bytes(b"MP3" * 100)
            foreign_json = user_dir / "my-export.json"
            foreign_json.write_text('{"this": "is not a queue record"}')

            b = _bridge(td)
            b.cfg.outbox_dirs = [str(user_dir)]
            outbox = b._ensure_outbox()
            self.assertIsNotNone(outbox, "the queue was not created — the test is wrong")
            b._send_final("some reply")
            b._flush_pending()

            self.assertTrue(recording.exists(), "the queue touched a user recording")
            self.assertTrue(foreign_json.exists(), "the queue seized a foreign .json from the user directory")
            self.assertNotIn("dead-letter", [p.name for p in user_dir.iterdir()],
                             "the queue created its own folders inside the user outbox")


class LedgerWriteFailureTests(unittest.TestCase):
    def test_record_stays_queued_when_the_ledger_cannot_be_written(self):
        """If the ledger cannot be written to disk, the record MUST NOT leave the queue.

        `_mark_sent` used to swallow OSError, so the key existed only in memory, the caller
        deleted the record, and after a restart the SAME reply went out again — a duplicate
        precisely when the disk is full. The test deliberately targets a real OSError from the
        append, which the production code ignored, rather than SystemExit.
        """
        with tempfile.TemporaryDirectory() as td:
            b = _bridge(td)
            b._sent_path = Path(td) / "unwritable" / "sent_uuids"
            real_open = open

            def _broken_open(file, *a, **kw):
                if str(file) == str(b._sent_path):
                    raise OSError(28, "No space left on device")
                return real_open(file, *a, **kw)

            import builtins
            builtins.open = _broken_open
            try:
                self.assertFalse(b._mark_sent("key-1"),
                                 "_mark_sent must report that the write to disk failed")
            finally:
                builtins.open = real_open

            # and once the disk is back, it writes normally
            b._sent_path = Path(td) / "sent_uuids"
            self.assertTrue(b._mark_sent("key-2"))
            self.assertIn("key-2", b._sent_path.read_text())


class QueuedAckTests(unittest.TestCase):
    """A message arriving mid-work should be acknowledged immediately.

    Without an acknowledgement the sender cannot tell whether the message was lost, so they send
    it again — which happened repeatedly in practice.
    """

    def _busy_bridge(self, td):
        b = _bridge(td)
        b._turn_active.set()       # something is already running
        b._last_queue_ack = None   # never acknowledged before
        return b

    def test_message_during_work_is_acknowledged(self):
        with tempfile.TemporaryDirectory() as td:
            b = self._busy_bridge(td)
            b._handle_update_once(_msg(9001, "one more thing"), 9001)
            self.assertTrue(any("Got your message" in s for s in b.tg.sent),
                            "a message arriving mid-work was not acknowledged — the sender cannot tell it arrived")

    def test_five_messages_produce_one_ack_not_five(self):
        with tempfile.TemporaryDirectory() as td:
            b = self._busy_bridge(td)
            for i in range(5):
                b._handle_update_once(_msg(9100 + i, f"message {i}"), 9100 + i)
            acks = [s for s in b.tg.sent if "Got your message" in s]
            self.assertEqual(len(acks), 1,
                             f"{len(acks)} acknowledgements arrived instead of one — that is spam")

    def test_no_ack_when_nothing_is_running(self):
        with tempfile.TemporaryDirectory() as td:
            b = _bridge(td)
            b._turn_active.clear()     # nothing running -> the reply comes straight away
            b._last_queue_ack = None   # never acknowledged before
            b._handle_update_once(_msg(9200, "hi"), 9200)
            self.assertFalse(any("Got your message" in s for s in b.tg.sent),
                             "an acknowledgement is sent even when nothing is running — needless noise")


class ReplyContextTests(unittest.TestCase):
    """A reply to a specific message must tell the agent what the user is responding to.

    Without it the agent gets nothing but bare text — and a terse "fix this" under a daily report
    is impossible to interpret.
    """

    def test_reply_adds_quoted_context(self):
        with tempfile.TemporaryDirectory() as td:
            b = _bridge(td)
            upd = _msg(7001, "fix this")
            upd["message"]["reply_to_message"] = {"text": "📊 Bridge today\n- 2× could not write into the window."}
            b._handle(upd)
            injected = "\n".join(b._session.injected)
            self.assertIn("replying to:", injected, "the agent was not told what the user is responding to")
            self.assertIn("could not write into the window", injected)
            self.assertIn("fix this", injected)

    def test_plain_message_has_no_quote(self):
        with tempfile.TemporaryDirectory() as td:
            b = _bridge(td)
            b._handle(_msg(7002, "an ordinary message"))
            self.assertNotIn("replying to:", "\n".join(b._session.injected))

    def test_long_quote_is_trimmed(self):
        with tempfile.TemporaryDirectory() as td:
            b = _bridge(td)
            upd = _msg(7003, "and what about this")
            upd["message"]["reply_to_message"] = {"text": "x" * 2000}
            b._handle(upd)
            injected = "\n".join(b._session.injected)
            self.assertIn("…", injected, "a long quote was not trimmed — it would flood the prompt")
            self.assertLess(len(injected), 700)


class VoiceTranscriptMarkerTests(unittest.TestCase):
    """A transcribed voice note must be labelled so the agent does not read it as literal text.

    A voice note once came back transcribed into an entirely different language. Without a label
    the agent asserts that the message arrived exactly as sent — confidently wrong. With the
    label it is obvious that recognition failed and the meaning can be inferred from context.
    """

    def test_voice_message_is_marked_as_transcript(self):
        with tempfile.TemporaryDirectory() as td:
            b = _bridge(td)
            b._transcribe = lambda *a, **k: "广路有什么姓名"
            upd = _msg(8001)
            upd["message"].pop("text")
            upd["message"]["voice"] = {"file_id": "abc", "duration": 3}
            b._handle(upd)
            injected = "\n".join(b._session.injected)
            self.assertIn("voice transcript", injected,
                          "the agent cannot tell it is reading a machine transcript")
            self.assertIn("广路有什么姓名", injected)

    def test_typed_message_has_no_marker(self):
        with tempfile.TemporaryDirectory() as td:
            b = _bridge(td)
            b._handle(_msg(8002, "I typed this"))
            self.assertNotIn("voice transcript", "\n".join(b._session.injected))


class BackstopDuplicateTests(unittest.TestCase):
    """The backstop must not resend a reply the normal path already delivered.

    Proven from the production log: the user received one reply twice, with two
    different queue ids (433fa145 / 52df94c3) but the same text, in the same second. The
    backstop called _send_final without a dedup key, so the queue treated it as a new message.
    The fix that prevented a lost reply had started producing duplicates instead.
    """

    def test_finish_turn_passes_the_dedup_key(self):
        """Goes through _finish_turn (the production path), not _send_final directly.

        The first version of this test called _send_final itself, so removing the key from the
        backstop did not make it fail — it proved nothing. Third time this trap appeared today.
        """
        with tempfile.TemporaryDirectory() as td:
            b = _bridge(td)
            b._turn_active.set()
            b._turn_from_tg = True
            b._turn_text_sent = False
            b._transcript = Path(td) / "transcript.jsonl"
            b._transcript.write_text("")
            b._last_backstop_key = "uuid-from-transcript"
            captured = {}

            b._retry_last_assistant_text = lambda: "final reply"
            b._has_turn_end_backstop_source = lambda: True
            b._send_final = lambda text, key=None, **kw: captured.update(text=text, key=key)
            b._status_clear = lambda *a, **k: None
            b._consume_turn_end = lambda *a, **k: None

            b._finish_turn()

            self.assertEqual(captured.get("text"), "final reply", "the backstop sent nothing")
            self.assertEqual(captured.get("key"), "uuid-from-transcript",
                             "the backstop sent WITHOUT a dedup key — the queue cannot spot the duplicate")

    def test_backstop_does_not_resend_what_was_already_sent(self):
        with tempfile.TemporaryDirectory() as td:
            b = _bridge(td)
            b._turn_active.set()
            b._turn_from_tg = True
            b._turn_text_sent = False
            b._last_backstop_key = "uuid-abc"

            # normal path delivered it first, under its own dedup key
            b._send_final("final reply", key="uuid-abc")
            b._flush_pending()
            first = list(b.tg.sent)
            self.assertEqual(len(first), 1, "the first delivery did not happen — the test is wrong")

            # now the backstop fires with the same message
            b._send_final("final reply", key="uuid-abc")
            b._flush_pending()

            self.assertEqual(b.tg.sent, first,
                             "the backstop sent the message a second time — the user gets it twice")

    def test_backstop_still_sends_when_nothing_went_out(self):
        """The guard must not break the case it exists for: a turn with no reply at all."""
        with tempfile.TemporaryDirectory() as td:
            b = _bridge(td)
            b._turn_text_sent = False
            b._last_backstop_key = "uuid-xyz"
            b._send_final("the only reply", key="uuid-xyz")
            b._flush_pending()
            self.assertEqual(len(b.tg.sent), 1, "the backstop failed to send a reply that never went out")


class StaleTurnEndTests(unittest.TestCase):
    """A leftover end-of-turn signal must not end the NEXT turn.

    Found 2026-08-02: a voice note got no answer at all. The log showed the turn starting and
    ending 1.5 s later with nothing sent and no backstop. Codex writes task_complete to its
    rollout with a delay, so a late one from the previous turn landed inside the new one and
    closed it immediately.
    """

    def test_begin_turn_clears_a_stale_end_signal(self):
        with tempfile.TemporaryDirectory() as td:
            b = _bridge(td)
            b._pending_turn_end = True          # leftover from the previous turn
            b._status = {"mid": None, "shown": ""}
            b._session = _OkSession()
            b._tui_seen = set()

            b._begin_turn()

            self.assertFalse(b._pending_turn_end,
                             "stale end-of-turn signal survived into the new turn — "
                             "the reply would never be sent")

    def test_begin_turn_records_when_it_started(self):
        """Needed to spot a turn that ends suspiciously fast."""
        with tempfile.TemporaryDirectory() as td:
            b = _bridge(td)
            b._status = {"mid": None, "shown": ""}
            b._session = _OkSession()
            b._tui_seen = set()
            b._begin_turn()
            self.assertGreater(getattr(b, "_turn_begun_at", 0), 0)


class InboundDoneOrderingTests(unittest.TestCase):
    """Finishing a message must never leave it visible to the replay.

    `_inbound_done` used to drop the record from the in-flight set BEFORE deleting it from the
    inbox. In that window the record is "not in flight" and still "pending on disk", so a
    concurrent replay submits the message a SECOND time. It passed on one machine and failed on
    CI, where the timing differs — the worst kind of bug to trust to luck.

    This test does not race: it runs the replay from INSIDE the delete, which is exactly the
    window, so it either always passes or always fails.
    """

    def test_a_replay_during_cleanup_does_not_resubmit_the_message(self):
        with tempfile.TemporaryDirectory() as td:
            b = _bridge(td)
            b._ensure_inbound_worker_state()
            inbox = b._ensure_inbox()
            record_id = inbox.reserve(_msg(7777, "exactly once"))
            b._inbound_inflight().add(record_id)

            submitted = []
            b._submit_inbound_update = lambda upd, rid=None: submitted.append(rid)

            real_done = inbox.done

            def done_but_replay_first(rid):
                # The replay runs while the record is being cleaned up — the exact window.
                b._replay_pending_inbound()
                return real_done(rid)

            inbox.done = done_but_replay_first
            b._inbound_done(record_id)

            self.assertEqual(
                submitted, [],
                "a replay during cleanup resubmitted the message — the user would get it twice",
            )
            self.assertNotIn(record_id, b._inbound_inflight(),
                             "the id stayed marked in flight, so the message could never retry")

    def test_a_failed_cleanup_still_releases_the_in_flight_id(self):
        """If the delete throws, the id must still be released — otherwise it is stuck forever."""
        with tempfile.TemporaryDirectory() as td:
            b = _bridge(td)
            b._ensure_inbound_worker_state()
            inbox = b._ensure_inbox()
            record_id = inbox.reserve(_msg(7778, "cleanup fails"))
            b._inbound_inflight().add(record_id)

            def boom(_rid):
                raise OSError(28, "No space left on device")

            inbox.done = boom
            b._inbound_done(record_id)

            self.assertNotIn(record_id, b._inbound_inflight())
