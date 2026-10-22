"""Tests for the globally-registered Claude Code Stop hook routing a turn end to the right
bridge. Before this (22. 10. 2026), the hook marked every bridge without a session guard — a
foreign session's turn end closed a Telegram turn that was still being written. Now each bridge
publishes the transcript it tails, and the hook marks only the bridge whose published transcript
matches the payload's (falling back to the old guard-based guess only for bridges that published
nothing)."""
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from agent2telegram import stop_hook
from agent2telegram.attach import AttachBridge


class SelectConfigsTests(unittest.TestCase):
    """Unit tests for the pure routing function — no filesystem involved."""

    def test_foreign_publisher_with_different_transcript_never_marked_even_without_guard(self):
        """The 22. 10. bug: a bridge that published a DIFFERENT transcript and has no session
        guard must still be left out — it is definitely not this turn's bridge."""
        cfgs = [{"signal_file": "/a/signal", "_published": "/other/project/transcript.jsonl"}]
        result = stop_hook._select_configs(cfgs, "/mine/project/transcript.jsonl")
        self.assertEqual(result, [])

    def test_exact_match_wins_over_same_directory_match(self):
        """Two bridges publish transcripts in the same directory; only the one whose published
        transcript is byte-for-byte the payload's must be marked."""
        want = "/state/dir/transcript-123.jsonl"
        exact = {"_published": want}
        same_dir_only = {"_published": "/state/dir/transcript-999.jsonl"}
        result = stop_hook._select_configs([exact, same_dir_only], want)
        self.assertEqual(result, [exact])

    def test_sibling_transcript_in_same_directory_is_not_marked(self):
        """The directory-match branch was dropped in round 2: `claude -p` runs from scripts
        (zeptej-se-lenky, lenka-katalog-api, ai-novinky) and two overlapping sessions in one cwd
        all publish into the same project directory — matching on it was the original bug with
        one more step. Only an exact realpath match may mark a publishing bridge."""
        sibling = {"_published": "/state/dir/transcript-old.jsonl"}
        result = stop_hook._select_configs([sibling], "/state/dir/transcript-new.jsonl")
        self.assertEqual(result, [])

    def test_empty_publication_is_never_marked_even_as_a_guess(self):
        """An empty published file means 'this bridge routes by path, nothing resolved yet' — it
        must never be marked, not even by falling back to the old guard-based guess."""
        empty_publisher = {"_published": "", "claude_session_id": "abc123"}
        result = stop_hook._select_configs([empty_publisher], "/x/abc123-session.jsonl")
        self.assertEqual(result, [])

    def test_without_publishers_old_guess_behavior_is_preserved(self):
        """No bridge published anything (e.g. an older build): fall back to the session guard,
        and only to all guard-less configs when no guard matches."""
        guard_matches = {"claude_session_id": "abc123"}
        guard_other = {"claude_session_id": "zzz999"}
        guardless_1 = {}
        guardless_2 = {}

        with self.subTest("a matching guard wins over guard-less configs"):
            result = stop_hook._select_configs(
                [guard_matches, guard_other, guardless_1],
                "/x/abc123-session.jsonl",
            )
            self.assertEqual(result, [guard_matches])

        with self.subTest("no guard matches -> every guard-less config is marked"):
            result = stop_hook._select_configs(
                [guard_other, guardless_1, guardless_2],
                "/x/abc123-session.jsonl",
            )
            self.assertEqual(result, [guardless_1, guardless_2])

    def test_foreign_publisher_plus_unpublished_config_marks_only_unpublished(self):
        """One bridge published someone else's transcript (excluded outright), one published
        nothing — only the one that published nothing is still eligible for the old guess, and
        since it carries no guard, it gets marked."""
        foreign = {"_published": "/foreign/transcript.jsonl"}
        unpublished = {}
        result = stop_hook._select_configs([foreign, unpublished], "/mine/transcript.jsonl")
        self.assertEqual(result, [unpublished])


