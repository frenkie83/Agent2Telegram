import errno
import json
import multiprocessing
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from agent2telegram.durable import (
    CapacityError,
    DurableInbox,
    DurableOutbox,
    InvalidTransitionError,
    RecordConflictError,
)


def _concurrent_reserve(root: str, update_ids: list[int], start) -> None:
    inbox = DurableInbox(Path(root))
    if not start.wait(timeout=10):
        raise RuntimeError("concurrency test did not start")
    for update_id in update_ids:
        inbox.reserve({"update_id": update_id, "message": {"text": str(update_id)}})


class DurableInboxTests(unittest.TestCase):
    def test_reserve_survives_crash_before_done(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            record_id = DurableInbox(root).reserve(
                {"update_id": 42, "message": {"text": "must not vanish"}}
            )

            after_restart = DurableInbox(root)
            pending = after_restart.pending()
            self.assertEqual([record.id for record in pending], [record_id])
            self.assertEqual(pending[0].update["message"]["text"], "must not vanish")

    def test_pending_reloads_repeatedly_and_done_is_durable(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            first = DurableInbox(root)
            ids = [
                first.reserve({"update_id": update_id, "message": {"text": str(update_id)}})
                for update_id in (9, 10, 11)
            ]

            for _ in range(3):
                self.assertEqual(
                    [record.id for record in DurableInbox(root).pending()],
                    ids,
                )

            DurableInbox(root).done(ids[0])
            self.assertEqual(
                [record.id for record in DurableInbox(root).pending()],
                ids[1:],
            )
            DurableInbox(root).done(ids[0])  # done is intentionally idempotent

    def test_same_update_reservation_is_idempotent_but_conflicts_are_loud(self):
        with tempfile.TemporaryDirectory() as td:
            inbox = DurableInbox(Path(td))
            update = {"update_id": 7, "message": {"text": "stejne"}}
            first = inbox.reserve(update)
            self.assertEqual(inbox.reserve(dict(update)), first)
            with self.assertRaises(RecordConflictError):
                inbox.reserve({"update_id": 7, "message": {"text": "jine"}})
            self.assertEqual(len(inbox.pending()), 1)

    def test_fail_count_and_give_up_survive_restart(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            inbox = DurableInbox(root)
            record_id = inbox.reserve({"update_id": 51})
            self.assertEqual(inbox.fail(record_id, "tmux timeout"), 1)
            self.assertEqual(inbox.fail(record_id, "tmux stale"), 2)

            restarted = DurableInbox(root)
            self.assertEqual(restarted.pending()[0].attempts, 2)
            restarted.give_up(record_id, "retry limit reached")

            final = DurableInbox(root)
            self.assertEqual(final.pending(), [])
            dead = final.dead_letters()
            self.assertEqual([record.id for record in dead], [record_id])
            self.assertEqual(dead[0].last_error, "retry limit reached")

    def test_full_disk_error_propagates_and_does_not_publish_record(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            inbox = DurableInbox(root)
            update = {"update_id": 88, "message": {"text": "retry me"}}
            disk_full = OSError(errno.ENOSPC, "No space left on device")

            with mock.patch("agent2telegram.durable.os.fsync", side_effect=disk_full):
                with self.assertRaises(OSError) as raised:
                    inbox.reserve(update)
            self.assertEqual(raised.exception.errno, errno.ENOSPC)
            self.assertEqual(list((root / "inbox").glob("*.json")), [])
            self.assertEqual(list((root / "inbox").glob(".*.tmp")), [])

            # A caller that did not advance Telegram's offset can safely retry.
            record_id = inbox.reserve(update)
            self.assertEqual(inbox.pending()[0].id, record_id)

    def test_processes_can_publish_distinct_records_without_corruption(self):
        with tempfile.TemporaryDirectory() as td:
            context = multiprocessing.get_context("spawn")
            start = context.Event()
            processes = [
                context.Process(
                    target=_concurrent_reserve,
                    args=(td, list(range(worker * 25, (worker + 1) * 25)), start),
                )
                for worker in range(4)
            ]
            for process in processes:
                process.start()
            start.set()
            for process in processes:
                process.join(timeout=20)
                self.assertFalse(process.is_alive(), "writer process hung")
                self.assertEqual(process.exitcode, 0)

            records = DurableInbox(Path(td)).pending()
            self.assertEqual(len(records), 100)
            self.assertEqual(
                sorted(record.update["update_id"] for record in records),
                list(range(100)),
            )
            for path in (Path(td) / "inbox").glob("*.json"):
                self.assertIsInstance(json.loads(path.read_text("utf-8")), dict)
            self.assertEqual(list((Path(td) / "inbox").glob(".*.tmp")), [])

    def test_count_limit_includes_dead_letters_until_retention_expires(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            inbox = DurableInbox(
                root,
                max_records=1,
                dead_letter_retention_seconds=1,
            )
            record_id = inbox.reserve({"update_id": 1})
            inbox.give_up(record_id, "permanent")
            with self.assertRaises(CapacityError):
                inbox.reserve({"update_id": 2})

            dead_path = next((root / "dead-letter" / "inbox").glob("*.json"))
            old = time.time() - 10
            os.utime(dead_path, (old, old))
            restarted = DurableInbox(
                root,
                max_records=1,
                dead_letter_retention_seconds=1,
            )
            restarted.reserve({"update_id": 2})
            self.assertEqual(len(restarted.pending()), 1)
            self.assertEqual(restarted.dead_letters(), [])

    def test_byte_limit_rejects_a_record_before_it_is_published(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            inbox = DurableInbox(root, max_bytes=1_024)
            with self.assertRaises(CapacityError):
                inbox.reserve(
                    {"update_id": 5, "message": {"text": "x" * 2_000}}
                )
            self.assertEqual(inbox.pending(), [])
            self.assertEqual(list((root / "inbox").glob("*.json")), [])

    def test_pending_retention_moves_record_to_dead_letter(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            inbox = DurableInbox(
                root,
                pending_retention_seconds=0,
                dead_letter_retention_seconds=3600,
            )
            record_id = inbox.reserve({"update_id": 71})
            self.assertEqual(inbox.pending(), [])
            self.assertEqual(
                [record.id for record in inbox.dead_letters()],
                [record_id],
            )

    def test_legacy_offset_is_loaded_and_updated_without_regression(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            offset = root / "offset"
            offset.write_text(json.dumps({"offset": 12_345}), encoding="utf-8")

            inbox = DurableInbox(root)
            self.assertEqual(inbox.load_offset(), 12_345)
            self.assertEqual(inbox.offset, 12_345)
            inbox.save_offset(100)
            self.assertEqual(inbox.load_offset(), 12_345)
            inbox.save_offset(12_400)
            self.assertEqual(inbox.load_offset(), 12_400)
            self.assertEqual(json.loads(offset.read_text("utf-8"))["offset"], 12_400)


class DurableOutboxTests(unittest.TestCase):
    def test_restart_continues_at_first_unconfirmed_part(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            outbox = DurableOutbox(root)
            record_id = outbox.enqueue(
                ["cast 0", "cast 1", "cast 2"],
                ["/tmp/a.png", "/tmp/b.pdf"],
                "turn-1",
            )
            outbox.mark_chunk_sent(record_id, 0)

            restarted = DurableOutbox(root)
            head = restarted.head()
            self.assertIsNotNone(head)
            self.assertEqual(head.pending_chunks, ((1, "cast 1"), (2, "cast 2")))
            self.assertEqual(head.pending_files, ("/tmp/a.png", "/tmp/b.pdf"))
            with self.assertRaises(InvalidTransitionError):
                restarted.mark_chunk_sent(record_id, 2)
            with self.assertRaises(InvalidTransitionError):
                restarted.mark_file_sent(record_id, "/tmp/a.png")
            with self.assertRaises(InvalidTransitionError):
                restarted.done(record_id)

            restarted.mark_chunk_sent(record_id, 1)
            DurableOutbox(root).mark_chunk_sent(record_id, 2)
            DurableOutbox(root).mark_file_sent(record_id, "/tmp/a.png")

            final_restart = DurableOutbox(root)
            self.assertEqual(final_restart.head().pending_chunks, ())
            self.assertEqual(final_restart.head().pending_files, ("/tmp/b.pdf",))
            final_restart.mark_file_sent(record_id, "/tmp/b.pdf")
            final_restart.done(record_id)
            self.assertIsNone(DurableOutbox(root).head())

    def test_marks_are_idempotent_after_restart(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            outbox = DurableOutbox(root)
            record_id = outbox.enqueue(["jedna"], [], None)
            outbox.mark_chunk_sent(record_id, 0)

            restarted = DurableOutbox(root)
            restarted.mark_chunk_sent(record_id, 0)
            self.assertTrue(restarted.head().complete)
            restarted.done(record_id)
            restarted.done(record_id)

    def test_key_deduplicates_pending_enqueue(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            first = DurableOutbox(root).enqueue(["reply"], ["/tmp/x"], "uuid-1")
            second = DurableOutbox(root).enqueue(["reply"], ["/tmp/x"], "uuid-1")
            self.assertEqual(first, second)
            self.assertEqual(len(DurableOutbox(root).pending()), 1)
            with self.assertRaises(RecordConflictError):
                DurableOutbox(root).enqueue(["jina"], ["/tmp/x"], "uuid-1")

    def test_fifo_order_survives_restart(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            outbox = DurableOutbox(root)
            first = outbox.enqueue(["prvni"], [], "a")
            time.sleep(0.001)
            second = outbox.enqueue(["druhy"], [], "b")

            restarted = DurableOutbox(root)
            self.assertEqual(restarted.head().id, first)
            restarted.mark_chunk_sent(first, 0)
            restarted.done(first)
            self.assertEqual(DurableOutbox(root).head().id, second)


if __name__ == "__main__":
    unittest.main()
