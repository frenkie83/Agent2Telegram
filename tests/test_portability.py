"""Portability tests for :mod:`agent2telegram.compat`.

Written to pass on BOTH macOS and Linux — no assumption about which one is running. The
behaviour under test is the *same answer on both systems*, so the assertions are about
semantics (a shell that merely names the needle is excluded; ages are sane; a second lock
fails), never about a specific OS's tool output.
"""
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from agent2telegram import compat


def _spawn(args):
    proc = subprocess.Popen(
        args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL
    )
    return proc


def _wait_until(predicate, timeout=5.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class ProcessPidsTests(unittest.TestCase):
    def setUp(self):
        # A marker unique to this run so we never collide with unrelated processes.
        self.marker = f"a2t_portability_{os.getpid()}_{int(time.monotonic() * 1000)}"
        self.procs = []

    def tearDown(self):
        for p in self.procs:
            p.terminate()
        for p in self.procs:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()

    def test_matches_python_process_by_argv(self):
        # A real python process whose argv contains the marker.
        py = _spawn([sys.executable, "-c", f"import time; time.sleep(30)  # {self.marker}"])
        self.procs.append(py)
        found = _wait_until(lambda: py.pid in compat.process_pids(self.marker, interpreter="python"))
        self.assertTrue(found, "python process carrying the marker should be found")

    def test_excludes_shell_that_only_mentions_the_needle(self):
        # This is the 2026-07-30 failure mode: a shell whose command line merely NAMES the
        # marker (a `pgrep`/until wrapper). Its argv[0] is the shell, not python → excluded.
        # Two statements (`sleep …; : …`) stop the shell from exec-replacing itself with the
        # single last command (which would drop the marker from argv and pass for the wrong
        # reason) — here the shell stays a shell and keeps the marker on its command line.
        py = _spawn([sys.executable, "-c", f"import time; time.sleep(30)  # {self.marker}"])
        sh = _spawn(["/bin/sh", "-c", f"sleep 30; : {self.marker}"])
        self.procs += [py, sh]
        _wait_until(lambda: py.pid in compat.process_pids(self.marker, interpreter="python"))
        pids = compat.process_pids(self.marker, interpreter="python")
        self.assertIn(py.pid, pids)
        self.assertNotIn(sh.pid, pids, "a shell that only names the needle must be excluded")

    def test_interpreter_none_is_loose(self):
        # With the filter off, the same shell IS matched (documented loose behaviour).
        sh = _spawn(["/bin/sh", "-c", f"sleep 30; : {self.marker}"])
        self.procs.append(sh)
        found = _wait_until(lambda: sh.pid in compat.process_pids(self.marker, interpreter=None))
        self.assertTrue(found, "interpreter=None should match on argv alone")

    def test_excludes_self(self):
        # The current process's argv contains the marker string (it's in this test file's
        # memory but not its argv) — assert the helper never returns our own pid regardless.
        self.assertNotIn(os.getpid(), compat.process_pids("python", interpreter=None))

    def test_empty_needle_returns_empty(self):
        self.assertEqual(compat.process_pids("", interpreter="python"), [])


class AgeTests(unittest.TestCase):
    def test_process_age_of_self_is_small_and_nonnegative(self):
        age = compat.process_age_seconds(os.getpid())
        self.assertIsNotNone(age)
        self.assertGreaterEqual(age, 0.0)
        self.assertLess(age, 24 * 3600, "the test process is not a day old")

    def test_process_age_of_dead_pid_is_none(self):
        # A pid that cannot exist (max pid space) → None, not a crash.
        self.assertIsNone(compat.process_age_seconds(2 ** 31 - 1))

    def test_process_age_grows(self):
        p = _spawn([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            first = _wait_until(lambda: compat.process_age_seconds(p.pid) is not None)
            self.assertTrue(first)
            a1 = compat.process_age_seconds(p.pid)
            time.sleep(1.2)
            a2 = compat.process_age_seconds(p.pid)
            self.assertIsNotNone(a1)
            self.assertIsNotNone(a2)
            self.assertGreaterEqual(a2, a1)
        finally:
            p.terminate()
            p.wait(timeout=5)

    def test_file_age_of_fresh_file_is_small(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "x"
            f.write_text("hi")
            age = compat.file_age_seconds(f)
            self.assertIsNotNone(age)
            self.assertGreaterEqual(age, 0.0)
            self.assertLess(age, 60)

    def test_file_age_of_missing_is_none(self):
        self.assertIsNone(compat.file_age_seconds("/no/such/file/at/all"))


class ParseEtimeTests(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(compat._parse_etime("05"), None)          # not a valid ps form
        self.assertEqual(compat._parse_etime("01:02"), 62.0)       # mm:ss
        self.assertEqual(compat._parse_etime("01:02:03"), 3723.0)  # hh:mm:ss
        self.assertEqual(compat._parse_etime("2-03:04:05"), 2 * 86400 + 3 * 3600 + 4 * 60 + 5)
        self.assertIsNone(compat._parse_etime(""))
        self.assertIsNone(compat._parse_etime("garbage"))


class SingleInstanceLockTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "sub" / "bridge.lock"

    def tearDown(self):
        self.dir.cleanup()

    def test_second_acquire_raises(self):
        with compat.single_instance_lock(self.path):
            with self.assertRaises(compat.AlreadyRunning):
                with compat.single_instance_lock(self.path):
                    pass

    def test_writes_pid_and_creates_parent(self):
        with compat.single_instance_lock(self.path):
            self.assertTrue(self.path.exists())
            self.assertEqual(self.path.read_text().strip(), str(os.getpid()))

    def test_lock_released_after_context(self):
        with compat.single_instance_lock(self.path):
            pass
        # Re-acquiring after release must succeed.
        with compat.single_instance_lock(self.path):
            pass


if __name__ == "__main__":
    unittest.main()
