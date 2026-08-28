"""Measures 52103fd: "attach: silence is not the end of a turn — only an authoritative end
lowers the origin".

52103fd fixed a regression that 14f204a itself introduced while closing the leak measured in
tests/test_unik_do_chatu.py: `_finish_turn()` used to lower `_turn_from_tg`/`_tg_since`
UNCONDITIONALLY, on every call — including the two non-authoritative ones (the 90 s idle
fallback, and a stale end-of-turn signal judged "suspiciously fast"). Both of those fire while
the agent is still working (a long build, a full test run) — lowering the origin there means
whatever the agent writes afterwards is silently dropped: no forward, no backstop (it already
ran), and the turn already logged itself as answered. That is a Telegram turn going unanswered —
the exact defect this fork exists to fix, reintroduced by the fix for a DIFFERENT leak.

Review found these regressions mutationally: each of the four `Task 2` tests below pins three
lines that passed the entire 338-test suite when mutated away.

Same harness as tests/test_unik_do_chatu.py and tests/test_regrese_mostu.py: a real
`AttachBridge` (`_regrese_bridge()`), a real `ClaudeCodeReader`, fixture records shaped like a
real transcript, and assertions on what actually leaves the bridge (`bridge.tg.sent`) or on the
bridge's own turn-origin/boundary state — never on private field names that could be renamed
without changing behaviour.
"""
import tempfile
import threading
import time
import unittest
from pathlib import Path

from agent2telegram import attach as attach_mod

from tests.test_regrese_mostu import (
    _assistant_record,
    _regrese_bridge,
    _user_record,
    _write_transcript,
)
from tests.test_unik_do_chatu import _append_transcript
from tests.test_v2_durability import _msg


