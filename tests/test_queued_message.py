"""Regression tests for the 2026-08-20 bug: a Telegram message sent into a turn that is
ALREADY RUNNING never reaches Claude Code's transcript as a ``type: "user"`` record — Claude
Code queues it and only logs it once picked up, as ``type: "attachment"`` /
``attachment.type == "queued_command"``. ``AttachBridge`` used to look only for ``"user"``
records, so such a turn's reply was silently dropped (``_turn_from_tg`` stayed False).

Also covers the fix's OWN regression, found by review the same day: rewinding ``_tpos`` to the
turn's own (terminal) ``user`` record after a queued Telegram message joined it re-forwarded,
after a restart, everything the agent had written BEFORE that message too — including whatever
the terminal-originated part of the turn contained. The rewind point must land right after the
record that actually raised the flag, and the turn-end backstop must never read before that
boundary either.

The fixture records below (``_QUEUE_ENQUEUE``, ``_QUEUE_REMOVE``, ``_attachment_record()``,
``_TOTAL_TOKENS_REMINDER``, ``_user_record()``) are copied VERBATIM (all keys, all nesting) from
a real Claude Code transcript, lines 19, 44, 49, 51 and 151 respectively. The ONLY values changed
are personal fields (``cwd``, ``sessionId``/``session_id``, ``uuid``/``parentUuid``/``promptId``/
``source_uuid``) — replaced with placeholders of the same shape — plus, where a template is
reused for a second scenario below, the ``content``/``prompt``/``text`` and its ``timestamp``
(the reused cases say so explicitly). ``_assistant_record()`` and ``_BROKEN_ASSISTANT_RECORD`` are
synthetic (not from that transcript) — assistant records are unrelated to this bug and were never
captured for it; only the four kinds above needed to be literal.
"""
import json
import tempfile
import threading
import types
import unittest
from pathlib import Path

from agent2telegram.attach import AttachBridge
from agent2telegram.readers import ClaudeCodeReader, Ev

_CWD = "/workdir/example-project"                 # placeholder for the real transcript's cwd
_SID = "00000000-0000-0000-0000-000000000000"     # placeholder for the real transcript's session id

# Real transcript, line 44: type=queue-operation / enqueue — written the instant the Telegram
# message is typed into the already-running turn.
_QUEUE_ENQUEUE = {
    "type": "queue-operation",
    "operation": "enqueue",
    "timestamp": "2026-08-20T06:53:09.329Z",
    "sessionId": _SID,
    "content": "[TG] jsi tam<",
}

# Real transcript, line 49: type=queue-operation / remove — written once the queued text is
# pulled off the queue into the running turn (same text, different operation).
_QUEUE_REMOVE = {
    "type": "queue-operation",
    "operation": "remove",
    "timestamp": "2026-08-20T06:53:13.774Z",
    "sessionId": _SID,
    "content": "[TG] jsi tam<",
}

# Real transcript, line 51: type=attachment / attachment.type=queued_command — the record the
# fix keys off. ``prompt`` and its ``timestamp`` are parameterized below so the same verbatim
# shape can be reused for the reverse-direction scenario (a terminal message queued into a
# turn that started from Telegram).
def _attachment_record(prompt: str, *, uuid="11111111-1111-1111-1111-111111111111",
                        parent_uuid="22222222-2222-2222-2222-222222222222",
                        source_uuid="33333333-3333-3333-3333-333333333333",
                        timestamp="2026-08-20T06:53:09.329Z") -> dict:
    return {
        "parentUuid": parent_uuid,
        "isSidechain": False,
        "attachment": {
            "type": "queued_command",
            "prompt": prompt,
            "source_uuid": source_uuid,
            "commandMode": "prompt",
            "origin": {"kind": "human"},
            "timestamp": timestamp,
        },
        "type": "attachment",
        "uuid": uuid,
        "timestamp": timestamp,
        "session_id": _SID,
        "userType": "external",
        "entrypoint": "cli",
        "cwd": _CWD,
        "sessionId": _SID,
        "version": "2.1.235",
        "gitBranch": "HEAD",
    }


