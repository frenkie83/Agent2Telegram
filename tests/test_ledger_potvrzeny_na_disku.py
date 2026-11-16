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

⛔ Every bridge here but `RealInitLoadsTheLedgerTests` is hand-built with `object.__new__`, so
it never runs `__init__`. The ledger read is therefore NOT copied into this file — `_bridge()`
calls production's `_load_sent_ledger()`, and `RealInitLoadsTheLedgerTests` runs the real
constructor (temporary `HOME` + a throwaway tmux session) so the `__init__` CALL of it is covered
too. Before that, gutting the read out of `__init__` left the whole suite green.

⛔ Trap: `_unledgered()` is lazy so hand-built bridges (this file, `selftest.py`) that set
`_sent_keys` directly don't blow up with `AttributeError` — a missing `_unledgered_keys`
attribute just means "nothing has failed yet", which for a hand-built, freshly-constructed
bridge is simply true. There is nothing to seed by hand any more: the small set starts empty
either way, so (unlike the removed `_ledgered_keys` copy) there is no second state to get wrong
when a test sets `_sent_keys` directly.
"""
import builtins
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from agent2telegram.attach import OUTBOX_MAX_ATTEMPTS, AttachBridge
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
    # ⛔ Not a copy of production's ledger read — the production function itself, so a change
    # there cannot leave this file quietly measuring a stale shape (`_load_sent_ledger`).
    b._sent_keys = b._load_sent_ledger()
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
        `_flush_pending` a second time over a key with no record left to retry it. The outbox
        record from the first call is STILL queued (its ledger write failed), so the second cycle
        is forced past it by clearing `_owner_chat`: the KEY itself, 'k2', must get written by the
        retry line, not by re-visiting any queued record."""
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
            b._owner_chat = None

            # Disk heals. Only `_retry_unconfirmed_ledger()`, called at the top of
            # `_flush_pending`, can possibly write k2 now — the main outbox loop does not even
            # run (`_owner_chat` is None).
            b._flush_pending()

            self.assertIn("k2", b._sent_path.read_text("utf-8").split(),
                           "the previously failed key must actually be written by the outbound "
                           "cycle's retry, not just by revisiting its own queued record")
            self.assertNotIn("k2", b._unledgered())
            self.assertEqual(b.tg.sent, [(7, "hello")],
                              "healing the ledger must not put the text on the wire a second time")


