"""T-1139: neznamy tmux cwd nesmi prepnout bezici most na cizi prepis."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
from test_regrese_mostu import _regrese_bridge, _touch  # noqa: E402


def _fresh(b):
    b._last_resolve = 0.0
    b._signal = None          # publikace neni predmetem testu


class ClaudeUnknownCwdTests(unittest.TestCase):
    def test_known_then_unknown_cwd_stays_on_own_transcript(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as td:
            base = Path(home) / ".claude" / "projects"
            cwd = "/work/a"
            a = _touch(base / cwd.replace("/", "-") / "a.jsonl", 1000.0)
            with mock.patch.object(Path, "home", return_value=Path(home)):
                b = _regrese_bridge(td)
                b.cfg.transcript_path = ""
                cur = {"v": cwd}
                b._session_cwd = lambda: cur["v"]
                b._transcript = b._resolve_transcript()
                self.assertEqual(b._transcript, a)
                # tmux vypadne a v jinem projektu je novejsi prepis B
                _touch(base / "-work-b" / "b.jsonl", 9000.0)
                cur["v"] = None
                for turn_active in (False, True):
                    (b._turn_active.set if turn_active else b._turn_active.clear)()
                    _fresh(b)
                    b._maybe_reresolve()
                    self.assertEqual(b._transcript, a, f"skocil na cizi prepis (turn_active={turn_active})")

    def test_startup_without_cwd_may_guess_but_does_not_publish(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as td:
            base = Path(home) / ".claude" / "projects"
            bb = _touch(base / "-work-b" / "b.jsonl", 9000.0)
            with mock.patch.object(Path, "home", return_value=Path(home)):
                b = _regrese_bridge(td)
                b.cfg.transcript_path = ""
                b._session_cwd = lambda: None
                self.assertEqual(b._resolve_transcript(), bb)
                self.assertTrue(b._transcript_best_effort)


class CodexUnknownCwdTests(unittest.TestCase):
    def test_known_then_unknown_cwd_stays_on_own_rollout(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as td:
            sess = Path(home) / ".codex" / "sessions" / "2026" / "11" / "23"
            a = sess / "rollout-a.jsonl"
            a.parent.mkdir(parents=True)
            a.write_text(json.dumps({"payload": {"cwd": "/work/a"}}) + "\n")
            os.utime(a, (1000.0, 1000.0))
            with mock.patch.object(Path, "home", return_value=Path(home)):
                b = _regrese_bridge(td)
                b.cfg.agent = "codex"
                b.cfg.transcript_path = ""
                b._codex_sessions_dir = lambda: Path(home) / ".codex" / "sessions"
                cur = {"v": "/work/a"}
                b._session_cwd = lambda: cur["v"]
                b._transcript = b._resolve_transcript()
                self.assertEqual(b._transcript, a)
                other = sess / "rollout-b.jsonl"
                other.write_text(json.dumps({"payload": {"cwd": "/work/b"}}) + "\n")
                os.utime(other, (9000.0, 9000.0))
                cur["v"] = None
                _fresh(b)
                b._maybe_reresolve()
                self.assertEqual(b._transcript, a)


class SessionCwdTests(unittest.TestCase):
    def _bridge(self, td):
        return _regrese_bridge(td)

    def test_retries_once_then_succeeds(self):
        calls = []
        def fake(*a, **k):
            calls.append(1)
            if len(calls) == 1:
                raise subprocess.TimeoutExpired("tmux", 5)
            return subprocess.CompletedProcess(a, 0, "/work/a\n", "")
        with tempfile.TemporaryDirectory() as td, mock.patch("subprocess.run", fake), \
                mock.patch("time.sleep"):
            self.assertEqual(self._bridge(td)._session_cwd(), "/work/a")
        self.assertEqual(len(calls), 2)

    def test_failure_logs_warning_with_rc_and_stderr(self):
        res = subprocess.CompletedProcess([], 1, "", "can't find session")
        with tempfile.TemporaryDirectory() as td, mock.patch("subprocess.run", return_value=res), \
                mock.patch("time.sleep"):
            b = self._bridge(td)
            with self.assertLogs("agent2telegram.attach", level="WARNING") as cm:
                self.assertIsNone(b._session_cwd())
        self.assertIn("rc=1", cm.output[0])
        self.assertIn("can't find session", cm.output[0])

    def test_empty_output_is_failure(self):
        res = subprocess.CompletedProcess([], 0, "\n", "")
        with tempfile.TemporaryDirectory() as td, mock.patch("subprocess.run", return_value=res), \
                mock.patch("time.sleep"):
            with self.assertLogs("agent2telegram.attach", level="WARNING"):
                self.assertIsNone(self._bridge(td)._session_cwd())


if __name__ == "__main__":
    unittest.main()