# Real transcript, line 19: type=attachment / attachment.type=total_tokens_reminder — a
# housekeeping attachment Claude Code writes after nearly every turn (token budget nudge),
# NOT a user message. Injected here to pin the boundary the fix must not cross: parse() must
# recognize `attachment.type` and only treat `queued_command` as a user message — anything
# else (this one, and file-attachment records seen elsewhere in real transcripts) must yield
# nothing, even if its text happens to contain "[TG]" (see the test below).
_TOTAL_TOKENS_REMINDER = {
    "parentUuid": "77777777-7777-7777-7777-777777777777",
    "isSidechain": False,
    "attachment": {
        "type": "total_tokens_reminder",
        "text": "<total_tokens>14957215 tokens left</total_tokens>",
    },
    "type": "attachment",
    "uuid": "88888888-8888-8888-8888-888888888888",
    "timestamp": "2026-08-20T06:52:56.591Z",
    "session_id": _SID,
    "userType": "external",
    "entrypoint": "cli",
    "cwd": _CWD,
    "sessionId": _SID,
    "version": "2.1.235",
    "gitBranch": "HEAD",
}


# Real transcript, line 151: a classic ``type: "user"`` record with the ``[TG]`` marker — a
# turn that STARTS from Telegram (the regression path that must keep working). ``content`` is
# parameterized below so the same verbatim shape can build a terminal-originated ("no [TG]")
# variant too.
def _user_record(content: str, *, uuid="44444444-4444-4444-4444-444444444444",
                  parent_uuid="55555555-5555-5555-5555-555555555555",
                  prompt_id="66666666-6666-6666-6666-666666666666",
                  timestamp="2026-08-20T06:59:12.293Z") -> dict:
    return {
        "parentUuid": parent_uuid,
        "isSidechain": False,
        "promptId": prompt_id,
        "type": "user",
        "message": {"role": "user", "content": content},
        "uuid": uuid,
        "timestamp": timestamp,
        "permissionMode": "bypassPermissions",
        "origin": {"kind": "human"},
        "promptSource": "typed",
        "userType": "external",
        "entrypoint": "cli",
        "cwd": _CWD,
        "sessionId": _SID,
        "version": "2.1.235",
        "gitBranch": "HEAD",
    }


def _assistant_record(text: str) -> dict:
    """Synthetic (NOT from a real transcript) — the shape ``ClaudeCodeReader.parse()`` expects
    for assistant text. Assistant records are unrelated to the bug this file pins down; only the
    queue-operation lines, the ``queued_command`` attachment and the plain ``user`` record above
    needed to be literal copies. No ``uuid`` key on purpose — the reader falls back to hashing the
    text for its dedup key, so distinct texts never collide."""
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}


# A malformed record: ``message`` is present but ``None`` instead of a dict, so
# ``rec.get("message", {}).get("content")`` raises AttributeError inside parse() — the default
# in ``.get("message", {})`` only applies when the KEY is missing, not when its value is None.
_BROKEN_ASSISTANT_RECORD = {"type": "assistant", "message": None}


def _write_transcript(path: Path, records: list, *, trailing_newline: bool = True) -> list:
    """Write one JSON object per line and return, for each record, the byte offset immediately
    after it (including its terminating newline). If ``trailing_newline`` is False the LAST
    record is written without one — simulating a line the writer hasn't flushed yet — and its
    returned offset is simply end-of-file."""
    offsets = []
    data = b""
    for i, rec in enumerate(records):
        chunk = json.dumps(rec, ensure_ascii=False).encode("utf-8")
        is_last = i == len(records) - 1
        if not is_last or trailing_newline:
            chunk += b"\n"
        data += chunk
        offsets.append(len(data))
    path.write_bytes(data)
    return offsets


class _FakeTelegram:
    def __init__(self):
        self.sent = []

    def send_message(self, chat_id, text, parse_mode=None):
        self.sent.append((chat_id, text))

    def delete_message(self, chat_id, message_id):
        pass