# --------------------------------------------------------------------------------------
# 3 — a disk that never recovers holds the record for the bounded retries, then RELEASES it
# --------------------------------------------------------------------------------------
class PermanentlyBrokenDiskTests(unittest.TestCase):
    """⛔ This class used to assert the opposite ("must NEVER release the record") and that was
    the bug: the record is the HEAD of the FIFO, so holding it forever holds every later reply
    too. Measured on that shape: three replies queued, fifty cycles, exactly one delivered — the
    agent answers and the user never hears from it again. Bounded retries keep the guarantee that
    matters (a transient failure never duplicates) and give up the one that cannot be kept."""

    def test_the_record_is_held_while_the_retries_last(self):
        with tempfile.TemporaryDirectory() as td:
            b = _bridge(td)
            outbox = b._ensure_outbox()
            outbox.enqueue(["hello"], [], "k1 k2")

            with _FailAfter(b._sent_path, fail_from=1):   # every write to this path fails
                for _ in range(OUTBOX_MAX_ATTEMPTS - 1):
                    b._flush_pending()

                self.assertIsNotNone(outbox.head(),
                                      "a transient failure must keep the record queued — that is "
                                      "what stops the reply being sent twice after a restart")
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

        The restart itself is modelled by `_bridge()`, which calls the SAME
        `_load_sent_ledger()` production calls — not a copy of it — so bridge #2's `_sent_keys` is
        what that function reads off disk, not a value this test decides for it. ⛔ What this test
        does NOT cover is the `__init__` CALL of that function; `RealInitLoadsTheLedgerTests`
        below does, through a real constructor.
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
# 6 — the race the 2nd review round found: inbound writes the ledger while the outbound cycle
#     retries it, concurrently, over a failed set that is genuinely NON-EMPTY the whole time.
# --------------------------------------------------------------------------------------
class ConcurrentMarkSentAndRetryTests(unittest.TestCase):
    def test_two_threads_over_a_non_empty_failed_set_raise_nothing(self):
        """`_retry_unconfirmed_ledger` used to iterate `_sent_keys` directly. The inbound thread
        (`_flush_batch` → `_send_final` → the voice branch) adds to that very set, so a retry
        pass running at the same time could hit `RuntimeError: Set changed size during
        iteration` — and because the retry call is the FIRST statement of `_flush_pending`, that
        exception took down the whole outbound cycle. The fix iterates only the small
        `_unledgered_keys` set, under `_ledger_lock`.

        ⛔ The point of the seeding below: an earlier shape of this test never produced a single
        FAILED write, so `_unledgered_keys` stayed empty and the retrier walked an empty set
        every pass. Here the disk refuses EVERY ledger write, so the failed set fills up and
        stays full, and the test asserts that (`min(seen_sizes)`). No `fsync` ever runs (every
        write fails), so the run is bounded by a write count, not by a sleep.

        ⛔ What this does NOT cover is `_ledger_lock` itself. Measured: with
        `_ledger_lock_held()` replaced by `contextlib.nullcontext()` — the lock gone outright,
        `list()` kept — this test is green 8/8 even at a 1 µs switch interval. `list(a_set)` is a
        single C call that never releases the GIL, so the window the lock would close does not
        exist in a GIL build. It is kept for the free-threaded builds and for any future shape
        where the snapshot is not one C call, and that is a claim no test here can make."""
        SEED = 3000           # keys already waiting for a disk that never took them
        WRITES = 5000         # deterministic bound: the writer stops after this many marks
        with tempfile.TemporaryDirectory() as td:
            b = _bridge(td)
            errors = []
            writes = []
            seen_sizes = []

            # Every mark logs an error (the write fails by design); 20k of those cost more than
            # the race does, and would bury a real failure in noise.
            self.addCleanup(logging.disable, logging.NOTSET)
            logging.disable(logging.CRITICAL)
            # The old shape raised only when a thread switch landed INSIDE the comprehension
            # over `_sent_keys`. At the default switch interval that needs tens of thousands of
            # marks to hit reliably (measured: 2/5 at 20k, 5/5 at 50k, ~4 s a run); forcing a
            # short interval makes the same window hit at 5k in well under a second.
            self.addCleanup(sys.setswitchinterval, sys.getswitchinterval())
            sys.setswitchinterval(1e-6)
            with _FailAfter(b._sent_path, fail_from=1):   # the disk takes nothing, ever
                for i in range(SEED):
                    b._mark_sent(f"seed-{i}")
                self.assertGreaterEqual(
                    len(b._unledgered()), SEED,
                    "setup: the failed set must be big enough for the race to have a window")

                done = threading.Event()

                def writer():
                    try:
                        for i in range(WRITES):
                            b._mark_sent(f"key-{i}")
                            writes.append(i)
                    except Exception as e:               # pragma: no cover - the assertion
                        errors.append(("writer", e))
                    finally:
                        done.set()

                def retrier():
                    while not done.is_set():
                        try:
                            seen_sizes.append(len(b._unledgered()))
                            b._retry_unconfirmed_ledger()
                        except Exception as e:           # pragma: no cover - the assertion
                            errors.append(("retrier", e))
                            done.set()

                t1 = threading.Thread(target=writer)
                t2 = threading.Thread(target=retrier)
                t1.start()
                t2.start()
                t1.join(60)
                t2.join(60)

            self.assertEqual(errors, [], f"concurrent ledger access must never raise: {errors!r}")
            self.assertEqual(len(writes), WRITES,
                              "the inbound side must have really run — a writer that dies on its "
                              "first key leaves nothing for the race to happen in")
            self.assertGreater(len(seen_sizes), 0, "the retry side must have really run")
            self.assertGreaterEqual(min(seen_sizes), SEED,
                                     "the retrier must have been walking a non-empty failed set "
                                     "on every pass, not an empty one")


