"""Regression tests measuring the three vadas our fork used to carry against UPSTREAM behaviour.

Our fork had its own tests/test_queued_message.py (875 lines) that fixed the same three findings
a different way. That file is gone now that the repository is back on upstream — it also broke
against upstream for the wrong reason: it built `cfg` as a bare `types.SimpleNamespace`, which is
missing fields upstream reads (e.g. `file_marker`), so 19 of 25 failures were the harness, not a
real defect.

This file drives the real `AttachBridge` (built the same way as tests/test_v2_durability.py's
`_bridge()`) through the real `ClaudeCodeReader`, with fixture transcript records copied verbatim
(anonymised) from a real Claude Code transcript, and asserts on what actually leaves the bridge
(`bridge.tg.sent`) or on the turn-origin flag the bridge itself uses to decide whether to answer —
never on private field names that could be renamed without changing behaviour.

The three findings:

  1. (2026-08-20) A message delivered into an ALREADY-RUNNING turn is queued by Claude Code and
     logged only as `type: "attachment"` / `attachment.type == "queued_command"` — never as
     `type: "user"`. The old bridge derived Telegram origin purely from `type: "user"` records, so
     `_turn_from_tg` stayed False and the reply for that message was silently dropped.

  2. (2026-08-23) Claude Code files a tool result under `type: "user"` too. The old bridge used the
     LAST `type: "user"` text to decide the turn's origin, so a tool result landing mid-turn could
     silently reclassify a live Telegram turn as terminal-originated — the final answer was never
     forwarded, the backstop never ran, and nothing was logged.

  3. (2026-08-11) Claude Code writes subagent transcripts under "<conversation>/subagents/". A
     running subagent writes far more often than the main conversation, so by mtime it always won
     the "newest transcript" race — the bridge then tailed the subagent and the summary written
     for the user was never forwarded. tests/test_transcript_resolution.py already pins the low-
     level filter (`_newest_under`); this file drives it through the method the bridge actually
     calls at startup (`_newest_claude` / `_resolve_transcript`) on the real
     `~/.claude/projects/<cwd-with-slashes-as-dashes>/` directory shape.

No network, no tmux, no real files outside `tempfile`.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent2telegram import readers
from tests.test_v2_durability import _bridge, _msg

# --------------------------------------------------------------------------------------
# Fixture records for findings 1 and 2 — copied VERBATIM (all keys, all nesting) from a real
# Claude Code transcript. The only values changed are personal fields (cwd, sessionId/session_id,
# uuid/parentUuid/promptId/source_uuid) — replaced with placeholders of the same shape — plus,
# where a template is reused below, the content/prompt/text and its timestamp.
# `_assistant_record()` is synthetic: assistant records are unrelated to either bug and were
# never captured for them; only the shapes below needed to be literal.
# --------------------------------------------------------------------------------------
_CWD = "/workdir/example-project"
_SID = "00000000-0000-0000-0000-000000000000"

# Real transcript, line 44: type=queue-operation / enqueue — written the instant the Telegram
# message is typed into the already-running turn.
_QUEUE_ENQUEUE = {
    "type": "queue-operation",
    "operation": "enqueue",
    "timestamp": "2026-08-20T06:53:09.329Z",
    "sessionId": _SID,
    "content": "[TG] are you there<",
}

# Real transcript, line 49: type=queue-operation / remove — written once the queued text is
# pulled off the queue into the running turn (same text, different operation).
_QUEUE_REMOVE = {
    "type": "queue-operation",
    "operation": "remove",
    "timestamp": "2026-08-20T06:53:13.774Z",
    "sessionId": _SID,
    "content": "[TG] are you there<",
}


def _attachment_record(prompt: str, *, uuid="11111111-1111-1111-1111-111111111111",
                        parent_uuid="22222222-2222-2222-2222-222222222222",
                        source_uuid="33333333-3333-3333-3333-333333333333",
                        timestamp="2026-08-20T06:53:09.329Z") -> dict:
    """Real transcript, line 51: type=attachment / attachment.type=queued_command — the record
    a message queued mid-turn is filed under. Never a `type: "user"` record."""
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
# housekeeping attachment Claude Code writes after nearly every turn, NOT a user message.
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


def _user_record(content: str, *, uuid="44444444-4444-4444-4444-444444444444",
                  parent_uuid="55555555-5555-5555-5555-555555555555",
                  prompt_id="66666666-6666-6666-6666-666666666666",
                  timestamp="2026-08-20T06:59:12.293Z") -> dict:
    """Real transcript, line 151: a classic `type: "user"` record with the [TG] marker — a turn
    that STARTS from Telegram."""
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
    """Synthetic (not from a real transcript) — the shape ClaudeCodeReader.parse() expects for
    assistant text. No `uuid` key on purpose: the reader falls back to hashing the text for its
    dedup key, so distinct texts never collide."""
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}


# Shape taken verbatim from a real transcript (2026-08-23, an image the agent read mid-turn),
# trimmed to the fields that matter — same fixture as tests/test_tool_result_not_a_prompt.py.
_TOOL_RESULT_IMAGE = {
    "type": "user",
    "toolUseResult": {"type": "image"},
    "message": {"content": [{"tool_use_id": "toolu_011EtqadNnd41qkrYeR45kvJ",
                             "type": "tool_result",
                             "content": "[Image: original 3300x1880, displayed at 2000x1139.]"}]},
}

# From the SAME real incident (2026-08-23 commit message, 558674d): of the three records
# involved, two were proper tool results like the one above (caught by ClaudeCodeReader's
# `_is_tool_result` — neither `toolUseResult` nor a `tool_result` content block is present here),
# but the THIRD was a plain-string `type: "user"` record with nothing marking it as
# machine-generated at all. That third shape is the one that actually needs the OTHER layer of
# the fix — the turn-active guard in `_handle_event` — because no reader-level filter can tell it
# apart from something a person typed.
_IMAGE_NOTE_PLAIN_STRING = {
    "type": "user",
    "message": {"role": "user", "content": "[Image: 3300x1880, displayed at 2000x1139.]"},
}


def _write_transcript(path: Path, records: list) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _regrese_bridge(td):
    """Same hand-assembled bridge as test_v2_durability._bridge(), wired for Claude Code with a
    real reader and a real transcript cursor — needed because _drain_transcript()/_newest_claude()
    are never exercised by the existing hand-built harnesses (they all stub _drain_transcript out
    or leave _transcript as None)."""
    b = _bridge(td)
    b.cfg.agent = "claude-code"
    b._reader = readers.for_agent(b.cfg.agent)
    b._origins = tuple({p for p in (b.cfg.origin_prefix.strip(), "Telegram:", "[TG]") if p})
    b._signal = Path(td) / "signal.txt"
    b._tpos = 0
    b._use_durable_outbox = False   # assert directly on bridge.tg.sent, like test_voice.py does
    return b


# ========================================================================================
# Finding 1 — a message delivered into an already-running turn must still get an answer
# ========================================================================================
class QueuedMidTurnMessageGetsAnAnswerTests(unittest.TestCase):
    """2026-08-20: Claude Code never files a mid-turn-queued message as `type: "user"` — only as
    `type: "attachment"` / `attachment.type == "queued_command"`, once it is finally picked up.
    A bridge that infers Telegram origin only from `type: "user"` records would never see that
    origin and the reply would be silently dropped. Upstream fixes this a different way:
    `AttachBridge._handle()` calls `_begin_turn()` (which sets `_turn_from_tg = True`) the instant
    the message is injected — before anything about it exists in the transcript at all.

    The real incident's turn had NOT started from Telegram: František typed straight into the
    tmux pane (or a previous turn was still finishing) when the Telegram message landed in the
    middle of it. That distinction matters for the test, not just the narrative: if the FIRST
    message of the turn also came from Telegram, `_turn_from_tg` is already True before the
    second `_handle()` call ever runs, and asserting it is still True afterwards proves nothing —
    it would stay True even under a bridge that only stamps the origin on a turn's first message
    and never on one joining an already-running turn. The turn here is therefore started as a
    turn that is NOT Telegram-originated (`_turn_active` set, `_turn_from_tg` left False) before
    the Telegram message ever arrives — the bridge has no other way to observe a purely local
    turn starting, since only `_begin_turn()`/`_inject()` ever set `_turn_active`, and both run
    only from `_handle()`."""

    def test_second_message_delivered_mid_turn_still_gets_answered(self):
        with tempfile.TemporaryDirectory() as td:
            b = _regrese_bridge(td)
            transcript = Path(td) / "transcript.jsonl"
            b._transcript = transcript

            # A turn is already running, but it did NOT start from Telegram — the state a
            # locally started turn (typed into tmux, or still finishing earlier local work)
            # leaves the bridge in.
            b._turn_active.set()
            b._turn_from_tg = False

            # A Telegram message now lands in the middle of that running, non-Telegram turn.
            # Claude Code QUEUES it — it will show up in the transcript only as
            # `type: "attachment"` / `attachment.type == "queued_command"`, never as
            # `type: "user"`. The bridge still runs its normal inbound path for it (this is the
            # fix: origin is stamped at injection time, not derived later from the transcript).
            b._handle(_msg(1, "[TG] are you still there"))
            self.assertTrue(
                b._turn_from_tg,
                "a Telegram message joining an ALREADY-RUNNING turn that did NOT start from "
                "Telegram must still mark it Telegram-originated from here on — origin is "
                "stamped at injection time in _begin_turn(), not derived later from the "
                "transcript, precisely because the queued message never gets its own "
                "type:\"user\" record to derive it from",
            )

            # What Claude Code actually writes to disk once it catches up: the queue
            # bookkeeping, a housekeeping total_tokens_reminder attachment, the queued prompt
            # ONLY as an attachment — never as "user" — followed by the assistant's answer.
            _write_transcript(transcript, [
                _QUEUE_ENQUEUE,
                _TOTAL_TOKENS_REMINDER,
                _QUEUE_REMOVE,
                _attachment_record("[TG] are you still there"),
                _assistant_record("[tg] yes, still here"),
            ])

            b._drain_transcript()

            self.assertIn(
                "yes, still here", "\n".join(b.tg.sent),
                "the reply for the mid-turn-queued message never reached Telegram — Claude Code "
                "shows the turn as answered, but the user saw nothing",
            )
            # NOT re-asserting `b._turn_from_tg` here on purpose: `ClaudeCodeReader.parse()`
            # yields NOTHING at all for queue-operation and attachment records (see
            # `test_queue_bookkeeping_and_housekeeping_records_carry_no_reader_event` below), so
            # draining them can never touch `_turn_from_tg` either way — an assert repeating the
            # one above would look like it measured that, and would never be able to fail.

    def test_queue_bookkeeping_and_housekeeping_records_carry_no_reader_event(self):
        """Why the assert above doesn't need to (and can't usefully) re-check `_turn_from_tg`
        after draining queue/housekeeping records: `ClaudeCodeReader.parse()` produces NO event
        at all for `type: "queue-operation"` or `type: "attachment"` records — its `parse()`
        only recognises `type: "user"` and `type: "assistant"`. This is not incidental, it is the
        actual reason the fix stamps Telegram origin at injection time (`_begin_turn()`) instead
        of deriving it from the transcript: for a queued message there is structurally nothing in
        the transcript for a reader to derive it FROM."""
        reader = readers.for_agent("claude-code")
        for rec in (_QUEUE_ENQUEUE, _QUEUE_REMOVE, _TOTAL_TOKENS_REMINDER,
                    _attachment_record("[TG] are you still there")):
            self.assertEqual(
                list(reader.parse(rec)), [],
                f"expected no reader event for record type {rec.get('type')!r} — if this ever "
                "yields something, _handle_event() may act on it and the turn-origin reasoning "
                "above (and in _begin_turn()) needs to be re-examined",
            )

    def test_backstop_still_delivers_if_no_interim_text_was_forwarded(self):
        """Same scenario, but the agent produces only a FINAL answer (no interim [tg] text) and
        the Stop hook fires — i.e. the delivery path a silent turn actually depends on."""
        with tempfile.TemporaryDirectory() as td:
            b = _regrese_bridge(td)
            transcript = Path(td) / "transcript.jsonl"
            b._transcript = transcript

            b._handle(_msg(1, "[TG] first question"))
            b._handle(_msg(2, "[TG] second question, are you still there"))

            _write_transcript(transcript, [
                _user_record("[TG] first question"),
                _QUEUE_ENQUEUE,
                _attachment_record("[TG] second question, are you still there"),
                _QUEUE_REMOVE,
                _assistant_record("[tg] final answer for both"),
            ])

            # Simulates the Claude Stop-hook path: drain whatever landed, then finish the turn.
            b._drain_transcript()
            b._finish_turn()

            self.assertIn("final answer for both", "\n".join(b.tg.sent),
                          "turn ended and the mid-turn-queued message's answer never arrived")
            self.assertFalse(b._turn_active.is_set())


# ========================================================================================
# Finding 2 — a tool result mid-turn must not cancel the Telegram origin of a live turn
# ========================================================================================
class ToolResultMidTurnDoesNotCancelTelegramOriginTests(unittest.TestCase):
    """2026-08-23: Claude Code files tool results under `type: "user"` too. A bridge that reads
    the turn's origin from the LAST `type: "user"` text would see the tool result's text (no [TG]
    prefix) and silently reclassify a live Telegram turn as terminal-originated — the final
    answer then never forwards, the backstop never runs, and nothing is logged.
    tests/test_tool_result_not_a_prompt.py pins this at the reader/`_handle_event` unit level;
    this drives the SAME scenario through the real bridge end to end."""

    def test_final_answer_after_a_mid_turn_tool_result_still_reaches_telegram(self):
        with tempfile.TemporaryDirectory() as td:
            b = _regrese_bridge(td)
            transcript = Path(td) / "transcript.jsonl"
            b._transcript = transcript

            b._handle(_msg(1, "[TG] what does this screenshot show?"))
            self.assertTrue(b._turn_from_tg)

            _write_transcript(transcript, [
                _user_record("[TG] what does this screenshot show?"),
                _assistant_record("[tg] let me look at the image"),
                _TOOL_RESULT_IMAGE,                # Claude reads the image mid-turn
                _assistant_record("[tg] it shows the dashboard after the deploy"),
            ])

            b._drain_transcript()
            b._finish_turn()

            joined = "\n".join(b.tg.sent)
            self.assertIn(
                "it shows the dashboard after the deploy", joined,
                "a tool result mid-turn reclassified the live Telegram turn as local — the final "
                "answer never left the bridge, the backstop never ran, and nothing was logged",
            )
            self.assertEqual(
                b.tg.sent.count("it shows the dashboard after the deploy"), 1,
                "must be delivered exactly once — a second send would mean the backstop fired "
                "as well as the normal transcript path (2026-08-02 duplicate-send class of bug)",
            )

    def test_tool_result_arriving_before_any_reply_does_not_stop_the_final_answer(self):
        """The tool result is the very first thing after the prompt (no interim text at all
        yet) — the narrowest window in which "last user text" reasoning would misfire."""
        with tempfile.TemporaryDirectory() as td:
            b = _regrese_bridge(td)
            transcript = Path(td) / "transcript.jsonl"
            b._transcript = transcript

            b._handle(_msg(1, "[TG] check this file for me"))

            _write_transcript(transcript, [
                _user_record("[TG] check this file for me"),
                _TOOL_RESULT_IMAGE,
                _assistant_record("[tg] looks fine, nothing to fix"),
            ])

            b._drain_transcript()

            self.assertIn("looks fine, nothing to fix", "\n".join(b.tg.sent),
                          "answer lost when a tool result preceded any interim reply")

    def test_plain_string_machine_note_mid_turn_does_not_cancel_the_origin_either(self):
        """The commit that fixed this (558674d) is explicit that the reader-level filter alone
        would NOT have fixed the real incident: of the three records involved, two were proper
        tool results, but the third was a plain-string `type: "user"` record
        (`[Image: 3300x1880 …]`) with nothing marking it as machine-generated — indistinguishable
        from something a person typed by shape alone. Only the OTHER layer of the fix (a record
        without the origin prefix may never downgrade a turn that is already RUNNING) catches
        this one. This is the sharpest test of the two: it isolates exactly the record the
        reader-level filter cannot help with."""
        with tempfile.TemporaryDirectory() as td:
            b = _regrese_bridge(td)
            transcript = Path(td) / "transcript.jsonl"
            b._transcript = transcript

            b._handle(_msg(1, "[TG] what does this screenshot show?"))
            self.assertTrue(b._turn_from_tg)

            _write_transcript(transcript, [
                _user_record("[TG] what does this screenshot show?"),
                _assistant_record("[tg] let me look at the image"),
                _IMAGE_NOTE_PLAIN_STRING,          # indistinguishable from a typed prompt by shape
                _assistant_record("[tg] it shows the dashboard after the deploy"),
            ])

            b._drain_transcript()

            self.assertTrue(
                b._turn_from_tg,
                "a plain machine-written note with no origin prefix downgraded a RUNNING "
                "Telegram turn to local — the reader-level tool-result filter cannot see this "
                "record at all, so only the turn-active guard can save it",
            )
            self.assertIn(
                "it shows the dashboard after the deploy", "\n".join(b.tg.sent),
                "the final answer was lost because the turn was silently reclassified as local",
            )


# ========================================================================================
# Finding 3 — a busier subagent transcript must not be tailed instead of the main conversation
# ========================================================================================
def _touch(path: Path, mtime: float) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}")
    os.utime(path, (mtime, mtime))
    return path


class NewestClaudeTranscriptResolutionTests(unittest.TestCase):
    """2026-08-11: Claude Code writes subagent transcripts under "<conversation>/subagents/". A
    running subagent writes far more often than the main conversation and always wins the
    "newest" race by mtime — the bridge then tails the subagent and the summary written for the
    user is never forwarded. tests/test_transcript_resolution.py already pins the low-level
    filter (`_newest_under`) on a synthetic base directory; this drives the SAME scenario through
    the methods the bridge actually calls (`_newest_claude` / `_resolve_transcript`) on the real
    `~/.claude/projects/<cwd-with-slashes-as-dashes>/` directory Claude Code writes to."""

    def test_newest_claude_prefers_the_main_transcript_over_a_busier_subagent(self):
        with tempfile.TemporaryDirectory() as home:
            cwd = "/workdir/example-project"
            project_dir = Path(home) / ".claude" / "projects" / cwd.replace("/", "-")
            main = _touch(project_dir / "conversation.jsonl", 1000.0)
            _touch(project_dir / "conversation" / "subagents" / "agent-x.jsonl", 9000.0)

            with tempfile.TemporaryDirectory() as td, \
                    mock.patch.object(Path, "home", return_value=Path(home)):
                b = _regrese_bridge(td)
                b._session_cwd = lambda: cwd
                resolved = b._newest_claude()

            self.assertEqual(
                resolved, main,
                "the bridge picked a subagent transcript over the main conversation — the "
                "agent's summary for the user would never be forwarded",
            )

    def test_resolve_transcript_reaches_the_same_result_for_claude_code(self):
        """_resolve_transcript() is the actual entry point used at startup and on re-resolve."""
        with tempfile.TemporaryDirectory() as home:
            cwd = "/workdir/another-project"
            project_dir = Path(home) / ".claude" / "projects" / cwd.replace("/", "-")
            main = _touch(project_dir / "session.jsonl", 500.0)
            _touch(project_dir / "session" / "subagents" / "tester.jsonl", 8000.0)

            with tempfile.TemporaryDirectory() as td, \
                    mock.patch.object(Path, "home", return_value=Path(home)):
                b = _regrese_bridge(td)
                b.cfg.transcript_path = ""     # "" / "auto" → auto-detect
                b._session_cwd = lambda: cwd
                resolved = b._resolve_transcript()

            self.assertEqual(resolved, main)

    def test_only_a_subagent_transcript_exists_means_nothing_is_tailed(self):
        """Better silence than the wrong file: tailing a subagent forwards the wrong text as if
        it were the agent's own reply to the user."""
        with tempfile.TemporaryDirectory() as home:
            cwd = "/workdir/only-subagent-project"
            project_dir = Path(home) / ".claude" / "projects" / cwd.replace("/", "-")
            _touch(project_dir / "conversation" / "subagents" / "agent-x.jsonl", 9000.0)

            with tempfile.TemporaryDirectory() as td, \
                    mock.patch.object(Path, "home", return_value=Path(home)):
                b = _regrese_bridge(td)
                b._session_cwd = lambda: cwd
                resolved = b._newest_claude()

            self.assertIsNone(resolved)


if __name__ == "__main__":
    unittest.main()
