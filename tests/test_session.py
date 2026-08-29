"""Tests for tmux session input handling."""
import subprocess
import unittest
from unittest.mock import patch

from agent2telegram import session
from agent2telegram.session import (
    MAX_TMUX_INJECTION_CHARS,
    SUBMIT_TAIL_CHARS,
    SessionError,
    TmuxSession,
    sanitize_for_tmux,
)


class SanitizeForTmuxTests(unittest.TestCase):
    def test_regular_text_is_unchanged(self):
        text = "Hello, world 123. Regular punctuation: []{};:,./?\n\tDone."
        self.assertEqual(sanitize_for_tmux(text), text)

    def test_empty_text_stays_empty(self):
        self.assertEqual(sanitize_for_tmux(""), "")

    def test_unicode_is_preserved(self):
        text = "Grüße ✅ Привет こんにちは 🚀"
        self.assertEqual(sanitize_for_tmux(text), text)

    def test_control_characters_are_stripped_except_newline_and_tab(self):
        control_chars = "".join(chr(i) for i in [*range(0x20), 0x7f, *range(0x80, 0xa0)])
        self.assertEqual(sanitize_for_tmux(f"a{control_chars}b"), "a\t\nb")

    def test_text_is_truncated(self):
        text = "x" * (MAX_TMUX_INJECTION_CHARS + 1)
        self.assertEqual(sanitize_for_tmux(text), "x" * MAX_TMUX_INJECTION_CHARS)

    def test_truncation_counts_only_surviving_characters(self):
        text = ("\x00" * 100) + ("x" * (MAX_TMUX_INJECTION_CHARS + 50))
        self.assertEqual(sanitize_for_tmux(text), "x" * MAX_TMUX_INJECTION_CHARS)


