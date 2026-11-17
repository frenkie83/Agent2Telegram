"""Tests for the daily traffic report: what it counts and what it reports as a change."""
import importlib.util
import tempfile
import unittest
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "daily_report", Path(__file__).resolve().parents[1] / "tools" / "daily_report.py")
report = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(report)


class NetworkEventTests(unittest.TestCase):
    """A long poll ends ~1700× a day and the peer usually closes the connection. Reporting that
    as an outage buries the real errors in noise."""

    def test_connection_reset_counts_as_routine_not_as_an_error(self):
        lines = ["12:00:00 WARNING telegram: getUpdates: <urlopen error [Errno 54] "
                 "Connection reset by peer>, retry 1/5 (backoff)"] * 3

        v = report.analyse(lines)

        self.assertEqual(v["net_routine"], 3)
        self.assertEqual(v["net_errors"], 0, "a routine reconnect was counted as an error")

    def test_real_failures_are_counted_separately(self):
        lines = [
            "12:00:00 WARNING telegram: getUpdates: HTTP 409 Conflict",
            "12:00:01 WARNING telegram: getUpdates: HTTP 502, retry 1/5",
            "12:00:02 WARNING telegram: getUpdates: <urlopen error [Errno 8] nodename nor servname>",
            "12:00:03 WARNING telegram: getUpdates: The read operation timed out",
        ]

        v = report.analyse(lines)

        self.assertEqual(v["net_errors"], 4)
        self.assertEqual(v["net_routine"], 0)


class ChangeTests(unittest.TestCase):
    """Without a comparison against the previous run the report looks the same every day and
    stops being read."""

    def test_first_run_says_it_has_nothing_to_compare_with(self):
        result = report.changes({"backstop": 2}, 0, {})

        self.assertEqual(len(result), 1)
        self.assertIn("compare", result[0])

    def test_a_metric_appearing_from_zero_is_reported_as_new(self):
        result = report.changes({"backstop": 2}, 0, {"backstop": 0})

        self.assertTrue(any("New" in r and "backstop" in r for r in result), result)

    def test_a_metric_dropping_to_zero_is_reported_as_resolved(self):
        result = report.changes({"backstop": 0}, 0, {"backstop": 4})

        self.assertTrue(any("Resolved" in r for r in result), result)

    def test_a_move_below_the_threshold_is_not_reported(self):
        # restarts have a threshold of 5 — two extra restarts are not news
        result = report.changes({"restarts": 12}, 0, {"restarts": 10})

        self.assertEqual(result, ["- Nothing new, same as the previous report."])

    def test_a_growing_dead_letter_queue_is_always_reported(self):
        result = report.changes({}, 3, {"dead_letter": 1})

        self.assertTrue(any("dead-letter grew" in r for r in result), result)


