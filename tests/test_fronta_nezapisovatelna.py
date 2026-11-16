"""T-1060 — an unwritable QUEUE directory must not take the outbound cycle down.

`_flush_pending` used to let `PermissionError`/`OSError` from the queue's own writes
(`mark_chunk_sent`, `mark_file_sent`, `fail`, `give_up`, `done`, and the reads that run the
store's maintenance) escape to `_outbound_loop`, which logged `outbound error` and ENDED the
tick — no transcript drain, no turn end, no heartbeat — on every tick until the disk healed.
The watchdog then restarted the bridge in a loop.

Real `chmod`, no `builtins.open` patch: the queue writes through `os.open`, which such a patch
would not model.
"""
import logging
import os
import tempfile
import threading
import unittest
from pathlib import Path

from agent2telegram.attach import AttachBridge
from tests.test_ledger_potvrzeny_na_disku import _bridge


def _loop_bridge(td):
    """A bridge that can run ONE real `_outbound_loop` tick; everything the tick calls besides
    `_flush_pending` and `_beat` is inert, so the heartbeat file proves the tick reached its end."""
    b = _bridge(td)
    b._stop = threading.Event()
    b._heartbeat = Path(td) / "hb"
    b._pending_turn_end = False
    b._turn_end = None
    b._turn_active = threading.Event()
    b._last_inbound_retry = 10 ** 12
    for name in ("_maybe_reresolve", "_drain_transcript", "_drain_signal", "_flush_overdue_batch"):
        setattr(b, name, lambda: None)
    b._transcript_turn_end_ready = lambda: False
    # one tick, then stop
    b._stop.wait = lambda timeout=None: b._stop.set()
    return b


@unittest.skipIf(os.geteuid() == 0, "root defeats chmod — the test would pass for the wrong reason")
class UnwritableQueueDirectoryTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(logging.disable, logging.NOTSET)
        logging.disable(logging.CRITICAL)

    def test_tick_reaches_the_heartbeat_and_does_not_resend_while_the_queue_is_readonly(self):
        with tempfile.TemporaryDirectory() as td:
            b = _loop_bridge(td)
            outbox = b._ensure_outbox()
            outbox.enqueue(["one", "two"], [], "k1")
            os.chmod(outbox.pending_directory, 0o555)
            try:
                for _ in range(3):
                    b._flush_pending()               # must not raise — any tick, any number
                b._outbound_loop()                    # a whole tick, up to `_beat()`
            finally:
                os.chmod(outbox.pending_directory, 0o755)
            self.assertTrue(b._heartbeat.exists(), "the tick must reach `_beat()`")
            self.assertEqual([t for _, t in b.tg.sent], ["one", "two"],
                             "a part whose confirmation could not be written must not be re-sent "
                             "every tick while the disk is bad")

    def test_after_the_disk_heals_the_record_is_confirmed_and_released_without_resending(self):
        with tempfile.TemporaryDirectory() as td:
            b = _loop_bridge(td)
            outbox = b._ensure_outbox()
            outbox.enqueue(["one", "two"], [], "k1")
            os.chmod(outbox.pending_directory, 0o555)
            try:
                b._flush_pending()
            finally:
                os.chmod(outbox.pending_directory, 0o755)
            b._flush_pending()
            self.assertIsNone(outbox.head(), "healed disk: the record must leave the queue")
            self.assertEqual([t for _, t in b.tg.sent], ["one", "two"])
            self.assertIn("k1", b._sent_keys)

    def test_an_attachment_that_keeps_failing_does_not_raise_or_repeat_its_report(self):
        with tempfile.TemporaryDirectory() as td:
            b = _loop_bridge(td)
            b._send_one_file = lambda p: (_ for _ in ()).throw(RuntimeError("HTTP 400"))
            outbox = b._ensure_outbox()
            outbox.enqueue([], [str(Path(td) / "gone.bin")], "kf")
            os.chmod(outbox.pending_directory, 0o555)
            try:
                for _ in range(8):
                    b._flush_pending()               # `fail`/`give_up` cannot be written
            finally:
                os.chmod(outbox.pending_directory, 0o755)
            self.assertLessEqual(len(b.tg.sent), 1,
                                 "the permanent-refusal notice must not go out once per tick")


class _FlakyClient:
    """Sends like `_FakeClient`, but raises on the listed call numbers (1-based) — a network
    hiccup that stops the tick after some parts are already confirmed ON DISK."""

    def __init__(self, fail_on=()):
        self.sent = []
        self.calls = 0
        self.fail_on = set(fail_on)

    def send_chat_action(self, chat_id, action="typing"):
        pass

    def send_message(self, chat_id, text, parse_mode=None):
        self.calls += 1
        if self.calls in self.fail_on:
            raise RuntimeError("network hiccup")
        self.sent.append((chat_id, text))


def _recording_files(b):
    """`_send_one_file` that succeeds and records into the same list as the text, so ORDER and
    DUPLICATES across text and attachments are visible in one place."""
    b._send_one_file = lambda p: b.tg.sent.append((None, "FILE:" + Path(p).name))


