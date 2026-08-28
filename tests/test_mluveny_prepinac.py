"""The Czech spoken voice-reply switch ("zapni hlas" / "vypni hlas").

``/voice`` is unreachable hands-free — dictation renders it as "slash voice" or "lomitko vojs",
never the literal command — so 91ee845 added two Czech phrases that are recognised on the raw
text, typed OR spoken. This file holds down the contract from the task: exact-message match
only, tolerant of what real speech-to-text actually produces, reachable from both typed text and
a voice transcript, never delivered to the agent, always confirmed in TEXT, and persisted like
``/voice`` already was.

Uses the existing harnesses rather than re-building them: ``_voice_bridge`` from test_voice.py
(built on ``_bridge``/``_msg`` from test_v2_durability.py, which use a REAL ``Config``, not a
stand-in). No network, no tmux, no file outside ``tempfile``, and no bridge ever points at a real
state directory — see NEAR-MISSES.md for what happens when a test does.
"""
import tempfile
import unittest
from pathlib import Path

from agent2telegram import stt
from agent2telegram.attach import _normalize_spoken, _spoken_voice_switch
from tests.test_v2_durability import _bridge, _msg
from tests.test_voice import _voice_bridge


# ---------------------------------------------------------------------------
# Pure functions: _normalize_spoken / _spoken_voice_switch
# ---------------------------------------------------------------------------

class NormalizeSpokenTests(unittest.TestCase):
    """``_normalize_spoken`` is the folding step every phrase comparison relies on. If it folds
    too little, ordinary STT output ("Zapni hlas.") never matches and the switch looks dead;
    if it folds too much, unrelated text starts matching by accident."""

    def test_folds_case_and_trailing_stop(self):
        self.assertEqual(_normalize_spoken("Zapni hlas."), "zapni hlas")

    def test_folds_case_and_trailing_exclamation(self):
        self.assertEqual(_normalize_spoken("ZAPNI HLAS!"), "zapni hlas")

    def test_collapses_doubled_and_multiple_internal_spaces(self):
        self.assertEqual(_normalize_spoken("zapni  hlas"), "zapni hlas")
        self.assertEqual(_normalize_spoken("zapni     hlas"), "zapni hlas")

    def test_strips_surrounding_whitespace(self):
        self.assertEqual(_normalize_spoken("  zapni hlas  "), "zapni hlas")

    def test_drops_combining_diacritics_so_a_mis_accented_rendering_still_folds(self):
        # The phrase table itself is ASCII ("zapni hlas"); STT occasionally over-accents a word
        # it doesn't recognise well. Docstring in attach.py promises this survives.
        self.assertEqual(_normalize_spoken("zápni hlás"), "zapni hlas")

    def test_empty_and_whitespace_only_fold_to_empty_string(self):
        self.assertEqual(_normalize_spoken(""), "")
        self.assertEqual(_normalize_spoken("   "), "")

    def test_none_like_falsy_input_does_not_raise(self):
        # `text or ""` guard inside — a caller passing None must not crash the update loop.
        self.assertEqual(_normalize_spoken(None), "")


class SpokenVoiceSwitchTests(unittest.TestCase):
    """``_spoken_voice_switch`` is the decision function itself, tested without any bridge."""

    def test_exact_phrase_switches_on(self):
        self.assertIs(_spoken_voice_switch("zapni hlas"), True)

    def test_exact_phrase_switches_off(self):
        self.assertIs(_spoken_voice_switch("vypni hlas"), False)

    def test_phrase_embedded_in_a_longer_sentence_is_not_a_switch(self):
        """The single most important guarantee in this file: a sentence that merely CONTAINS the
        phrase must not flip anything, or the bridge would randomly toggle voice mode mid
        conversation any time the owner happened to say those two words in a sentence."""
        self.assertIsNone(_spoken_voice_switch("zapni hlas az dojedu"))
        self.assertIsNone(_spoken_voice_switch("pak mi zapni hlas"))
        self.assertIsNone(_spoken_voice_switch("vypni hlas, prosim, az budes moct"))

    def test_word_order_swapped_is_not_a_switch(self):
        self.assertIsNone(_spoken_voice_switch("hlas zapni"))

    def test_unrelated_text_is_not_a_switch(self):
        self.assertIsNone(_spoken_voice_switch("jak se dneska mas?"))

    def test_empty_message_is_not_a_switch(self):
        self.assertIsNone(_spoken_voice_switch(""))
        self.assertIsNone(_spoken_voice_switch("   "))

    def test_tolerates_realistic_stt_capitalisation_punctuation_and_spacing(self):
        for variant in (
            "Zapni hlas.",
            "ZAPNI HLAS!",
            "zapni  hlas",
            "  zapni hlas  ",
            "Zapni hlas...",
        ):
            with self.subTest(variant=variant):
                self.assertIs(_spoken_voice_switch(variant), True)

    def test_tolerates_the_same_noise_for_the_off_phrase(self):
        for variant in ("Vypni hlas.", "VYPNI HLAS!", "vypni  hlas"):
            with self.subTest(variant=variant):
                self.assertIs(_spoken_voice_switch(variant), False)