class StateTests(unittest.TestCase):
    def test_state_survives_a_write_and_reads_back(self):
        with tempfile.TemporaryDirectory() as d:
            original = report.STATE_FILE
            report.STATE_FILE = str(Path(d) / "state.json")
            try:
                report.save_state({"backstop": 5, "restarts": 2}, dead_letters=1)
                loaded = report.load_state()
            finally:
                report.STATE_FILE = original

            self.assertEqual(loaded["backstop"], 5)
            self.assertEqual(loaded["dead_letter"], 1)

    def test_a_corrupted_state_file_does_not_crash_the_report(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.json"
            path.write_text("{this is not JSON", "utf-8")
            original = report.STATE_FILE
            report.STATE_FILE = str(path)
            try:
                self.assertEqual(report.load_state(), {})
            finally:
                report.STATE_FILE = original


class DaySelectionTests(unittest.TestCase):
    """Before 2026-08-04 the log had no date and the "daily" report summed the whole window —
    about a month. A number presented as daily must either be daily, or be loudly labelled."""

    def test_undated_lines_are_flagged_loudly(self):
        lines = ["09:00:00 INFO x", "09:00:01 INFO y"]

        selected, described = report.select_days(lines, days=1)

        self.assertEqual(selected, lines)
        self.assertIn("NOT a daily figure", described)

    def test_only_the_last_day_is_measured(self):
        lines = [
            "2026-08-02 09:00:00 INFO old",
            "2026-08-03 09:00:00 INFO yesterday",
            "2026-08-04 09:00:00 INFO today",
            "2026-08-04 10:00:00 INFO today2",
        ]

        selected, described = report.select_days(lines, days=1)

        self.assertEqual(len(selected), 2, "other days leaked into the daily figure")
        self.assertIn("2026-08-04", described)

    def test_more_days_can_be_requested(self):
        lines = [f"2026-08-0{d} 09:00:00 INFO r" for d in (1, 2, 3, 4)]

        selected, described = report.select_days(lines, days=3)

        self.assertEqual(len(selected), 3)
        self.assertIn("3 days", described)

    def test_a_specific_day_can_be_picked(self):
        lines = ["2026-08-03 09:00:00 INFO a", "2026-08-04 09:00:00 INFO b"]

        selected, described = report.select_days(lines, days=1, day="2026-08-03")

        self.assertEqual(selected, [lines[0]])
        self.assertIn("2026-08-03", described)

    def test_undated_lines_are_dropped_once_dated_ones_exist(self):
        lines = ["09:00:00 INFO no date", "2026-08-04 09:00:00 INFO dated"]

        selected, described = report.select_days(lines, days=1)

        self.assertEqual(selected, [lines[1]], "an undated line leaked into the daily figure")
        self.assertIn("Skipped", described)

    def test_time_is_still_parsed_from_a_dated_line(self):
        self.assertEqual(report._time_of("2026-08-04 01:02:03 INFO x"), 3723)
        self.assertEqual(report._time_of("01:02:03 INFO x"), 3723)


class ReplyCountingTests(unittest.TestCase):
    """The report claimed "0 messages" on days when more than fifty went through.

    It counted the string `FWD (send)`, but the bridge had moved to `FWD (delivered)` and writes
    the old form as `FWD (send, legacy path)` — with a comma, so even that no longer matched.
    The lines below are EXCERPTS OF A REAL LOG, not an invented format.
    """

    REAL_LINES = [
        "2026-08-09 09:02:09 INFO    agent2telegram.attach: FWD (delivered) id=out-017 1 parts, 0 attachments 'hi'",
        "13:34:03 INFO    agent2telegram.attach: FWD (send, legacy path) '**Switched and running.'",
        "2026-08-09 08:11:00 INFO    agent2telegram.attach: FWD (re-delivered) 'something'",
        "2026-08-09 08:12:00 INFO    agent2telegram.attach: FWD (voice) 'voice note'",
    ]

    def test_every_send_variant_is_counted(self):
        counted = sum(1 for r in self.REAL_LINES if report.FWD in r)

        self.assertEqual(counted, len(self.REAL_LINES),
                         "a FWD variant is not counted — the report will under-report traffic")

    def test_a_line_without_a_send_is_not_counted(self):
        other = "2026-08-09 09:00:00 INFO    agent2telegram.attach: TURN START t=123"

        self.assertNotIn(report.FWD, other)


class HealthTests(unittest.TestCase):
    """The score must be able to fall, and it must never praise silence.

    A number showing 100 % on days when nothing happened is worse than no number at all.
    """

    CLEAN = {"replies": 50, "restarts": 0, "turns_over_2min": 0, "net_errors": 0,
             "inject_failed": 0, "queue_retries": 0, "abandoned_files": 0}

    def test_a_flawless_day_scores_one_hundred(self):
        pct, reasons = report.health(self.CLEAN, dead_letters=0, complaints=0)

        self.assertEqual(pct, 100)
        self.assertEqual(reasons, [])

    def test_a_quiet_day_is_not_scored(self):
        pct, reasons = report.health({"replies": 1}, dead_letters=0, complaints=0)

        self.assertIsNone(pct, "low traffic was being passed off as perfect health")
        self.assertTrue(reasons)

    def test_a_lost_message_costs_the_most(self):
        pct, _ = report.health(self.CLEAN, dead_letters=1, complaints=0)

        self.assertLess(pct, 70, "a lost message must be visible at a glance")

    def test_a_user_complaint_lowers_the_score(self):
        pct, reasons = report.health(self.CLEAN, dead_letters=0, complaints=1)

        self.assertLessEqual(pct, 75)
        self.assertTrue(any("missing reply" in d for d in reasons))

    def test_the_score_never_goes_below_zero(self):
        awful = dict(self.CLEAN, restarts=99, turns_over_2min=99, net_errors=99,
                     inject_failed=99, queue_retries=99, abandoned_files=99)

        pct, _ = report.health(awful, dead_letters=99, complaints=99)

        self.assertEqual(pct, 0)

    def test_each_penalty_has_a_cap(self):
        """One recurring phenomenon must not drown out everything else on its own."""
        pct, _ = report.health(dict(self.CLEAN, net_errors=1000), dead_letters=0, complaints=0)

        self.assertGreaterEqual(pct, 90)


if __name__ == "__main__":
    unittest.main()


class EndToEndTests(unittest.TestCase):
    """The report must survive the one event it exists to report: a user complaint.

    The previous version passed the LIST of complaints where a COUNT was expected, so
    `int(list)` raised TypeError. It never showed up, because every unit test passed an integer
    directly and an empty list happens to work. The report therefore died at exactly the moment
    it mattered most — the day someone wrote that a reply never arrived.
    """

    LOG = [
        "2026-08-09 09:00:00 INFO    agent2telegram.attach: TURN START t=1",
        *[f"2026-08-09 09:0{i}:00 INFO    agent2telegram.attach: FWD (delivered) 'reply {i}'"
          for i in range(1, 8)],
        "2026-08-09 09:20:00 INFO    agent2telegram.attach: inject: '[TG] are you there?'",
    ]

    def _run(self, lines):
        # Point the phrase lookup at an empty directory, so the built-in defaults are used and
        # the result does not depend on whatever phrases this machine happens to have configured.
        with tempfile.TemporaryDirectory() as d:
            original, report.STATE = report.STATE, d
            try:
                found = report.complaints(lines)
            finally:
                report.STATE = original
        v = report.analyse(lines)
        return report.health(v, 0, len(found)), found

    def test_a_complaint_in_the_log_lowers_the_score_without_crashing(self):
        (pct, reasons), found = self._run(self.LOG)

        self.assertEqual(len(found), 1, f"the complaint was not detected at all: {found}")
        self.assertIsNotNone(pct)
        self.assertLess(pct, 100, "a reported missing reply must cost something")
        self.assertTrue(any("missing reply" in r for r in reasons), reasons)

    def test_the_count_is_passed_as_a_number_not_as_the_list(self):
        """Guards the exact shape of the old bug: health() must never be handed the list."""
        with self.assertRaises(TypeError):
            report.health({"replies": 50}, 0, ["a complaint"])
