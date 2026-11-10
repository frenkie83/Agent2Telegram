"""Tests for T-1047 — `_sent_keys` (Telegram confirmed) vs the small `_unledgered_keys` set of
ids whose disk write failed ("on disk" = in `_sent_keys` and NOT in `_unledgered`).

The original vada: `_mark_sent` added the key to `_sent_keys` BEFORE writing it to disk, and its
shortcut asked `_sent_keys` ("already seen it? → True") instead of asking whether the disk
actually has it. The first write of a batched key could fail (record correctly stays queued),
but the SECOND call found the key already in `_sent_keys` and returned True WITHOUT EVER
RETRYING THE WRITE — the record then left the queue with an incomplete ledger, and after a
restart the same text went out to the user a second time.

Commit 4a22dc1 (review round 2) found a second, more serious bug in the first fix:
`_retry_unconfirmed_ledger` iterated `_sent_keys` without a lock, and the INBOUND thread
(`_flush_batch` → `_send_final` → the voice branch) writes into that very set — so two threads
running at once could raise `RuntimeError: Set changed size during iteration`, and because that
call is the FIRST statement of `_flush_pending`, the exception took down the WHOLE outbound
cycle (drain, turn-end, heartbeat) and delivered nothing. The fix iterates only the small
`_unledgered_keys` set, under `_ledger_lock`.

⛔ Trap: `_unledgered()` is lazy so hand-built bridges (this file, `selftest.py`) that set
`_sent_keys` directly don't blow up with `AttributeError` — a missing `_unledgered_keys`
attribute just means "nothing has failed yet", which for a hand-built, freshly-constructed
bridge is simply true. There is nothing to seed by hand any more: the small set starts empty
either way, so (unlike the removed `_ledgered_keys` copy) there is no second state to get wrong
when a test sets `_sent_keys` directly.
"""
import builtins
import tempfile
import threading
import time
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
    state = Path(state_dir)
    b._sent_path = state / "sent_uuids"
    b._queue_path = state / "outbox.json"
    try:
        b._sent_keys = set(b._sent_path.read_text("utf-8").split())
    except OSError:
        b._sent_keys = set()
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
            self.assertIn("k1", b._sent_keys)
            self.assertNotIn("k1", b._unledgered(), "the key that DID write must not be unledgered")
            self.assertIn("k2", b._unledgered(), "the key that failed must be unledgered")
            on_disk = b._sent_path.read_text("utf-8").split()
            self.assertEqual(on_disk, ["k1"], "only the successfully-written key is on disk")


