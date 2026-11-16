"""Tests for attach-mode inbound handling."""
import json
import tempfile
import threading
import unittest
from pathlib import Path

from agent2telegram import attach as attach_mod
from agent2telegram import stt
from agent2telegram.attach import (
    AttachBridge,
    MAX_AUDIO_BYTES,
    MAX_INBOUND_PROMPT_CHARS,
    PROCESSED_UPDATE_LEDGER_LIMIT,
)
from agent2telegram.config import Config


class _FakeClient:
    def __init__(self):
        self.actions = []
        self.deleted = []
        self.sent = []
        self.file_paths = []
        self.downloads = []

    def send_chat_action(self, chat_id, action="typing"):
        self.actions.append((chat_id, action))

    def send_message(self, chat_id, text, parse_mode=None):
        self.sent.append((chat_id, text))

    def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))

    def get_file_path(self, file_id):
        self.file_paths.append(file_id)
        return f"voice/{file_id}.ogg"

    def download(self, file_path, timeout=120):
        self.downloads.append(file_path)
        return b"VOICE-BYTES"


class _PollingClient(_FakeClient):
    def __init__(self, stop_event):
        super().__init__()
        self.stop_event = stop_event
        self.calls = []

    def _call(self, method, params, timeout=None):
        self.calls.append((method, dict(params), timeout))
        self.stop_event.set()
        return []


class _SlowSTTPollingClient(_FakeClient):
    def __init__(self, stop_poll):
        super().__init__()
        self.stop_poll = stop_poll
        self.calls = []
        self.second_call = threading.Event()

    def _call(self, method, params, timeout=None):
        self.calls.append((method, dict(params), timeout))
        if len(self.calls) == 1:
            return [_voice(update_id=1)]
        self.second_call.set()
        self.stop_poll.wait(2)
        return []


class _FakeSession:
    def __init__(self):
        self.injected = []

    def inject(self, text):
        self.injected.append(text)

    def _capture(self):
        return ""


def _message(text, *, update_id=1, message_id=10, edited=False):
    key = "edited_message" if edited else "message"
    return {
        "update_id": update_id,
        key: {
            "chat": {"id": 7},
            "from": {"id": 7},
            "message_id": message_id,
            "text": text,
        },
    }


def _voice(*, update_id=1, message_id=10, size=None):
    media = {"file_id": "v1"}
    if size is not None:
        media["file_size"] = size
    return {
        "update_id": update_id,
        "message": {
            "chat": {"id": 7},
            "from": {"id": 7},
            "message_id": message_id,
            "voice": media,
        },
    }


def _document(*, update_id=1, message_id=10, size=None, name="report.pdf"):
    doc = {"file_id": "doc1", "file_name": name}
    if size is not None:
        doc["file_size"] = size
    return {
        "update_id": update_id,
        "message": {
            "chat": {"id": 7},
            "from": {"id": 7},
            "message_id": message_id,
            "caption": "see attached",
            "document": doc,
        },
    }


def _bridge(state_dir=None):
    b = object.__new__(AttachBridge)
    b.cfg = Config(agent="generic", token="1:2", allowed_user_ids=[7], tmux_session="a2t")
    b.tg = _FakeClient()
    b._allowed = {7}
    b._owner_chat = 7
    b._turn_end = None
    b._session = _FakeSession()
    b._sent_keys = set()
    b._queue_path = None
    b._use_durable_outbox = False       # focused inbound tests use the legacy in-memory fake
    b._pending_send = []
    b._turn_active = threading.Event()
    b._turn_from_tg = False
    b._transcript = None
    b._last_activity = 0.0
    b._status = {"mid": None, "shown": ""}
    b._last_typing = 0.0
    b._typing_count = 0
    b._turn_started = 0.0
    b._max_gap = 0.0
    b._last_pane_warning = 0.0
    b._status_path = None
    b._seen_tools = set()
    b._tui_seen = set()
    b._turn_text_sent = True
    b._pending_turn_end = False
    b._marker = "[tg]"
    b._stop = threading.Event()
    if state_dir is not None:
        state = Path(state_dir)
        b._offset_file = state / "offset"
        b._processed_updates_file = state / "processed_updates"
        b._processed_update_ids, b._processed_update_order = b._read_processed_updates()
    return b


def _wait_inbound(b):
    if hasattr(b, "_inbound_queue"):
        b._inbound_queue.join()


