"""T-1043: the merge of fix/idle-done-dlouhe-tahy (queued_command origin, `_tg_since` boundary,
`_events()`) into fork/main (`_begin_turn` origin at injection, definitive-only origin reset,
durable outbox, dedup ledger). Only COMBINATIONS of the two lines are measured here — each line on
its own is pinned in tests/test_queued_message.py, tests/test_regrese_mostu.py,
tests/test_definitive_konec_tahu.py and tests/test_ledger_potvrzeny_na_disku.py.

Same harness as tests/test_regrese_mostu.py (a real `AttachBridge`, the real `ClaudeCodeReader`,
fixture records copied from a real transcript), asserting on what actually reaches Telegram.
"""
import json
import tempfile
import unittest
from pathlib import Path

from tests.test_regrese_mostu import (
    _assistant_record,
    _attachment_record,
    _regrese_bridge,
    _user_record,
    _write_transcript,
)
from tests.test_v2_durability import _msg


def _append(path: Path, records: list) -> None:
    with open(path, "a", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _bridge_over(td: str, transcript: Path, *, durable: bool):
    b = _regrese_bridge(td)
    b._transcript = transcript
    b._use_durable_outbox = durable
    b._wait_backstop_retry = lambda: None     # the turn-end checks' sleeps, not under test
    return b


def _local_turn_running_then_telegram_joins(b, transcript: Path) -> None:
    """A terminal turn is running and has already written something local; the bridge has read
    it (nothing forwarded). Then a Telegram message is injected into that running turn through
    the real inbound path (`_handle` -> `_begin_turn`)."""
    _write_transcript(transcript, [
        _user_record("pokracuj v uklidu"),
        _assistant_record("Nasel jsem heslo v .env: hunter2"),
    ])
    b._turn_active.set()
    b._turn_from_tg = False
    b._drain_transcript()
    b._handle(_msg(1, "[TG] are you there"))


def _restarted(td: str, transcript: Path, *, durable: bool):
    """A fresh bridge process over the same state dir: ledger read from disk, cursor at EOF, then
    the startup rewind — exactly what `_run_locked` does when the ledger is not empty."""
    b = _bridge_over(td, transcript, durable=durable)
    b._sent_keys = b._load_sent_ledger()
    b._tpos = transcript.stat().st_size
    b._resume_position()
    return b


class QueuedJoinLifecycleTests(unittest.TestCase):
    """(a) A Telegram message joining a running terminal turn gets its answer, the origin
    survives an idle fallback, dies with the definitive end, and the next local turn stays local."""

    def test_chat_gets_only_the_telegram_part_across_idle_fallback_and_definitive_end(self):
        with tempfile.TemporaryDirectory() as td:
            transcript = Path(td) / "transcript.jsonl"
            b = _bridge_over(td, transcript, durable=False)
            _local_turn_running_then_telegram_joins(b, transcript)

            _append(transcript, [
                _attachment_record("[TG] are you there"),
                _assistant_record("Jo, jedu dal, build bezi."),
            ])
            b._drain_transcript()
            b._end_turn(definitive=False)          # 90 s of silence during the build

            _append(transcript, [_assistant_record("Build prosel.")])
            b._drain_transcript()
            b._end_turn()                          # Stop hook: the real end

            # Next turn is local only — typed into the tail of the finishing turn, so it exists
            # in the transcript ONLY as a queued_command without [TG].
            _append(transcript, [
                _attachment_record("a ted smaz tmp", uuid="99999999-9999-9999-9999-999999999999"),
                _assistant_record("Smazano, tajny vystup terminalu."),
            ])
            b._drain_transcript()
            b._end_turn()

            self.assertEqual(b.tg.sent, ["Jo, jedu dal, build bezi.", "Build prosel."])


class RestartAfterQueuedJoinTests(unittest.TestCase):
    """(b) A restart in the middle of a turn a Telegram message joined: the origin and the
    boundary come back from the transcript, and nothing goes out twice."""

    def test_batch_waiting_unconfirmed_in_the_durable_queue_is_delivered_exactly_once(self):
        """The turn's two texts were batched into ONE durable record (joined key) and the bridge
        died before Telegram confirmed it. After the restart the drain re-reads both texts one by
        one (no turn is open, so no batching) under their single keys; the queue must recognise
        them, and the secret written before the Telegram message must not come back either."""
        with tempfile.TemporaryDirectory() as td:
            transcript = Path(td) / "transcript.jsonl"
            b1 = _bridge_over(td, transcript, durable=True)
            _local_turn_running_then_telegram_joins(b1, transcript)
            _append(transcript, [
                _attachment_record("[TG] are you there"),
                _assistant_record("Prvni cast odpovedi."),
                _assistant_record("Druha cast odpovedi."),
            ])
            b1._drain_transcript()
            b1._finish_turn()                       # batch -> durable record, never flushed
            self.assertEqual(b1.tg.sent, [])        # crash before the outbound loop delivered

            b2 = _restarted(td, transcript, durable=True)
            b2._drain_transcript()
            b2._flush_pending()

            self.assertEqual(b2.tg.sent, ["Prvni cast odpovedi.\n\nDruha cast odpovedi."])

    def test_confirmed_reply_is_not_resent_and_reply_written_while_down_is(self):
        """The first reply after the queued join was delivered and is in the ledger on disk; the
        bridge then died mid-turn and the agent wrote a second reply while it was down."""
        with tempfile.TemporaryDirectory() as td:
            transcript = Path(td) / "transcript.jsonl"
            b1 = _bridge_over(td, transcript, durable=False)
            b1.cfg.turn_batch_seconds = 0           # stream: reply 1 goes out before the crash
            _local_turn_running_then_telegram_joins(b1, transcript)
            _append(transcript, [
                _attachment_record("[TG] are you there"),
                _assistant_record("Odpoved pred padem."),
            ])
            b1._drain_transcript()
            self.assertEqual(b1.tg.sent, ["Odpoved pred padem."])

            _append(transcript, [_assistant_record("Odpoved napsana behem vypadku.")])
            b2 = _restarted(td, transcript, durable=False)
            b2._drain_transcript()

            self.assertEqual(b2.tg.sent, ["Odpoved napsana behem vypadku."])


class MalformedRecordInDrainChunkTests(unittest.TestCase):
    """(c) `_events()` contains a record the READER trips on. A record `json.loads` itself
    rejects with something other than `JSONDecodeError` must not take the rest of the chunk
    down either — `_tpos` has already moved past the whole chunk when the loop runs."""

    def test_queued_telegram_message_after_a_line_json_rejects_with_valueerror_still_answered(self):
        with tempfile.TemporaryDirectory() as td:
            transcript = Path(td) / "transcript.jsonl"
            b = _bridge_over(td, transcript, durable=False)
            b.cfg.turn_batch_seconds = 0
            _write_transcript(transcript, [_user_record("pokracuj v uklidu")])
            b._drain_transcript()
            with open(transcript, "a", encoding="utf-8") as f:
                # Valid JSON syntax, but Python refuses ints over 4300 digits with a plain
                # ValueError (not JSONDecodeError) — one garbled record.
                f.write('{"type": "assistant", "n": ' + "1" * 5000 + "}\n")
            _append(transcript, [
                _attachment_record("[TG] are you there"),
                _assistant_record("odpoved"),
            ])

            try:
                b._drain_transcript()
            except Exception:
                pass                                # the outbound loop logs and carries on
            b._drain_transcript()                   # the next tick

            self.assertEqual(b.tg.sent, ["odpoved"])


if __name__ == "__main__":
    unittest.main()