def _make_bridge(tmpdir: Path) -> AttachBridge:
    """A bare AttachBridge with only the fields ``_handle_event``/``_send_final`` touch —
    same style as test_marker.py's ``_strip`` helper (no tmux, no transcript, no network)."""
    b = object.__new__(AttachBridge)
    b.tg = _FakeTelegram()
    b.cfg = types.SimpleNamespace(agent="claude-code")
    b._origins = ("[TG]", "Telegram:")
    b._turn_from_tg = False
    b._owner_chat = 555
    b._marker = "[TG]"
    b._sent_keys = set()
    b._sent_path = tmpdir / "sent.txt"
    b._status = {"mid": None, "shown": ""}
    b._status_path = None
    b._seen_tools = set()
    b._pending_send = []
    b._turn_text_sent = False
    return b


def _make_bridge_with_transcript(tmpdir: Path, transcript: Path) -> AttachBridge:
    """``_make_bridge`` plus the fields ``_resume_position``/``_drain_transcript``/``_finish_turn``
    touch: the transcript cursor, the Telegram-boundary offset (``_tg_since``), and turn-active /
    turn-end bookkeeping. Still no tmux, no network — ``_turn_end`` is None so ``_consume_turn_end``
    (called by ``_finish_turn``) has no file to touch."""
    b = _make_bridge(tmpdir)
    b._reader = ClaudeCodeReader()
    b._transcript = transcript
    b._tpos = 0
    b._tg_since = 0
    b._turn_active = threading.Event()
    b._turn_end = None
    b._turn_started = 0.0
    b._typing_count = 0
    b._max_gap = 0.0
    b._pending_turn_end = False
    b._last_activity = 0.0
    return b


