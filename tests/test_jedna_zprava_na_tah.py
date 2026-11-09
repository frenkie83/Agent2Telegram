"""T-0461: one turn = one message, and a second end-of-turn signal independent of the Stop hook.

Drives the real `AttachBridge` + real `ClaudeCodeReader` through `_regrese_bridge()` (see
`tests/test_regrese_mostu.py`), the same way `tests/test_attach_backstop.py` already does for the
late-final backstop. No network, no tmux, no real files outside `tempfile`.

Row 1 — batching (`_forward_turn_text` / `_batching` / `_flush_batch` / `_batch_limit`):
  1. the batch goes out at the end of the turn, as ONE message, texts in writing order
  2. past `turn_batch_seconds` the held text (and the rest of the turn) streams immediately
  3. `turn_batch_seconds = 0` reproduces the pre-change behaviour: every text, at once, alone
  4. a restart mid-turn does not resend what the ledger already has, and does not lose the rest
  5. the backstop never fires a second time over a batch it just flushed itself

Row 2 — end of turn from the transcript alone (`_transcript_turn_end_ready` / `_handle_event`):
  6. a text with `stop_reason: "end_turn"` closes the turn; one followed by a tool call does not
  7. a quiet shorter than `turn_end_quiet_seconds` does not close the turn
  8. a record filed before `_tg_since` does not close the turn
  9. with a working hook the turn closes once, not twice
  10. `IDLE_DONE` is still reachable — the new branch did not displace it
"""
import tempfile
import threading
import time
import unittest
from pathlib import Path

from agent2telegram import attach as attach_mod
from agent2telegram import readers

from tests.test_regrese_mostu import (
    _assistant_record,
    _regrese_bridge,
    _user_record,
    _write_transcript,
)
from tests.test_v2_durability import _msg


def _assistant_stop(text: str, stop_reason: "str | None", *, uuid: str = "", tool: str | None = None) -> dict:
    """Like `_assistant_record()`, but WITH `stop_reason` — needed here and absent there (that
    fixture predates this change and nothing in it depended on the field)."""
    content = []
    if text:
        content.append({"type": "text", "text": text})
    if tool:
        content.append({"type": "tool_use", "id": tool, "name": "Bash", "input": {"command": "echo hi"}})
    return {"type": "assistant", "message": {"content": content, "stop_reason": stop_reason}, "uuid": uuid}