# ========================================================================================
# Task 1 — only an authoritative end may lower the Telegram origin
# ========================================================================================
class NonDefinitiveTurnEndsDoNotLowerTheOriginTests(unittest.TestCase):
    """The 90 s idle fallback and a stale/"suspiciously fast" end-of-turn signal both close a
    turn (`_turn_active.clear()`), but neither may lower `_turn_from_tg`/`_tg_since` — the agent
    is still working, and the real answer it writes afterwards must still be forwarded."""

    def test_idle_fallback_does_not_lower_the_origin_and_the_real_answer_still_arrives(self):
        """Reproduces the scenario from the commit message: a Telegram message kicks off a long
        tool run, the agent sends an intro line, then the transcript goes quiet for 90s+ while
        the tool/build/test-suite runs — the idle fallback fires — and only THEN does the agent
        write its real answer. That answer must still reach Telegram.

        Calls `_end_turn(definitive=False)` — the exact call `_outbound_loop`'s idle branch
        makes — not `_finish_turn()` with defaults, so this measures what production actually
        does, not some other call shape."""
        with tempfile.TemporaryDirectory() as td:
            b = _regrese_bridge(td)
            transcript = Path(td) / "transcript.jsonl"
            b._transcript = transcript

            b._handle(_msg(1, "[TG] run the full test suite"))
            boundary = b._tg_since

            _write_transcript(transcript, [
                _user_record("[TG] run the full test suite"),
                _assistant_record("[tg] Starting the test suite, this will take a while..."),
            ])
            b._drain_transcript()
            self.assertIn("Starting the test suite", "\n".join(b.tg.sent),
                          "test setup is wrong: the intro line should have forwarded")
            b.tg.sent.clear()               # only care about what arrives AFTER the idle fallback

            # The transcript goes quiet for 90s+ while the suite runs — _outbound_loop's idle
            # branch fires `self._end_turn(definitive=False)`.
            b._end_turn(definitive=False)

            self.assertFalse(b._turn_active.is_set(), "the idle fallback must still close the turn")
            self.assertTrue(
                b._turn_from_tg,
                "an idle fallback (the agent is still working) must not lower the Telegram "
                "origin — the real answer written afterwards would then be silently dropped",
            )
            self.assertEqual(
                b._tg_since, boundary,
                "an idle fallback must not reset the boundary either",
            )

            # The agent finishes and writes its real answer.
            _append_transcript(transcript, [_assistant_record("[tg] all 338 tests passed")])
            b._drain_transcript()

            self.assertIn(
                "all 338 tests passed", "\n".join(b.tg.sent),
                "the answer written after an idle-triggered fallback never reached Telegram — "
                "exactly the regression 52103fd fixed",
            )

    def test_idle_fallback_through_the_real_outbound_loop_still_delivers_afterwards(self):
        """Same scenario as above, but driven through the real `_outbound_loop` thread — the
        code path production actually runs — rather than calling `_end_turn` by hand. Only
        `_maybe_reresolve`/`_beat` are stubbed: they touch state (`_last_resolve`, `_heartbeat`)
        that only `AttachBridge.__init__` sets, which the hand-assembled test bridge skips (same
        stub list `TurnEndBackstopTests` in test_v2_durability.py uses, for the same reason)."""
        with tempfile.TemporaryDirectory() as td:
            b = _regrese_bridge(td)
            transcript = Path(td) / "transcript.jsonl"
            transcript.write_text("")
            b._transcript = transcript
            b._maybe_reresolve = lambda: None
            b._beat = lambda: None

            b._handle(_msg(1, "[TG] run the full test suite"))
            boundary = b._tg_since

            _write_transcript(transcript, [
                _user_record("[TG] run the full test suite"),
                _assistant_record("[tg] Starting the test suite, this will take a while..."),
            ])

            orig_idle = attach_mod.IDLE_DONE
            attach_mod.IDLE_DONE = 0.02
            try:
                t = threading.Thread(target=b._outbound_loop, daemon=True)
                t.start()
                time.sleep(0.5)             # several ticks: intro forwards, then idle fires

                self.assertFalse(b._turn_active.is_set(),
                                  "test setup is wrong: the idle fallback should have fired by now")
                self.assertTrue(
                    b._turn_from_tg,
                    "the real outbound loop's idle branch lowered the Telegram origin — the "
                    "regression 52103fd fixed",
                )
                self.assertEqual(b._tg_since, boundary)

                _append_transcript(transcript, [_assistant_record("[tg] all 338 tests passed")])
                time.sleep(0.5)              # give the loop a few more ticks to drain it
                b._stop.set()
                t.join(timeout=3)
            finally:
                attach_mod.IDLE_DONE = orig_idle

            self.assertIn(
                "all 338 tests passed", "\n".join(b.tg.sent),
                "the answer written after the real outbound loop's idle fallback never reached "
                "Telegram",
            )

    def test_suspiciously_fast_turn_end_does_not_lower_the_origin_either(self):
        """The other non-authoritative path: `_outbound_loop` judges an end-of-turn signal
        arriving implausibly soon after the turn began to be stale, and calls
        `_finish_turn(definitive=False)` directly (no `_drain_transcript()` first — that already
        ran earlier in the same tick) — the exact call this test makes."""
        with tempfile.TemporaryDirectory() as td:
            b = _regrese_bridge(td)
            transcript = Path(td) / "transcript.jsonl"
            b._transcript = transcript

            b._handle(_msg(1, "[TG] quick question"))
            boundary = b._tg_since
            _write_transcript(transcript, [
                _user_record("[TG] quick question"),
                _assistant_record("[tg] here is the answer so far"),
            ])
            b._drain_transcript()
            b.tg.sent.clear()

            b._finish_turn(definitive=False)

            self.assertFalse(b._turn_active.is_set())
            self.assertTrue(
                b._turn_from_tg,
                "a stale/suspiciously-fast end-of-turn signal must not lower the Telegram "
                "origin — the agent is still working",
            )
            self.assertEqual(b._tg_since, boundary)

            _append_transcript(transcript, [_assistant_record("[tg] the real final answer")])
            b._drain_transcript()
            self.assertIn(
                "the real final answer", "\n".join(b.tg.sent),
                "the answer written after a suspicious-fast turn end never reached Telegram",
            )

    def test_suspicious_turn_end_through_the_real_outbound_loop_does_not_lower_the_origin(self):
        """Same case as above, but through the real `_outbound_loop`, so a wiring mistake — the
        loop's suspicious-end branch forgetting to pass `definitive=False` — is caught too, not
        just a bug inside `_finish_turn` itself. Mirrors
        `test_idle_fallback_through_the_real_outbound_loop_still_delivers_afterwards` above."""
        with tempfile.TemporaryDirectory() as td:
            b = _regrese_bridge(td)
            transcript = Path(td) / "transcript.jsonl"
            transcript.write_text("")
            b._transcript = transcript
            b._maybe_reresolve = lambda: None
            b._beat = lambda: None

            b._handle(_msg(1, "[TG] quick question"))
            boundary = b._tg_since
            _write_transcript(transcript, [
                _user_record("[TG] quick question"),
                _assistant_record("[tg] here is the answer so far"),
            ])
            b.tg.sent.clear()
            # Signals a stale/implausibly-fast end-of-turn marker without any real elapsed time —
            # `_outbound_loop` judges this "suspiciously fast" (well under SUSPICIOUS_TURN_SECONDS)
            # and must route it through `_finish_turn(definitive=False)`.
            b._pending_turn_end = True

            t = threading.Thread(target=b._outbound_loop, daemon=True)
            t.start()
            time.sleep(0.3)

            self.assertFalse(b._turn_active.is_set(),
                              "test setup is wrong: the suspicious end should have fired by now")
            self.assertTrue(
                b._turn_from_tg,
                "the real outbound loop's suspicious-end branch lowered the Telegram origin",
            )
            self.assertEqual(b._tg_since, boundary)

            _append_transcript(transcript, [_assistant_record("[tg] the real final answer")])
            time.sleep(0.3)
            b._stop.set()
            t.join(timeout=3)

            self.assertIn(
                "the real final answer", "\n".join(b.tg.sent),
                "the answer written after the real outbound loop's suspicious-end branch never "
                "reached Telegram",
            )