class QueuedMessageMidTurnTests(unittest.TestCase):
    """Criterion 1: a Telegram message sent mid-turn must still get its reply forwarded."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.bridge = _make_bridge(Path(self._tmp.name))
        self.reader = ClaudeCodeReader()

    def tearDown(self):
        self._tmp.cleanup()

    def _feed(self, rec: dict) -> None:
        for ev in self.reader.parse(rec):
            self.bridge._handle_event(ev)

    def test_queued_tg_message_mid_terminal_turn_gets_its_reply_forwarded(self):
        # The turn was started from the terminal (no [TG] prefix) — not forwarded so far.
        self._feed(_user_record("pokracuj v uklidu"))
        self.assertFalse(self.bridge._turn_from_tg)

        # Both queue-operation lines carry the SAME text as the attachment below; they must
        # not, by themselves, flip the turn to Telegram-originated (parse() yields nothing for
        # them — only the attachment record is the fact we key off, see readers.py comment).
        self._feed(_QUEUE_ENQUEUE)
        self._feed(_QUEUE_REMOVE)
        self.assertFalse(self.bridge._turn_from_tg)

        # The Telegram message actually joins the running turn here.
        self._feed(_attachment_record("[TG] jsi tam<"))
        self.assertTrue(self.bridge._turn_from_tg)

        # The turn's answer must now be forwarded.
        self.bridge._handle_event(Ev("text", text="Jo, jedu dál.", key="reply-1"))
        self.assertEqual(self.bridge.tg.sent, [(555, "Jo, jedu dál.")])

    def test_tg_turn_start_still_forwards_reply_regression(self):
        """Criterion 2 — the classic path (a turn that STARTS from Telegram) must keep working."""
        self._feed(_user_record("[TG] ok, takže teď už vše běží?"))
        self.assertTrue(self.bridge._turn_from_tg)

        self.bridge._handle_event(Ev("text", text="Ano, běží.", key="reply-2"))
        self.assertEqual(self.bridge.tg.sent, [(555, "Ano, běží.")])

    def test_terminal_only_turn_is_never_forwarded(self):
        """Criterion 3 — a turn nobody from Telegram touched stays local."""
        self._feed(_user_record("pokracuj v uklidu"))
        self.assertFalse(self.bridge._turn_from_tg)

        self.bridge._handle_event(Ev("text", text="Hotovo.", key="reply-3"))
        self.assertEqual(self.bridge.tg.sent, [])

    def test_terminal_message_queued_into_tg_turn_does_not_silence_it(self):
        """Criterion 5 (reverse direction) — a turn that started from Telegram, then someone at
        the terminal types into it while it's running (no [TG] prefix, queued). The flag may
        only be ADDED, never cleared: the turn's reply must still go out."""
        self._feed(_user_record("[TG] ok, takže teď už vše běží?"))
        self.assertTrue(self.bridge._turn_from_tg)

        # Same verbatim attachment shape as the real line 51, but the prompt has no [TG] — a
        # terminal message queued mid-turn.
        self._feed(_attachment_record("diky, uz to vidim"))
        self.assertTrue(self.bridge._turn_from_tg, "terminal message must not clear TG origin")

        self.bridge._handle_event(Ev("text", text="Super.", key="reply-4"))
        self.assertEqual(self.bridge.tg.sent, [(555, "Super.")])

    def test_non_queued_command_attachment_is_not_a_user_message(self):
        """A ``type: "attachment"`` record whose ``attachment.type`` is NOT ``queued_command``
        (here: ``total_tokens_reminder``, a housekeeping nudge written after nearly every turn —
        the same is true of file-attachment records elsewhere in real transcripts) must not be
        treated as a user message at all: parse() yields nothing for it, and even a turn that
        would otherwise be terminal-only must NOT flip to Telegram-origin just because such a
        record's text happens to contain "[TG]"."""
        self.assertEqual(list(self.reader.parse(_TOTAL_TOKENS_REMINDER)), [])

        # Same verbatim shape, only the reminder text is swapped for one containing "[TG]" — the
        # boundary the fix must not cross: only attachment.type == "queued_command" is a message.
        lookalike = json.loads(json.dumps(_TOTAL_TOKENS_REMINDER))
        lookalike["attachment"]["text"] = "[TG] <total_tokens>1 tokens left</total_tokens>"
        self.assertEqual(list(self.reader.parse(lookalike)), [])

        self._feed(_user_record("pokracuj v uklidu"))     # terminal-started turn
        self._feed(lookalike)
        self.assertFalse(self.bridge._turn_from_tg,
                          "a non-queued_command attachment must never set Telegram origin")

    def test_new_terminal_turn_after_a_tg_turn_resets_the_flag(self):
        """Review, 2026-08-20: the flag must be able to go back to False. A turn that STARTS
        fresh from the terminal — a plain, non-queued 'user' record with no [TG] — must clear
        Telegram origin even though the PREVIOUS turn was Telegram-originated; otherwise, once
        any turn in a session touched Telegram, every later terminal-only turn would leak too.
        (Mutation this catches: replacing the non-queued branch's plain assignment with
        ``self._turn_from_tg = self._turn_from_tg or from_tg`` for every 'user' event.)"""
        self._feed(_user_record("[TG] ok, takže teď už vše běží?"))
        self.assertTrue(self.bridge._turn_from_tg)
        self.bridge._handle_event(Ev("text", text="Ano, běží.", key="tg-reply"))
        self.assertEqual(self.bridge.tg.sent, [(555, "Ano, běží.")])

        # A brand new turn starts from the terminal — not queued, no [TG].
        self._feed(_user_record("dalsi ukol, tentokrat z terminalu"))
        self.assertFalse(self.bridge._turn_from_tg)

        self.bridge._handle_event(Ev("text", text="Hotovo lokálně.", key="term-reply"))
        self.assertEqual(self.bridge.tg.sent, [(555, "Ano, běží.")],
                          "the terminal turn's reply must not leak to Telegram")

    def test_queued_message_without_tg_prefix_into_terminal_turn_stays_local(self):
        """Second review round, 2026-08-20: a queued message is not automatically Telegram
        origin just because it's queued — it still needs the [TG] prefix. On this machine, 4 of
        6 real ``queued_command`` records are terminal (no [TG]), so this is the COMMON case, not
        a theoretical one: a terminal turn joined by a terminal-typed queued message must stay
        local. (Mutation this catches: the queued branch of ``_handle_event`` setting
        ``self._turn_from_tg = True`` unconditionally, without checking the prefix.)"""
        self._feed(_user_record("pokracuj v uklidu"))                  # terminal-started turn
        self.assertFalse(self.bridge._turn_from_tg)

        # Queued, but typed at the terminal too — no [TG].
        self._feed(_attachment_record("diky, zvladnu to sam"))
        self.assertFalse(self.bridge._turn_from_tg,
                          "a queued message without [TG] must not flip a terminal turn to TG")

        self.bridge._handle_event(Ev("text", text="Hotovo.", key="term-reply-2"))
        self.assertEqual(self.bridge.tg.sent, [])