class JednaZpravaNaTahTests(unittest.TestCase):

    # ---- 1: one turn, one message, in order --------------------------------------------
    def test_batch_posle_se_az_na_konci_tahu_jednou_zpravou_ve_spravnem_poradi(self):
        with tempfile.TemporaryDirectory() as td:
            b = _regrese_bridge(td)
            transcript = Path(td) / "transcript.jsonl"
            b._transcript = transcript
            b._status_push = lambda *a, **k: None

            b._handle(_msg(1, "[TG] hello"))
            _write_transcript(transcript, [
                _user_record("[TG] hello"),
                _assistant_record("first part"),
                _assistant_record("second part"),
            ])
            b._drain_transcript()
            # Nothing may leave before the turn ends — that is the whole point of batching.
            self.assertEqual(b.tg.sent, [])

            b._finish_turn()

            self.assertEqual(len(b.tg.sent), 1, f"expected exactly one message, got {b.tg.sent!r}")
            self.assertEqual(b.tg.sent[0], "first part\n\nsecond part")

    # ---- 2: past the limit, text goes at once and the rest streams --------------------
    def test_po_prekroceni_meze_odejde_text_hned_a_zbytek_tahu_proudi(self):
        with tempfile.TemporaryDirectory() as td:
            b = _regrese_bridge(td)
            transcript = Path(td) / "transcript.jsonl"
            b._transcript = transcript
            b._status_push = lambda *a, **k: None
            b.cfg.turn_batch_seconds = 0.05

            b._handle(_msg(1, "[TG] hello"))
            b._turn_begun_at -= 5  # the limit has already passed when the first text lands

            _write_transcript(transcript, [
                _user_record("[TG] hello"),
                _assistant_record("over the limit, goes now"),
            ])
            b._drain_transcript()

            self.assertEqual(
                b.tg.sent, ["over the limit, goes now"],
                "the held text must go out the instant the batch limit is passed",
            )

            # The REST of the turn now streams — no more holding for the end.
            with open(transcript, "a", encoding="utf-8") as f:
                import json
                f.write(json.dumps(_assistant_record("streamed afterwards")) + "\n")
            b._drain_transcript()

            self.assertEqual(
                b.tg.sent, ["over the limit, goes now", "streamed afterwards"],
                "once streaming, each later text must arrive as its OWN message",
            )

    # ---- 3: turn_batch_seconds = 0 -> pre-change behaviour -----------------------------
    def test_turn_batch_seconds_nula_posle_kazdy_text_zvlast_hned(self):
        with tempfile.TemporaryDirectory() as td:
            b = _regrese_bridge(td)
            transcript = Path(td) / "transcript.jsonl"
            b._transcript = transcript
            b._status_push = lambda *a, **k: None
            b.cfg.turn_batch_seconds = 0

            b._handle(_msg(1, "[TG] hello"))
            _write_transcript(transcript, [
                _user_record("[TG] hello"),
                _assistant_record("one"),
            ])
            b._drain_transcript()
            self.assertEqual(b.tg.sent, ["one"], "batching off must deliver text as soon as it lands")

            with open(transcript, "a", encoding="utf-8") as f:
                import json
                f.write(json.dumps(_assistant_record("two")) + "\n")
            b._drain_transcript()
            self.assertEqual(b.tg.sent, ["one", "two"])

    # ---- 4: restart mid-turn — ledger dedups, nothing is lost --------------------------
    def test_restart_uprostred_tahu_neposle_znovu_odeslane_a_neztrati_neodeslane(self):
        with tempfile.TemporaryDirectory() as td:
            b1 = _regrese_bridge(td)
            transcript = Path(td) / "transcript.jsonl"
            b1._transcript = transcript
            b1._status_push = lambda *a, **k: None

            b1._handle(_msg(1, "[TG] hello"))
            _write_transcript(transcript, [
                _user_record("[TG] hello"),
                _assistant_record("already delivered one"),
                _assistant_record("already delivered two"),
            ])
            b1._drain_transcript()
            b1._finish_turn()
            self.assertEqual(len(b1.tg.sent), 1)
            self.assertEqual(b1.tg.sent[0], "already delivered one\n\nalready delivered two")

            # --- restart: a fresh bridge instance, SAME disk ledger ---
            b2 = _regrese_bridge(td)
            b2._transcript = transcript
            b2._status_push = lambda *a, **k: None
            b2._sent_keys = set(b2._sent_path.read_text("utf-8").split())
            self.assertEqual(len(b2._sent_keys), 2, "the ledger must carry BOTH texts' ids, not just one")

            # A new turn resumes mid-way: the transcript is re-read from the start of the current
            # turn (as `_resume_position()` would leave it), carrying the two already-delivered
            # texts plus one the restart never got to send.
            b2._tpos = 0
            b2._turn_active.set()
            b2._turn_from_tg = True
            b2._turn_text_sent = False
            b2._turn_sent_keys = set()
            # `_batch` is deliberately NOT reset here: pre-setting it to `[]` would force the
            # exact value the real restart path must produce on its own (via `_drain_transcript`
            # re-reading from `_tpos = 0`), and so would hide a regression where that rebuild
            # goes wrong — this is precisely the state the test name claims to measure.
            b2._batch_streaming = False
            b2._turn_begun_at = time.monotonic()
            b2._tg_since = 0

            with open(transcript, "a", encoding="utf-8") as f:
                import json
                f.write(json.dumps(_assistant_record("never sent before the restart")) + "\n")
            b2._drain_transcript()
            b2._finish_turn()

            self.assertEqual(
                len(b2.tg.sent), 1,
                f"must deliver exactly the one NEW text, not re-send old ones: {b2.tg.sent!r}",
            )
            self.assertEqual(b2.tg.sent[0], "never sent before the restart")

    # ---- 5: the backstop must not double-send a batch it just flushed -----------------
    def test_backstop_se_nespusti_naprazdno_nad_prave_odeslanou_davkou(self):
        with tempfile.TemporaryDirectory() as td:
            b = _regrese_bridge(td)
            transcript = Path(td) / "transcript.jsonl"
            b._transcript = transcript
            b._status_push = lambda *a, **k: None

            b._handle(_msg(1, "[TG] hello"))
            # Only a FINAL answer — nothing forwarded yet, exactly the shape that used to make the
            # late-final backstop compare against "nothing sent" and fire a second time.
            _write_transcript(transcript, [
                _user_record("[TG] hello"),
                _assistant_record("the only answer"),
            ])
            b._drain_transcript()
            self.assertEqual(b.tg.sent, [], "still held — the turn has not ended yet")

            b._finish_turn()

            sent = "\n".join(b.tg.sent)
            self.assertEqual(
                sent.count("the only answer"), 1,
                f"the backstop fired a second time over a batch _finish_turn itself just flushed: {b.tg.sent!r}",
            )

    # ---- 6: a text alone closes the turn; a text + tool call does not -----------------
    def test_text_bez_tool_use_zavre_tah_text_s_tool_use_ne(self):
        with tempfile.TemporaryDirectory() as td:
            b = _regrese_bridge(td)
            transcript = Path(td) / "transcript.jsonl"
            b._transcript = transcript
            b._status_push = lambda *a, **k: None
            b.cfg.turn_end_quiet_seconds = 0.05

            b._handle(_msg(1, "[TG] hello"))
            b._turn_begun_at -= 5  # past SUSPICIOUS_TURN_SECONDS

            _write_transcript(transcript, [
                _user_record("[TG] hello"),
                _assistant_stop("final, nothing pending", "end_turn", uuid="a1"),
            ])
            b._drain_transcript()
            time.sleep(0.1)  # past turn_end_quiet_seconds
            self.assertTrue(
                b._transcript_turn_end_ready(),
                "an assistant record with stop_reason='end_turn' and no tool call must close the turn",
            )

        with tempfile.TemporaryDirectory() as td:
            b = _regrese_bridge(td)
            transcript = Path(td) / "transcript.jsonl"
            b._transcript = transcript
            b._status_push = lambda *a, **k: None
            b.cfg.turn_end_quiet_seconds = 0.05

            b._handle(_msg(1, "[TG] hello"))
            b._turn_begun_at -= 5

            _write_transcript(transcript, [
                _user_record("[TG] hello"),
                _assistant_stop("let me check", "tool_use", uuid="a2", tool="tool-1"),
            ])
            b._drain_transcript()
            time.sleep(0.1)
            self.assertFalse(
                b._transcript_turn_end_ready(),
                "a record calling a tool must NOT close the turn, however long the wait",
            )

    # ---- 7: too little quiet doesn't close the turn -----------------------------------
    def test_prodleva_kratsi_nez_quiet_seconds_tah_nezavre(self):
        with tempfile.TemporaryDirectory() as td:
            b = _regrese_bridge(td)
            transcript = Path(td) / "transcript.jsonl"
            b._transcript = transcript
            b._status_push = lambda *a, **k: None
            b.cfg.turn_end_quiet_seconds = 5.0

            b._handle(_msg(1, "[TG] hello"))
            b._turn_begun_at -= 5

            _write_transcript(transcript, [
                _user_record("[TG] hello"),
                _assistant_stop("final answer", "end_turn", uuid="b1"),
            ])
            b._drain_transcript()
            # No sleep: essentially 0s have passed since the record was read.
            self.assertFalse(
                b._transcript_turn_end_ready(),
                "the turn must stay open until turn_end_quiet_seconds has actually passed",
            )

    # ---- 8: a record before _tg_since must not close the turn -------------------------
    def test_zaznam_pred_tg_since_tah_nezavre(self):
        with tempfile.TemporaryDirectory() as td:
            b = _regrese_bridge(td)
            b._status_push = lambda *a, **k: None
            b.cfg.turn_end_quiet_seconds = 0.01

            b._handle(_msg(1, "[TG] hello"))
            b._turn_begun_at -= 5
            b._tg_since = 1000  # the Telegram message joined far later in the file

            # A record at offset 5 — well before the join point — says "end_turn".
            b._handle_event(readers.Ev("assistant_done", key="leftover"), offset=5)
            time.sleep(0.05)

            self.assertFalse(
                b._transcript_turn_end_ready(),
                "a leftover record from before this Telegram turn joined must never close it",
            )

    # ---- 9: with a working hook, the turn closes once, not twice ----------------------
    def test_s_funkcnim_hookem_se_tah_zavre_jednou_ne_dvakrat(self):
        with tempfile.TemporaryDirectory() as td:
            b = _regrese_bridge(td)
            transcript = Path(td) / "transcript.jsonl"
            b._transcript = transcript
            b._status_push = lambda *a, **k: None
            b.cfg.turn_end_quiet_seconds = 0.01

            b._handle(_msg(1, "[TG] hello"))
            b._turn_begun_at -= 5

            _write_transcript(transcript, [
                _user_record("[TG] hello"),
                _assistant_stop("final via hook", "end_turn", uuid="c1"),
            ])
            # Hook path: drain, then close — exactly what `_end_turn()` does.
            b._end_turn()

            self.assertEqual(len(b.tg.sent), 1, f"expected one delivery, got {b.tg.sent!r}")
            self.assertFalse(b._turn_active.is_set())

            # The transcript STILL says "end_turn" (same last record) — the second, hook-
            # independent path must not be free to close an already-closed turn a second time.
            time.sleep(0.05)
            self.assertFalse(
                b._transcript_turn_end_ready(),
                "a turn already ended must not be closable a second time via the transcript path",
            )
            self.assertEqual(len(b.tg.sent), 1, "no duplicate delivery after the turn had ended")

    # ---- 10: IDLE_DONE remains reachable -----------------------------------------------
    def test_idle_done_zustava_poslednim_zpusobem_konce_tahu(self):
        with tempfile.TemporaryDirectory() as td:
            b = _regrese_bridge(td)
            transcript = Path(td) / "transcript.jsonl"
            b._transcript = transcript
            b._status_push = lambda *a, **k: None
            # No Stop-hook marker (_turn_end stays None from _bridge()) and no stop_reason at all
            # in the transcript, so neither the hook nor the transcript path can close the turn —
            # only silence (IDLE_DONE) is left.
            b._handle(_msg(1, "[TG] hello"))
            _write_transcript(transcript, [
                _user_record("[TG] hello"),
                _assistant_record("held, waiting for the end of the turn"),
            ])
            b._drain_transcript()
            self.assertEqual(b.tg.sent, [], "held by batching — nothing sent yet")
            self.assertIsNone(b._turn_end)
            self.assertFalse(b._transcript_turn_end_ready())

            original_idle_done = attach_mod.IDLE_DONE
            attach_mod.IDLE_DONE = 0.05
            try:
                b._last_activity = time.monotonic() - 10
                b._last_resolve = time.monotonic()  # skip re-resolution — not under test here
                b._last_inbound_retry = time.monotonic()
                b._beat = lambda: b._stop.set()  # run exactly one iteration of the real loop
                b._outbound_loop()
            finally:
                attach_mod.IDLE_DONE = original_idle_done

            self.assertEqual(
                b.tg.sent, ["held, waiting for the end of the turn"],
                "the idle fallback must still be able to close a silent turn and flush its batch",
            )
            self.assertFalse(b._turn_active.is_set())


if __name__ == "__main__":
    unittest.main()