class DefinitiveTurnEndStillLowersTheOriginTests(unittest.TestCase):
    """Guards the OTHER direction of the same change: an authoritative end
    (`_finish_turn()`/`_finish_turn(definitive=True)`) must still lower the origin — otherwise
    the leak tests/test_unik_do_chatu.py fixed (14f204a) comes back. This is a fresh, minimal
    pin of that same fact (tests/test_unik_do_chatu.py already has one — see this file's own
    module docstring for how that one was re-verified)."""

    def test_finish_turn_with_default_arguments_still_lowers_the_origin(self):
        with tempfile.TemporaryDirectory() as td:
            b = _regrese_bridge(td)
            transcript = Path(td) / "transcript.jsonl"
            b._transcript = transcript

            b._handle(_msg(1, "[TG] hello"))
            _write_transcript(transcript, [
                _user_record("[TG] hello"),
                _assistant_record("[tg] hi there"),
            ])
            b._drain_transcript()

            b._finish_turn()          # default definitive=True — an authoritative end

            self.assertFalse(b._turn_active.is_set())
            self.assertFalse(
                b._turn_from_tg,
                "an authoritative turn end (default arguments) must still lower the Telegram "
                "origin — otherwise the local-turn leak 14f204a fixed comes back",
            )


class TurnSeqGuardsAFinishingBackstopTests(unittest.TestCase):
    """`_finish_turn` runs on the outbound thread and can sit in the backstop retry loop
    (`_retry_last_assistant_text`) for over a second. If a NEW turn starts on the inbound thread
    while the old one is still sitting there, the old turn's `seq_at_entry` (captured on entry)
    must stop it from wiping the state the new turn already owns when it finally reaches the
    bottom of `_finish_turn` and would otherwise unconditionally lower `_turn_from_tg`/`_tg_since`."""

    def test_a_turn_that_begins_during_the_backstop_retry_keeps_its_origin(self):
        with tempfile.TemporaryDirectory() as td:
            b = _regrese_bridge(td)
            transcript = Path(td) / "transcript.jsonl"
            b._transcript = transcript

            # Local content already on disk so the boundary _handle() stamps is a real nonzero
            # offset, not zero by coincidence — a mutation that resets to a stale nonzero value
            # instead of 0 would otherwise slip past an assertion that only checks "!= 0".
            _write_transcript(transcript, [
                {"type": "assistant", "message": {"content": [{"type": "text",
                             "text": "earlier, unrelated local output"}]}},
            ])
            # Drain it BEFORE the Telegram turn begins — otherwise the retry loop below would
            # drain (and forward) this pre-existing text itself on its first pass, satisfying
            # `_retry_last_assistant_text()` before `_wait_backstop_retry()` is ever reached.
            b._drain_transcript()
            b._handle(_msg(1, "[TG] first question"))
            first_seq = b._turn_seq
            first_boundary = b._tg_since
            self.assertGreater(first_boundary, 0, "test setup is wrong: boundary should be nonzero")
            # Nothing was ever forwarded for turn 1 — the backstop has to actually retry.
            self.assertFalse(b._turn_text_sent)

            started_new_turn = []

            def _wait_and_start_a_new_turn():
                if not started_new_turn:
                    started_new_turn.append(True)
                    # Simulate the inbound thread starting a brand-new turn WHILE this
                    # `_finish_turn()` call is still sitting in the backstop retry loop below.
                    b._handle(_msg(2, "[TG] second question, unrelated to the first"))

            orig_delay = attach_mod.BACKSTOP_RETRY_DELAY
            orig_attempts = attach_mod.BACKSTOP_RETRY_ATTEMPTS
            attach_mod.BACKSTOP_RETRY_DELAY = 0.0
            attach_mod.BACKSTOP_RETRY_ATTEMPTS = 2
            b._wait_backstop_retry = _wait_and_start_a_new_turn
            try:
                b._finish_turn()          # authoritative end of turn 1
            finally:
                attach_mod.BACKSTOP_RETRY_DELAY = orig_delay
                attach_mod.BACKSTOP_RETRY_ATTEMPTS = orig_attempts

            self.assertTrue(started_new_turn, "test setup is wrong: the new turn never started")
            self.assertNotEqual(
                b._turn_seq, first_seq,
                "test setup is wrong: the new turn must have bumped _turn_seq",
            )
            self.assertTrue(
                b._turn_from_tg,
                "turn 1's finishing backstop wiped the origin of turn 2, which started WHILE "
                "it was still retrying — the _turn_seq guard should have stopped it",
            )
            self.assertEqual(
                b._tg_since, first_boundary,
                "turn 1's finishing backstop reset the boundary out from under turn 2",
            )