# --------------------------------------------------------------------------------------
# 7 — the real `__init__` loads the ledger off disk (the call site, not just the function)
# --------------------------------------------------------------------------------------
@unittest.skipUnless(shutil.which("tmux"), "attach mode's constructor needs a live tmux session")
class RealInitLoadsTheLedgerTests(unittest.TestCase):
    """⛔ Every other test in this file hand-builds the bridge with `object.__new__`, so NONE of
    them runs `__init__` — a mutation that gutted the ledger read out of the constructor left the
    whole suite green, which is exactly the duplicate-delivery bug T-1047 exists to prevent.

    This runs the real constructor. It needs two things from the environment: `HOME` (the ledger
    path is `~/.config/agent2telegram/attach_sent.txt`, so a temporary HOME keeps the test off the
    developer's own ledger) and an existing tmux session (attach mode refuses to construct
    without one, by design).
    """

    def setUp(self):
        self._home = tempfile.mkdtemp()
        self._old_home = os.environ.get("HOME")
        os.environ["HOME"] = self._home
        self._session = f"a2t-test-{os.getpid()}"
        subprocess.run(["tmux", "new-session", "-d", "-s", self._session, "sleep 300"],
                       check=True, capture_output=True)
        self.ledger = Path(self._home) / ".config" / "agent2telegram" / "attach_sent.txt"
        self.ledger.parent.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        subprocess.run(["tmux", "kill-session", "-t", self._session], capture_output=True)
        if self._old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self._old_home
        shutil.rmtree(self._home, ignore_errors=True)

    def _real_bridge(self):
        cfg = Config(agent="claude-code", token="1:2", allowed_user_ids=[7],
                     tmux_session=self._session)
        return AttachBridge(cfg, client=_FakeClient())

    def test_a_fresh_process_loads_the_keys_an_earlier_one_confirmed(self):
        self.ledger.write_text("k1\nk2\n", "utf-8")
        b = self._real_bridge()
        self.assertEqual(b._sent_path, self.ledger)
        self.assertEqual(b._sent_keys, {"k1", "k2"},
                          "a restarted process must remember what it already delivered — "
                          "without this the same text goes out to the user a second time")
        self.assertTrue(b._mark_sent("k1"),
                         "a key loaded from a prior process must count as already delivered")

    def test_a_missing_ledger_starts_empty_instead_of_crashing(self):
        b = self._real_bridge()
        self.assertEqual(b._sent_keys, set())

    def test_an_undecodable_ledger_starts_empty_instead_of_crashing(self):
        """A truncated/garbled ledger raises `UnicodeDecodeError` — a `ValueError`, NOT an
        `OSError`. Catching only `OSError` meant the bridge refused to start at all after a
        half-written file, which is strictly worse than losing the dedup memory."""
        self.ledger.write_bytes(b"k1\n\xff\xfe not utf-8\n")
        b = self._real_bridge()
        self.assertEqual(b._sent_keys, set(),
                          "an unreadable ledger must degrade to 'nothing confirmed yet', "
                          "not stop the bridge from starting")