# --------------------------------------------------------------------------------------
# 2 — the jádro of the fix: `_flush_pending` itself must retry the failed write, not just the
#     main outbox branch dopisovat its own key.
# --------------------------------------------------------------------------------------
class SecondPassRetriesTheWriteTests(unittest.TestCase):
    def test_flush_pending_retries_a_previously_failed_key_via_the_outbound_cycle(self):
        """The failure mode this guards: a prior fix made `_sent_keys` hold the key the moment
        the write was attempted, and the shortcut asked exactly that set — so once `k2` sat in
        `_sent_keys` after its FIRST failed write, every later call returned True without ever
        writing it to disk again.

        Deliberately routed through a SECOND, otherwise-empty call: `_flush_pending()` with an
        empty outbox and `_pending_send`. If `_mark_sent_keys(rec.key, queued=True)` alone were
        enough to close the gap (as it would be the first time the record's own branch runs),
        this test would pass even with the `_retry_unconfirmed_ledger()` call deleted from
        `_flush_pending` — which is exactly the gap review found: nothing in the suite called
        `_flush_pending` a second time over a key with no record left to retry it. Here the
        outbox record from the first call is already gone from pending (its chunks and files are
        both empty afterwards is not the point — the KEY itself, 'k2', must still get written by
        the retry line, not by re-visiting any queued record)."""
        with tempfile.TemporaryDirectory() as td:
            b = _bridge(td)
            outbox = b._ensure_outbox()
            outbox.enqueue(["hello"], [], "k1 k2")

            with _FailAfter(b._sent_path, fail_from=2):
                b._flush_pending()
            self.assertIn("k2", b._unledgered(), "setup: k2 must still be unledgered after the failure")
            # The outbox record is STILL queued (its ledger write failed) — mark_sent_keys was
            # never satisfied, so nothing besides `_retry_unconfirmed_ledger()` has touched k2
            # since. Simulate a cycle where the record itself cannot be retried (e.g. the owner
            # chat vanished) to isolate the retry line from the main branch:
            b._owner_chat_backup, b._owner_chat = b._owner_chat, None

            # Disk heals. Only `_retry_unconfirmed_ledger()`, called at the top of
            # `_flush_pending`, can possibly write k2 now — the main outbox loop does not even
            # run (`_owner_chat` is None).
            b._flush_pending()

            self.assertIn("k2", b._sent_path.read_text("utf-8").split(),
                           "the previously failed key must actually be written by the outbound "
                           "cycle's retry, not just by revisiting its own queued record")
            self.assertNotIn("k2", b._unledgered())


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
# 4 — restart over the same state goes through real __init__, not a hand-copied set
# --------------------------------------------------------------------------------------
class RestartOverSameStateTests(unittest.TestCase):
    def test_a_fresh_bridge_over_an_existing_ledger_does_not_resend_and_does_not_rewrite_the_file(self):
        """Bridge #1 hits a partial-ledger failure (same as test 1) and, BECAUSE of the fix,
        leaves the record queued rather than dropping it with an incomplete ledger. Disk heals,
        a second `_flush_pending` finishes it off — `k1` and `k2` both land on disk and the
        record leaves the queue, matching ordinary (non-restart) operation.

        The restart itself is modelled by `_bridge()` the ordinary way (NOT a copy this test
        invents): its ledger-loading line is the exact same `read_text().split()` /
        `except OSError` production uses in `__init__`, so bridge #2's `_sent_keys` is genuinely
        "what a fresh process reads off disk", not a value this test decides for it.
        `_unledgered_keys` is deliberately left UNSET — unlike the removed `_ledgered_keys`
        copy, there is nothing to pre-seed any more: a process that has not yet seen a single
        failed write starts with an empty small set, which the lazy `_unledgered()` getter
        already gives for free. Seeding it by hand here would be exactly the kind of
        predetermined state this file's own trap-warning used to describe.
        """
        with tempfile.TemporaryDirectory() as td:
            b1 = _bridge(td)
            outbox1 = b1._ensure_outbox()
            outbox1.enqueue(["hello"], [], "k1 k2")
            with _FailAfter(b1._sent_path, fail_from=2):
                b1._flush_pending()
            self.assertIsNotNone(outbox1.head(), "setup: the fix must have kept the record queued")
            b1._flush_pending()   # disk healed by now, record completes normally
            self.assertIsNone(outbox1.head())
            before_restart = b1._sent_path.read_text("utf-8")
            self.assertEqual(sorted(before_restart.split()), ["k1", "k2"])
            before_mtime = b1._sent_path.stat().st_mtime_ns

            # "Restart": a brand-new bridge object, own client, pointed at the SAME state files
            # as bridge #1 — modelling a process restart, not a copy of its memory.
            b2 = _bridge(td, client=_FakeClient())
            self.assertEqual(b2._sent_keys, {"k1", "k2"},
                              "a fresh bridge must load the keys an earlier process already "
                              "confirmed on disk, the same way __init__ reads the ledger file")

            b2._flush_pending()   # nothing queued, nothing to do — just proves it stays quiet

            self.assertEqual(b2.tg.sent, [], "nothing was queued at startup, nothing should be sent")
            self.assertEqual(b2._sent_path.read_text("utf-8"), before_restart,
                              "a restart over an existing ledger must not rewrite the file")
            self.assertEqual(b2._sent_path.stat().st_mtime_ns, before_mtime,
                              "the file must not even be touched, let alone rewritten")
            self.assertEqual(b2._unledgered(), set(),
                              "nothing failed during this process's lifetime yet — nothing to seed")
            self.assertTrue(b2._mark_sent("k1"),
                             "a key already on disk from a PRIOR process must be recognised as sent")
            self.assertEqual(b2._sent_path.read_text("utf-8"), before_restart,
                              "recognising an already-written key must not append it again")


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
            self.assertIn("voice-1", b._unledgered(), "but the disk never got it")

            b._retry_unconfirmed_ledger()

            self.assertNotIn("voice-1", b._unledgered())
            self.assertIn("voice-1", b._sent_path.read_text("utf-8").split())


# --------------------------------------------------------------------------------------
# 6 — the race the 2nd review round found: inbound writes into `_sent_keys` while the outbound
#     cycle retries the ledger, concurrently, with no exception escaping either side.
# --------------------------------------------------------------------------------------
class ConcurrentMarkSentAndRetryTests(unittest.TestCase):
    def test_mark_sent_from_one_thread_and_retry_from_another_raise_nothing(self):
        """`_retry_unconfirmed_ledger` used to iterate `_sent_keys` directly. The inbound thread
        (`_flush_batch` → `_send_final` → the voice branch) adds to that very set, so a retry
        pass running at the same time could hit `RuntimeError: Set changed size during
        iteration` — and because the retry call is the FIRST statement of `_flush_pending`, that
        exception took down the whole outbound cycle. The fix iterates only the small
        `_unledgered_keys` set under `_ledger_lock`; this test fails against the old shape (see
        the mutation note in the PR) because mutating a set DURING its OWN iteration is reliably
        caught by CPython, while `_unledgered_keys` is tiny and short-lived enough that two
        threads churning it never collide with their own iteration of it."""
        with tempfile.TemporaryDirectory() as td:
            b = _bridge(td)
            errors = []
            stop = threading.Event()

            def writer():
                i = 0
                while not stop.is_set():
                    b._mark_sent(f"key-{i}")
                    i += 1

            def retrier():
                while not stop.is_set():
                    try:
                        b._retry_unconfirmed_ledger()
                    except Exception as e:                      # pragma: no cover - the assertion
                        errors.append(e)
                        stop.set()

            t1 = threading.Thread(target=writer)
            t2 = threading.Thread(target=retrier)
            t1.start()
            t2.start()
            time.sleep(1.0)
            stop.set()
            t1.join(5)
            t2.join(5)

            self.assertEqual(errors, [], f"concurrent ledger access must never raise: {errors!r}")


if __name__ == "__main__":
    unittest.main()