class ResumePositionQueuedMessageTests(unittest.TestCase):
    """Criterion 6 — after a restart, ``_resume_position`` must recover the Telegram origin from
    a queued message, and land the rewind point in the RIGHT place: right after whichever record
    actually raised the flag — the queued attachment if IT raised it, the turn's own user record
    if the flag was already up before the queued message arrived."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.transcript = Path(self._tmp.name) / "transcript.jsonl"

    def tearDown(self):
        self._tmp.cleanup()

    def _resumed_bridge(self) -> AttachBridge:
        b = object.__new__(AttachBridge)
        b._reader = ClaudeCodeReader()
        b._origins = ("[TG]", "Telegram:")
        b._transcript = self.transcript
        b._tpos = self.transcript.stat().st_size
        b._turn_from_tg = False
        b._resume_position()
        return b

    def test_queued_tg_message_into_terminal_turn_moves_rewind_point_past_it(self):
        """The fix's OWN regression (caught by review): a queued TG message that RAISES the flag
        must move the rewind point to just after ITSELF, not leave it at the turn's own (terminal)
        user record — otherwise a restart re-forwards everything the agent wrote BEFORE the
        Telegram message ever joined, even though none of it should ever reach Telegram."""
        offsets = _write_transcript(self.transcript, [
            _user_record("pokracuj v uklidu"),             # terminal-started turn — offsets[0]
            _attachment_record("[TG] jsi tam<"),            # TG joins mid-turn    — offsets[1]
        ])

        b = self._resumed_bridge()

        self.assertTrue(b._turn_from_tg, "queued TG message must be recovered as TG origin")
        self.assertEqual(b._tpos, offsets[1],
                          "the rewind point must land right after the queued_command record, "
                          "not the turn's own (terminal) user record")

    def test_terminal_message_queued_into_tg_turn_does_not_move_rewind_point(self):
        """The mirror case: a queued message that does NOT raise the flag (the turn was already
        Telegram-originated) must NOT move the rewind point — text written between the turn's own
        user record and the queued line still belongs to the turn and must be re-read (and
        forwarded) after a restart, not skipped."""
        offsets = _write_transcript(self.transcript, [
            _user_record("[TG] ok, takže teď už vše běží?"),   # starts as a TG turn — offsets[0]
            _assistant_record("Mezitím pracuju dál."),          # written while the turn runs
            _attachment_record("diky, uz to vidim"),             # terminal message queued, no [TG]
        ])

        b = self._resumed_bridge()

        self.assertTrue(b._turn_from_tg)
        self.assertEqual(b._tpos, offsets[0],
                          "a queued message that doesn't raise the flag must not move the "
                          "rewind point — the text written before it still belongs to the turn")

    def test_resume_position_is_monotonic_for_a_non_tg_queued_message(self):
        """Same monotonicity as the live path (_handle_event), mirrored for the restart replay:
        a queued message with NO [TG] prefix must not be able to CLEAR an already-True Telegram
        origin either. (Mutation this catches: in the queued branch of _resume_position,
        assigning ``from_tg = ev.text.lstrip().startswith(self._origins)`` unconditionally
        instead of only ever raising it.)"""
        _write_transcript(self.transcript, [
            _user_record("[TG] ok, takže teď už vše běží?"),
            _assistant_record("Mezitím pracuju dál."),
            _attachment_record("diky, uz to vidim"),          # no [TG] — queued from the terminal
        ])

        b = self._resumed_bridge()

        self.assertTrue(b._turn_from_tg,
                         "a queued terminal message must not clear TG origin during resume")

    def test_malformed_record_does_not_crash_resume_position(self):
        """A record parse() can't handle (Claude Code writing a genuinely malformed line) must
        not crash the bridge's startup — it must be skipped, and a valid TG origin found
        elsewhere in the scanned window must still be recovered. (Mutation this catches: removing
        the try/except around the ``parse()`` call in ``_resume_position``.)"""
        _write_transcript(self.transcript, [
            _user_record("[TG] ok, takže teď už vše běží?"),
            _BROKEN_ASSISTANT_RECORD,
        ])

        b = self._resumed_bridge()          # must not raise

        self.assertTrue(b._turn_from_tg)

    def test_queued_message_without_tg_prefix_into_terminal_turn_stays_local_after_resume(self):
        """Mirror of the live-path test, for the restart replay. (Mutation this catches: the
        queued branch of ``_resume_position`` collapsing to ``if from_tg: continue`` — dropping
        the prefix check, so ANY queued message, terminal or not, raises the flag as soon as one
        queued message anywhere in the window happens to have raised it, or worse, raises it on
        its own.)"""
        _write_transcript(self.transcript, [
            _user_record("pokracuj v uklidu"),                  # terminal-started turn
            _attachment_record("diky, zvladnu to sam"),          # queued, no [TG] — terminal too
        ])

        b = self._resumed_bridge()

        self.assertFalse(b._turn_from_tg,
                          "a queued message without [TG] must not flip a terminal turn to TG "
                          "on resume either")


class ResumeAloneSetsTheBackstopBoundaryTests(unittest.TestCase):
    """Uncovered load-bearing line: ``_resume_position`` must set ``_tg_since`` ITSELF when it
    recovers a raised flag. ``_drain_transcript`` only updates ``_tg_since`` when the flag
    CHANGES during a live drain — after a resume-only restart (no drain call yet), nothing else
    will set it, so the backstop would fall back to the full 2 MB tail scan and read straight
    through the terminal part of the turn. (Mutation this catches: deleting
    ``self._tg_since = last_user_end if from_tg else 0`` at the end of ``_resume_position``.)"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.transcript = Path(self._tmp.name) / "transcript.jsonl"

    def tearDown(self):
        self._tmp.cleanup()

    def test_backstop_after_resume_alone_does_not_reach_before_the_queued_message(self):
        _write_transcript(self.transcript, [
            _user_record("pokracuj v uklidu"),                      # terminal-started turn
            _assistant_record("Nasel jsem heslo v .env: hunter2"),  # MUST stay local
            _attachment_record("[TG] jsi tam<"),                    # TG joins mid-turn, no reply yet
        ])

        b = _make_bridge_with_transcript(Path(self._tmp.name), self.transcript)
        b._tpos = self.transcript.stat().st_size

        b._resume_position()          # NOTE: no _drain_transcript() call in between — _tg_since
                                       # must come from resume itself, not from a later toggle
        self.assertTrue(b._turn_from_tg)
        b._turn_active.set()

        b._finish_turn()

        self.assertEqual(b.tg.sent, [], "the backstop must not reach back for the .env secret")