# --------------------------------------------------------------------------------------
# 8 — a PERMANENTLY unwritable ledger (not merely a `_FailAfter` patch) must not block the
#     whole FIFO: this is the blocking review finding T-1047 exists to fix.
# --------------------------------------------------------------------------------------
class PermanentlyReadOnlyLedgerDoesNotBlockTheQueueTests(unittest.TestCase):
    @unittest.skipIf(os.geteuid() == 0, "running as root defeats chmod — the write would "
                                          "succeed and the test would pass for the wrong reason")
    def test_three_queued_replies_all_get_delivered_despite_a_readonly_ledger_file(self):
        """No `builtins.open` patch here on purpose — a real read-only file, so `chmod` alone
        has to be what makes the write fail; a patch would not prove that. Before the fix only
        the FIRST of the three texts ever reached the user: the ledger write for its key failed
        and the record (being the FIFO head) was held forever, so the second and third replies
        sat behind it untouched."""
        self.addCleanup(logging.disable, logging.NOTSET)
        logging.disable(logging.CRITICAL)
        with tempfile.TemporaryDirectory() as td:
            b = _bridge(td)
            b._sent_path.write_text("", "utf-8")
            os.chmod(b._sent_path, 0o444)
            outbox = b._ensure_outbox()
            outbox.enqueue(["one"], [], "k1")
            outbox.enqueue(["two"], [], "k2")
            outbox.enqueue(["three"], [], "k3")

            try:
                for _ in range(20):      # far more than the 3×3 cycles the bounded retry needs
                    b._flush_pending()
                    if outbox.head() is None:
                        break
            finally:
                os.chmod(b._sent_path, 0o644)   # must heal before TemporaryDirectory cleans up

            self.assertIsNone(outbox.head(), "the queue must drain even though the ledger never "
                                              "accepts a single write")
            texts = [msg for chat, msg in b.tg.sent if not msg.startswith("⚠️")]
            self.assertEqual(texts, ["one", "two", "three"],
                              "every queued reply must reach the user, not just the first one "
                              "whose ledger write happened to fail")


# --------------------------------------------------------------------------------------
# 9 — a transient failure that heals before the retries run out: no release, no duplicate
# --------------------------------------------------------------------------------------
class TransientLedgerFailureHealsWithoutDuplicatingOrReleasingTests(unittest.TestCase):
    def test_disk_healing_mid_retry_completes_the_record_and_sends_the_text_only_once(self):
        with tempfile.TemporaryDirectory() as td:
            b = _bridge(td)
            outbox = b._ensure_outbox()
            outbox.enqueue(["hello"], [], "k1")

            with _FailAfter(b._sent_path, fail_from=1):
                for _ in range(OUTBOX_MAX_ATTEMPTS - 1):   # fewer cycles than the give-up bound
                    b._flush_pending()
                self.assertIsNotNone(outbox.head(),
                                      "setup: a transient failure must still keep the record "
                                      "queued while retries remain")

            b._flush_pending()         # disk healed — this cycle must finish the record

            self.assertIsNone(outbox.head(), "the record must complete once the ledger heals")
            self.assertIn("k1", b._sent_path.read_text("utf-8").split())
            self.assertEqual(b.tg.sent, [(7, "hello")],
                              "healing before the retries are exhausted must not send the text "
                              "a second time")


# --------------------------------------------------------------------------------------
# 10 — a stump left by an interrupted write must not glue onto the next append
# --------------------------------------------------------------------------------------
class SelfDelimitingLedgerWriteTests(unittest.TestCase):
    def test_an_unterminated_stump_does_not_swallow_the_next_appended_key(self):
        """Models a write cut off mid-line (e.g. `ENOSPC` after the id but before its `\\n`):
        the file ends in "k2st" with no trailing newline. The next append must still produce a
        separately-readable "k3", not a glued "k2stk3" that matches neither real id."""
        with tempfile.TemporaryDirectory() as td:
            b = _bridge(td)
            b._sent_path.write_text("k1\nk2st", "utf-8")   # stump, no trailing newline
            b._sent_keys = b._load_sent_ledger()            # reload, as a restart would

            self.assertTrue(b._mark_sent("k3"))

            tokens = b._sent_path.read_text("utf-8").split()
            self.assertIn("k3", tokens, "the new key must be its own token")
            self.assertNotIn("k2stk3", tokens, "it must not have glued onto the stump")
            reloaded = b._load_sent_ledger()
            self.assertIn("k3", reloaded)


