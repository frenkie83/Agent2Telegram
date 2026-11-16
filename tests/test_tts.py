"""Tests for tts.py — the ROUGH sanitisation safety net and the ElevenLabs call.

No network: synthesize() is driven with a fake opener. The key must never appear in an error.
The heavy lifting (speakable phrasing, numbers as words) is the AGENT's job, not tested here.
"""
import unittest

from agent2telegram.tts import TTSError, sanitize_for_speech, synthesize


class SanitiseSafetyNetTests(unittest.TestCase):
    def test_leftover_markdown_removed(self):
        out = sanitize_for_speech("**bold** and ## heading and | table |")
        for junk in ("**", "#", "|"):
            self.assertNotIn(junk, out)
        self.assertIn("bold", out)

    def test_inline_code_keeps_text_without_backticks(self):
        out = sanitize_for_speech("run `command` now")
        self.assertNotIn("`", out)
        self.assertIn("command", out)  # safety net keeps words; the AGENT decides what to say

    def test_blank_lines_and_bullets_collapse(self):
        out = sanitize_for_speech("first\n\n- second\n- third")
        self.assertNotIn("\n\n", out)
        self.assertNotIn("- ", out)
        for w in ("first", "second", "third"):
            self.assertIn(w, out)

    def test_does_not_rewrite_numbers(self):
        # Deliberately NOT a smart rewrite — that is the agent's job now.
        out = sanitize_for_speech("190 tests")
        self.assertIn("190", out)


class _Resp:
    def __init__(self, data):
        self._data = data
    def read(self):
        return self._data
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


class _Opener:
    def __init__(self, data=b"MP3DATA"):
        self.data = data
        self.calls = 0
    def open(self, req, timeout=None):
        self.calls += 1
        self.req = req
        return _Resp(self.data)


class SynthesizeTests(unittest.TestCase):
    def test_returns_audio_bytes(self):
        op = _Opener(b"\xff\xfb audio")
        out = synthesize("ahoj", api_key="k", voice_id="V", opener=op)
        self.assertEqual(out, b"\xff\xfb audio")
        self.assertEqual(op.calls, 1)
        self.assertEqual(op.req.headers.get("Xi-api-key"), "k")   # key in header only

    def test_no_key_raises_clean(self):
        with self.assertRaises(TTSError):
            synthesize("ahoj", api_key="", voice_id="V", opener=_Opener())

    def test_key_never_in_error_message(self):
        secret = "sk_supersecret_12345"

        class _Boom:
            def open(self, req, timeout=None):
                raise ConnectionError("network down")

        try:
            synthesize("ahoj", api_key=secret, voice_id="V", opener=_Boom(),
                       retry_backoffs=(), sleeper=lambda *_: None)
            self.fail("expected TTSError")
        except TTSError as e:
            self.assertNotIn(secret, str(e))


if __name__ == "__main__":
    unittest.main()