class TmuxSessionSendKeysTests(unittest.TestCase):
    def _bare(self, expected=("codex",)):
        s = object.__new__(TmuxSession)
        s.name = "a2t-test"
        s._origin = ""
        s._expected_agent_commands = tuple(expected)
        return s

    @staticmethod
    def _cp(args, *, stdout="", returncode=0):
        return subprocess.CompletedProcess(args, returncode, stdout=stdout, stderr="")

    def test_send_keys_passes_sanitized_literal_argument_to_tmux(self):
        with patch.object(session, "_tmux") as tmux, patch.object(session.time, "sleep"):
            s = object.__new__(TmuxSession)
            s.name = "a2t-test"
            s._origin = "from tg: "
            s._expected_agent_commands = ()
            s._send_keys("hello\x0bthere\x03\x1b[31m\x7fworld\x85!")

        calls = [call.args for call in tmux.call_args_list]
        literal_calls = [c for c in calls if c[:5] == ("send-keys", "-t", "a2t-test", "-l", "--")]
        self.assertEqual(len(literal_calls), 1)
        self.assertEqual(literal_calls[0][-1], "from tg: hellothere[31mworld!")

    def test_send_keys_collapses_newlines_to_one_literal_submit(self):
        with patch.object(session, "_tmux") as tmux, patch.object(session.time, "sleep"):
            s = self._bare(())
            s._send_keys("first line\nsecond line\r\nthird\tline")

        calls = [call.args for call in tmux.call_args_list]
        literal = [c[-1] for c in calls if c[:5] == ("send-keys", "-t", "a2t-test", "-l", "--")]
        submits = [c for c in calls if c == ("send-keys", "-t", "a2t-test", "Enter")]
        self.assertEqual(literal, ["first line second line third\tline"])
        self.assertEqual(len(submits), 1)

    def test_allowed_agent_pane_allows_injection(self):
        calls = []

        def tmux(*args, **kwargs):
            calls.append(args)
            if args[:4] == ("display-message", "-p", "-t", "a2t-test"):
                return self._cp(args, stdout="codex\n")
            return self._cp(args)

        with patch.object(session, "_tmux", side_effect=tmux), \
             patch.object(session.time, "sleep"), \
             patch.object(TmuxSession, "_exists", return_value=True):
            self._bare(("codex",)).inject("hello")

        self.assertIn(("send-keys", "-t", "a2t-test", "Enter"), calls)

    def test_node_wrapper_for_expected_agent_allows_injection(self):
        calls = []

        def tmux(*args, **kwargs):
            calls.append(args)
            if args[:4] == ("display-message", "-p", "-t", "a2t-test"):
                if args[-1] == "#{pane_current_command}":
                    return self._cp(args, stdout="node\n")
                if args[-1] == "#{pane_pid}":
                    return self._cp(args, stdout="100\n")
            return self._cp(args)

        ps_out = "\n".join([
            "100 1 /bin/zsh zsh",
            "101 100 /usr/local/bin/node node /opt/tools/codex/bin/codex.js",
        ])
        with patch.object(session, "_tmux", side_effect=tmux), \
             patch.object(session.subprocess, "run", return_value=self._cp(["ps"], stdout=ps_out)), \
             patch.object(session.time, "sleep"):
            self._bare(("codex",))._send_keys("hello")

        self.assertIn(("send-keys", "-t", "a2t-test", "Enter"), calls)

    def test_full_path_expected_agent_allows_basename_match(self):
        with patch.object(session, "_pane_value", return_value="/opt/homebrew/bin/codex"):
            ok, detail = session._agent_alive("a2t-test", ("/Users/me/.local/bin/codex",))

        self.assertTrue(ok)
        self.assertIn("codex", detail)

    def test_python_wrapper_that_mentions_expected_agent_allows_injection(self):
        calls = []
        processes = [
            (100, 1, "/bin/zsh", "zsh"),
            (101, 100, "/usr/bin/python3", "python3 /opt/wrappers/run_agent.py --agent codex"),
        ]

        def tmux(*args, **kwargs):
            calls.append(args)
            return self._cp(args)

        with patch.object(session, "_pane_value", return_value="python3"), \
             patch.object(session, "_pane_processes", return_value=processes), \
             patch.object(session, "_tmux", side_effect=tmux), \
             patch.object(session.time, "sleep"):
            self._bare(("codex",))._send_keys("hello")

        self.assertIn(("send-keys", "-t", "a2t-test", "Enter"), calls)

    def test_pane_without_agent_child_blocks_injection_even_when_not_shell(self):
        calls = []
        processes = [(100, 1, "/usr/bin/vim", "vim README.md")]

        def tmux(*args, **kwargs):
            calls.append(args)
            return self._cp(args)

        with patch.object(session, "_pane_value", return_value="vim"), \
             patch.object(session, "_pane_processes", return_value=processes), \
             patch.object(session, "_tmux", side_effect=tmux), \
             patch.object(session.time, "sleep"):
            with self.assertRaises(SessionError):
                self._bare(("codex",))._send_keys("echo pwned")

        send_calls = [c for c in calls if c and c[0] == "send-keys"]
        self.assertEqual(send_calls, [])

    def test_shell_leader_with_full_path_claude_child_allows_injection(self):
        calls = []
        processes = [
            (100, 1, "-sh", "-sh"),
            (
                101,
                100,
                "/Users/you/.local/bin/claude",
                "/Users/you/.local/bin/claude --resume abc123 --dangerously-skip-permissions",
            ),
        ]

        def tmux(*args, **kwargs):
            calls.append(args)
            return self._cp(args)

        with patch.object(session, "_pane_value", return_value="2.1.185"), \
             patch.object(session, "_pane_processes", return_value=processes), \
             patch.object(session, "_tmux", side_effect=tmux), \
             patch.object(session.time, "sleep"):
            self._bare(("claude",))._send_keys("hello")

        self.assertIn(("send-keys", "-t", "a2t-test", "Enter"), calls)

    def test_shell_leader_without_agent_child_blocks_injection(self):
        calls = []
        processes = [(100, 1, "-sh", "-sh")]

        def tmux(*args, **kwargs):
            calls.append(args)
            return self._cp(args)

        with patch.object(session, "_pane_value", return_value="2.1.185"), \
             patch.object(session, "_pane_processes", return_value=processes), \
             patch.object(session, "_tmux", side_effect=tmux), \
             patch.object(session.time, "sleep"):
            with self.assertRaises(SessionError):
                self._bare(("claude",))._send_keys("echo pwned")

        send_calls = [c for c in calls if c and c[0] == "send-keys"]
        self.assertEqual(send_calls, [])

    def test_shell_prompt_blocks_injection(self):
        calls = []

        def tmux(*args, **kwargs):
            calls.append(args)
            if args[:4] == ("display-message", "-p", "-t", "a2t-test"):
                return self._cp(args, stdout="zsh\n")
            return self._cp(args)

        with patch.object(session, "_tmux", side_effect=tmux), \
             patch.object(session.time, "sleep"), \
             patch.object(TmuxSession, "_exists", return_value=True):
            with self.assertRaises(SessionError):
                self._bare(("codex",)).inject("echo pwned")

        send_calls = [c for c in calls if c and c[0] == "send-keys"]
        self.assertEqual(send_calls, [])