# --------------------------------------------------------------------------------------
# 11 — the retry path must not claim "nothing is holding it" about a key that a queued
#      record DOES hold
# --------------------------------------------------------------------------------------
class RetryReportingDoesNotClaimNothingHoldsAQueuedKeyTests(unittest.TestCase):
    def test_retry_of_a_queued_keys_write_never_logs_nothing_is_holding_it(self):
        with tempfile.TemporaryDirectory() as td:
            b = _bridge(td)
            outbox = b._ensure_outbox()
            outbox.enqueue(["hello"], [], "k1")

            with _FailAfter(b._sent_path, fail_from=1):
                self.assertFalse(b._mark_sent_keys("k1", queued=True),
                                  "setup: the write must fail so k1 ends up unledgered")
                self.assertIn("k1", b._unledgered())

                # No throttle reset needed: `_ledger_warn` keeps one window PER MESSAGE KIND and
                # the setup above used the "queued" one, so "retry" is still untouched here.
                with self.assertLogs("agent2telegram.attach", level="ERROR") as cm:
                    b._retry_unconfirmed_ledger()

            self.assertTrue(cm.output, "the retry must log something about the still-failing write")
            for line in cm.output:
                self.assertNotIn("nothing is holding it", line,
                                  "k1 is the key of a record still sitting in the outbox queue — "
                                  "the retry path does not know that and must not claim otherwise")


# --------------------------------------------------------------------------------------
# 12 — the queue's own storage, not just the ledger file, can be unwritable too. The revised
#      fix counts attempts IN MEMORY precisely so this does not need a disk write — verify a
#      tick under BOTH conditions at once does not raise, while retries remain.
# --------------------------------------------------------------------------------------
@unittest.skipIf(os.geteuid() == 0, "running as root defeats chmod — the write would "
                                      "succeed and the test would pass for the wrong reason")
class UnwritableQueueDirectoryPlusUnwritableLedgerTests(unittest.TestCase):
    def test_a_tick_does_not_raise_when_both_the_ledger_and_the_pending_directory_are_readonly(self):
        """Before this fix, counting attempts via `outbox.fail(...)` meant every failed-ledger
        tick ALSO tried to write the outbox's own record file — so once the pending directory
        itself turned unwritable (the usual case: ENOSPC or a permission problem hits the whole
        state tree, not just one file in it), that write raised and the exception escaped
        `_flush_pending`, taking the whole outbound cycle with it. The fix counts in memory, so a
        tick with no pending chunks left to confirm must not touch the pending directory at all.

        The chunk is confirmed in a FIRST tick, while the pending directory is still writable
        (`mark_chunk_sent` itself is unrelated to this fix and is not under test here) — then the
        directory is made read-only and a SECOND tick, still below `OUTBOX_MAX_ATTEMPTS`, must
        complete without raising."""
        self.addCleanup(logging.disable, logging.NOTSET)
        logging.disable(logging.CRITICAL)
        with tempfile.TemporaryDirectory() as td:
            b = _bridge(td)
            outbox = b._ensure_outbox()
            outbox.enqueue(["one"], [], "k1")
            b._sent_path.write_text("", "utf-8")
            os.chmod(b._sent_path, 0o444)

            # Tick 1: pending directory still writable — the chunk gets confirmed, only the
            # ledger write fails (attempt 1 of OUTBOX_MAX_ATTEMPTS).
            b._flush_pending()
            self.assertIsNotNone(outbox.head(), "setup: the ledger failure must keep it queued")

            os.chmod(outbox.pending_directory, 0o555)
            try:
                # Tick 2 (attempt 2 of 3): below the give-up bound, so nothing here needs to
                # write to the now-readonly pending directory — must not raise.
                b._flush_pending()
            finally:
                os.chmod(outbox.pending_directory, 0o755)
                os.chmod(b._sent_path, 0o644)

            self.assertIsNotNone(outbox.head(), "still within the bounded retries — stays queued")
            self.assertEqual(b.tg.sent, [(7, "one")],
                              "the text must not be sent a second time while retrying the ledger")

    def test_a_tick_that_gives_up_does_not_raise_when_the_pending_directory_stays_readonly(self):
        """Same setup as above, carried past `OUTBOX_MAX_ATTEMPTS`: once the ledger write gives
        up, releasing the record means `outbox.done()`, which unlinks the pending file — itself a
        write to the very directory this test keeps read-only. That release is expected to KEEP
        FAILING (releasing it is impossible without write access; tracked separately as T-1060),
        but the failure must not escape `_flush_pending` — the outbound cycle (drain, turn end,
        heartbeat) must survive every tick regardless."""
        self.addCleanup(logging.disable, logging.NOTSET)
        logging.disable(logging.CRITICAL)
        with tempfile.TemporaryDirectory() as td:
            b = _bridge(td)
            outbox = b._ensure_outbox()
            outbox.enqueue(["one"], [], "k1")
            b._sent_path.write_text("", "utf-8")
            os.chmod(b._sent_path, 0o444)

            b._flush_pending()   # pending directory still writable: chunk confirmed (attempt 1)

            os.chmod(outbox.pending_directory, 0o555)
            try:
                for _ in range(OUTBOX_MAX_ATTEMPTS + 2):   # carries well past the give-up bound
                    b._flush_pending()   # must never raise, even on the tick that gives up
            finally:
                os.chmod(outbox.pending_directory, 0o755)
                os.chmod(b._sent_path, 0o644)

            self.assertIsNotNone(outbox.head(), "releasing it needs a write the directory still "
                                                  "refuses — it stays queued, not lost (T-1060)")
            texts = [msg for chat, msg in b.tg.sent if not msg.startswith("⚠️")]
            self.assertEqual(texts, ["one"],
                              "the text must not be sent a second time across any of these ticks")


