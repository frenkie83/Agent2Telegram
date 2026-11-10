"""Tests for T-1047 — `_sent_keys` (Telegram confirmed) vs `_ledgered_keys` (and the disk
knows it too).

The vada: `_mark_sent` used to add the key to `_sent_keys` BEFORE writing it to disk, and its
shortcut asked `_sent_keys` ("already seen it? → True") instead of the disk-confirmed set. The
first write of a batched key could fail (record correctly stays queued), but the SECOND call
found the key already in `_sent_keys` and returned True WITHOUT EVER RETRYING THE WRITE — the
record then left the queue with an incomplete ledger, and after a restart the same text went
out to the user a second time.

Fixed shape: `_sent_keys` = "Telegram confirmed", `_ledgered_keys` (+ lazy `_ledger_confirmed()`)
= "and the disk knows it too". Only the second may let a record leave the queue.

⛔ Trap: `_ledger_confirmed()` is lazy so hand-built bridges (this file, `selftest.py`) that set
`_sent_keys` directly don't blow up with `AttributeError`. Every test below that sets
`_sent_keys` by hand therefore ALSO sets `_ledgered_keys` explicitly, with a comment saying why
— pre-seeding both sets identically is exactly the shortcut that would hide this bug again.
"""
import builtins
import tempfile
import unittest
from pathlib import Path

from agent2telegram.attach import AttachBridge
from agent2telegram.config import Config


class _FakeClient:
    def __init__(self):
        self.sent = []

    def send_chat_action(self, chat_id, action="typing"):
        pass

    def send_message(self, chat_id, text, parse_mode=None):
        self.sent.append((chat_id, text))


def _bridge(state_dir, *, client=None):
    """A hand-assembled bridge — same minimal shape as test_v2_durability.py's `_bridge`."""
    b = object.__new__(AttachBridge)
    b.cfg = Config(agent="generic", token="1:2", allowed_user_ids=[7], tmux_session="a2t")
    b.tg = client or _FakeClient()
    b._owner_chat = 7
    b._pending_send = []
    # Fresh bridge, nothing sent yet and nothing ledgered yet — the lazy `_ledger_confirmed()`
    # will create an empty `_ledgered_keys` on first use, which is the right value here.
    b._sent_keys = set()
    state = Path(state_dir)
    b._sent_path = state / "sent_uuids"
    b._queue_path = state / "outbox.json"
    return b


class _FailAfter:
    """Patches `builtins.open` so that appends to `target` fail on the Nth call onward (1-based),
    and every other open (including earlier/later calls to a DIFFERENT path) behaves normally.
    Models a disk that goes bad partway through writing a batched ledger and can be healed again
    by raising `fail_from` past the call count."""

    def __init__(self, target: Path, fail_from: int):
        self.target = str(target)
        self.fail_from = fail_from
        self.calls = 0
        self._real_open = builtins.open

    def __enter__(self):
        def _patched(file, *a, **kw):
            if str(file) == self.target:
                self.calls += 1
                if self.calls >= self.fail_from:
                    raise OSError(28, "No space left on device")
            return self._real_open(file, *a, **kw)
        builtins.open = _patched
        return self

    def __exit__(self, *exc):
        builtins.open = self._real_open


# --------------------------------------------------------------------------------------
# 1 — a batch with a partial ledger write: the record stays queued
# --------------------------------------------------------------------------------------
class PartialBatchWriteTests(unittest.TestCase):
    def test_second_key_of_a_batch_failing_to_write_keeps_the_whole_record_queued(self):
        with tempfile.TemporaryDirectory() as td:
            b = _bridge(td)
            outbox = b._ensure_outbox()
            outbox.enqueue(["hello"], [], "k1 k2")

            # k1's append succeeds, k2's fails.
            with _FailAfter(b._sent_path, fail_from=2):
                b._flush_pending()

            self.assertEqual(b.tg.sent, [(7, "hello")],
                              "the text itself was delivered — only the ledger write failed")
            self.assertIsNotNone(outbox.head(),
                                  "an incomplete ledger write must not let the record leave the queue")
            self.assertIn("k1", b._ledger_confirmed(), "the key that DID write must be confirmed")
            self.assertNotIn("k2", b._ledger_confirmed(), "the key that failed must not be confirmed")
            on_disk = b._sent_path.read_text("utf-8").split()
            self.assertEqual(on_disk, ["k1"], "only the successfully-written key is on disk")


# --------------------------------------------------------------------------------------
# 2 — the jádro of the fix: the SECOND pass retries the failed write, not just the lookup
# --------------------------------------------------------------------------------------
class SecondPassRetriesTheWriteTests(unittest.TestCase):
    def test_disk_recovery_retries_and_writes_the_previously_failed_key(self):
        """The failure mode this guards: a prior fix made `_sent_keys` hold the key the moment
        the write was attempted, and the shortcut asked exactly that set — so once `k2` sat in
        `_sent_keys` after its FIRST failed write, every later call returned True without ever
        writing it to disk again. This test fails against that shape (see the mutation note in
        the PR) and passes here because the shortcut asks `_ledger_confirmed()`, which `k2`
        never entered."""
        with tempfile.TemporaryDirectory() as td:
            b = _bridge(td)
            outbox = b._ensure_outbox()
            outbox.enqueue(["hello"], [], "k1 k2")

            with _FailAfter(b._sent_path, fail_from=2):
                b._flush_pending()
            self.assertIsNotNone(outbox.head(), "setup: record must still be queued after the failure")

            # Disk heals. The very next outbound cycle must retry k2's write, not just recall
            # that it was already "seen".
            b._flush_pending()

            self.assertIn("k2", b._sent_path.read_text("utf-8").split(),
                           "the previously failed key must actually be written once the disk heals")
            self.assertIsNone(outbox.head(),
                               "once BOTH keys are confirmed on disk the record may leave the queue")
            self.assertEqual(b.tg.sent, [(7, "hello")],
                              "healing the ledger write must not re-send the text a second time")