class AttachInboundTests(unittest.TestCase):
    def test_message_during_active_turn_injects_immediately(self):
        b = _bridge()
        b._turn_active.set()

        b._handle(_message("second turn"))

        self.assertEqual(b._session.injected, ["second turn"])
        self.assertTrue(b._turn_active.is_set())

    def test_voice_transcription_failure_notifies_owner(self):
        b = _bridge()
        b.cfg.elevenlabs_api_key = "fake-key"
        b._turn_text_sent = False
        orig = stt.transcribe

        def fail_transcribe(*_a, **_kw):
            raise stt.STTError("ElevenLabs request failed after 3 attempts: timed out")

        stt.transcribe = fail_transcribe
        try:
            b._handle(_voice())
        finally:
            stt.transcribe = orig

        self.assertEqual(b._session.injected, [])
        self.assertEqual(len(b.tg.sent), 1)
        self.assertEqual(b.tg.sent[0][0], 7)
        self.assertIn("Voice transcription failed", b.tg.sent[0][1])
        self.assertIn("timed out", b.tg.sent[0][1])
        self.assertFalse(b._turn_text_sent)

    def test_poll_thread_does_not_block_on_slow_stt(self):
        with tempfile.TemporaryDirectory() as d:
            b = _bridge(d)
            b.cfg.elevenlabs_api_key = "fake-key"
            stop_poll = threading.Event()
            client = _SlowSTTPollingClient(stop_poll)
            b.tg = client
            stt_started = threading.Event()
            release_stt = threading.Event()
            orig = stt.transcribe

            def slow_transcribe(*_a, **_kw):
                stt_started.set()
                release_stt.wait(2)
                return "voice text"

            stt.transcribe = slow_transcribe
            t = threading.Thread(target=b._inbound_loop)
            try:
                t.start()
                self.assertTrue(stt_started.wait(2))
                self.assertTrue(client.second_call.wait(0.5))
                self.assertEqual(b._session.injected, [])

                release_stt.set()
                _wait_inbound(b)

                # Since 2026-08-01 the transcript is labelled as machine-made so the agent does
                # not take it literally (a voice note once came back transcribed as Chinese).
                # Both the text and the label are checked.
                self.assertEqual(len(b._session.injected), 1)
                self.assertIn("voice text", b._session.injected[0])
                self.assertIn("voice transcript", b._session.injected[0])
            finally:
                stt.transcribe = orig
                release_stt.set()
                b._stop.set()
                stop_poll.set()
                t.join(2)

    def test_too_large_audio_is_rejected_before_download(self):
        b = _bridge()
        b.cfg.elevenlabs_api_key = "fake-key"

        b._handle(_voice(size=MAX_AUDIO_BYTES + 1))

        self.assertEqual(b._session.injected, [])
        self.assertEqual(b.tg.file_paths, [])
        self.assertEqual(b.tg.downloads, [])
        self.assertEqual(len(b.tg.sent), 1)
        self.assertIn("Voice/audio limit is 25 MB", b.tg.sent[0][1])

    def test_downloaded_audio_over_limit_is_rejected_before_stt(self):
        b = _bridge()
        b.cfg.elevenlabs_api_key = "fake-key"
        b.tg.download = lambda file_path, timeout=120: b"x" * 6
        original_limit = attach_mod.MAX_AUDIO_BYTES
        original_transcribe = stt.transcribe
        stt_calls = []
        attach_mod.MAX_AUDIO_BYTES = 5
        stt.transcribe = lambda *_a, **_kw: stt_calls.append(True) or "should not run"
        try:
            b._handle(_voice(size=5))
        finally:
            attach_mod.MAX_AUDIO_BYTES = original_limit
            stt.transcribe = original_transcribe

        self.assertEqual(b._session.injected, [])
        self.assertEqual(stt_calls, [])
        self.assertEqual(len(b.tg.sent), 1)
        self.assertIn("Voice/audio limit is 25 MB", b.tg.sent[0][1])

    def test_too_long_stt_prompt_is_rejected(self):
        b = _bridge()
        b.cfg.elevenlabs_api_key = "fake-key"
        orig = stt.transcribe

        stt.transcribe = lambda *_a, **_kw: "x" * (MAX_INBOUND_PROMPT_CHARS + 1)
        try:
            b._handle(_voice())
        finally:
            stt.transcribe = orig

        self.assertEqual(b._session.injected, [])
        self.assertEqual(len(b.tg.sent), 1)
        self.assertIn("too long after processing", b.tg.sent[0][1])

    def test_unauthorized_message_is_refused_and_not_injected(self):
        b = _bridge()
        upd = _message("steal secrets")
        upd["message"]["from"]["id"] = 999

        b._handle(upd)

        self.assertEqual(b._session.injected, [])
        self.assertEqual(len(b.tg.sent), 1)
        self.assertIn("Not authorized", b.tg.sent[0][1])

    def test_control_chars_in_caption_reach_session_as_data_not_commands(self):
        b = _bridge()
        b._handle({
            "update_id": 1,
            "message": {
                "chat": {"id": 7},
                "from": {"id": 7},
                "message_id": 10,
                "caption": "look\x00\nnow\tplease\x85!",
            },
        })

        self.assertEqual(b._session.injected, ["look\x00\nnow\tplease\x85!"])