# ========================================================================================
# Task 2 — three lines review found survive the whole 338-test suite when mutated away
# ========================================================================================
class FinishTurnResetsBothOriginFlagsTests(unittest.TestCase):
    """`_finish_turn`'s authoritative-end guard must reset `_tg_since` alongside
    `_turn_from_tg` — a mutation that drops only the `_tg_since = 0` line passed the entire
    suite: no existing test reads `_tg_since` after a `_finish_turn()` call at all."""

    def test_finish_turn_resets_tg_since_not_only_turn_from_tg(self):
        with tempfile.TemporaryDirectory() as td:
            b = _regrese_bridge(td)
            transcript = Path(td) / "transcript.jsonl"
            b._transcript = transcript

            _write_transcript(transcript, [
                {"type": "assistant", "message": {"content": [{"type": "text",
                             "text": "earlier, unrelated local output"}]}},
            ])
            b._handle(_msg(1, "[TG] hello"))
            self.assertGreater(b._tg_since, 0, "test setup is wrong: boundary should be nonzero")

            _append_transcript(transcript, [
                _user_record("[TG] hello"),
                _assistant_record("[tg] hi there"),
            ])
            b._drain_transcript()
            b._finish_turn()

            self.assertFalse(b._turn_active.is_set())
            self.assertFalse(b._turn_from_tg)
            self.assertEqual(
                b._tg_since, 0,
                "an authoritative turn end must reset _tg_since to 0, not just _turn_from_tg — "
                "a stale nonzero boundary left behind would misfire the "
                "`boundary > size` guard the next time the file shrinks, or silently clamp a "
                "future purely-local backstop read that should have been unrestricted",
            )