# --------------------------------------------------------------------------------------
# 13 — give-up on a permanently broken ledger must warn the owner ONCE, not once per record
# --------------------------------------------------------------------------------------
@unittest.skipIf(os.geteuid() == 0, "running as root defeats chmod — the write would "
                                    "succeed, the ledger would never fail and no warning "
                                    "would be produced at all")
class GiveUpDoesNotFloodTheChatTests(unittest.TestCase):
    def test_ten_queued_replies_giving_up_on_the_ledger_produce_exactly_one_warning(self):
        """Ten replies queued behind a ledger that never heals: every one of them eventually
        gives up and is released (so none is lost), but the owner-facing warning
        (`_report_ledger_giveup`) must fire once, not once per released record — the throttle is
        `_ledger_warn_due("giveup-report")`, shared across all ten give-ups."""
        self.addCleanup(logging.disable, logging.NOTSET)
        logging.disable(logging.CRITICAL)
        with tempfile.TemporaryDirectory() as td:
            b = _bridge(td)
            outbox = b._ensure_outbox()
            for i in range(10):
                outbox.enqueue([f"text{i}"], [], f"k{i}")
            b._sent_path.write_text("", "utf-8")
            os.chmod(b._sent_path, 0o444)
            try:
                for _ in range(60):      # far more ticks than 10 records * OUTBOX_MAX_ATTEMPTS need
                    b._flush_pending()
                    if outbox.head() is None:
                        break
            finally:
                os.chmod(b._sent_path, 0o644)

            self.assertIsNone(outbox.head(), "every record must eventually be released, "
                                              "not held forever by an unhealable ledger")
            texts = [msg for chat, msg in b.tg.sent if not msg.startswith("⚠️")]
            warnings = [msg for chat, msg in b.tg.sent if msg.startswith("⚠️")]
            self.assertEqual(texts, [f"text{i}" for i in range(10)],
                              "every queued reply must still reach the user")
            self.assertEqual(len(warnings), 1,
                              "ten give-ups within the throttle window must produce ONE warning, "
                              "not one per released record")


if __name__ == "__main__":
    unittest.main()
