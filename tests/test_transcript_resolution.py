"""Resolving the newest transcript must ignore subagent transcripts.

Claude Code stores subagent transcripts under "<conversation>/subagents/". A running subagent
writes far more often than the main conversation, so by mtime it always wins the "newest" race.
The bridge then tails the subagent and the summary the agent writes to the user is never
forwarded — from the outside it looks as if the agent silently stopped reporting.
"""
import os
import tempfile
import unittest
from pathlib import Path

from agent2telegram.attach import AttachBridge

_newest_under = AttachBridge._newest_under


def _touch(path: Path, mtime: float) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}")
    os.utime(path, (mtime, mtime))
    return path


class NewestTranscriptTests(unittest.TestCase):
    def test_a_newer_subagent_transcript_never_wins(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            main = _touch(base / "conversation.jsonl", 1000.0)
            _touch(base / "conversation" / "subagents" / "agent-abc.jsonl", 9000.0)

            self.assertEqual(_newest_under(base), main,
                             "a subagent transcript won the newest race — replies would be lost")

    def test_the_newest_main_transcript_still_wins(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            _touch(base / "older.jsonl", 1000.0)
            newer = _touch(base / "newer.jsonl", 2000.0)

            self.assertEqual(_newest_under(base), newer)

    def test_only_subagent_transcripts_means_no_match(self):
        """Better nothing than the wrong file: tailing a subagent forwards the wrong text."""
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            _touch(base / "conversation" / "subagents" / "agent-abc.jsonl", 9000.0)

            self.assertIsNone(_newest_under(base))

    def test_a_directory_merely_named_subagents_elsewhere_is_not_special(self):
        """The filter matches the path segment, not a substring of a file name."""
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            kept = _touch(base / "subagents-notes.jsonl", 5000.0)

            self.assertEqual(_newest_under(base), kept)


if __name__ == "__main__":
    unittest.main()
