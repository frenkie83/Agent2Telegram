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


if __name__ == "__main__":
    unittest.main()