class TmuxSessionConfirmSubmittedTests(unittest.TestCase):
    """`tmux send-keys` returning 0 only means tmux delivered the keystroke — it says nothing
    about whether the TUI acted on it. `_confirm_submitted`/`_submitted`/`_prompt_row` turn that
    silent gap into either silence-because-it-really-worked or a raised `SessionError`.

    All of these patch `agent2telegram.session._tmux` so nothing here touches a real tmux, and
    all of them shrink `SUBMIT_CONFIRM_S`/`SUBMIT_POLL_S` so the deadline-polling loop inside
    `_submitted` cannot turn a unit test into a 2.5 s real-time wait.
    """

    def _bare(self):
        s = object.__new__(TmuxSession)
        s.name = "a2t-confirm"
        return s

    @staticmethod
    def _cp(args, *, stdout="", returncode=0):
        return subprocess.CompletedProcess(args, returncode, stdout=stdout, stderr="")

    def _fast_deadline(self):
        # A short but non-zero window: long enough that a genuine "still there" case gets
        # polled more than once, short enough that the whole suite stays well under a second.
        return patch.multiple(session, SUBMIT_CONFIRM_S=0.06, SUBMIT_POLL_S=0.01)

    def test_prompt_already_cleared_does_nothing_and_sends_no_second_enter(self):
        calls = []

        def tmux(*args, **kwargs):
            calls.append(args)
            if args[:2] == ("display-message", "-p"):
                return self._cp(args, stdout="5\n")
            if args[0] == "capture-pane":
                return self._cp(args, stdout="❯ \n")   # empty prompt — nothing left of the text
            return self._cp(args)

        with patch.object(session, "_tmux", side_effect=tmux), self._fast_deadline():
            self._bare()._confirm_submitted("hello world")

        self.assertFalse(any(c[:1] == ("send-keys",) and c[-1] == "Enter" for c in calls),
                          "a cleared prompt must not trigger a second Enter")

    def test_text_still_in_prompt_gets_one_more_enter_and_then_succeeds(self):
        calls = []
        state = {"enter_sent": 0}
        name = "a2t-confirm"

        def tmux(*args, **kwargs):
            calls.append(args)
            if args[:2] == ("display-message", "-p"):
                return self._cp(args, stdout="5\n")
            if args[0] == "capture-pane":
                if state["enter_sent"] == 0:
                    return self._cp(args, stdout="❯ hello world\n")   # still sitting there
                return self._cp(args, stdout="❯ \n")                  # cleared after the retry
            if args == ("send-keys", "-t", name, "Enter"):
                state["enter_sent"] += 1
            return self._cp(args)

        with patch.object(session, "_tmux", side_effect=tmux), self._fast_deadline(), \
             self.assertLogs("agent2telegram.session", level="INFO") as logs:
            self._bare()._confirm_submitted("hello world")   # must not raise

        second_enters = [c for c in calls if c == ("send-keys", "-t", name, "Enter")]
        self.assertEqual(len(second_enters), 1, "exactly one extra Enter, not zero and not many")
        self.assertTrue(any("second Enter submitted it" in m for m in logs.output))

    def test_text_still_in_prompt_after_second_enter_raises(self):
        def tmux(*args, **kwargs):
            if args[:2] == ("display-message", "-p"):
                return self._cp(args, stdout="5\n")
            if args[0] == "capture-pane":
                return self._cp(args, stdout="❯ hello world\n")   # never clears, ever
            return self._cp(args)

        with patch.object(session, "_tmux", side_effect=tmux), self._fast_deadline():
            with self.assertRaises(SessionError):
                self._bare()._confirm_submitted("hello world")

    def test_unmeasurable_cursor_row_is_not_treated_as_failure(self):
        """tmux staying silent about #{cursor_y} must not be read as either success or failure —
        `_submitted` returns None and `_confirm_submitted` must not raise or retry on a guess."""
        calls = []

        def tmux(*args, **kwargs):
            calls.append(args)
            if args[:2] == ("display-message", "-p"):
                return self._cp(args, stdout="")   # tmux has nothing to say
            return self._cp(args)

        with patch.object(session, "_tmux", side_effect=tmux), self._fast_deadline():
            self._bare()._confirm_submitted("hello world")   # must not raise

        self.assertFalse(any(c[0] == "capture-pane" for c in calls),
                          "an unreadable cursor row must short-circuit before capture-pane")
        self.assertFalse(any(c[:1] == ("send-keys",) and c[-1] == "Enter" for c in calls),
                          "an unmeasurable result must not trigger a retry")

    def test_long_wrapped_text_is_checked_by_its_tail_not_by_a_prompt_marker(self):
        """A long prompt wraps over several rows; the cursor sits on the LAST one, where the
        text is only partly visible and there is no '❯' at all. The check must still work by
        looking for the TAIL of the injected text on that row, not for a prompt character."""
        text = "please " + ("x" * 40) + " review the attached diff and reply"
        tail = text[-SUBMIT_TAIL_CHARS:]
        self.assertNotIn("❯", tail)
        state = {"submitted": False}

        def tmux(*args, **kwargs):
            if args[:2] == ("display-message", "-p"):
                return self._cp(args, stdout="7\n")
            if args[0] == "capture-pane":
                if not state["submitted"]:
                    # The wrapped row: just the tail of the text, no leading prompt marker.
                    return self._cp(args, stdout=tail + "\n")
                return self._cp(args, stdout="\n")   # row is blank once it really submitted
            if args[-1] == "Enter":
                state["submitted"] = True
            return self._cp(args)

        with patch.object(session, "_tmux", side_effect=tmux), self._fast_deadline():
            s = self._bare()
            self.assertIs(s._submitted(text), False,
                          "the tail is on the cursor row — must be read as still unsubmitted")
            s._confirm_submitted(text)   # the retry Enter flips state["submitted"]; must not raise

    def test_prompt_row_returns_none_when_capture_pane_fails(self):
        def tmux(*args, **kwargs):
            if args[:2] == ("display-message", "-p"):
                return self._cp(args, stdout="3\n")
            if args[0] == "capture-pane":
                return self._cp(args, returncode=1)
            return self._cp(args)

        with patch.object(session, "_tmux", side_effect=tmux):
            self.assertIsNone(self._bare()._prompt_row())

    def test_prompt_row_returns_the_captured_line(self):
        def tmux(*args, **kwargs):
            if args[:2] == ("display-message", "-p"):
                return self._cp(args, stdout="2\n")
            if args[0] == "capture-pane":
                return self._cp(args, stdout="❯ some text\n")
            return self._cp(args)

        with patch.object(session, "_tmux", side_effect=tmux):
            self.assertEqual(self._bare()._prompt_row(), "❯ some text")


if __name__ == "__main__":
    unittest.main()