# ---------------------------------------------------------------------------
# Wiring: AttachBridge._handle / _handle_spoken_switch / _set_voice_mode
# ---------------------------------------------------------------------------

class _MediaClient:
    """Minimal fake Telegram client that also answers the media calls a voice note needs
    (``_voice_bridge``'s bare client does not implement them, and letting `_transcribe` blow up
    on AttributeError before reaching the STT mock would silently pass every voice-note test
    for the wrong reason)."""

    def __init__(self):
        self.sent = []
        self.actions = []
        self.voice_calls = []

    def send_chat_action(self, chat_id, action="typing"):
        self.actions.append((chat_id, action))

    def send_message(self, chat_id, text, parse_mode=None):
        self.sent.append((chat_id, text))

    def send_voice(self, chat_id, path):
        self.voice_calls.append((chat_id, path))

    def get_file_path(self, file_id):
        return f"voice/{file_id}.ogg"

    def download(self, file_path, timeout=120):
        return b"FAKE-AUDIO-BYTES"


def _voice_update(update_id=1, message_id=10):
    return {
        "update_id": update_id,
        "message": {
            "chat": {"id": 7},
            "from": {"id": 7},
            "message_id": message_id,
            "voice": {"file_id": "v1"},
        },
    }


def _photo_update(caption, update_id=1, message_id=10):
    return {
        "update_id": update_id,
        "message": {
            "chat": {"id": 7},
            "from": {"id": 7},
            "message_id": message_id,
            "caption": caption,
            "photo": [{"file_id": "p1", "file_size": 123}],
        },
    }


def _document_update(caption, update_id=1, message_id=10):
    return {
        "update_id": update_id,
        "message": {
            "chat": {"id": 7},
            "from": {"id": 7},
            "message_id": message_id,
            "caption": caption,
            "document": {"file_id": "d1", "file_name": "notes.pdf", "file_size": 123},
        },
    }


class TypedSwitchDoesNotReachAgentTests(unittest.TestCase):
    """Requirement: the switch is a bridge instruction, not a message to the agent — it must
    never show up as an injected tmux keystroke."""

    def test_typed_on_phrase_flips_mode_and_stays_out_of_the_session(self):
        with tempfile.TemporaryDirectory() as td:
            b = _voice_bridge(td, on=False)
            b._handle(_msg(1, "zapni hlas"))
            self.assertTrue(b._voice_reply_on())
            self.assertEqual(b._session.injected, [], "the switch must not reach the agent")

    def test_typed_off_phrase_flips_mode_and_stays_out_of_the_session(self):
        with tempfile.TemporaryDirectory() as td:
            b = _voice_bridge(td, on=True)
            b._handle(_msg(1, "vypni hlas"))
            self.assertFalse(b._voice_reply_on())
            self.assertEqual(b._session.injected, [], "the switch must not reach the agent")

    def test_sentence_merely_containing_the_phrase_reaches_the_agent_unchanged(self):
        """End-to-end version of the most important guarantee: through `_handle`, not just the
        bare decision function, in case the wiring re-introduces substring matching that the
        pure-function test above wouldn't see (e.g. a `.startswith`/`in` check added at the call
        site instead of inside `_spoken_voice_switch`)."""
        with tempfile.TemporaryDirectory() as td:
            b = _voice_bridge(td, on=False)
            b._handle(_msg(1, "zapni hlas az dojedu"))
            self.assertFalse(b._voice_reply_on(), "mode must not have flipped")
            self.assertEqual(len(b._session.injected), 1)
            self.assertIn("zapni hlas az dojedu", b._session.injected[0])


