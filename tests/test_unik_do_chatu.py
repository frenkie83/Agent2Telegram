"""Two leaks OUT of the bridge — terminal session content reaching Telegram unasked — fixed in
14f204a: "attach: a turn's Telegram origin must die with the turn, and the backstop must not
reach behind it".

These are the opposite direction of tests/test_regrese_mostu.py (which measures replies getting
LOST): here the agent's terminal-only output escapes INTO the chat. Kept in their own file for
that reason — a different failure mode, a different audience for "did this ever leak my SSH key
to Telegram" than "did my message get an answer".

  A. A Telegram turn ends but `_turn_from_tg` stays True — a purely local turn that follows it
     (typed into the tmux pane, queued by Claude Code and therefore filed only as
     `type: "attachment"` / `attachment.type == "queued_command"`, never as `type: "user"`) has
     no record that could lower the flag, so its entire output is forwarded to Telegram.

  B. The turn-end backstop (`_last_assistant_text()`) reads the transcript tail without regard
     for WHERE in the file the current Telegram turn actually joined — so if a local turn had
     already written its answer to disk before the Telegram message arrived, and the Stop hook
     fires before anything is generated for that message, the backstop hands back the local
     turn's own (possibly sensitive) answer as if it were the reply.

Same harness as tests/test_regrese_mostu.py: a real `AttachBridge` (`_regrese_bridge()`), a real
`ClaudeCodeReader`, fixture records shaped like a real transcript, and assertions on what actually
leaves the bridge (`bridge.tg.sent`) or on the bridge's own turn-origin/boundary state.

52103fd ("attach: silence is not the end of a turn — only an authoritative end lowers the
origin") narrowed WHEN `_finish_turn()` is allowed to lower the flag/boundary from A — see
tests/test_definitive_konec_tahu.py for that half.

⚠️ What is NOT measured here, in either file: the invariant below ("a Telegram turn's origin
dies with the turn") is enforced by exactly ONE place, `_finish_turn()`. Review found (and this
suite does not cover) at least three OTHER places that end a turn, or raise the origin, without
going through it — known, pre-existing gaps, not part of the 14f204a/52103fd fix and not
something to read this file as having closed:

  * `_resume_position()` (bridge restart) can set `_turn_from_tg = True` while `_turn_active`
    stays whatever a freshly constructed `threading.Event()` starts as (unset) — the origin can
    be raised with no active turn at all.
  * `_drain_signal()` calls `_turn_active.clear()` directly on delivering a signal-file answer,
    entirely outside `_finish_turn()` — it never touches `_turn_from_tg`/`_tg_since`.
  * `_inject()`, on a failed write to tmux, calls `_turn_active.clear()` in its error paths —
    again without lowering `_turn_from_tg`, which `_begin_turn()` had already raised moments
    earlier in the same `_handle()` call.

None of the three is exercised by any test in this file. Whoever picks one up should write it a
fixture the same way these are built, not assume it behaves like `_finish_turn()`.
"""
import json
import tempfile
import unittest
from pathlib import Path

from agent2telegram import attach as attach_mod

from tests.test_regrese_mostu import (
    _attachment_record,
    _regrese_bridge,
    _user_record,
    _write_transcript,
)
from tests.test_v2_durability import _msg