class MainIntegrationTests(unittest.TestCase):
    """Integration through stop_hook.main() over a real config directory and real signal files."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self._old_env = os.environ.get("AGENT2TELEGRAM_CONFIG")
        os.environ["AGENT2TELEGRAM_CONFIG"] = str(self.root / "config.json")
        self.addCleanup(self._restore_env)
        self._old_stdin = sys.stdin

    def _restore_env(self):
        if self._old_env is None:
            os.environ.pop("AGENT2TELEGRAM_CONFIG", None)
        else:
            os.environ["AGENT2TELEGRAM_CONFIG"] = self._old_env

    def tearDown(self):
        sys.stdin = self._old_stdin

    def _write_cfg(self, name: str, signal_file: Path) -> None:
        (self.root / name).write_text(json.dumps({"signal_file": str(signal_file)}), "utf-8")

    def test_main_marks_only_bridge_whose_published_transcript_matches_payload(self):
        """Bridge A tails transcript X, bridge B tails transcript Y; a Stop-hook payload for X
        must flip turn_end for A only — B's Telegram turn must stay open."""
        signal_a = self.root / "a" / "signal"
        signal_b = self.root / "b" / "signal"
        self._write_cfg("a.json", signal_a)
        self._write_cfg("b.json", signal_b)
        transcript_x = str(self.root / "sessions" / "x.jsonl")
        transcript_y = str(self.root / "sessions" / "y.jsonl")
        (signal_a.parent).mkdir(parents=True)
        (signal_b.parent).mkdir(parents=True)
        (signal_a.parent / "transcript").write_text(transcript_x, "utf-8")
        (signal_b.parent / "transcript").write_text(transcript_y, "utf-8")

        sys.stdin = io.StringIO(json.dumps({"transcript_path": transcript_x}))
        stop_hook.main()

        marker_a = signal_a.parent / "turn_end"
        marker_b = signal_b.parent / "turn_end"
        self.assertTrue(marker_a.exists(), "bridge watching the payload's transcript must be marked")
        self.assertFalse(marker_b.exists(), "bridge watching a different transcript must stay untouched")
        # _mark derives the marker path from signal_file, not from any other config field.
        self.assertEqual(marker_a, Path(signal_a).parent / "turn_end")

    def test_corrupted_transcript_file_does_not_crash_and_others_route_correctly(self):
        """A `transcript` file with an embedded NUL (e.g. a write caught mid-flush) must not
        break routing for every bridge — main() must still exit cleanly and still mark the
        bridge whose published transcript is intact and matches the payload."""
        signal_broken = self.root / "broken" / "signal"
        signal_ok = self.root / "ok" / "signal"
        self._write_cfg("broken.json", signal_broken)
        self._write_cfg("ok.json", signal_ok)
        transcript_x = str(self.root / "sessions" / "x.jsonl")
        signal_broken.parent.mkdir(parents=True)
        signal_ok.parent.mkdir(parents=True)
        (signal_broken.parent / "transcript").write_bytes(b"/some\x00path.jsonl")
        (signal_ok.parent / "transcript").write_text(transcript_x, "utf-8")

        sys.stdin = io.StringIO(json.dumps({"transcript_path": transcript_x}))
        stop_hook.main()  # must not raise

        self.assertTrue((signal_ok.parent / "turn_end").exists())
        self.assertFalse((signal_broken.parent / "turn_end").exists())


class PublishTranscriptTests(unittest.TestCase):
    """Tests for AttachBridge._publish_transcript, built without full __init__ (tmux/transcript
    init is irrelevant here — only _signal and _transcript matter), as in test_marker.py."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def _bridge(self):
        return object.__new__(AttachBridge)

    def test_writes_absolute_transcript_path_for_the_hook_to_read(self):
        b = self._bridge()
        b._signal = self.root / "signal"
        b._transcript = self.root / "sessions" / "current.jsonl"
        b._publish_transcript()
        published = self.root / "transcript"
        self.assertTrue(published.exists())
        content = published.read_text("utf-8")
        self.assertTrue(os.path.isabs(content))
        self.assertEqual(content, os.path.realpath(b._transcript))

    def test_writes_empty_file_when_transcript_is_none(self):
        """No transcript resolved yet (e.g. right after a session restart): the published file is
        never removed (its mere existence means 'route by path, don't guess about me'), so it is
        overwritten with an empty body instead — the hook reads that as 'nothing of mine yet' and
        never marks this bridge, rather than falling back to guessing."""
        b = self._bridge()
        b._signal = self.root / "signal"
        published = self.root / "transcript"
        published.parent.mkdir(parents=True, exist_ok=True)
        published.write_text("/stale/old.jsonl", "utf-8")
        b._transcript = None
        b._publish_transcript()
        self.assertTrue(published.exists())
        self.assertEqual(published.read_text("utf-8"), "")

    def test_noop_and_no_exception_without_a_signal_file(self):
        """A bridge configured with no signal_file (no Stop-hook wiring) must not try to publish
        anywhere and must not raise."""
        b = self._bridge()
        b._signal = None
        b._transcript = self.root / "sessions" / "current.jsonl"
        b._publish_transcript()  # must not raise
        self.assertFalse((self.root / "transcript").exists())


if __name__ == "__main__":
    unittest.main()