class MaybeReresolveResetsTheBoundaryTests(unittest.TestCase):
    """`_maybe_reresolve()` switching to a different transcript file must reset `_tg_since` —
    the comment right there says why: "offsets do not carry across to a different file". A
    mutation dropping only that line passed the whole suite:
    tests/test_unik_do_chatu.py's rotation test exercises the OTHER reset site
    (`_drain_transcript`'s `size < self._tpos` branch), not this one."""

    def test_switching_transcripts_resets_tg_since(self):
        with tempfile.TemporaryDirectory() as td:
            b = _regrese_bridge(td)
            old_transcript = Path(td) / "old.jsonl"
            new_transcript = Path(td) / "new.jsonl"
            new_transcript.write_text("")
            b._transcript = old_transcript

            _write_transcript(old_transcript, [
                {"type": "assistant", "message": {"content": [{"type": "text",
                             "text": "earlier, unrelated"}]}},
            ])
            b._handle(_msg(1, "[TG] hi"))
            self.assertGreater(b._tg_since, 0, "test setup is wrong: boundary should be nonzero")
            # Not mid-turn: `_maybe_reresolve` refuses to jump transcripts mid-turn when the
            # session cwd can't be confirmed — irrelevant to what this test measures (the reset
            # itself), so take that branch out of the way.
            b._turn_active.clear()

            b._resolve_transcript = lambda: new_transcript
            b._last_resolve = 0.0
            b._maybe_reresolve()

            self.assertEqual(b._transcript, new_transcript, "test setup is wrong: no switch happened")
            self.assertEqual(
                b._tg_since, 0,
                "_maybe_reresolve switched transcripts but left _tg_since pointing at an offset "
                "into the OLD file",
            )


class LastAssistantTextStaleBoundaryGuardTests(unittest.TestCase):
    """The `boundary > size` guard in `_last_assistant_text()` is the one place the backstop is
    meant to go quiet on purpose (a stale offset into a file that has since shrunk/rotated —
    every route that shortens or swaps the file is supposed to clear the boundary first, so
    reaching this guard at all means something upstream didn't). A mutation dropping the
    `log.warning(...)` call — leaving the silent `return None` in place — passed the whole
    suite: nothing asserted on the log before."""

    def test_stale_boundary_past_eof_returns_none_and_logs_a_warning(self):
        with tempfile.TemporaryDirectory() as td:
            b = _regrese_bridge(td)
            transcript = Path(td) / "transcript.jsonl"
            transcript.write_text("short")
            b._transcript = transcript
            b._tg_since = 10_000        # far past EOF

            with self.assertLogs("agent2telegram.attach", level="WARNING") as cm:
                result = b._last_assistant_text()

            self.assertIsNone(result)
            self.assertTrue(
                any("boundary" in line and "EOF" in line for line in cm.output),
                f"the stale-boundary guard must log a warning, not go quiet silently: {cm.output!r}",
            )


class ResumePositionRestoresTheBoundaryTests(unittest.TestCase):
    """`_resume_position()` (run at startup) restores `_turn_from_tg` from the transcript after a
    restart — 52103fd made it restore `_tg_since` alongside it, since a boundary left at its
    pre-restart value (or left at 0 while the origin comes back True) is exactly the same class
    of bug as the other two resets above: an offset that no longer means what it claims to."""

    def test_resuming_a_telegram_turn_restores_a_nonzero_boundary(self):
        with tempfile.TemporaryDirectory() as td:
            b = _regrese_bridge(td)
            transcript = Path(td) / "transcript.jsonl"
            _write_transcript(transcript, [
                {"type": "assistant", "message": {"content": [{"type": "text",
                             "text": "earlier, unrelated"}]}},
                _user_record("[TG] resumed question"),
            ])
            b._transcript = transcript
            b._tpos = transcript.stat().st_size
            b._turn_from_tg = False
            b._tg_since = 0

            b._resume_position()

            self.assertTrue(b._turn_from_tg,
                             "test setup is wrong: origin should have been recovered as Telegram")
            self.assertEqual(
                b._tg_since, transcript.stat().st_size,
                "_resume_position recovered the Telegram origin but left _tg_since at 0 — the "
                "backstop after a restart would be free to reach back into whatever preceded "
                "the resumed message",
            )

    def test_resuming_a_local_turn_clears_a_stale_boundary(self):
        with tempfile.TemporaryDirectory() as td:
            b = _regrese_bridge(td)
            transcript = Path(td) / "transcript.jsonl"
            _write_transcript(transcript, [
                {"type": "assistant", "message": {"content": [{"type": "text",
                             "text": "earlier, unrelated"}]}},
                _user_record("typed straight into the tmux pane, not from Telegram"),
            ])
            b._transcript = transcript
            b._tpos = transcript.stat().st_size
            b._turn_from_tg = False
            b._tg_since = 12345         # a stale nonzero value left over from a PRIOR turn

            b._resume_position()

            self.assertFalse(b._turn_from_tg)
            self.assertEqual(
                b._tg_since, 0,
                "resuming a turn that is NOT Telegram-originated must clear a stale nonzero "
                "boundary left over from before the restart",
            )


