"""T-0461 / claudi revize, nalez 10: pet scenaru, ktere smoke3.py v `scratchpad` dolozil rucne a
ktere chybely v sade. Prevedeno do `unittest`, pouzivaji stejne stavebnice jako
`tests/test_jedna_zprava_na_tah.py` a `tests/test_regrese_mostu.py`.
"""
import tempfile
import threading
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


def _filetoolrec(tid, paths):
    """A `SendUserFile` tool_use — the harness runs it itself, so the reader only ever sees this
    record and its result (see `agent2telegram.readers._file_tool_paths`)."""
    return {
        "type": "assistant",
        "uuid": "t-" + tid,
        "message": {
            "stop_reason": "tool_use",
            "content": [{"type": "tool_use", "id": tid, "name": "SendUserFile", "input": {"files": paths}}],
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
    """Nalez 1: druha [TG] zprava uprostred tahu nesmi zahodit rozdelany text tahu.

    Puvodni tvar testu overoval jen to, ze se text objevil NEKDE v `tg.sent` — ne pocet vyskytu,
    ne stav davky po jejim vyklopeni, ne co se stane po SKUTECNEM konci tahu. Scenar, ktery by tim
    prolezl: prichod druhe zpravy text odesle, ale chybny reset ho necha i v `_batch` — po konci
    tahu by ho `_finish_turn` vyklopil znovu a uzivatel by ho dostal podruhe. Test ted jde az za
    konec tahu a pocita presne jeden vyskyt."""

    def test_druha_zprava_uprostred_tahu_davku_nezahodi_a_nezdvoji(self):
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

            self.assertEqual(
                b.tg.sent, ["zacinam na X"],
                "rozdelany text tahu se po prichodu druhe zpravy musi odeslat PRESNE jednou, "
                f"hned: {b.tg.sent!r}",
            )
            self.assertEqual(
                list(getattr(b, "_batch", ()) or ()), [],
                "po vyklopeni davky druhou zpravou nesmi `_batch` text drzet dal — "
                "jinak ho konec tahu posle znovu",
            )

            _append_transcript(transcript, [_arec("[tg] dokoncuji", stop="end_turn", uuid="u2")])
            b._drain_transcript()
            b._finish_turn()

            joined = "\n".join(b.tg.sent)
            self.assertEqual(
                joined.count("zacinam na X"), 1,
                f"text uz jednou odeslany druhou zpravou se po konci tahu posel znovu: {b.tg.sent!r}",
            )
            self.assertIn("dokoncuji", joined, "zbytek tahu po druhe zprave se ztratil")


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
        """Co se tu meri: OTEVRENEHO volani (`task1` ma v `_open_calls` zaznamenany `tool_use`,
        zadny vysledek jeste neprisel) ten FIRST `tool_result` po end_turn signal konce zrusit
        MUSI — jinak se tah zavre, dokud agent na task1 jeste ceka, a skutecna odpoved se nikdy
        neprepose.

        Reader (`agent2telegram/readers.py`, `ClaudeCodeReader.continues_turn`) rozhoduje podle
        tri stavu: OTEVRENE (vyhrava, cancel), NEZNAME (cancel — most mohl pripojit uprostred
        tahu), VSECHNA UZAVRENA (nerusi). Task se tu nejdriv OTEVRE (`_toolrec`) a pak dostane
        svuj FIRST vysledek — presne ten stav, ktery musi cancelovat. DRUHY (duplicitni) vysledek
        TEHOZ volani uz nerusi (`test_druhy_vysledek_jiz_vyrizeneho_volani_signal_konce_nerusi`,
        nize), a REPLAY stejneho `tool_use`+`tool_result` po rewindu `_tpos` se chova jako prvni
        pruchod (`test_replay_po_rewindu_tpos_se_nezavre_uprostred_prace`, nize — to je ten
        blokujici nalez)."""
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
                "end_turn hlavniho agenta bez pokracovani tah zavrit musi, i kdyz task1 "
                "jeste nema vysledek",
            )

            _append_transcript(transcript, [_resultrec("task1")])
            b._drain_transcript()
            time.sleep(0.1)
            self.assertFalse(
                b._transcript_turn_end_ready(),
                "prvni (zatim NEvyrizeny) vysledek task1 musi signal konce zrusit — agent na "
                "nej jeste cekal, kdyz end_turn padl",
            )

    def test_druhy_vysledek_jiz_vyrizeneho_volani_signal_konce_nerusi(self):
        """`ClaudeCodeReader` (`agent2telegram/readers.py`) si vede dve mnoziny — `_open_calls`
        (`tool_use` videno, vysledek jeste ne) a `_closed_calls` (vysledek uz viden). Oprava
        `8e1a7c0` zavedla parovani, `11bd6a9` ho pak opravila na tri stavy (otevrene/nezname
        vyhrava nad uzavrenym — viz `test_replay_po_rewindu_tpos_se_nezavre_uprostred_prace`),
        ale tenhle smer (DRUHY vysledek jiz uzavreneho volani nerusi) zustal stejny: opozdeny
        nebo znovu zalozeny DRUHY `tool_result` TEHOZ `tool_use_id` uz nic nedokazuje (agent uz na
        nej odpoved mel, kdyz rekl `end_turn`), takze signal konce nerusi — a tah se zavre na
        skutecne finalni odpovedi. Id task1 musi byt v transkripci OTEVRENO `tool_use` zaznamem,
        jinak by bylo NEZNAME, a neznamy id rusi dal (`test_vysledek_s_neznamym_id_signal_konce_rusi`)
        — ten test by pak mel opacny vysledek."""
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
                _resultrec("task1"),  # FIRST result — task1 je uz vyrizeny, nez tah vubec skonci
            ])
            b._drain_transcript()

            _append_transcript(transcript, [_arec("[tg] hotovo", stop="end_turn", uuid="u1")])
            b._drain_transcript()
            time.sleep(0.1)
            self.assertTrue(
                b._transcript_turn_end_ready(),
                "test setup is wrong: end_turn po jiz vyrizenem task1 musi signal vztycit",
            )

            # Opozdeny DRUHY vysledek stejneho volani (duplikat/znovu zalozeny zaznam).
            _append_transcript(transcript, [_resultrec("task1")])
            b._drain_transcript()
            time.sleep(0.1)
            self.assertTrue(
                b._transcript_turn_end_ready(),
                "druhy vysledek JIZ VYRIZENEHO volani zrusil platny signal konce — tah by visel "
                "az do idle fallbacku a pozdejsi zprava by neprisla vcas",
            )
            b._end_turn()
            self.assertIn("hotovo", "\n".join(b.tg.sent))

    def test_replay_po_rewindu_tpos_se_nezavre_uprostred_prace(self):
        """⭐ BLOKUJICI nalez opraveny v `11bd6a9`: `_closed_calls`/`_answered_calls`-only pareni
        (`8e1a7c0`) PREZIVALO rewind `_tpos` na 0 (zkraceny prepis, prepnuti souboru pres
        `_maybe_reresolve`), takze pri REPLAYi stejneho `tool_use`+`tool_result` byl kazdy
        vysledek viden jako duplikat, signal se nezrusil a tah se zavrel, dokud agent jeste
        pracoval — presne to selhani, kvuli kteremu cela tahle cesta vznikla.

        Test vede most SKUTECNOU cestou rewindu, ne dvojim volanim readeru, ani rucnim predsazenim
        `_tpos`/`_tg_since` — ten rucni reset by zakryl prave tu detekci, kterou chceme merit
        (revize). Po prvnim pruchodu (end_turn → cancel vysledkem task1, presne jako
        `test_vysledek_nastroje_po_end_turn_zrusi_signal_konce`) se `before_result` zapise ZNOVU
        se stejnym obsahem — soubor se tim fyzicky ZKRATI (ztraci append-nuty `tool_result`), a
        `_drain_transcript`'s vlastni detekce zkraceni (`size < self._tpos`) vynuluje `_tpos`
        SAMA, bez naseho zasahu. To je replay. Druhe misto rewindu —`_maybe_reresolve` (prepnuti
        souboru) — je zvlast nize
        (`test_replay_pri_prepnuti_souboru_pres_maybe_reresolve_se_nezavre_uprostred_prace`).

        Tah nesmi v zadnem okamziku REPLAYe zustat `ready=True`, dokud `task1` opet neni
        vyrizen; teprve genuinne NOVA finalni odpoved tah zavre."""
        with tempfile.TemporaryDirectory() as td:
            b = _regrese_bridge(td)
            b._status_push = lambda *a, **k: None
            transcript = Path(td) / "t.jsonl"
            b._transcript = transcript
            b.cfg.turn_end_quiet_seconds = 0.05

            b._handle(_msg(1, "[TG] zjisti neco"))
            b._turn_begun_at -= 5

            before_result = [
                _user_record("[TG] zjisti neco"),
                _toolrec("task1", name="Task"),
                _arec("[tg] hotovo", stop="end_turn", uuid="u1"),
            ]

            # --- prvni pruchod ---
            _write_transcript(transcript, before_result)
            b._drain_transcript()
            time.sleep(0.1)
            self.assertTrue(
                b._transcript_turn_end_ready(),
                "test setup is wrong: end_turn s task1 jeste otevrenym musi signal vztycit",
            )

            _append_transcript(transcript, [_resultrec("task1")])
            b._drain_transcript()
            time.sleep(0.1)
            self.assertFalse(
                b._transcript_turn_end_ready(),
                "test setup is wrong: prvni vysledek task1 musi signal zrusit",
            )
            tpos_pred_rewindem = b._tpos

            # --- REWIND vyvolany SAMOTNOU detekci, ne predsazeny: stejny obsah znovu, soubor je
            # tim KRATSI nez aktualni `_tpos` (prisel o appendnuty `tool_result`), takze
            # `_drain_transcript` rewind udela sam, pri prvnim dalsim volani.
            _write_transcript(transcript, before_result)
            b._drain_transcript()
            self.assertLess(
                b._tpos, tpos_pred_rewindem,
                "test setup is wrong: zkraceny soubor musel vynutit vlastni detekci rewindu "
                "(size < _tpos), jinak tenhle test meri neco jineho",
            )
            time.sleep(0.1)
            self.assertTrue(
                b._transcript_turn_end_ready(),
                "test setup is wrong: replay end_turnu musi signal vztycit znovu",
            )

            _append_transcript(transcript, [_resultrec("task1")])
            b._drain_transcript()
            time.sleep(0.1)
            self.assertFalse(
                b._transcript_turn_end_ready(),
                "BLOKUJICI: replay vysledku task1 signal konce nezrusil — tah by se zavrel "
                "uprostred prace, presne jako pred opravou 11bd6a9",
            )

            # Genuinne NOVA finalni odpoved musi tah zavrit normalne.
            _append_transcript(transcript, [_arec("[tg] skutecna odpoved", stop="end_turn", uuid="u9")])
            b._drain_transcript()
            time.sleep(0.1)
            self.assertTrue(
                b._transcript_turn_end_ready(),
                "skutecna odpoved po replayi tah zavrit musi",
            )
            b._end_turn()
            self.assertIn("skutecna odpoved", "\n".join(b.tg.sent))

    def test_replay_senduserfile_se_nezavre_uprostred_prace(self):
        """Nalez z revize (`b823791`): `parse` ted registruje KAZDE volani do `_open_calls` PRED
        vetvenim na attachment (`if paths: ... continue`), ne jen to, co se zmeni v bublinu
        nastroje. Pred opravou by `SendUserFile` registrovalo id jen svym VYSLEDKEM
        (`continues_turn`), takze na replay by se chovalo jinak nez `Task` — zmereno: `Task`
        True/True (spravne), `SendUserFile` True/False (spatne, tah by se zavrel pri replayi,
        presne ten blokujici nalez, jen dverma jineho nastroje). Test je tvarem shodny s
        `test_replay_po_rewindu_tpos_se_nezavre_uprostred_prace`, jen s `SendUserFile` tool_use
        misto `Task`."""
        with tempfile.TemporaryDirectory() as td:
            b = _regrese_bridge(td)
            b._status_push = lambda *a, **k: None
            transcript = Path(td) / "t.jsonl"
            b._transcript = transcript
            b.cfg.turn_end_quiet_seconds = 0.05

            b._handle(_msg(1, "[TG] poslat soubor"))
            b._turn_begun_at -= 5

            before_result = [
                _user_record("[TG] poslat soubor"),
                _filetoolrec("file1", ["/tmp/some.txt"]),
                _arec("[tg] hotovo", stop="end_turn", uuid="u1"),
            ]

            # --- prvni pruchod ---
            _write_transcript(transcript, before_result)
            b._drain_transcript()
            time.sleep(0.1)
            self.assertTrue(
                b._transcript_turn_end_ready(),
                "test setup is wrong: end_turn s file1 jeste otevrenym musi signal vztycit",
            )

            _append_transcript(transcript, [_resultrec("file1")])
            b._drain_transcript()
            time.sleep(0.1)
            self.assertFalse(
                b._transcript_turn_end_ready(),
                "test setup is wrong: prvni vysledek file1 musi signal zrusit",
            )
            tpos_pred_rewindem = b._tpos

            # --- REWIND vyvolany samotnou detekci zkraceni, stejne jako u Tasku vyse.
            _write_transcript(transcript, before_result)
            b._drain_transcript()
            self.assertLess(b._tpos, tpos_pred_rewindem, "test setup is wrong: rewind se nespustil")
            time.sleep(0.1)
            self.assertTrue(
                b._transcript_turn_end_ready(),
                "test setup is wrong: replay end_turnu musi signal vztycit znovu",
            )

            _append_transcript(transcript, [_resultrec("file1")])
            b._drain_transcript()
            time.sleep(0.1)
            self.assertFalse(
                b._transcript_turn_end_ready(),
                "replay vysledku SendUserFile volani (file1) signal konce nezrusil — tah by se "
                "zavrel uprostred prace, presne jako Task pred opravou b823791",
            )

    def test_replay_pri_prepnuti_souboru_pres_maybe_reresolve_se_nezavre_uprostred_prace(self):
        """Druhe misto rewindu: `_maybe_reresolve` prepne `self._transcript` na novy soubor a
        vynuluje `_tpos`/`_tg_since` primo (ne detekci velikosti) tesne pred volanim
        `_resume_position()`. Samoopravnost replaye tam zavisi na tom, ze `_resume_position`
        pocita `size = self._tpos` — v okamziku jejiho volani uz je `_tpos` 0, takze `size=0` a
        kurzor po najiti posledniho `[TG]` zaznamu zustane na 0, ne skoci na skutecnou delku
        noveho souboru. Kdyby to nekdo „opravil" na realnou velikost souboru, kurzor by se posunul
        DOVNITR noveho souboru a replay by zase nebyl samoopravny — bez jedineho cerveneho testu,
        protoze tenhle rewind nejde pres detekci zkraceni (`size < _tpos`), kterou meri test vyse."""
        with tempfile.TemporaryDirectory() as td:
            b = _regrese_bridge(td)
            b._status_push = lambda *a, **k: None
            transcript = Path(td) / "t.jsonl"
            b._transcript = transcript
            b.cfg.turn_end_quiet_seconds = 0.05

            b._handle(_msg(1, "[TG] zjisti neco"))
            b._turn_begun_at -= 5

            before_result = [
                _user_record("[TG] zjisti neco"),
                _toolrec("task1", name="Task"),
                _arec("[tg] hotovo", stop="end_turn", uuid="u1"),
            ]

            _write_transcript(transcript, before_result)
            b._drain_transcript()
            time.sleep(0.1)
            self.assertTrue(b._transcript_turn_end_ready(), "test setup is wrong")

            _append_transcript(transcript, [_resultrec("task1")])
            b._drain_transcript()
            time.sleep(0.1)
            self.assertFalse(b._transcript_turn_end_ready(), "test setup is wrong")

            # --- REWIND pres _maybe_reresolve: NOVY soubor se stejnym obsahem, bridge na nej
            # prepne sam — zadne rucni `_tpos = 0` z naseho testu.
            transcript2 = Path(td) / "t2.jsonl"
            _write_transcript(transcript2, before_result)
            b._last_resolve = 0.0
            b._resolve_transcript = lambda: transcript2
            b._session_cwd = lambda: "whatever-not-none"  # at nebrani prepnuti uprostred tahu
            b._signal = None    # _publish_transcript neni predmetem tohoto testu
            b._maybe_reresolve()

            self.assertEqual(b._transcript, transcript2, "test setup is wrong: neprepnulo se")
            self.assertEqual(b._tpos, 0, "test setup is wrong: _resume_position musi kurzor "
                              "vratit na 0 (size=_tpos=0 v okamziku jejiho volani)")

            b._drain_transcript()
            time.sleep(0.1)
            self.assertTrue(
                b._transcript_turn_end_ready(),
                "test setup is wrong: replay end_turnu po prepnuti souboru musi signal vztycit",
            )

            _append_transcript(transcript2, [_resultrec("task1")])
            b._drain_transcript()
            time.sleep(0.1)
            self.assertFalse(
                b._transcript_turn_end_ready(),
                "replay vysledku task1 PO PREPNUTI SOUBORU signal konce nezrusil — tah by se "
                "zavrel uprostred prace",
            )

    def test_vysledek_s_neznamym_id_signal_konce_rusi(self):
        """Negativni protivaha k predchozimu testu: vysledek s ID, ktere tento reader NIKDY
        nevidel (ani jako `_open_calls`, ani jako `_closed_calls`), musi signal konce rusit dal —
        to je jeden ze tri stavu `continues_turn` rozlisuje (`agent2telegram/readers.py`): NEZNAME
        cancelluje stejne jako OTEVRENE, protoze most mohl pripojit uprostred tahu
        (`_resume_position` zacina uvnitr souboru) — "nevim" neni "uzavreno". Kdyby se tahle vetev
        omylem zacala chovat jako u ZNAMEHO/uzavreneho volani, tah by se zavrel uprostred prace a
        skutecna odpoved by se nikdy neprepose."""
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
                _arec("[tg] hotovo", stop="end_turn", uuid="u1"),
            ])
            b._drain_transcript()
            time.sleep(0.1)
            self.assertTrue(
                b._transcript_turn_end_ready(),
                "test setup is wrong: end_turn musi signal vztycit",
            )

            _append_transcript(transcript, [_resultrec("neznamy-id-nikdy-nevidany")])
            b._drain_transcript()
            time.sleep(0.1)
            self.assertFalse(
                b._transcript_turn_end_ready(),
                "vysledek s neznamym tool_use_id musi signal konce zrusit — bezpecny smer "
                "('vsechno ostatni rusi dal, neznamy id vcetne') nesmi prestat platit",
            )

    def test_cela_posloupnost_task_sidechain_end_turn_tool_result_nezavre_hlavni_tah(self):
        """Nalez z revize: `test_sidechain_end_turn_nezavre_hlavni_tah` a
        `test_vysledek_nastroje_po_end_turn_zrusi_signal_konce` merily kazdy jen svuj kousek
        oddelene — jeden sidechain `end_turn` bez navazujiciho `tool_result`, druhy pozdni
        `tool_result` POTVRZENEHO (otevreneho) `Task` volani, ale bez sidechain odbocky a bez
        predchoziho skutecneho `end_turn` hlavniho agenta V TETO sekvenci. Tenhle test jde celou
        posloupnosti najednou — `Task tool_use` → sidechain `end_turn` (nedotkne se
        hlavniho tahu) → hlavni agent sam napise zaznam se `stop_reason: end_turn` DRIV, nez se
        `task1` dokonci (presne ten zavod, ktery API umi vyrobit) → teprve PAK dorazi `task1`
        `tool_result`, ktery tenhle predcasny signal musi zrusit — a tah zustane otevreny az do
        skutecne finalni odpovedi. Bez teto mezikrocke (`stop_reason: end_turn` PRED
        `tool_result`) by `_turn_done_at` nikdy nebyl `True` v okamziku, kdy `tool_result`
        dorazi, a cely test by byl vuci `continues_turn` slepy."""
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
                _arec("[tg] deleguji", stop=None, uuid="u1"),
                _toolrec("task1", name="Task"),
                {"type": "user", "isSidechain": True,
                 "message": {"role": "user", "content": "You are a subagent..."}},
                _arec("subagent: hotovo", stop="end_turn", uuid="u2", sidechain=True),
                _arec("[tg] hotovo (predcasny zavod s task1)", stop="end_turn", uuid="u3"),
            ])
            b._drain_transcript()
            time.sleep(0.1)
            self.assertTrue(
                b._transcript_turn_end_ready(),
                "test setup is wrong: hlavni agent uz rekl end_turn, signal musi byt vztyceny "
                "pred tim, nez ho `task1` zrusi — jinak dalsi assert nic nemeri",
            )

            _append_transcript(transcript, [_resultrec("task1")])
            b._drain_transcript()
            time.sleep(0.1)
            self.assertFalse(
                b._transcript_turn_end_ready(),
                "vysledek KONKRETNIHO Task volani z teto posloupnosti nezrusil predcasny "
                "signal konce — tah by se zavrel, dokud `task1` jeste bezel",
            )

            _append_transcript(transcript, [_arec("[tg] skutecna odpoved", stop="end_turn", uuid="u4")])
            b._drain_transcript()
            time.sleep(0.1)
            self.assertTrue(
                b._transcript_turn_end_ready(),
                "skutecna odpoved hlavniho agenta tah zavrit musi",
            )
            b._end_turn()
            self.assertIn("skutecna odpoved", "\n".join(b.tg.sent))

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

    Technika je stejna jako v `tests/test_attach_backstop.py`,
    `test_a_definitive_end_waits_for_a_final_answer_that_has_not_started_writing_yet` —
    `_wait_backstop_retry` se
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
    behem behu smycky, a navic pres skutecne TELO `_outbound_loop`, ne jen volanim pomocne
    metody `_flush_overdue_batch` napřímo. Technika jedne iterace smycky je stejna jako
    `JednaZpravaNaTahTests.test_idle_done_zustava_poslednim_zpusobem_konce_tahu`
    (`tests/test_jedna_zprava_na_tah.py`): `_beat` se prepise na `_stop.set()`, takze
    `_outbound_loop()` provede presne jeden pruchod a vrati se."""

    def _jedna_iterace_smycky(self, b):
        b._last_resolve = time.monotonic()      # skip re-resolution — not under test here
        b._last_inbound_retry = time.monotonic()
        b._last_activity = time.monotonic()
        b._beat = lambda: b._stop.set()
        b._outbound_loop()

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

            self._jedna_iterace_smycky(b)
            self.assertEqual(b.tg.sent, [], "mez jeste neuplynula — nesmi odejit")

            b._turn_begun_at -= 5  # mez (2.0 s) uz je dávno za námi, bez spoléhání na real sleep
            b._stop = threading.Event()         # nova iterace — predchozi _beat uz ji vypnul
            self._jedna_iterace_smycky(b)       # zadny novy text, jen beh tela smycky
            self.assertEqual(
                b.tg.sent, ["jedna veta"],
                "po uplynuti meze musi SMYCKA (ne jen pomocna metoda samotna) davku vyklopit, "
                "i kdyz nedorazil zadny novy text",
            )


if __name__ == "__main__":
    unittest.main()
