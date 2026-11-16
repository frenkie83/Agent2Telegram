"""Optional text-to-speech for Telegram voice replies (ElevenLabs).

Mirrors :mod:`stt.py`: enabled only when the user provides their own key, no third-party
dependency (the request is plain ``urllib``), and the key never lands in a log line or an
exception message — it only ever goes into the ``xi-api-key`` header.

Division of labour (a 2026-08-01 design decision): the AGENT writes the spoken text itself — short, spoken,
numbers as words, no paths — because it knows what it's saying and does it better than a regex
ever could. The bridge's job is to TELL the agent that voice mode is on (a marker in the injected
message, see attach.py). :func:`sanitize_for_speech` here is only a ROUGH SAFETY NET for leftover
markdown / blank lines; it deliberately does NOT rewrite numbers or guess at paths.

:func:`synthesize` turns text into mp3 bytes via the multilingual model, so the voice speaks
whatever language the reply is in (no hard-coded Czech).
"""
from __future__ import annotations

import json
import logging
import re
import socket
import time
import urllib.error
import urllib.request

log = logging.getLogger("agent2telegram.tts")

TTS_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
#: Eleven v3 (out of alpha 2026-08). Multilingual, so the voice follows the CONVERSATION's
#: language from the text itself. Against v2 it reads numbers more accurately and generates more
#: steadily (error rate 15.3% -> 4.9%). Speech-to-text (Scribe) is a separate path, unaffected.
DEFAULT_MODEL_ID = "eleven_v3"
#: ElevenLabs returns mp3 here; the bridge converts to OGG/OPUS (ffmpeg) before sendVoice.
DEFAULT_OUTPUT_FORMAT = "mp3_44100_128"
TRANSIENT_BACKOFFS = (1.0, 3.0)

_EMOJI_RE = re.compile(r"[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U00002190-\U000021FF️]")


class TTSError(Exception):
    pass


def sanitize_for_speech(text: str) -> str:
    """ROUGH safety net only. The agent is asked to write speakable text; this just removes
    leftover markdown noise and squashes blank lines so a stray ``**`` or table pipe doesn't get
    read aloud. It does NOT spell numbers, expand units, or strip paths — that is the agent's job
    (a regex guessing 'is this a path?' is exactly what we want to avoid)."""
    t = text or ""
    t = re.sub(r"```.*?```", " ", t, flags=re.S)     # fenced code
    t = re.sub(r"`([^`]*)`", r"\1", t)               # inline code → keep text, drop backticks
    t = re.sub(r"\[([^\]]+)\]\((?:[^)]+)\)", r"\1", t)   # links → label
    t = _EMOJI_RE.sub(" ", t)
    t = t.replace("**", "").replace("__", "").replace("*", "").replace("#", "")
    t = t.replace("|", " ").replace(">", " ")
    t = re.sub(r"(?m)^\s*(?:[-•·–]|\d+[.)])\s+", "", t)   # bullet markers
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{2,}", "\n", t)
    t = re.sub(r"[ \t]*\n[ \t]*", ". ", t)           # line breaks → sentence breaks
    t = re.sub(r"(?:\.\s*){2,}", ". ", t)            # tidy doubled periods
    return t.strip()


def _describe_error(err: BaseException) -> str:
    if isinstance(err, urllib.error.HTTPError):
        reason = getattr(err, "reason", None) or getattr(err, "msg", "") or ""
        return f"HTTP {err.code}: {reason}".strip()
    return str(err) or err.__class__.__name__


def synthesize(text: str, *, api_key: str, voice_id: str, model_id: str = DEFAULT_MODEL_ID,
               output_format: str = DEFAULT_OUTPUT_FORMAT, opener=None, timeout: float = 60,
               retry_backoffs: tuple[float, ...] = TRANSIENT_BACKOFFS,
               sleeper=time.sleep) -> bytes:
    """Synthesize *text* to mp3 bytes with ElevenLabs. The key only ever goes into the header."""
    if not api_key:
        raise TTSError("no ElevenLabs API key configured")
    if not (text or "").strip():
        raise TTSError("nothing to speak")
    url = TTS_URL.format(voice_id=voice_id) + f"?output_format={output_format}"
    body = json.dumps({"text": text, "model_id": model_id}).encode("utf-8")
    req = urllib.request.Request(
        url, data=body,
        headers={"xi-api-key": api_key, "Content-Type": "application/json",
                 "Accept": "audio/mpeg"},
        method="POST",
    )
    op = opener or urllib.request.build_opener()
    attempts = len(retry_backoffs) + 1
    for attempt in range(attempts):
        try:
            with op.open(req, timeout=timeout) as resp:
                audio = resp.read()
            if not audio:
                raise TTSError("ElevenLabs returned no audio")
            return audio
        except urllib.error.HTTPError as e:
            retryable = 500 <= e.code <= 599
            if not retryable or attempt == attempts - 1:
                raise TTSError(f"ElevenLabs TTS failed: {_describe_error(e)}") from e
            detail = _describe_error(e)
        except (urllib.error.URLError, TimeoutError, ConnectionError, socket.timeout) as e:
            if attempt == attempts - 1:
                raise TTSError(f"ElevenLabs TTS failed after {attempt + 1} attempts: "
                               f"{_describe_error(e)}") from e
            detail = _describe_error(e)
        log.warning("ElevenLabs TTS transient failure (%d/%d): %s", attempt + 1, attempts, detail)
        sleeper(retry_backoffs[attempt])
    raise TTSError("ElevenLabs TTS failed")   # pragma: no cover