class AttachOffsetPersistenceTests(unittest.TestCase):
    def test_inbound_loop_loads_persisted_offset(self):
        with tempfile.TemporaryDirectory() as d:
            b = _bridge(d)
            b._save_offset(42)
            b.tg = _PollingClient(b._stop)

            b._inbound_loop()

            self.assertEqual(b.tg.calls[0][0], "getUpdates")
            self.assertEqual(b.tg.calls[0][1]["offset"], 42)

    def test_processed_update_is_not_reinjected_after_restart(self):
        with tempfile.TemporaryDirectory() as d:
            b = _bridge(d)

            offset = b._handle_update_once(_message("first", update_id=10), b._load_offset())
            _wait_inbound(b)

            self.assertEqual(offset, 11)
            self.assertEqual(b._session.injected, ["first"])
            self.assertEqual(b._load_offset(), 11)
            b._offset_file.unlink()

            restarted = _bridge(d)
            offset = restarted._handle_update_once(_message("first", update_id=10), restarted._load_offset())

            self.assertEqual(offset, 11)
            self.assertEqual(restarted._session.injected, [])

    def test_malformed_update_id_is_skipped_without_worker_submission(self):
        with tempfile.TemporaryDirectory() as d:
            b = _bridge(d)

            offset = b._handle_update_once({"message": _message("bad")["message"]}, 5)

            self.assertEqual(offset, 5)
            self.assertEqual(b._session.injected, [])
            self.assertFalse(hasattr(b, "_inbound_queue"))

    def test_corrupt_offset_file_falls_back_to_zero(self):
        with tempfile.TemporaryDirectory() as d:
            b = _bridge(d)
            b._offset_file.parent.mkdir(parents=True, exist_ok=True)
            b._offset_file.write_text("{not json", "utf-8")

            self.assertEqual(b._load_offset(), 0)

    def test_corrupt_processed_updates_file_is_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            state = Path(d)
            (state / "processed_updates").write_text("{not json", "utf-8")

            b = _bridge(d)

            self.assertEqual(b._processed_update_ids, set())
            self.assertEqual(list(b._processed_update_order), [])

    def test_processed_update_ledger_dedups_invalid_values_and_trims(self):
        with tempfile.TemporaryDirectory() as d:
            b = _bridge(d)
            values = ["bad", None, 1, "1"] + list(range(2, PROCESSED_UPDATE_LEDGER_LIMIT + 7))
            b._processed_updates_file.parent.mkdir(parents=True, exist_ok=True)
            b._processed_updates_file.write_text(json.dumps({"update_ids": values}), "utf-8")

            reloaded = _bridge(d)

            self.assertEqual(len(reloaded._processed_update_order), PROCESSED_UPDATE_LEDGER_LIMIT)
            self.assertEqual(len(reloaded._processed_update_ids), PROCESSED_UPDATE_LEDGER_LIMIT)
            self.assertNotIn(1, reloaded._processed_update_ids)
            self.assertIn(PROCESSED_UPDATE_LEDGER_LIMIT + 6, reloaded._processed_update_ids)

    def test_outbound_queue_corruption_is_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            b = _bridge()
            b._queue_path = Path(d) / "queue.jsonl"
            b._queue_path.write_text('{"text": "ok"}\n{bad json\n', "utf-8")

            self.assertEqual(b._load_queue(), [])

    def test_document_over_bot_api_limit_is_rejected_before_download(self):
        b = _bridge()

        b._handle(_document(size=21 * 1024 * 1024))

        self.assertEqual(b.tg.file_paths, [])
        self.assertEqual(b.tg.downloads, [])
        self.assertEqual(b._session.injected, [])
        self.assertEqual(len(b.tg.sent), 1)
        self.assertIn("20 MB", b.tg.sent[0][1])


