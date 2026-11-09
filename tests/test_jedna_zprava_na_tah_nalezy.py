"""T-0461 / claudi revize, nalez 10: pet scenaru, ktere smoke3.py v `scratchpad` dolozil rucne a
ktere chybely v sade. Prevedeno do `unittest`, pouzivaji stejne stavebnice jako
`tests/test_jedna_zprava_na_tah.py` a `tests/test_regrese_mostu.py`.
"""
import tempfile
import time
import unittest
from pathlib import Path

from tests.test_regrese_mostu import _regrese_bridge, _write_transcript, _user_record
from tests.test_unik_do_chatu import _append_transcript
from tests.test_v2_durability import _msg


def _arec(text, stop="tool_use", uuid=None, sidechain=False):
    rec = {
        "type": "assistant",
        "isSidechain": sidechain,
        "message": {"content": [{"type": "text", "text": text}], "stop_reason": stop},
    }
    if uuid:
        rec["uuid"] = uuid
    return rec


def _toolrec(tid, name="Bash", sidechain=False):
    return {
        "type": "assistant",
        "uuid": "t-" + tid,
        "isSidechain": sidechain,
        "message": {
            "stop_reason": "tool_use",
            "content": [{"type": "tool_use", "id": tid, "name": name, "input": {"description": "work"}}],
        },
    }


def _resultrec(tid):
    return {
        "type": "user",
        "toolUseResult": {"type": "text"},
        "message": {"content": [{"type": "tool_result", "tool_use_id": tid, "content": "done"}]},
    }


_HOUSEKEEPING = {
    "type": "attachment",
    "attachment": {"type": "total_tokens_reminder", "text": "<tokens/>"},
}


class _Dead:
    """A Telegram client whose network is down — used to drive a record into the durable
    outbox without actually needing a real send to fail."""

    def __init__(self):
        self.sent = []

    def send_message(self, *a, **k):
        raise OSError("network down")

    def send_chat_action(self, *a, **k):
        pass


class DruhaZpravaUprostredTahuTest(unittest.TestCase):
    """Nalez 1: druha [TG] zprava uprostred tahu nesmi zahodit rozdelany text tahu."""

    def test_druha_zprava_uprostred_tahu_davku_nezahodi(self):
        with tempfile.TemporaryDirectory() as td:
            b = _regrese_bridge(td)
            b._status_push = lambda *a, **k: None
            transcript = Path(td) / "t.jsonl"
            b._transcript = transcript

            b._handle(_msg(1, "[TG] udelej X"))
            _write_transcript(transcript, [
                _user_record("[TG] udelej X"),
                _arec("[tg] zacinam na X", uuid="u1"),
                _toolrec("c1"),
            ])
            b._drain_transcript()
            self.assertEqual(b.tg.sent, [], "jeste nic nemelo odejit — tah nekonci")

            b._handle(_msg(2, "[TG] a jeste Y"))

            self.assertIn(
                "zacinam na X", "\n".join(b.tg.sent),
                "text rozdelaneho tahu se ztratil pri prichodu druhe zpravy",
            )


