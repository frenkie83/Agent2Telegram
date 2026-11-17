"""Tests for progress-marker stripping — the routing token must never leak into the chat."""
import unittest

from agent2telegram.attach import AttachBridge


def _strip(text, marker="[TG]"):
    # Build a bare instance without the full tmux/transcript init — _strip_marker only needs _marker.
    b = object.__new__(AttachBridge)
    b._marker = marker
    return b._strip_marker(text)


class StripMarkerTests(unittest.TestCase):
    def test_leading_marker_removed(self):
        self.assertEqual(_strip("[TG] hello"), "hello")

    def test_marker_on_a_later_line_removed(self):
        """Narration first, then a [TG] reply line — the stray marker must not survive (the
        2026-06-20 'orphaned [tg]' bug)."""
        out = _strip("Done. Here is the answer:\n\n[TG] 🐱 All good, it works.")
        self.assertNotIn("[TG]", out)
        self.assertIn("🐱 All good, it works.", out)

    def test_case_insensitive(self):
        self.assertEqual(_strip("[tg] hi"), "hi")
        self.assertEqual(_strip("[Tg] hi"), "hi")

    def test_no_marker_unchanged(self):
        self.assertEqual(_strip("just text"), "just text")

    def test_marker_inside_line_is_left_alone(self):
        """Only a line-START marker is a routing token; one mid-sentence is real content."""
        self.assertEqual(_strip("see the [TG] flag"), "see the [TG] flag")


if __name__ == "__main__":
    unittest.main()
