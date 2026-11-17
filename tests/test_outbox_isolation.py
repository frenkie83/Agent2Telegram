"""A test must never be able to deliver a message to a real chat.

2026-08-23: an older copy of test_attach_backstop.py was run against newer code. It does not
opt out of the durable outbox, so its fixture strings were enqueued into the SHARED outbox in
the state directory — and the live bridge, a different process watching that same queue,
delivered eleven of them ("the real answer", "thanks!") to a real user.

Isolation must not depend on every test author remembering to switch the outbox off. The code
holds that guarantee instead.
"""
import os
import unittest
from pathlib import Path

from agent2telegram import attach


class _FakeBridge:
    """The bare minimum `_ensure_outbox` needs — exactly what a forgetful test provides."""
    _use_durable_outbox = True
    cfg = None

    _ensure_outbox = attach.AttachBridge._ensure_outbox


class OutboxIsolationTests(unittest.TestCase):
    def test_test_run_without_isolated_queue_gets_no_outbox(self):
        self.assertTrue(attach._in_test_run(), "a test run must recognise itself")
        self.assertIsNone(_FakeBridge()._ensure_outbox(),
                          "a test without an isolated queue reached the real outbox")

    def test_the_guard_holds_under_every_runner_not_just_pytest(self):
        """The project's CI runs `python -m unittest discover`, where PYTEST_CURRENT_TEST does
        not exist. A guard that only works under pytest is absent exactly where an unfamiliar
        contributor first runs the suite."""
        saved = os.environ.pop("PYTEST_CURRENT_TEST", None)
        try:
            self.assertTrue(attach._in_test_run(),
                            "without PYTEST_CURRENT_TEST the run is no longer recognised as a test")
            self.assertIsNone(_FakeBridge()._ensure_outbox())
        finally:
            if saved is not None:
                os.environ["PYTEST_CURRENT_TEST"] = saved

    def test_isolated_queue_path_still_works(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            b = _FakeBridge()
            b._queue_path = str(Path(d) / "queue" / "offset")
            self.assertIsNotNone(b._ensure_outbox(),
                                 "an isolated queue must keep working, otherwise tests cannot be written")


if __name__ == "__main__":
    unittest.main()