class PodagentNezavreHlavniTahTest(unittest.TestCase):
    """Nalez 2 (+ 2b): Task tool_use a sidechain end_turn nesmi zavrit hlavni tah; uklidovy
    attachment zaznam za koncem tahu signal konce take nerusi.

    Druha cast nalezu 2 — vysledek nastroje prichazejici PO end_turn hlavniho agenta — je ve
    vlastnim testu nize (`test_vysledek_nastroje_po_end_turn_zrusi_signal_konce`): v teto tride
    zde je v okamziku `_resultrec` jiz `_turn_done_at` None (sidechain zaznam zadny
    `assistant_done` nevyrobi), takze assert na `_resultrec` v tomhle tvaru nic nemeril — viz
    revize."""

    def test_sidechain_end_turn_nezavre_hlavni_tah(self):
        with tempfile.TemporaryDirectory() as td:
            b = _regrese_bridge(td)
            b._status_push = lambda *a, **k: None
            transcript = Path(td) / "t.jsonl"
            b._transcript = transcript
            b.cfg.turn_end_quiet_seconds = 0.2

            b._handle(_msg(1, "[TG] zjisti neco"))
            b._turn_begun_at -= 5

            _write_transcript(transcript, [
                _user_record("[TG] zjisti neco"),
                _arec("[tg] deleguji", stop=None, uuid="u1"),
                _toolrec("task1", name="Task"),
                {"type": "user", "isSidechain": True,
                 "message": {"role": "user", "content": "You are a subagent..."}},
                _arec("subagent: hotovo", stop="end_turn", uuid="u2", sidechain=True),
            ])
            b._drain_transcript()
            time.sleep(0.25)
            self.assertFalse(
                b._transcript_turn_end_ready(),
                "sidechain end_turn (vysledek podagenta) zavrel hlavni tah",
            )

            _append_transcript(transcript, [_arec("[tg] skutecna odpoved", stop="end_turn", uuid="u3")])
            b._drain_transcript()
            time.sleep(0.25)
            self.assertTrue(
                b._transcript_turn_end_ready(),
                "skutecna odpoved hlavniho agenta tah zavrit musi",
            )
            b._end_turn()
            self.assertIn("skutecna odpoved", "\n".join(b.tg.sent))

    def test_vysledek_nastroje_po_end_turn_zrusi_signal_konce(self):
        """Blokujici nalez z revize: `continues_turn` vykuchane na `return False` nechalo celou
        sadu zelenou, protoze v puvodnim tvaru byl `_turn_done_at` uz None (sidechain zadny
        `assistant_done` neprodukuje) — ready byla False nezavisle na `continues_turn`. Tenhle
        tvar overil reviewer mutaci: na cistem kode True -> False, s vykuchanym `continues_turn`
        True -> True (overeno mutaci `continues_turn` na `return False`, viz popis PR)."""
        with tempfile.TemporaryDirectory() as td:
            b = _regrese_bridge(td)
            b._status_push = lambda *a, **k: None
            transcript = Path(td) / "t.jsonl"
            b._transcript = transcript
            b.cfg.turn_end_quiet_seconds = 0.05

            b._handle(_msg(1, "[TG] zjisti neco"))
            b._turn_begun_at -= 5

            _write_transcript(transcript, [
                _user_record("[TG] zjisti neco"),
                _toolrec("task1", name="Task"),
                _arec("[tg] hotovo", stop="end_turn", uuid="u1"),
            ])
            b._drain_transcript()
            time.sleep(0.1)
            self.assertTrue(
                b._transcript_turn_end_ready(),
                "end_turn hlavniho agenta bez pokracovani tah zavrit musi",
            )

            _append_transcript(transcript, [_resultrec("task1")])
            b._drain_transcript()
            time.sleep(0.1)
            self.assertFalse(
                b._transcript_turn_end_ready(),
                "vysledek nastroje prichazejici PO end_turn musi signal konce zrusit — "
                "nastroj byl v okamziku end_turn jeste nedoreseny, agent pracuje dal",
            )

    def test_uklidovy_attachment_za_koncem_tahu_signal_konce_nerusi(self):
        with tempfile.TemporaryDirectory() as td:
            b = _regrese_bridge(td)
            b._status_push = lambda *a, **k: None
            transcript = Path(td) / "t.jsonl"
            b._transcript = transcript
            b.cfg.turn_end_quiet_seconds = 0.2

            b._handle(_msg(1, "[TG] ahoj"))
            b._turn_begun_at -= 5

            _write_transcript(transcript, [
                _user_record("[TG] ahoj"),
                _arec("[tg] hotovo", stop="end_turn", uuid="u1"),
                _HOUSEKEEPING,
            ])
            b._drain_transcript()
            time.sleep(0.25)
            self.assertTrue(
                b._transcript_turn_end_ready(),
                "uklidovy zaznam (total_tokens_reminder) za koncem tahu zrusil signal konce",
            )


class TextBehemKonceTahuTest(unittest.TestCase):
    """Nalez 3: text, ktery dorazi do prepisu BEHEM konce tahu (ne pred nim), se neztrati ani
    neodejde dvakrat.

    Technika je stejna jako v `tests/test_attach_backstop.py:432` — `_wait_backstop_retry` se
    prepise na funkci, ktera text dopise do prepisu AZ za behu cekani uvnitr `_finish_turn`
    (`_unsent_final_text`). Dopsani B a C PRED volanim `_finish_turn()` (jak to delala puvodni
    verze tohoto testu) by je pobral uz prvni drain uvnitr, a retry smycka z T-0404 by se vubec
    nespustila — test by prosel i kdyby prestala fungovat."""

    def test_text_dorazivsi_behem_konce_tahu_se_neztrati_ani_neodesle_dvakrat(self):
        with tempfile.TemporaryDirectory() as td:
            b = _regrese_bridge(td)
            b._status_push = lambda *a, **k: None
            transcript = Path(td) / "t.jsonl"
            b._transcript = transcript

            b._handle(_msg(1, "[TG] ahoj"))
            _write_transcript(transcript, [
                _user_record("[TG] ahoj"),
                _arec("[tg] A prubezne", uuid="uA"),
            ])
            b._drain_transcript()
            self.assertEqual(b.tg.sent, [], "A je jen drzena davkou, jeste nesmi odejit")

            def _dopis_b_a_c_behem_cekani():
                _append_transcript(transcript, [
                    _arec("[tg] B prubezne", uuid="uB"),
                    _arec("[tg] C finalni", stop="end_turn", uuid="uC"),
                ])

            b._wait_backstop_retry = _dopis_b_a_c_behem_cekani

            b._finish_turn()

            joined = "\n".join(b.tg.sent)
            for part in ("A prubezne", "B prubezne", "C finalni"):
                self.assertIn(part, joined, f"{part} se ztratilo")
                self.assertEqual(joined.count(part), 1, f"{part} odeslano dvakrat")
            self.assertFalse(b._batch, "davka po konci tahu nebyla vyprazdnena")