class VoiceNoteSwitchTests(unittest.TestCase):
    """Requirement: the phrase must be recognised on the BARE transcript, before the
    "[voice transcript …]" marker is prepended for the agent's benefit."""

    def _handle_voice_with_transcript(self, td, transcript, *, on=False):
        b = _voice_bridge(td, on=on)
        b.tg = _MediaClient()
        orig = stt.transcribe
        stt.transcribe = lambda *a, **kw: transcript
        try:
            b._handle(_voice_update())
        finally:
            stt.transcribe = orig
        return b

    def test_bare_transcript_flips_mode_without_the_marker_leaking_into_the_match(self):
        with tempfile.TemporaryDirectory() as td:
            b = self._handle_voice_with_transcript(td, "zapni hlas", on=False)
            self.assertTrue(b._voice_reply_on())
            self.assertEqual(b._session.injected, [])

    def test_bare_transcript_tolerates_stt_capitalisation_and_stop(self):
        # Real dictation: capitalised first word, trailing full stop — exactly what ElevenLabs
        # STT actually returns for a short dictated sentence.
        with tempfile.TemporaryDirectory() as td:
            b = self._handle_voice_with_transcript(td, "Vypni hlas.", on=True)
            self.assertFalse(b._voice_reply_on())
            self.assertEqual(b._session.injected, [])

    def test_transcript_that_only_mentions_the_phrase_is_forwarded_to_the_agent(self):
        with tempfile.TemporaryDirectory() as td:
            b = self._handle_voice_with_transcript(
                td, "az budu doma tak mi zapni hlas", on=False)
            self.assertFalse(b._voice_reply_on())
            self.assertEqual(len(b._session.injected), 1)
            # The forwarded text must carry the machine-transcript marker — this proves the
            # marker step still runs for ordinary voice notes when the switch does NOT fire.
            self.assertIn("voice transcript", b._session.injected[0])
            self.assertIn("zapni hlas", b._session.injected[0])


class CaptionIsNotASwitchTests(unittest.TestCase):
    """Requirement: a caption on a photo/document is a note about the FILE, not a command to the
    bridge — the same boundary already drawn for slash commands. The file must still reach the
    agent even when its caption happens to equal a switch phrase word-for-word."""

    def test_photo_caption_equal_to_switch_phrase_does_not_flip_mode(self):
        with tempfile.TemporaryDirectory() as td:
            b = _voice_bridge(td, on=False)
            b._download_note = lambda msg, chat_id: "[photo saved]"
            b._handle(_photo_update("zapni hlas"))
            self.assertFalse(b._voice_reply_on(), "a caption must never flip voice mode")
            self.assertEqual(len(b._session.injected), 1, "the file must still reach the agent")
            self.assertIn("zapni hlas", b._session.injected[0])

    def test_document_caption_equal_to_switch_phrase_does_not_flip_mode(self):
        with tempfile.TemporaryDirectory() as td:
            b = _voice_bridge(td, on=True)
            b._download_note = lambda msg, chat_id: "[document saved]"
            b._handle(_document_update("vypni hlas"))
            self.assertTrue(b._voice_reply_on(), "a caption must never flip voice mode")
            self.assertEqual(len(b._session.injected), 1, "the file must still reach the agent")
            self.assertIn("vypni hlas", b._session.injected[0])


class ConfirmationIsAlwaysTextTests(unittest.TestCase):
    """Requirement: the confirmation must be readable text, never a voice note — if TTS is what
    just broke, a spoken confirmation would be silence and the user could never tell the switch
    worked."""

    def test_on_confirmation_is_text_and_never_attempts_synthesis(self):
        with tempfile.TemporaryDirectory() as td:
            b = _voice_bridge(td, on=False)
            calls = []
            b._try_send_voice = lambda t: calls.append(t) or True
            b._handle(_msg(1, "zapni hlas"))
            self.assertEqual(calls, [], "the confirmation must not go through the voice path")
            self.assertTrue(b.tg.sent, "the user must be told the switch happened")
            self.assertTrue(any("on" in s.lower() for s in b.tg.sent))

    def test_off_confirmation_is_text_and_never_attempts_synthesis(self):
        with tempfile.TemporaryDirectory() as td:
            b = _voice_bridge(td, on=True)
            calls = []
            b._try_send_voice = lambda t: calls.append(t) or True
            b._handle(_msg(1, "vypni hlas"))
            self.assertEqual(calls, [], "the confirmation must not go through the voice path")
            self.assertTrue(b.tg.sent, "the user must be told the switch happened")
            self.assertTrue(any("off" in s.lower() for s in b.tg.sent))


class PersistenceTests(unittest.TestCase):
    """Requirement: the state set by a spoken phrase survives a restart, exactly like `/voice`
    already does — it is written to the SAME file (`voice_mode` in the state dir)."""

    def test_spoken_on_persists_to_the_voice_mode_file_and_survives_a_fresh_bridge(self):
        with tempfile.TemporaryDirectory() as td:
            b = _voice_bridge(td, on=False)
            b._handle(_msg(1, "zapni hlas"))
            self.assertEqual((Path(td) / "voice_mode").read_text().strip(), "on")

            b2 = _voice_bridge(td)
            self.assertTrue(b2._load_voice_state(), "a fresh bridge over the same state must see ON")

    def test_spoken_off_persists_to_the_voice_mode_file_and_survives_a_fresh_bridge(self):
        with tempfile.TemporaryDirectory() as td:
            b = _voice_bridge(td, on=True)
            b._handle(_msg(1, "vypni hlas"))
            self.assertEqual((Path(td) / "voice_mode").read_text().strip(), "off")

            b2 = _voice_bridge(td)
            self.assertFalse(b2._load_voice_state(), "a fresh bridge over the same state must see OFF")


