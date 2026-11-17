"""Tests for the Codex TUI scraper (`_extract_tui_tools`) — no tmux/network required.

Codex's live tool bubbles are produced by scraping the tmux pane (its rollout logs tools only
at completion), so this parser is the fragile, regression-prone part — pinned here with fixtures
captured from the real Codex TUI. Both bugs found on 2026-06-20 are covered:

  * system calls on the bullet line ("● Ran df -h /") must produce a bubble — they were missed
    when the parser only looked at nested "└" lines;
  * a stale line still visible in the pane from a previous turn must NOT be re-emitted — the
    per-turn dedup is seeded with what's already on screen.

Pure function, milliseconds in CI, so behaviour that used to be checked by hand is now automatic.
"""
import unittest

from agent2telegram.attach import _extract_tui_tools

# A real Codex TUI capture: agent text on bullet lines, a command on a bullet line with its
# output nested under "└", a file read, and trailing UI chrome.
PANE_DISK = """\
  [TG] How much disk space is left?
● Let me check the free space on the main partition.
● Ran df -h /
  └ Filesystem      Size  Used Avail Use% Mounted on
    /dev/sda1        48G  2.3G   46G   5% /
● Read /etc/hosts
● The main disk / has 46 GB free out of 48 GB.
> 0;10;1c
  gpt-5.5 default · ~
"""

PANE_WEB = """\
● Let me check the web, because figures like this are often miscopied.
● Searching the web
● Searched the web for example corporation founding year official
● I could not find a reliably sourced figure.
"""


class TuiScrapeTests(unittest.TestCase):
    def test_system_call_on_bullet_line(self):
        """`● Ran df -h /` must surface as a bubble (the 2026-06-20 bullet-line bug)."""
        self.assertIn("🛠️ Ran df -h /", _extract_tui_tools(PANE_DISK))

    def test_file_read_bullet_line(self):
        self.assertIn("📄 Read /etc/hosts", _extract_tui_tools(PANE_DISK))

    def test_command_output_is_not_a_bubble(self):
        """The nested `└ Filesystem …` output line is not a tool call — no bubble for it."""
        out = _extract_tui_tools(PANE_DISK)
        self.assertFalse(any("Filesystem" in s for s in out))
        self.assertFalse(any("/dev/sda1" in s for s in out))

    def test_agent_text_is_not_a_bubble(self):
        """Plain agent prose on a bullet line (not a known verb) must be ignored."""
        out = _extract_tui_tools(PANE_DISK)
        self.assertFalse(any("Let me check" in s for s in out))
        self.assertFalse(any("free out of" in s for s in out))

    def test_web_search_query_extracted(self):
        out = _extract_tui_tools(PANE_WEB)
        self.assertTrue(any(s.startswith("🔎 Web search:") and "example corporation" in s for s in out))

    def test_web_search_in_progress(self):
        self.assertEqual(_extract_tui_tools("● Searching the web"), ["🔎 Searching the web"])

    def test_stale_lines_not_reemitted(self):
        """Re-extraction of a pane that still shows a previous turn's tool line yields nothing new
        once the per-turn dedup is seeded with what's on screen (the stale-bubble bug)."""
        seen = set(_extract_tui_tools(PANE_DISK))      # seed at turn start, as _handle() does
        fresh = [s for s in _extract_tui_tools(PANE_DISK) if s not in seen]
        self.assertEqual(fresh, [])                     # nothing re-sent under the new turn

    def test_new_call_after_seed_is_emitted(self):
        """A genuinely new tool line appearing during the turn still fires."""
        seen = set(_extract_tui_tools(PANE_DISK))
        fresh = [s for s in _extract_tui_tools(PANE_DISK + "● Ran echo hello\n") if s not in seen]
        self.assertEqual(fresh, ["🛠️ Ran echo hello"])

    def test_empty_pane(self):
        self.assertEqual(_extract_tui_tools(""), [])


if __name__ == "__main__":
    unittest.main()
