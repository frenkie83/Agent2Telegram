"""Voice-reply mode: the /voice toggle (persistent) and the core marker that tells the AGENT
to write speakable replies. Uses the same hand-built bridge as test_v2_durability."""
import tempfile
import unittest
from pathlib import Path

from agent2telegram import attach as attach_mod
from agent2telegram.attach import VOICE_MODE_HINT
from tests.test_v2_durability import _bridge, _msg


def _voice_bridge(td, *, key="el-key", on=False, session=None):
    b = _bridge(td, session=session)
    b.cfg.elevenlabs_api_key = key
    b._voice_state_path = Path(td) / "voice_mode"
    b._voice_on = on
    b._sent_path = Path(td) / "attach_sent.txt"     # _mark_sent persists here
    # Observe the immediate text send directly; the durable outbox path is covered elsewhere.
    b._use_durable_outbox = False
    return b


class ToggleTests(unittest.TestCase):
    def test_toggle_on_off_persists(self):
        with tempfile.TemporaryDirectory() as td:
            b = _voice_bridge(td, on=False)
            b._toggle_voice(7)
            self.assertTrue(b._voice_reply_on(), "first /voice should turn it on")
            self.assertEqual((Path(td) / "voice_mode").read_text().strip(), "on")

            # A fresh bridge over the same state dir must load it as ON (survives restart).
            b2 = _voice_bridge(td)
            self.assertTrue(b2._load_voice_state())

            b._toggle_voice(7)
            self.assertFalse(b._voice_reply_on(), "second /voice should turn it off")
            self.assertEqual((Path(td) / "voice_mode").read_text().strip(), "off")

    def test_toggle_refused_without_key(self):
        with tempfile.TemporaryDirectory() as td:
            b = _voice_bridge(td, key="", on=False)
            b._toggle_voice(7)
            self.assertFalse(b._voice_reply_on())
            self.assertTrue(any("key" in s.lower() for s in b.tg.sent),
                            "should tell the user a key is needed")

    def test_reply_on_requires_both_switch_and_key(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertFalse(_voice_bridge(td, key="el", on=False)._voice_reply_on())
            self.assertFalse(_voice_bridge(td, key="", on=True)._voice_reply_on())
            self.assertTrue(_voice_bridge(td, key="el", on=True)._voice_reply_on())


class MarkerInjectionTests(unittest.TestCase):
    def test_voice_on_injects_hint_ahead_of_message(self):
        with tempfile.TemporaryDirectory() as td:
            b = _voice_bridge(td, on=True)
            b._handle(_msg(1, "what is new?"))
            self.assertTrue(b._session.injected, "message should reach the session")
            injected = b._session.injected[0]
            self.assertIn(VOICE_MODE_HINT, injected)
            self.assertIn("what is new?", injected)

    def test_voice_off_does_not_inject_hint(self):
        with tempfile.TemporaryDirectory() as td:
            b = _voice_bridge(td, on=False)
            b._handle(_msg(1, "what is new?"))
            self.assertTrue(b._session.injected)
            self.assertNotIn(VOICE_MODE_HINT, b._session.injected[0])


class DeliveryDecisionTests(unittest.TestCase):
    """_send_final routing: voice on success replaces text; any failure or a too-long reply
    falls back to durable TEXT so the reply NEVER disappears."""

    def test_short_reply_goes_as_voice_and_not_as_text(self):
        with tempfile.TemporaryDirectory() as td:
            b = _voice_bridge(td, on=True)
            calls = []
            b._try_send_voice = lambda t: (calls.append(t) or True)
            b._send_final("Hi, all done.", key="k1")
            self.assertEqual(len(calls), 1, "voice should be attempted")
            self.assertEqual(b.tg.sent, [], "no text should be sent when voice succeeds")
            self.assertIn("k1", b._sent_keys)

    def test_voice_failure_falls_back_to_text(self):
        with tempfile.TemporaryDirectory() as td:
            b = _voice_bridge(td, on=True)
            b._try_send_voice = lambda t: False        # TTS/ffmpeg/network down
            b._send_final("Hi, all done.", key="k2")
            self.assertTrue(b.tg.sent, "reply must still arrive as text")
            self.assertTrue(any("voice unavailable" in s.lower() for s in b.tg.sent),
                            "and say why it came as text")

    def test_long_reply_stays_text(self):
        with tempfile.TemporaryDirectory() as td:
            b = _voice_bridge(td, on=True)
            called = []
            b._try_send_voice = lambda t: called.append(t) or True
            # Derived from the constant, not a hard-coded number: the limit went from 600 to
            # 1200 on 2026-08-01 and a test with the length written in broke. A test should guard
            # BEHAVIOUR, not a value.
            long_text = "x" * (attach_mod.VOICE_MAX_CHARS + 100)
            b._send_final(long_text, key="k3")
            self.assertEqual(called, [], "a long reply must not be spoken")
            self.assertTrue(b.tg.sent, "long reply arrives as text")
            self.assertTrue(any("too long" in s.lower() for s in b.tg.sent))

    def test_voice_off_sends_text_without_attempting_voice(self):
        with tempfile.TemporaryDirectory() as td:
            b = _voice_bridge(td, on=False)
            called = []
            b._try_send_voice = lambda t: called.append(t) or True
            b._send_final("Ahoj.", key="k4")
            self.assertEqual(called, [])
            self.assertEqual(b.tg.sent, ["Ahoj."])


class TrySendVoiceTests(unittest.TestCase):
    def test_missing_ffmpeg_is_a_clean_false(self):
        import agent2telegram.attach as attach_mod
        with tempfile.TemporaryDirectory() as td:
            b = _voice_bridge(td, on=True)
            orig = attach_mod.shutil.which
            attach_mod.shutil.which = lambda name: None      # pretend ffmpeg absent
            try:
                self.assertFalse(b._try_send_voice("ahoj"))  # no crash, just False
            finally:
                attach_mod.shutil.which = orig


class SendVoiceApiTests(unittest.TestCase):
    def test_send_voice_uses_sendVoice_method_and_voice_field(self):
        from agent2telegram.telegram import TelegramClient
        captured = {}

        def fake_multipart(method, fields, file_field, filename, payload, **kw):
            captured.update(method=method, file_field=file_field, fields=fields)
            return {}

        c = TelegramClient("123:abc")
        c._call_multipart = fake_multipart
        with tempfile.NamedTemporaryFile(suffix=".ogg") as f:
            f.write(b"OggS-fake")
            f.flush()
            c.send_voice(7, f.name)
        self.assertEqual(captured["method"], "sendVoice")
        self.assertEqual(captured["file_field"], "voice")
        self.assertEqual(captured["fields"], {"chat_id": 7})


if __name__ == "__main__":
    unittest.main()
