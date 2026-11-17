"""Tests for the ElevenLabs Scribe speech-to-text integration — no network."""
import io
import json
import urllib.error
import unittest

from agent2telegram import stt


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


class _FakeOpener:
    def __init__(self, payload):
        self.payload = payload
        self.last_request = None

    def open(self, req, timeout=None):
        self.last_request = req
        return _Resp(json.dumps(self.payload).encode())


class _SequenceOpener:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0
        self.timeouts = []

    def open(self, req, timeout=None):
        self.calls += 1
        self.timeouts.append(timeout)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return _Resp(json.dumps(outcome).encode())


class STTTests(unittest.TestCase):
    def test_transcribes_text(self):
        op = _FakeOpener({"text": "hello world"})
        out = stt.transcribe_elevenlabs(b"audio", api_key="k", opener=op)
        self.assertEqual(out, "hello world")

    def test_uses_scribe_model_in_multipart(self):
        op = _FakeOpener({"text": "x"})
        stt.transcribe_elevenlabs(b"audio", api_key="k", opener=op)
        body = op.last_request.data
        self.assertIn(b"scribe_v1", body)
        self.assertIn(b'name="file"', body)
        self.assertIn(b"audio", body)

    def test_sends_api_key_header(self):
        op = _FakeOpener({"text": "x"})
        stt.transcribe_elevenlabs(b"audio", api_key="secret-key", opener=op)
        # urllib normalizes header keys to title-case
        self.assertEqual(op.last_request.headers.get("Xi-api-key"), "secret-key")

    def test_missing_key_raises(self):
        with self.assertRaises(stt.STTError):
            stt.transcribe_elevenlabs(b"audio", api_key="")

    def test_empty_transcription_raises(self):
        op = _FakeOpener({"text": ""})
        with self.assertRaises(stt.STTError):
            stt.transcribe_elevenlabs(b"audio", api_key="k", opener=op)

    def test_retries_timeout_then_succeeds(self):
        op = _SequenceOpener([TimeoutError("timed out"), {"text": "after retry"}])
        sleeps = []

        out = stt.transcribe_elevenlabs(
            b"audio",
            api_key="k",
            opener=op,
            retry_backoffs=(1.0, 3.0),
            sleeper=sleeps.append,
        )

        self.assertEqual(out, "after retry")
        self.assertEqual(op.calls, 2)
        self.assertEqual(op.timeouts, [120, 120])
        self.assertEqual(sleeps, [1.0])

    def test_unauthorized_is_not_retried(self):
        err = urllib.error.HTTPError(
            stt.ELEVENLABS_URL, 401, "Unauthorized", {}, io.BytesIO(b"")
        )
        op = _SequenceOpener([err, {"text": "should not happen"}])
        sleeps = []

        with self.assertRaises(stt.STTError) as ctx:
            stt.transcribe_elevenlabs(
                b"audio",
                api_key="k",
                opener=op,
                retry_backoffs=(1.0, 3.0),
                sleeper=sleeps.append,
            )

        self.assertEqual(op.calls, 1)
        self.assertEqual(sleeps, [])
        self.assertIn("HTTP 401", str(ctx.exception))

    def test_bad_request_is_not_retried(self):
        err = urllib.error.HTTPError(
            stt.ELEVENLABS_URL, 400, "Bad Request", {}, io.BytesIO(b"")
        )
        op = _SequenceOpener([err, {"text": "should not happen"}])
        sleeps = []

        with self.assertRaises(stt.STTError) as ctx:
            stt.transcribe_elevenlabs(
                b"audio",
                api_key="k",
                opener=op,
                retry_backoffs=(1.0, 3.0),
                sleeper=sleeps.append,
            )

        self.assertEqual(op.calls, 1)
        self.assertEqual(sleeps, [])
        self.assertIn("HTTP 400", str(ctx.exception))

    def test_retries_5xx_then_succeeds(self):
        err = urllib.error.HTTPError(
            stt.ELEVENLABS_URL, 502, "Bad Gateway", {}, io.BytesIO(b"")
        )
        op = _SequenceOpener([err, {"text": "after 5xx retry"}])
        sleeps = []

        out = stt.transcribe_elevenlabs(
            b"audio",
            api_key="k",
            opener=op,
            retry_backoffs=(0.1, 0.2),
            sleeper=sleeps.append,
        )

        self.assertEqual(out, "after 5xx retry")
        self.assertEqual(op.calls, 2)
        self.assertEqual(sleeps, [0.1])

    def test_exhausted_5xx_retries_reports_attempt_count(self):
        err1 = urllib.error.HTTPError(
            stt.ELEVENLABS_URL, 503, "Unavailable", {}, io.BytesIO(b"")
        )
        err2 = urllib.error.HTTPError(
            stt.ELEVENLABS_URL, 503, "Unavailable", {}, io.BytesIO(b"")
        )
        err3 = urllib.error.HTTPError(
            stt.ELEVENLABS_URL, 503, "Unavailable", {}, io.BytesIO(b"")
        )
        op = _SequenceOpener([err1, err2, err3])
        sleeps = []

        with self.assertRaises(stt.STTError) as ctx:
            stt.transcribe_elevenlabs(
                b"audio",
                api_key="k",
                opener=op,
                retry_backoffs=(0.1, 0.2),
                sleeper=sleeps.append,
            )

        self.assertEqual(op.calls, 3)
        self.assertEqual(sleeps, [0.1, 0.2])
        self.assertIn("after 3 attempts", str(ctx.exception))
        self.assertIn("HTTP 503", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()


class ErrorSaysWhy(unittest.TestCase):
    """An HTTP status on its own says nothing. Every bot once reported plain
    "HTTP 400: Bad Request" while the response body had been saying all along that the key must
    start with "sk_" — an hour of searching, caused by a discarded response body."""

    def _http_error(self, code: int, body: bytes):
        return urllib.error.HTTPError(
            "https://api.elevenlabs.io/v1/speech-to-text", code, "Bad Request",
            {}, io.BytesIO(body),
        )

    def test_response_body_reaches_the_message(self):
        err = self._http_error(400, json.dumps({
            "detail": {"status": "invalid_api_key_prefix",
                       "message": "API key must start with 'sk_'."}
        }).encode())

        described = stt._describe_error(err)

        self.assertIn("400", described)
        self.assertIn("sk_", described, f"the response body was discarded: {described}")

    def test_a_key_in_the_body_never_reaches_the_log(self):
        err = self._http_error(400, json.dumps({
            "detail": "bad key sk_abcdef0123456789abcdef0123456789"
        }).encode())

        described = stt._describe_error(err)

        self.assertNotIn("sk_abcdef0123456789", described, "the key leaked into the message")
        self.assertIn("[redacted]", described)

    def test_an_unreadable_body_does_not_break_the_error(self):
        err = self._http_error(500, b"\xff\xfe garbage")

        described = stt._describe_error(err)

        self.assertIn("500", described)

    def test_an_empty_body_leaves_the_message_clean(self):
        err = self._http_error(404, b"")

        self.assertEqual(stt._describe_error(err), "HTTP 404: Bad Request")


class KeyShape(unittest.TestCase):
    """A malformed key should be caught when it is entered, not at the first voice note."""

    def test_the_old_format_is_rejected(self):
        self.assertFalse(stt.looks_like_api_key("5bd" + "0" * 61))

    def test_the_new_format_is_accepted(self):
        self.assertTrue(stt.looks_like_api_key("sk_" + "a" * 48))
        self.assertTrue(stt.looks_like_api_key("  sk_" + "a" * 48 + "  "))

    def test_prazdny_klic_neprojde(self):
        self.assertFalse(stt.looks_like_api_key(""))



class LanguagePinning(unittest.TestCase):
    """Bez jazyka Scribe hádá a na krátké nahrávce se strefí vedle — 2026-09-02 vrátil
    dvouvteřinovou českou zprávu jako portugalštinu. Detekce potřebuje materiál a hlasovka
    ho často nemá."""

    def _opener(self, zachyceno):
        class Op:
            def open(self, req, timeout=None):
                zachyceno["body"] = req.data
                class R:
                    def __enter__(self): return self
                    def __exit__(self, *a): return False
                    def read(self): return b'{"text":"ahoj"}'
                return R()
        return Op()

    def test_jazyk_se_posle_kdyz_je_nastaveny(self):
        """Měří, že se kód dostane DO REQUESTU — ⛔ ne že funkce nespadne."""
        z = {}
        out = stt.transcribe_elevenlabs(b"audio", api_key="k", language="ces",
                                        opener=self._opener(z))
        self.assertEqual(out, "ahoj")
        self.assertIn(b"language_code", z["body"])
        self.assertIn(b"ces", z["body"])

    def test_prazdny_jazyk_nechava_dosavadni_chovani(self):
        """Prázdno musí request nechat beze změny — jinak by upgrade změnil chování všem."""
        z = {}
        stt.transcribe_elevenlabs(b"audio", api_key="k", language="",
                                  opener=self._opener(z))
        self.assertNotIn(b"language_code", z["body"])