# ========================================================================================
# Task 3 — the boundary must not just stay silent, it must actually deliver
# ========================================================================================
class BackstopDeliversAcrossANonzeroBoundaryTests(unittest.TestCase):
    """tests/test_unik_do_chatu.py's BackstopHonoursTheTelegramBoundaryTests has three tests that
    the backstop stays SILENT behind the boundary, and zero that it actually DELIVERS across a
    non-zero one. test_regrese_mostu.py's `test_backstop_still_delivers_if_no_interim_text_was_
    forwarded` looks like it covers delivery, but it calls `_handle()` before the transcript
    file exists, so `_tg_since` is 0 there by construction (`_transcript_size()`'s `stat()`
    raises `OSError` and falls back to 0) — it can never catch a regression that only breaks a
    NON-zero boundary, which is exactly the risk of tightening that guard: closing the leak for
    free by never delivering anything past it."""

    def test_backstop_delivers_the_final_answer_when_the_boundary_is_nonzero(self):
        with tempfile.TemporaryDirectory() as td:
            b = _regrese_bridge(td)
            transcript = Path(td) / "transcript.jsonl"
            b._transcript = transcript

            # Local content already on disk BEFORE any Telegram message exists, so the boundary
            # _begin_turn() stamps below is a real nonzero offset, not zero by coincidence.
            _write_transcript(transcript, [
                {"type": "assistant", "message": {"content": [{"type": "text",
                             "text": "earlier, unrelated local output"}]}},
            ])
            b._drain_transcript()
            self.assertEqual(b.tg.sent, [], "test setup is wrong: nothing should forward yet")

            b._handle(_msg(1, "[TG] are you there?"))
            self.assertGreater(
                b._tg_since, 0,
                "test setup is wrong: the boundary must be nonzero for this test to mean anything",
            )

            # The agent's answer lands on disk, but is never drained through the normal forward
            # path (no _drain_transcript() call here) — the Stop hook fires having forwarded
            # NOTHING this turn, so only the backstop can deliver it.
            _append_transcript(transcript, [
                _user_record("[TG] are you there?"),
                _assistant_record("[tg] yes, right here"),
            ])

            # Isolation, not just intent: `_retry_last_assistant_text()`'s OWN retry loop calls
            # `_drain_transcript()` too — and THAT drains via the normal `_handle_event` forward
            # path, which does not consult the boundary at all. A first version of this test
            # left that door open: it "passed" even when `_last_assistant_text()`'s boundary
            # guard was mutated to always refuse (`boundary > size or boundary > 0`), because the
            # retry loop's own drain call silently delivered the text through the OTHER path
            # instead — the test was measuring normal forwarding, exactly the same mistake
            # `test_backstop_still_delivers_if_no_interim_text_was_forwarded` in
            # test_regrese_mostu.py made. Making `_drain_transcript` a hard failure here proves
            # delivery came from `_last_assistant_text()`'s own raw read, and nothing else.
            def _drain_must_not_run():
                raise AssertionError(
                    "the retry loop's own _drain_transcript() ran — the first raw "
                    "_last_assistant_text() check must find the answer directly; if it doesn't, "
                    "this test would measure normal forwarding, not the backstop"
                )
            b._drain_transcript = _drain_must_not_run

            orig_delay = attach_mod.BACKSTOP_RETRY_DELAY
            attach_mod.BACKSTOP_RETRY_DELAY = 0.0
            try:
                b._finish_turn()
            finally:
                attach_mod.BACKSTOP_RETRY_DELAY = orig_delay

            self.assertIn(
                "yes, right here", "\n".join(b.tg.sent),
                "the backstop failed to deliver a real answer sitting AFTER a nonzero boundary — "
                "closing the Vada B leak must not cost legitimate replies",
            )


if __name__ == "__main__":
    unittest.main()