class ElevenLabsKeyGateTests(unittest.TestCase):
    """Requirement: the missing-key refusal covers BOTH directions, not just turning ON.

    This used to be asymmetric on this fork: turning OFF without a key was allowed, on the
    reasoning that refusing to disable something that cannot even run makes no sense. Review
    found that this was an UNREQUESTED change to `/voice`'s existing behaviour — the task never
    asked for it — so it was reverted (see `_set_voice_mode`'s own docstring in attach.py) back
    to upstream: without a key, voice replies are off either way, so there is nothing to persist
    or confirm as a real state change in EITHER direction, and narrowing the refusal to just ON
    would itself have been a silent behaviour change nobody asked for. Symmetry, not asymmetry,
    is the deliberate contract now."""

    def test_turning_on_without_a_key_is_refused_and_explained(self):
        with tempfile.TemporaryDirectory() as td:
            b = _voice_bridge(td, key="", on=False)
            b._handle(_msg(1, "zapni hlas"))
            self.assertFalse(b._voice_reply_on())
            self.assertTrue(any("key" in s.lower() for s in b.tg.sent),
                            "must tell the user a key is needed")
            self.assertEqual(b._session.injected, [], "still a bridge instruction, not agent text")

    def test_turning_off_without_a_key_is_refused_and_explained_too(self):
        with tempfile.TemporaryDirectory() as td:
            b = _voice_bridge(td, key="", on=True)
            b._handle(_msg(1, "vypni hlas"))
            self.assertFalse(b._voice_reply_on(), "no key means voice replies are off regardless")
            self.assertTrue(any("key" in s.lower() for s in b.tg.sent),
                            "must tell the user a key is needed, same as the ON direction")
            self.assertEqual(b._session.injected, [], "still a bridge instruction, not agent text")
            self.assertFalse((Path(td) / "voice_mode").exists(),
                             "a refused switch must not persist a state change")


class SlashVoiceRegressionTests(unittest.TestCase):
    """Requirement: `/voice` must keep working exactly as before now that it shares
    `_set_voice_mode` with the spoken phrases — a regression here would hide behind the new
    code path instead of showing up as its own bug."""

    def test_slash_voice_still_toggles_through_handle(self):
        with tempfile.TemporaryDirectory() as td:
            b = _voice_bridge(td, on=False)
            b._handle(_msg(1, "/voice"))
            self.assertTrue(b._voice_reply_on())
            self.assertEqual(b._session.injected, [], "a slash command must not reach the agent")

    def test_slash_voice_refused_without_key_same_as_before(self):
        with tempfile.TemporaryDirectory() as td:
            b = _voice_bridge(td, key="", on=False)
            b._handle(_msg(1, "/voice"))
            self.assertFalse(b._voice_reply_on())
            self.assertTrue(any("key" in s.lower() for s in b.tg.sent))


class OwnEdgeCaseTests(unittest.TestCase):
    """Boundary cases not spelled out in the brief."""

    def test_repeating_the_same_on_phrase_still_confirms_each_time(self):
        """"zapni hlas" said twice is a question about the current state, not a mistake — the
        docstring on `_set_voice_mode` promises a confirmation even when nothing changes."""
        with tempfile.TemporaryDirectory() as td:
            b = _voice_bridge(td, on=False)
            b._handle(_msg(1, "zapni hlas"))
            b._handle(_msg(2, "zapni hlas"))
            self.assertTrue(b._voice_reply_on())
            self.assertEqual(len(b.tg.sent), 2, "each utterance should get its own confirmation")

    def test_punctuation_only_message_is_not_a_switch(self):
        self.assertIsNone(_spoken_voice_switch("..."))
        self.assertIsNone(_spoken_voice_switch("!?"))

    def test_phrase_with_extra_trailing_words_after_a_comma_is_not_a_switch(self):
        with tempfile.TemporaryDirectory() as td:
            b = _voice_bridge(td, on=False)
            b._handle(_msg(1, "zapni hlas, diky"))
            self.assertFalse(b._voice_reply_on())
            self.assertEqual(len(b._session.injected), 1)


if __name__ == "__main__":
    unittest.main()