if __name__ == "__main__":
    unittest.main()


class InboundLogLineTests(unittest.TestCase):
    """The IN log line is the record that a message ever arrived — it has to be readable.

    A rename pass once swapped its two arguments, so the message preview appeared under
    `reply_to=` and the quoted text after it. Nothing failed; the log simply lied, and the log is
    what gets read when someone says "I wrote and nothing happened".
    """

    def _log_inbound(self, upd):
        import logging
        import tempfile as _tempfile
        from agent2telegram.attach import AttachBridge
        with _tempfile.TemporaryDirectory() as d:
            b = object.__new__(AttachBridge)
            b.cfg = Config(agent="generic", token="1:2", allowed_user_ids=[7], tmux_session="a2t")
            b._offset_file = Path(d) / "offset"
            b._processed_updates_file = Path(d) / "processed"
            b._processed_update_ids, b._processed_update_order = set(), __import__("collections").deque()
            b._maybe_ack_queued = lambda *a, **k: None
            b._submit_inbound_update = lambda *a, **k: None
            with self.assertLogs("agent2telegram.attach", level=logging.INFO) as cm:
                b._handle_update_once(upd, 0)
            return [r.getMessage() for r in cm.records if r.getMessage().startswith("IN  ")]

    def test_reply_context_and_preview_are_not_swapped(self):
        upd = {
            "update_id": 1,
            "message": {
                "message_id": 1, "from": {"id": 7}, "chat": {"id": 7},
                "text": "fix this",
                "reply_to_message": {"text": "daily report: 2 failures"},
            },
        }
        lines = self._log_inbound(upd)
        self.assertTrue(lines, "the arrival of a message was not logged at all")
        line = lines[0]
        self.assertIn("reply_to='daily report: 2 failures'", line,
                      f"reply_to must carry the QUOTED message, got: {line}")
        self.assertTrue(line.rstrip().endswith("'fix this'"),
                        f"the line must end with the message itself, got: {line}")


class ReactionDedupTests(unittest.TestCase):
    """Jedno ❤️ nesmí vyvolat šest odpovědí.

    2026-08-26 poslal Telegram tutéž reakci jako šest samostatných updatů během 13 sekund
    (update_id 36303177-182). Agent na každý odpověděl jednou emoji, takže uživateli přišla
    salva koček. Reakce je jednoznačně určená trojicí chat + zpráva + emoji.
    """

    def _postav(self):
        b = _bridge()
        b._reaction_seen = {}
        b._injected = []
        b._inject = lambda text: (b._injected.append(text), True)[1]
        b._begin_turn = lambda: None
        b._turn_active = threading.Event()
        return b

    def _upd(self, uid, emoji="❤️", msg_id=20800, chat=1):
        return {"update_id": uid, "message_reaction": {
            "user": {"id": 7}, "chat": {"id": chat}, "message_id": msg_id,
            "new_reaction": [{"type": "emoji", "emoji": emoji}]}}

    def test_sestkrat_stejna_reakce_je_jedna_odpoved(self):
        b = self._postav()
        for uid in range(36303177, 36303183):
            b._handle(self._upd(uid))
        self.assertEqual(len(b._injected), 1, f"agent dostal {len(b._injected)} pobídek místo jedné")

    def test_jina_zprava_projde(self):
        b = self._postav()
        b._handle(self._upd(1, msg_id=100))
        b._handle(self._upd(2, msg_id=101))
        self.assertEqual(len(b._injected), 2, "reakce na jinou zprávu se nesmí zahodit")

    def test_jine_emoji_projde(self):
        b = self._postav()
        b._handle(self._upd(1, emoji="❤️"))
        b._handle(self._upd(2, emoji="👍"))
        self.assertEqual(len(b._injected), 2, "jiné emoji je jiná zpětná vazba")

    def test_po_uplynuti_okna_projde_znovu(self):
        b = self._postav()
        b._handle(self._upd(1))
        klic = list(b._reaction_seen)[0]
        b._reaction_seen[klic] -= attach_mod.REACTION_DEDUP_S + 1
        b._handle(self._upd(2))
        self.assertEqual(len(b._injected), 2, "po vypršení okna má reakce projít")

    def test_pamet_neroste_donekonecna(self):
        b = self._postav()
        for i in range(700):
            b._handle(self._upd(i, msg_id=i))
        self.assertLessEqual(len(b._reaction_seen), 512)