class RestartNadVisiciDavkouTest(unittest.TestCase):
    """Nalez 4: restart nad davkou visici v durable outboxu nepošle podruhe a neztrati."""

    def test_restart_nad_davkou_visici_ve_fronte_neposle_podruhe_a_neztrati(self):
        with tempfile.TemporaryDirectory() as td:
            b = _regrese_bridge(td)
            b._status_push = lambda *a, **k: None
            transcript = Path(td) / "t.jsonl"
            b._transcript = transcript
            b._use_durable_outbox = True
            b._queue_path = Path(td) / "q.jsonl"
            b._sent_path = Path(td) / "sent.txt"
            b._sent_keys = set()

            b._handle(_msg(1, "[TG] ahoj"))
            _write_transcript(transcript, [
                _user_record("[TG] ahoj"),
                _arec("[tg] A", uuid="uA"),
                _arec("[tg] B", uuid="uB"),
            ])
            b._drain_transcript()
            b._finish_turn()

            b.tg = _Dead()
            b._flush_pending()  # sit je dole -> zaznam zustava ve fronte
            pending = b._ensure_outbox().pending()
            self.assertEqual(len(pending), 1, "po vypadku site musi zbyt presne jeden zaznam ve fronte")

            # restart: novy most nad TYMZ stavem, prepis se cte znovu od zacatku tahu
            b2 = _regrese_bridge(td)
            b2._status_push = lambda *a, **k: None
            b2._transcript = transcript
            b2._use_durable_outbox = True
            b2._queue_path = Path(td) / "q.jsonl"
            b2._sent_path = Path(td) / "sent.txt"
            b2._sent_keys = set()
            b2._tpos = 0
            b2._drain_transcript()
            b2._finish_turn()

            pending2 = b2._ensure_outbox().pending()
            self.assertEqual(len(pending2), 1, "restart vyrobil duplikat ve fronte")

            # `_finish_turn()` on a durable-outbox bridge only ENQUEUES — it must not have pushed
            # anything straight to Telegram on its own. Asserting this directly (instead of
            # clearing `b2.tg.sent` right before the real check) keeps the evidence a duplicate
            # send here would leave.
            self.assertEqual(
                b2.tg.sent, [],
                "_finish_turn by nemel nic poslat primo — jen zapsat do fronty; "
                f"primy odesl pred obnovou site by byl duplikat: {b2.tg.sent!r}",
            )
            b2._flush_pending()
            self.assertEqual(
                b2.tg.sent, ["A\n\nB"],
                "po obnove site musi dojit presne jedna zprava se spravnym obsahem, ne duplikat ani ztrata",
            )


class MezDlouhehoTahuBezDalsihoTextuTest(unittest.TestCase):
    """Nalez 5: mez dlouheho tahu (turn_batch_seconds) se uplatni i bez dalsiho textu — jen
    behem behu smycky (_flush_overdue_batch)."""

    def test_mez_se_uplatni_i_bez_dalsiho_textu_jen_behem_smycky(self):
        # `_flush_overdue_batch` compares `time.monotonic() - _turn_begun_at` against the limit
        # (see `agent2telegram/attach.py`). Moving `_turn_begun_at` itself, instead of sleeping on
        # the wall clock, makes both sides of this test deterministic — a loaded machine cannot
        # turn "mez jeste neuplynula" into a false positive by being slow for 200ms.
        with tempfile.TemporaryDirectory() as td:
            b = _regrese_bridge(td)
            b._status_push = lambda *a, **k: None
            transcript = Path(td) / "t.jsonl"
            b._transcript = transcript
            b.cfg.turn_batch_seconds = 2.0

            b._handle(_msg(1, "[TG] ahoj"))
            _write_transcript(transcript, [
                _user_record("[TG] ahoj"),
                _arec("[tg] jedna veta", uuid="u1"),
            ])
            b._drain_transcript()
            self.assertEqual(b.tg.sent, [])

            b._flush_overdue_batch()
            self.assertEqual(b.tg.sent, [], "mez jeste neuplynula — nesmi odejit")

            b._turn_begun_at -= 5  # mez (2.0 s) uz je dávno za námi, bez spoléhání na real sleep
            b._flush_overdue_batch()  # zadny novy text, jen beh smycky
            self.assertEqual(
                b.tg.sent, ["jedna veta"],
                "po uplynuti meze musi smycka davku vyklopit, i kdyz nedorazil zadny novy text",
            )


if __name__ == "__main__":
    unittest.main()