@unittest.skipIf(os.geteuid() == 0, "root defeats chmod — the test would pass for the wrong reason")
class UnwritableQueueEdgeTests(unittest.TestCase):
    """Edges of T-1060: mixed records, a disk that turns read-only mid-record, two records
    (FIFO), and a queue that cannot even be READ (`head()` runs maintenance writes)."""

    def setUp(self):
        self.addCleanup(logging.disable, logging.NOTSET)
        logging.disable(logging.CRITICAL)

    def test_text_and_attachment_each_go_out_exactly_once_across_a_readonly_spell(self):
        with tempfile.TemporaryDirectory() as td:
            b = _loop_bridge(td)
            _recording_files(b)
            outbox = b._ensure_outbox()
            outbox.enqueue(["one", "two"], [str(Path(td) / "a.bin")], "km")
            os.chmod(outbox.pending_directory, 0o555)
            try:
                for _ in range(3):
                    b._flush_pending()
            finally:
                os.chmod(outbox.pending_directory, 0o755)
            b._flush_pending()
            self.assertEqual([t for _, t in b.tg.sent], ["one", "two", "FILE:a.bin"])
            self.assertIsNone(outbox.head())

    def test_disk_turning_readonly_mid_record_neither_resends_nor_ledgers_early(self):
        with tempfile.TemporaryDirectory() as td:
            b = _loop_bridge(td)
            b.tg = _FlakyClient(fail_on={2})            # tick 1: "one" confirmed on disk, "two" fails
            outbox = b._ensure_outbox()
            outbox.enqueue(["one", "two", "three"], [], "kp")
            b._flush_pending()
            self.assertNotIn("kp", b._sent_keys, "half a reply must not be in the ledger")
            os.chmod(outbox.pending_directory, 0o555)
            try:
                for _ in range(3):
                    b._flush_pending()
            finally:
                os.chmod(outbox.pending_directory, 0o755)
            b._flush_pending()
            self.assertEqual([t for _, t in b.tg.sent], ["one", "two", "three"])
            self.assertIsNone(outbox.head())

    def test_second_record_waits_behind_a_stuck_first_and_follows_it_after_healing(self):
        with tempfile.TemporaryDirectory() as td:
            b = _loop_bridge(td)
            outbox = b._ensure_outbox()
            outbox.enqueue(["first"], [], "k1")
            outbox.enqueue(["second"], [], "k2")
            os.chmod(outbox.pending_directory, 0o555)
            try:
                for _ in range(3):
                    b._flush_pending()
                stuck = [t for _, t in b.tg.sent]
            finally:
                os.chmod(outbox.pending_directory, 0o755)
            b._flush_pending()
            self.assertEqual((stuck, [t for _, t in b.tg.sent]),
                             (["first"], ["first", "second"]))

    def test_a_queue_whose_maintenance_cannot_write_still_lets_the_tick_reach_the_heartbeat(self):
        # Real refusal inside `head()`: an expired record must move to dead-letter during
        # `_maintain`, and the read-only directory refuses that write.
        with tempfile.TemporaryDirectory() as td:
            b = _loop_bridge(td)
            outbox = b._ensure_outbox()
            outbox.enqueue(["late"], [], "ke")
            outbox.pending_retention_seconds = 0
            os.chmod(outbox.pending_directory, 0o555)
            try:
                with self.assertRaises(OSError):
                    outbox.head()                    # premise: head() itself really fails
                b._outbound_loop()
            finally:
                os.chmod(outbox.pending_directory, 0o755)
            self.assertTrue(b._heartbeat.exists())


class GiveUpAfterUnrecordedChunksTests(unittest.TestCase):
    """The attachment gives up (dead-letter write succeeds) while the text's confirmations are
    only in memory — e.g. ENOSPC that frees between two writes of one tick. No chmod: a patched
    `mark_chunk_sent` is the only way to refuse one write and accept the next in one tick."""

    def setUp(self):
        self.addCleanup(logging.disable, logging.NOTSET)
        logging.disable(logging.CRITICAL)

    def test_text_delivered_before_the_attachment_gave_up_is_in_the_ledger(self):
        with tempfile.TemporaryDirectory() as td:
            b = _loop_bridge(td)
            b._send_one_file = lambda p: (_ for _ in ()).throw(RuntimeError("HTTP 400"))
            outbox = b._ensure_outbox()
            outbox.enqueue(["text"], [str(Path(td) / "x.bin")], "kg")

            def refuse(*a):
                raise OSError(28, "No space left on device")
            outbox.mark_chunk_sent = refuse
            for _ in range(5):
                b._flush_pending()
            self.assertEqual([t for _, t in b.tg.sent][0], "text")
            self.assertIn("kg", b._sent_keys,
                          "the text was delivered; without the key a restart sends it again")


if __name__ == "__main__":
    unittest.main()