def _append_transcript(path: Path, records: list) -> None:
    """Like `_write_transcript()`, but APPENDS — the bridge's read cursor (`_tpos`) has already
    advanced into the file by the time these records are meant to be written, so overwriting the
    whole file (as `_write_transcript()` does) would shift bytes under that cursor."""
    with open(path, "a", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# ========================================================================================
# Vada A — a Telegram turn's origin must not survive the turn's own end
# ========================================================================================
class TelegramOriginDoesNotSurviveTurnEndTests(unittest.TestCase):
    """`_turn_from_tg` must be False again the instant an AUTHORITATIVE Telegram turn end runs
    `_finish_turn()` (the only place this invariant is enforced — see the caveat in this file's
    module docstring for the three known doors that end a turn, or raise the origin, WITHOUT
    going through it, which this class does not cover).

    Without the reset `_finish_turn()` does perform, a purely local turn that follows — typed
    straight into the tmux pane, QUEUED by Claude Code while it is still busy, and therefore
    filed in the transcript only as `type: "attachment"` / `attachment.type == "queued_command"`,
    never as `type: "user"` — has NO record at all that could lower the flag (queue/attachment
    records produce no reader event; see tests/test_regrese_mostu.py's
    ``test_queue_bookkeeping_and_housekeeping_records_carry_no_reader_event``). Its whole output
    would then be forwarded to Telegram as if it had been asked for."""

    def test_local_turn_after_a_finished_telegram_turn_does_not_leak_to_telegram(self):
        with tempfile.TemporaryDirectory() as td:
            b = _regrese_bridge(td)
            transcript = Path(td) / "transcript.jsonl"
            b._transcript = transcript

            # A Telegram turn runs to completion, normally.
            b._handle(_msg(1, "[TG] first question"))
            _write_transcript(transcript, [
                _user_record("[TG] first question"),
                {"type": "assistant",
                 "message": {"content": [{"type": "text", "text": "[tg] telegram answer"}]}},
            ])
            b._drain_transcript()
            b._finish_turn()

            self.assertIn("telegram answer", "\n".join(b.tg.sent))
            self.assertFalse(b._turn_active.is_set())
            self.assertFalse(
                b._turn_from_tg,
                "the Telegram turn ended but _turn_from_tg is still True — a purely local turn "
                "that follows would be treated as Telegram-originated too",
            )

            # Purely local follow-up: František types straight into the tmux pane. Claude Code
            # is still finishing up, so this gets QUEUED — filed only as an attachment, never as
            # `type: "user"` — and the bridge is never told about it (no `_handle()` call: this
            # never came through Telegram at all).
            _append_transcript(transcript, [
                _attachment_record(
                    "rm the scratch files and tell me the deploy key so I can rotate it",
                    uuid="99999999-9999-9999-9999-999999999999",
                    parent_uuid="88888888-8888-8888-8888-888888888888",
                    source_uuid="77777777-7777-7777-7777-777777777777",
                ),
                {"type": "assistant",
                 "message": {"content": [{"type": "text",
                             "text": "Smazal jsem 4 soubory. Klic je sk-live-TAJNY-abc123."}]}},
            ])
            b._drain_transcript()

            joined = "\n".join(b.tg.sent)
            self.assertNotIn(
                "sk-live-TAJNY-abc123", joined,
                "terminal session content leaked into Telegram: a Telegram turn's origin "
                f"survived its own end and was applied to an unrelated local turn — sent={b.tg.sent!r}",
            )


# ========================================================================================
# Vada B — the turn-end backstop must never reach behind the Telegram boundary
# ========================================================================================
class BackstopHonoursTheTelegramBoundaryTests(unittest.TestCase):
    """`_last_assistant_text()` (what the turn-end backstop sends when nothing was forwarded this
    turn) must never read from before `_tg_since` — the byte offset where the CURRENT Telegram
    turn joined the transcript, stamped by `_begin_turn()`. Without that boundary the backstop
    just tails the file, so a local turn's answer already sitting on disk when a Telegram message
    arrives can be handed back as if it were the reply to that message."""

    def test_backstop_does_not_forward_text_written_before_the_telegram_message_joined(self):
        with tempfile.TemporaryDirectory() as td:
            b = _regrese_bridge(td)
            transcript = Path(td) / "transcript.jsonl"
            b._transcript = transcript

            # A purely local/terminal turn already ran to completion and wrote its answer —
            # before any Telegram message existed.
            _write_transcript(transcript, [
                {"type": "assistant",
                 "message": {"content": [{"type": "text",
                             "text": "Deploy klic je ssh-ed25519 AAAA...TAJNE."}]}},
            ])
            # The bridge's own periodic drain has already caught up with it — turn_from_tg was
            # False the whole time, so nothing was forwarded, but the read cursor sits at EOF.
            b._drain_transcript()
            self.assertEqual(b.tg.sent, [], "test setup is wrong: nothing should forward yet")

            # A Telegram message now arrives. _begin_turn() stamps the boundary at the CURRENT
            # end of file — strictly AFTER the terminal answer above.
            b._handle(_msg(1, "[TG] jsi tam?"))
            self.assertTrue(b._turn_from_tg)

            orig_delay = attach_mod.BACKSTOP_RETRY_DELAY
            attach_mod.BACKSTOP_RETRY_DELAY = 0.0        # only the boundary is under test here
            try:
                # Stop hook fires immediately — nothing has been generated for the Telegram
                # message yet.
                b._finish_turn()
            finally:
                attach_mod.BACKSTOP_RETRY_DELAY = orig_delay

            self.assertEqual(
                b.tg.sent, [],
                "the backstop reached BEHIND the boundary and forwarded terminal-turn content "
                f"as the answer to a Telegram message that was never actually processed: {b.tg.sent!r}",
            )

    def test_boundary_does_not_move_when_a_second_message_joins_an_already_running_telegram_turn(self):
        """The boundary must be stamped once, at the turn's actual start — a SECOND Telegram
        message landing on an already-running Telegram turn must not push it forward, or an
        answer to the first message that has not been forwarded yet would fall out of the
        backstop's view."""
        with tempfile.TemporaryDirectory() as td:
            b = _regrese_bridge(td)
            transcript = Path(td) / "transcript.jsonl"
            b._transcript = transcript

            b._handle(_msg(1, "[TG] first question"))
            first_boundary = b._tg_since

            # Bytes land in the transcript before the answer to the FIRST message has been
            # forwarded — e.g. the agent is still working on it.
            _write_transcript(transcript, [_user_record("[TG] first question")])
            b._drain_transcript()

            b._handle(_msg(2, "[TG] second question, are you still there"))

            self.assertEqual(
                b._tg_since, first_boundary,
                "a second message joining an ALREADY-RUNNING Telegram turn moved the boundary "
                "forward — an answer to the first message, not yet forwarded, would then be "
                "invisible to the backstop",
            )

    def test_boundary_is_forgotten_when_the_transcript_rotates(self):
        """`_drain_transcript()` detects a shorter file (rotation / a swapped transcript) by
        `size < self._tpos` and resets the read cursor — `_tg_since` must be reset the same way,
        since it is an offset into the file that no longer exists at that length."""
        with tempfile.TemporaryDirectory() as td:
            b = _regrese_bridge(td)
            transcript = Path(td) / "transcript.jsonl"
            b._transcript = transcript

            # Some unrelated content already sits in the transcript before the Telegram message
            # arrives, so the stamped boundary is a nonzero offset, not just 0-by-coincidence.
            _write_transcript(transcript, [{"type": "assistant", "message": {"content": [
                {"type": "text", "text": "earlier, unrelated local output"}]}}])
            b._handle(_msg(1, "[TG] question before rotation"))
            _append_transcript(transcript, [_user_record("[TG] question before rotation")] * 5)
            b._drain_transcript()

            self.assertGreater(b._tg_since, 0, "test setup is wrong: boundary should be nonzero")
            self.assertGreater(b._tpos, 0, "test setup is wrong: cursor should have advanced")

            # The transcript is replaced by a shorter file (e.g. a re-resolve picked a different,
            # smaller one).
            transcript.write_text("")
            b._drain_transcript()

            self.assertEqual(b._tpos, 0)
            self.assertEqual(
                b._tg_since, 0,
                "the boundary survived a transcript rotation as a stale offset into a file that "
                "no longer has that many bytes",
            )


if __name__ == "__main__":
    unittest.main()