# --------------------------------------------------------------------------------------
# 3 — a permanently broken disk never releases the record, however many cycles run
# --------------------------------------------------------------------------------------
class PermanentlyBrokenDiskTests(unittest.TestCase):
    def test_record_stays_queued_across_many_cycles_of_a_disk_that_never_recovers(self):
        with tempfile.TemporaryDirectory() as td:
            b = _bridge(td)
            outbox = b._ensure_outbox()
            outbox.enqueue(["hello"], [], "k1 k2")

            with _FailAfter(b._sent_path, fail_from=1):   # every write to this path fails
                for _ in range(5):
                    b._flush_pending()

            self.assertIsNotNone(outbox.head(), "a disk that never recovers must never release the record")
            self.assertEqual(b.tg.sent, [(7, "hello")],
                              "the chunk itself must be sent only once, not once per failed cycle")
            self.assertEqual(b._sent_path.read_text("utf-8") if b._sent_path.exists() else "", "",
                              "nothing ever reached disk")


# --------------------------------------------------------------------------------------
# 4 — a restart over the same state never resends what only the fix kept queued
# --------------------------------------------------------------------------------------
class RestartOverSameStateTests(unittest.TestCase):
    def test_a_fresh_bridge_over_the_same_state_completes_without_resending(self):
        """Real path, not two pre-seeded sets: bridge #1 hits a partial-ledger failure (same as
        test 1) and, BECAUSE of the fix, leaves the record queued rather than dropping it with
        an incomplete ledger — which is the exact state that, under the old bug, would have let
        a restart re-send the text (the ledger on disk does not know `k2`, so nothing on disk
        would have stopped a resend). Bridge #2 is a brand-new object pointing at the same
        `_sent_path`/`_queue_path`, modelling a process restart over the same state."""
        with tempfile.TemporaryDirectory() as td:
            b1 = _bridge(td)
            outbox1 = b1._ensure_outbox()
            outbox1.enqueue(["hello"], [], "k1 k2")
            with _FailAfter(b1._sent_path, fail_from=2):
                b1._flush_pending()
            self.assertIsNotNone(outbox1.head(), "setup: the fix must have kept the record queued")

            # "Restart": a brand-new bridge object, own client, same state dir. It reloads
            # `_sent_keys` from disk the way `AttachBridge.__init__` does — and, like `__init__`,
            # seeds `_ledgered_keys` as an identical copy, because a key just loaded FROM disk is
            # by definition already confirmed on disk. That is the one honest choice here; the
            # trap this file's docstring warns about is seeding them identically for a key that
            # was never actually confirmed, which this is not.
            b2 = _bridge(td, client=_FakeClient())
            b2._sent_keys = set(b2._sent_path.read_text("utf-8").split())
            b2._ledgered_keys = set(b2._sent_keys)
            outbox2 = b2._ensure_outbox()

            b2._flush_pending()

            self.assertEqual(b2.tg.sent, [],
                              "the chunk was already confirmed sent before the restart — resending "
                              "it now would be exactly the duplicate this fix prevents")
            self.assertIsNone(outbox2.head(), "once k2 is confirmed on disk the record may finally leave")
            self.assertEqual(sorted(b2._sent_path.read_text("utf-8").split()), ["k1", "k2"])


# --------------------------------------------------------------------------------------
# 5 — `_retry_unconfirmed_ledger` also catches keys that never sat in a queue at all
# --------------------------------------------------------------------------------------
class RetryUnconfirmedLedgerCatchesQueuelessKeysTests(unittest.TestCase):
    def test_a_sent_but_unledgered_key_with_no_queue_record_gets_written_once_the_disk_heals(self):
        """Models the voice-reply / legacy-direct-send paths: they call `_mark_sent_keys` after
        already delivering, with no outbox record left to hold them back. If their write fails,
        only `_retry_unconfirmed_ledger` (run every outbound cycle) can ever close the gap."""
        with tempfile.TemporaryDirectory() as td:
            b = _bridge(td)

            with _FailAfter(b._sent_path, fail_from=1):
                ok = b._mark_sent("voice-1")
            self.assertFalse(ok, "setup: the direct write must have failed")
            self.assertIn("voice-1", b._sent_keys, "delivered — every dedup check must see it right away")
            self.assertNotIn("voice-1", b._ledger_confirmed(), "but the disk never got it")

            b._retry_unconfirmed_ledger()

            self.assertIn("voice-1", b._ledger_confirmed())
            self.assertIn("voice-1", b._sent_path.read_text("utf-8").split())


if __name__ == "__main__":
    unittest.main()