class TranscriptRotationResetsTheBackstopBoundaryTests(unittest.TestCase):
    """The already-fixed rotation bug (nulling ``_tg_since`` alongside ``_tpos`` when the file
    shrinks) end-to-end: after the transcript is replaced by a shorter one, a later Telegram
    turn's backstop reply must still be delivered — a stale ``_tg_since`` from the OLD, longer
    file must not silently swallow it."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.transcript = Path(self._tmp.name) / "transcript.jsonl"

    def tearDown(self):
        self._tmp.cleanup()

    def test_backstop_still_delivers_after_the_transcript_is_rotated(self):
        """(Mutation this catches: the rotation branch of ``_drain_transcript`` resetting only
        ``self._tpos = 0``, leaving a stale ``_tg_since`` that pointed into the old, longer file
        and now lands mid-line in the new one.)"""
        # Turn 1: a TG turn, padded so the file is bigger than turn 2's file below — this is
        # what makes `size < self._tpos` trip on the switch.
        _write_transcript(self.transcript, [
            _user_record("[TG] prvni dotaz, hodne dlouhy text kolem dokola, aby soubor byl vetsi"),
            _assistant_record("Odpoved na prvni dotaz."),
        ])
        b = _make_bridge_with_transcript(Path(self._tmp.name), self.transcript)
        b._drain_transcript()
        self.assertEqual(b.tg.sent, [(555, "Odpoved na prvni dotaz.")])

        # "Rotation": the file is replaced wholesale by a SHORTER one (new session/log) — the
        # exact condition _drain_transcript checks for (`size < self._tpos`). Last line has no
        # trailing newline, so the live drain doesn't pick up the reply — only the backstop can.
        _write_transcript(self.transcript, [
            _user_record("[TG] druhy dotaz"),
            _assistant_record("Odpoved na druhy dotaz."),
        ], trailing_newline=False)

        # New turn's own bookkeeping (normally done by the inbound loop's "TURN START" handling).
        b._turn_active.set()
        b._turn_text_sent = False

        b._drain_transcript()          # must detect the rotation and reset both cursors
        self.assertTrue(b._turn_from_tg)

        b._finish_turn()

        self.assertEqual(b.tg.sent,
                          [(555, "Odpoved na prvni dotaz."), (555, "Odpoved na druhy dotaz.")])


class BackstopBoundaryPastEndOfFileTests(unittest.TestCase):
    """``_last_assistant_text``'s explicit ``_tg_since > size`` guard. NOTE: Python's own
    ``file.seek()``/``.read()`` already returns ``b""`` when seeking past EOF, so for THIS
    scenario removing the guard does not change the method's return value — the guard's only
    observable effect here is the diagnostic log line (deliberately: "radši ticho s logem než
    únik" — silence with a log beats reaching for stale content). This test pins that log, since
    it's the only thing the mutation actually changes."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.transcript = Path(self._tmp.name) / "transcript.jsonl"

    def tearDown(self):
        self._tmp.cleanup()

    def test_boundary_past_eof_returns_none_and_logs_instead_of_silently_reading_the_tail(self):
        """(Mutation this catches: removing the ``if self._tg_since > size: ... return None``
        guard in ``_last_assistant_text``.)"""
        _write_transcript(self.transcript, [_assistant_record("neni videt")])
        b = _make_bridge_with_transcript(Path(self._tmp.name), self.transcript)
        b._tg_since = self.transcript.stat().st_size + 10_000     # deliberately past EOF

        with self.assertLogs("agent2telegram.attach", level="WARNING") as cm:
            result = b._last_assistant_text()

        self.assertIsNone(result)
        self.assertTrue(any("past EOF" in m for m in cm.output), cm.output)


class LiveDrainSkipsMalformedRecordsTests(unittest.TestCase):
    """The live counterpart of ``_resume_position``'s malformed-record guard."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.transcript = Path(self._tmp.name) / "transcript.jsonl"

    def tearDown(self):
        self._tmp.cleanup()

    def test_malformed_record_does_not_abort_the_rest_of_the_chunk(self):
        """A malformed line in the MIDDLE of a chunk must not swallow the rest of that same
        chunk — before this fix, ``_tpos`` had already moved past the whole chunk when the
        exception propagated, so anything after the bad line (including a ``queued_command``)
        was never read again, reproducing the original 2026-08-20 bug by another route.
        (Mutation this catches: removing the try/except around ``parse()`` in
        ``_drain_transcript``.)"""
        _write_transcript(self.transcript, [
            _user_record("pokracuj v uklidu"),          # terminal-started turn
            _BROKEN_ASSISTANT_RECORD,                     # malformed — must be skipped, not fatal
            _attachment_record("[TG] jsi tam<"),          # TG joins mid-turn, further down the chunk
            _assistant_record("odpoved"),
        ])

        b = _make_bridge_with_transcript(Path(self._tmp.name), self.transcript)

        b._drain_transcript()          # must not raise

        self.assertTrue(b._turn_from_tg)
        self.assertEqual(b.tg.sent, [(555, "odpoved")])


class ResumeThenDrainDoesNotLeakContentBeforeTheQueuedMessageTests(unittest.TestCase):
    """The fix's own regression end-to-end: after a restart, replaying a turn a Telegram message
    joined mid-flight must forward ONLY what the agent wrote AFTER that message."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.transcript = Path(self._tmp.name) / "transcript.jsonl"

    def tearDown(self):
        self._tmp.cleanup()

    def test_secret_written_before_the_queued_tg_message_is_never_forwarded_after_restart(self):
        _write_transcript(self.transcript, [
            _user_record("pokracuj v uklidu"),                             # terminal start
            _assistant_record("Nasel jsem heslo v .env: hunter2"),         # MUST stay local
            _attachment_record("[TG] jsi tam<"),                           # TG joins mid-turn
            _assistant_record("Jo, jedu dal."),                            # MUST be forwarded
        ])

        b = _make_bridge_with_transcript(Path(self._tmp.name), self.transcript)
        b._tpos = self.transcript.stat().st_size     # as if we attached at EOF, then restarted

        b._resume_position()
        b._drain_transcript()

        self.assertEqual(b.tg.sent, [(555, "Jo, jedu dal.")])


class BackstopDoesNotReachBeforeTheTelegramBoundaryTests(unittest.TestCase):
    """The turn-end backstop (_finish_turn -> _last_assistant_text) must never read before
    ``_tg_since`` — otherwise a message queued at the very end of a terminal turn would get that
    turn's last (local, possibly secret) sentence back as its "answer". But it must still deliver
    a genuine reply the live forwarding path missed."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.transcript = Path(self._tmp.name) / "transcript.jsonl"

    def tearDown(self):
        self._tmp.cleanup()

    def test_backstop_sends_nothing_when_agent_wrote_nothing_after_the_queued_message(self):
        _write_transcript(self.transcript, [
            _user_record("pokracuj v uklidu"),
            _assistant_record("Nasel jsem heslo v .env: hunter2"),
            _attachment_record("[TG] jsi tam<"),           # agent hasn't answered it yet
        ])

        b = _make_bridge_with_transcript(Path(self._tmp.name), self.transcript)
        b._drain_transcript()
        self.assertFalse(b._turn_text_sent)
        b._turn_active.set()      # simulate a turn in flight (normally set by the inbound loop)

        b._finish_turn()

        self.assertEqual(b.tg.sent, [], "the backstop must not reach back for the .env secret")

    def test_backstop_still_delivers_a_reply_the_live_path_missed(self):
        # Last line has NO trailing newline — as if it was written but not yet fully flushed
        # when the drain ran, so the live path never saw it as a complete line.
        _write_transcript(self.transcript, [
            _user_record("pokracuj v uklidu"),
            _assistant_record("Nasel jsem heslo v .env: hunter2"),
            _attachment_record("[TG] jsi tam<"),
            _assistant_record("Diky, uz to vidim."),
        ], trailing_newline=False)

        b = _make_bridge_with_transcript(Path(self._tmp.name), self.transcript)
        b._drain_transcript()                  # only the first 3 (complete) lines are drained
        self.assertFalse(b._turn_text_sent)
        b._turn_active.set()

        b._finish_turn()

        self.assertEqual(b.tg.sent, [(555, "Diky, uz to vidim.")])


if __name__ == "__main__":
    unittest.main()
