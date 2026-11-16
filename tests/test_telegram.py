"""Tests for the Telegram client and message helpers — no network required."""
import io
import json
import os
import tempfile
import unittest
import urllib.error

from agent2telegram.telegram import TelegramClient, split_message, MAX_MESSAGE_LEN


class SplitMessageTests(unittest.TestCase):
    def test_short_text_single_chunk(self):
        self.assertEqual(split_message("hello"), ["hello"])

    def test_empty_text_no_chunks(self):
        self.assertEqual(split_message(""), [])

    def test_long_text_is_chunked_within_limit(self):
        text = "\n".join(f"line {i}" for i in range(5000))
        chunks = split_message(text)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(c) <= MAX_MESSAGE_LEN for c in chunks))

    def test_reassembles_to_original_content(self):
        text = "word " * 4000
        chunks = split_message(text)
        joined = " ".join(c.strip() for c in chunks)
        self.assertEqual(joined.split(), text.split())

    def test_hard_split_when_no_boundary(self):
        text = "x" * (MAX_MESSAGE_LEN * 2 + 5)
        chunks = split_message(text)
        self.assertEqual("".join(chunks), text)
        self.assertTrue(all(len(c) <= MAX_MESSAGE_LEN for c in chunks))


class _FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


class _FakeOpener:
    """Replays a scripted sequence of responses/exceptions for `.open()`."""
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def open(self, req, timeout=None):
        self.calls.append(req)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return _FakeResponse(json.dumps(item).encode())


def _ok(result):
    return {"ok": True, "result": result}


class TelegramClientTests(unittest.TestCase):
    def test_get_me(self):
        op = _FakeOpener([_ok({"username": "mybot"})])
        client = TelegramClient("123:abc", opener=op)
        self.assertEqual(client.get_me()["username"], "mybot")

    def test_api_error_raises(self):
        op = _FakeOpener([{"ok": False, "description": "Unauthorized"}])
        client = TelegramClient("123:abc", opener=op)
        with self.assertRaises(Exception):
            client.get_me()

    def test_flood_control_retry(self):
        # First call: HTTP 429 with retry_after; then success. Should transparently retry.
        flood = urllib.error.HTTPError(
            "url", 429, "Too Many Requests", {},
            io.BytesIO(json.dumps({"parameters": {"retry_after": 0}}).encode()),
        )
        op = _FakeOpener([flood, _ok({"username": "mybot"})])
        client = TelegramClient("123:abc", opener=op)
        self.assertEqual(client.get_me()["username"], "mybot")
        self.assertEqual(len(op.calls), 2)

    def test_send_message_splits_long_text(self):
        long_text = "a\n" * 5000
        op = _FakeOpener([_ok({}) for _ in range(10)])
        client = TelegramClient("123:abc", opener=op)
        client.send_message(42, long_text)
        self.assertGreater(len(op.calls), 1)

    def test_send_message_falls_back_to_plain_on_markdown_error(self):
        md_fail = {"ok": False, "description": "can't parse entities"}
        op = _FakeOpener([md_fail, _ok({})])
        client = TelegramClient("123:abc", opener=op)
        client.send_message(42, "*broken", parse_mode="Markdown")
        # Second call must omit parse_mode.
        self.assertEqual(len(op.calls), 2)
        self.assertNotIn(b"parse_mode", op.calls[1].data)


if __name__ == "__main__":
    unittest.main()


class SendFileFallbackTests(unittest.TestCase):
    """A file Telegram refuses to show as a photo/video must still arrive as a document.

    Real case (2026-08-25): a stitched chart 3300x9520 px is a valid PNG, but sendPhoto
    caps width + height at 10000, so the upload died with HTTP 400 and the file was lost.
    """

    def _png(self, tmp, name="chart.png"):
        p = os.path.join(tmp, name)
        with open(p, "wb") as fh:
            fh.write(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
        return p

    def test_photo_rejected_falls_back_to_document(self):
        bad = urllib.error.HTTPError("url", 400, "Bad Request", {}, io.BytesIO(b"{}"))
        op = _FakeOpener([bad, _ok({"message_id": 7})])
        client = TelegramClient("123:abc", opener=op)
        with tempfile.TemporaryDirectory() as tmp:
            client.send_file(42, self._png(tmp), caption="graf")
        self.assertEqual(len(op.calls), 2)
        self.assertTrue(op.calls[0].full_url.endswith("/sendPhoto"))
        self.assertTrue(op.calls[1].full_url.endswith("/sendDocument"))
        self.assertIn(b'name="caption"', op.calls[1].data)

    def test_document_failure_is_not_retried_forever(self):
        bad = urllib.error.HTTPError("url", 400, "Bad Request", {}, io.BytesIO(b"{}"))
        op = _FakeOpener([bad])
        client = TelegramClient("123:abc", opener=op)
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "notes.xyz")      # unknown type -> sendDocument
            with open(p, "wb") as fh:
                fh.write(b"x")
            with self.assertRaises(Exception):
                client.send_file(42, p)
        self.assertEqual(len(op.calls), 1)

    def test_successful_photo_sends_once(self):
        op = _FakeOpener([_ok({"message_id": 3})])
        client = TelegramClient("123:abc", opener=op)
        with tempfile.TemporaryDirectory() as tmp:
            client.send_file(42, self._png(tmp))
        self.assertEqual(len(op.calls), 1)
        self.assertTrue(op.calls[0].full_url.endswith("/sendPhoto"))

    def test_video_meta_dropped_on_document_fallback(self):
        bad = urllib.error.HTTPError("url", 400, "Bad Request", {}, io.BytesIO(b"{}"))
        op = _FakeOpener([bad, _ok({"message_id": 9})])
        client = TelegramClient("123:abc", opener=op)
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "reel.mp4")
            with open(p, "wb") as fh:
                fh.write(b"\x00" * 32)
            client.send_file(42, p, meta={"width": 1080, "height": 1920, "duration": 30})
        body = op.calls[1].data
        for gone in (b'name="width"', b'name="height"', b'name="duration"',
                     b'name="supports_streaming"'):
            self.assertNotIn(gone, body)
